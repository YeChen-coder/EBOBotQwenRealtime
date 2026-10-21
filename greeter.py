"""家人回家时主动打招呼（“妈妈回来啦，上班辛苦了”）。

Frigate 认出人脸后在 frigate/events 里带上 sub_label = [名字, 分数]。
“刚回家”的判断：这个人已经 greet_away_seconds（默认 3 小时）没被认出过，
且现在在 greet_hours 时段内（默认 11~23 点：妈妈早上 5、6 点起床、6、7 点出门，
不能把早上起床当成回家；最早可能中午回来，大多晚上 5~10 点）。
每次认出都会刷新“最近见过”，所以在家走来走去不会反复打招呼；每人每天最多欢迎一次
（晚上在别的房间待了 3 小时再出来，不该再说一遍“欢迎回家”）。
“最近见过”存在 timers 文件里，跨重启保留。
"""
from __future__ import annotations
import json
import logging
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Callable

import timers

LOG = logging.getLogger('ebo-greeter')


def parse_hours(text: str) -> tuple[int, int]:
    start, _, end = (text or '11-23').partition('-')
    return int(start), int(end)


class HomecomingGreeter:
    def __init__(self, config, greet: Callable[[str], bool]):
        self.config = config
        self.greet = greet  # greet(人脸名) -> 是否真的开口了
        self.names = tuple(n.strip() for n in config.greet_names.split(',') if n.strip())
        self.start_hour, self.end_hour = parse_hours(config.greet_hours)
        # 实测：程序跑在多伦多的电脑上，妈妈在国内晚上 7:49 到家，按多伦多早上 7:49 算成“不在迎接时段”
        self.tz = ZoneInfo(config.home_timezone) if config.home_timezone else None
        self.lock = threading.Lock()
        self.client = None
        # 第一次启用（没有记录）时当作“刚见过”：否则程序一启动，在家的人一出现就被欢迎回家
        for name in self.names:
            if not timers.has(config.timers_path, self._key(name)):
                self._mark_seen(name, time.time())

    @staticmethod
    def _key(name: str) -> str:
        return f'seen:{name}'

    def _mark_seen(self, name: str, now: float):
        # 存的是“到这个时刻为止都算刚见过”，用 timers.remaining 判断
        timers.save(self.config.timers_path, self._key(name), now + self.config.greet_away_seconds)

    def in_hours(self, now: float) -> bool:
        hour = datetime.fromtimestamp(now, self.tz).hour
        return self.start_hour <= hour < self.end_hour

    def observe(self, payload: dict, now: float | None = None) -> bool:
        """处理一条 frigate/events；该打招呼时调用 greet，返回是否打了招呼。"""
        now = time.time() if now is None else now
        event = payload.get('after') or {}
        sub = event.get('sub_label')
        if event.get('label') != 'person' or not isinstance(sub, (list, tuple)) or len(sub) < 2:
            return False
        name, score = sub[0], sub[1]
        if name not in self.names or not isinstance(score, (int, float)) or score < self.config.min_face_score:
            return False
        with self.lock:
            away = timers.remaining(self.config.timers_path, self._key(name)) <= 0
            self._mark_seen(name, now)
            if not away:
                return False
            if not self.in_hours(now):
                LOG.info('Recognized %s after a long absence, but outside greeting hours', name)
                return False
            if timers.remaining(self.config.timers_path, f'greeted:{name}') > 0:
                LOG.info('Recognized %s after a long absence, but already greeted today', name)
                return False
        LOG.info('Recognized %s (%.2f) after a long absence: greeting', name, score)
        try:
            greeted = bool(self.greet(name))
            if greeted:
                tomorrow = datetime.fromtimestamp(now, self.tz).replace(hour=0, minute=0, second=0, microsecond=0)
                timers.save(self.config.timers_path, f'greeted:{name}', tomorrow.timestamp() + 86400)
            return greeted
        except Exception as exc:
            LOG.warning('Greeting failed: %s', exc)
            return False

    def start_mqtt(self):
        import paho.mqtt.client as mqtt

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='ebo-qwen-greeter')

        def on_connect(c, _userdata, _flags, reason, _properties):
            if not reason.is_failure:
                c.subscribe('frigate/events', 0)

        def on_message(_c, _userdata, message):
            if message.retain:
                return
            try:
                payload = json.loads(message.payload)
            except ValueError:
                return
            if isinstance(payload, dict):
                self.observe(payload)

        client.on_connect, client.on_message = on_connect, on_message
        client.connect_async(self.config.mqtt_host, self.config.mqtt_port)
        client.loop_start()
        self.client = client
        LOG.info('Homecoming greeting enabled for %s (%d-%d h %s, after %.0f h away)',
                 ','.join(self.names), self.start_hour, self.end_hour, self.config.home_timezone or 'local time',
                 self.config.greet_away_seconds / 3600)

    def stop(self):
        if self.client is not None:
            self.client.loop_stop()
            self.client.disconnect()
