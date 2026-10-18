"""视觉跟随：Frigate 检测到人 -> 转向让人居中，太远则前进。

“动一下、看一下”：每次移动都很短（hold 到期由 Engine 自动停车），
之后必须收到移动结束后的新检测才会再动，避免按过时的 box 转过头。
不倒车（机器人没有后视），人太近时只停住。

自动回充：只在“跟随把机器人带离了充电座”之后才计时；连续 dock_after 秒
没看到人 -> 原地每次转 45° 转一圈找人 -> 仍没人就发回充命令。
"""
from __future__ import annotations
from dataclasses import dataclass
import json
import logging
import threading
import time
from typing import Callable
from urllib import request

LOG = logging.getLogger('ebo-person-tracker')

DEAD_ZONE = 0.2          # 画面中心 ±20% 内不转向
TURN_GAIN = 45           # 人在画面边缘时的转向量（EBO 取值范围 ±100）
MIN_TURN = 20            # 小于此值机器人可能转不动
FORWARD_SPEED = 60       # 实测 40 走得太少
FORWARD_MAX_OFFSET = 0.4 # 人偏离中心太多时只转不走
# 远近按人框高度判断（广角镜头贴地仰拍：面积会把远处站着的人算大；水平方向畸变也比垂直大）
FAR_HEIGHT = 0.5         # 头没被切掉、框高不到画面一半：远，往前走（实测隔壁房间站着的人约 0.29）
CLOSE_HEIGHT = 0.8       # 框高超过画面 80%：近，停住
EDGE = 0.03              # 框贴到画面上/下边缘（头或脚被切掉）
MOVE_HOLD = 0.6          # 每次移动的时长（秒）
# 在充电座上原地转向会卡在座上：先笔直开出去（实测 70 x 2 秒能明显离开），再开始跟人
EXIT_SPEED = 70
EXIT_HOLD = 2.5
# 离开充电座后累计前进量（速度/60 x 秒）。没走远时在座旁转圈会卡在座上：不找了，直接回充
NEAR_DOCK_TRAVEL = 6.0
SETTLE = 0.4             # 停车后等画面稳定再采信新检测
DETECTION_MAX_AGE = 1.5  # 超过此时长的检测视为过时
SEARCH_STEP_DEGREES = 45 # 找人时每次原地转的角度（正值右转，实测约 2 秒转完）
SEARCH_STEPS = 8         # 8 x 45° = 一整圈
SEARCH_STEP_WAIT = 3.5   # 转完 + 画面稳定 + Frigate 出检测结果
DOCK_RETRY_SECONDS = 300 # 发了回充但还没充上电，这么久之后再试一次


@dataclass
class Detection:
    object_id: str
    box: tuple[float, float, float, float]
    received_at: float


def _contains(outer, inner) -> bool:
    """outer 是否（几乎）整个包住另一个更小的 inner 框"""
    ix = max(0.0, min(outer[2], inner[2]) - max(outer[0], inner[0]))
    iy = max(0.0, min(outer[3], inner[3]) - max(outer[1], inner[1]))
    inner_area = (inner[2] - inner[0]) * (inner[3] - inner[1])
    outer_area = (outer[2] - outer[0]) * (outer[3] - outer[1])
    return inner_area > 0 and outer_area > inner_area * 1.5 and ix * iy >= 0.9 * inner_area


def person_distance(box, width: float, height: float) -> str:
    """'far' / 'ok' / 'close'。机器人贴地仰拍：离得近时头先被切掉，再近腿就占满整个画面。"""
    x1, y1, x2, y2 = box
    h = (y2 - y1) / height
    cut_top = y1 / height < EDGE
    cut_bottom = y2 / height > 1 - EDGE
    if (cut_top and cut_bottom) or h >= CLOSE_HEIGHT:
        return 'close'
    if h < FAR_HEIGHT and not cut_top:
        return 'far'
    return 'ok'


def fetch_detect_size(frigate_url: str, camera: str) -> tuple[int, int]:
    with request.urlopen(frigate_url.rstrip('/') + '/api/config', timeout=5) as response:
        detect = json.load(response)['cameras'][camera]['detect']
    return int(detect['width']), int(detect['height'])


class PersonTracker:
    def __init__(self, config, state, command: Callable[[str, str], None],
                 allowed: Callable[[], bool], frame_size: Callable[[], tuple[int, int]] | None = None):
        self.config = config
        self.state = state
        self.command = command
        self.allowed = allowed
        self._frame_size = frame_size or (lambda: fetch_detect_size(config.frigate_url, config.camera_name))
        self.size: tuple[int, int] | None = None
        self._size_retry_at = 0.0
        self.lock = threading.Lock()
        self.tracks: dict[str, Detection] = {}
        self.target: str | None = None
        self.next_look_at = 0.0  # 此时间之前收到的检测不采信
        self.dock_after = config.tracking_dock_after_seconds
        self.last_person_at = time.monotonic()
        self.left_dock = False   # 是否由跟随把机器人带离了充电座
        self.searching = False
        self.search_steps_left = 0
        self.travel = 0.0        # 离开充电座后累计前进量，用来判断是否还在座旁
        self.dock_requested_at: float | None = None
        self.last_move_wall = 0.0
        self.rest_until = 0.0     # 用户让它回去充电后，这段时间内不追人
        self.off_dock_reported = False
        self.stopping = threading.Event()
        self.client = None
        state.update(tracking_enabled=True, tracking_status='idle',
                     tracking_dock_after_seconds=self.dock_after)

    def frame_size(self, now: float) -> tuple[int, int]:
        if self.size is None and now >= self._size_retry_at:
            try:
                self.size = self._frame_size()
                LOG.info('Frigate detect size for %s: %dx%d', self.config.camera_name, *self.size)
            except Exception as exc:
                self._size_retry_at = now + 60
                LOG.warning('Cannot read Frigate detect size (%s); assuming 1280x720', type(exc).__name__)
        return self.size or (1280, 720)

    def observe(self, payload: dict, now: float | None = None):
        """处理一条 frigate/events 消息（MQTT 线程调用）。"""
        now = time.monotonic() if now is None else now
        event = payload.get('after')
        if not isinstance(event, dict):
            return
        if event.get('camera') != self.config.camera_name or event.get('label') != 'person':
            return
        object_id = event.get('id')
        if not isinstance(object_id, str):
            return
        with self.lock:
            if payload.get('type') == 'end' or event.get('end_time') is not None:
                self.tracks.pop(object_id, None)
                if self.target == object_id:
                    self.target = None
                return
            box = event.get('box')
            if not (isinstance(box, (list, tuple)) and len(box) == 4):
                return
            try:
                box = tuple(float(v) for v in box)
            except (TypeError, ValueError):
                return
            self.tracks[object_id] = Detection(object_id, box, now)

    def decide(self, now: float) -> dict | None:
        """返回本步要发的移动向量，或 None（不动）。"""
        with self.lock:
            for object_id in [k for k, d in self.tracks.items() if now - d.received_at > 30]:
                del self.tracks[object_id]
            if any(now - d.received_at <= DETECTION_MAX_AGE for d in self.tracks.values()):
                self.last_person_at = now
            fresh = [d for d in self.tracks.values()
                     if d.received_at >= self.next_look_at and now - d.received_at <= DETECTION_MAX_AGE]
            if not fresh:
                return None
            # Frigate 偶尔留下包住真人的大“幽灵框”（实测：真人框被另一个大框整个包住），去掉外层那个
            fresh = [d for d in fresh if not any(o is not d and _contains(d.box, o.box) for o in fresh)] or fresh
            # 继续跟当前目标；目标丢失时换成画面里最大（最近）的人
            current = next((d for d in fresh if d.object_id == self.target), None)
            width, height = self.frame_size(now)
            target = current or max(fresh, key=lambda d: (d.box[2] - d.box[0]) * (d.box[3] - d.box[1]))
            self.target = target.object_id

        x1, y1, x2, y2 = target.box
        offset = ((x1 + x2) / 2 / width) * 2 - 1  # -1 左 .. 1 右
        distance = person_distance(target.box, width, height)

        rx = 0
        if abs(offset) > DEAD_ZONE:
            magnitude = max(MIN_TURN, min(TURN_GAIN, round(abs(offset) * TURN_GAIN)))
            rx = magnitude if offset > 0 else -magnitude
        ly = 0
        if distance == 'far' and abs(offset) <= FORWARD_MAX_OFFSET:
            ly = -FORWARD_SPEED  # ly < 0 为前进

        status = ('too_close' if distance == 'close' else
                  'centered' if not (rx or ly) else 'following')
        self.state.update(tracking_status=status, tracking_offset=round(offset, 2), tracking_distance=distance,
                          tracking_height_ratio=round((y2 - y1) / height, 2))
        if not (rx or ly):
            return None
        return {'ly': ly, 'rx': rx}

    def _send(self, suffix: str, payload: str, what: str) -> bool:
        try:
            self.command(suffix, payload)
            if suffix != 'dock':
                self.last_move_wall = time.time()
            return True
        except Exception as exc:
            self.state.update(tracking_last_error=type(exc).__name__)
            LOG.warning('%s failed: %s', what, exc)
            return False

    def step(self, now: float | None = None) -> dict | None:
        now = time.monotonic() if now is None else now
        if not self.allowed():
            self.state.update(tracking_status='paused')
            return None
        if now < self.rest_until:
            self.state.update(tracking_status='resting', tracking_rest_seconds=round(self.rest_until - now))
            return None
        snapshot = self.state.snapshot()
        # 充电状态定期刷新；只采信最后一次移动之后读到的“在充电”，
        # 否则刚驶离充电座时的旧状态会被当成还在座上
        fresh_state = snapshot.get('robot_state_checked_at', 0) > self.last_move_wall + 5
        on_dock = snapshot.get('robot_charging') is True and fresh_state
        # “被跟随带离”只记在内存里，程序重启就丢了（实测：重启后机器人在外面待了 46 分钟没回去）。
        # 机器人自己报告“没在充电”也算不在座上；位置不明，所以这种情况不转圈找人、直接回充
        self.off_dock_reported = snapshot.get('robot_charging') is False and fresh_state
        if on_dock:
            # 回到充电座（不论谁送回去的）：停止计时，直到下次被跟随带离
            self.left_dock = self.searching = False
            self.search_steps_left = 0
            self.travel = 0.0
            self.dock_requested_at = None
        move = self.decide(now)
        if now - self.last_person_at <= DETECTION_MAX_AGE:
            if self.searching or self.dock_requested_at:
                LOG.info('Person found again; cancelling search / return to dock')
            self.searching = False
            self.search_steps_left = 0
            self.dock_requested_at = None
            if move is None:
                return None
            if on_dock:
                # 在座上不能原地转（会卡住），先笔直开出去，下一轮再按人的位置调整
                move = {'ly': -EXIT_SPEED, 'rx': 0}
                if self._send('move/vector', json.dumps({'lx': 0, 'ly': -EXIT_SPEED, 'rx': 0, 'ry': 0,
                                                         'hold': EXIT_HOLD, 'buttons': 1}), 'Leave dock'):
                    LOG.info('Person seen; driving straight off the dock first')
                    self.left_dock = True
                    self.travel += EXIT_SPEED / 60 * EXIT_HOLD
                self.next_look_at = now + EXIT_HOLD + SETTLE
                self.state.update(tracking_status='leaving_dock')
                return move
            payload = json.dumps({'lx': 0, 'ly': move['ly'], 'rx': move['rx'], 'ry': 0,
                                  'hold': MOVE_HOLD, 'buttons': 1})
            if self._send('move/vector', payload, 'Move command'):
                LOG.info('Following person: ly=%d rx=%d', move['ly'], move['rx'])
                self.left_dock = True
                self.travel += abs(move['ly']) / 60 * MOVE_HOLD
            self.next_look_at = now + MOVE_HOLD + SETTLE
            return move
        return self._absent_step(now)

    def _absent_step(self, now: float) -> dict | None:
        absent = now - self.last_person_at
        self.state.update(tracking_absent_seconds=round(absent))
        if not self.left_dock and not self.off_dock_reported:
            self.state.update(tracking_status='idle')
            return None
        if self.dock_requested_at is not None:
            if now - self.dock_requested_at < DOCK_RETRY_SECONDS:
                self.state.update(tracking_status='returning_to_dock')
                return None
            LOG.warning('Robot not charging %ds after dock command; retrying', DOCK_RETRY_SECONDS)
            self.dock_requested_at = None
        if absent < self.dock_after:
            self.state.update(tracking_status='waiting')
            return None
        if now < self.next_look_at:
            return None
        if not self.searching:
            self.searching = True
            if self.travel < NEAR_DOCK_TRAVEL:
                LOG.info('No person for %.0f min and still near the dock (travel %.1f); docking without a search',
                         absent / 60, self.travel)
                self.search_steps_left = 0
            else:
                LOG.info('No person for %.0f min; turning around to look before returning to dock',
                         absent / 60)
                self.search_steps_left = SEARCH_STEPS
        if self.search_steps_left > 0:
            self.search_steps_left -= 1
            self._send('cmd', json.dumps({'id': 103001, 'data': {'angle': SEARCH_STEP_DEGREES}}),
                       'Search rotation')
            self.next_look_at = now + SEARCH_STEP_WAIT
            self.state.update(tracking_status='searching')
            return {'rotate': SEARCH_STEP_DEGREES}
        self.searching = False
        if self._send('dock', '', 'Dock command'):
            LOG.info('Returning to dock (no person for %.0f min)', absent / 60)
        self.dock_requested_at = now
        self.state.update(tracking_status='returning_to_dock')
        return {'dock': True}

    def rest(self, seconds: float):
        """已经发了回充命令：一段时间内不追人，也不再自己转圈找人/重复回充。"""
        now = time.monotonic()
        self.rest_until = now + seconds
        self.searching = False
        self.search_steps_left = 0
        self.dock_requested_at = now
        self.state.update(tracking_status='resting', tracking_rest_seconds=round(seconds))
        LOG.info('Resting on the dock for %.0f min (voice request); not following people', seconds / 60)

    def run(self):
        while not self.stopping.wait(0.2):
            self.step()

    def start_mqtt(self):
        import paho.mqtt.client as mqtt

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='ebo-qwen-tracker')

        def on_connect(c, _userdata, _flags, reason, _properties):
            connected = not reason.is_failure
            self.state.update(mqtt_connected=connected)
            if connected:
                c.subscribe('frigate/events', 0)

        def on_disconnect(_c, _userdata, _flags, _reason, _properties):
            self.state.update(mqtt_connected=False)

        def on_message(_c, _userdata, message):
            if message.retain:
                return
            try:
                payload = json.loads(message.payload)
                if isinstance(payload, dict):
                    self.observe(payload)
            except ValueError:
                LOG.warning('Ignored malformed Frigate event')

        client.on_connect, client.on_disconnect, client.on_message = on_connect, on_disconnect, on_message
        client.connect_async(self.config.mqtt_host, self.config.mqtt_port)
        client.loop_start()  # 断线自动重连
        self.client = client

    def stop(self):
        self.stopping.set()
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
        try:
            self.command('move/vector', json.dumps({'lx': 0, 'ly': 0, 'rx': 0, 'ry': 0, 'hold': 0, 'buttons': 0}))
        except Exception:
            pass
