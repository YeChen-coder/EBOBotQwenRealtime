"""主运行时 - 简化版本，专注于通义千问 API 集成"""
from __future__ import annotations
import asyncio
import hmac
import ipaddress
import json
import logging
import signal
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

import websocket

from config import Config, Profile
from conversation import Conversation
from ebo_transport import AudioStore, AssistantOutputStore, EboAPI, Speaker, TranscriptStore
from monitoring import (DASHBOARD_HTML, LogBuffer, add_ice_host, mjpeg_url,
                        recent_conversation, setup_logging, whep_url)
from greeter import HomecomingGreeter
from weather_brief import WeatherBrief
from person_tracker import PersonTracker
from scan_detector import ScanDetector
from standby_voice import StandbyVoice, VoiceTurn
from state import RuntimeState

LOG = logging.getLogger('ebo-qwen-runtime')

GUEST_PROFILE = Profile('guest', '', '家人')


class Supervisor:
    """会话监督器 - 管理对话生命周期"""

    def __init__(self, config: Config, state: RuntimeState, speaker: Speaker,
                 transcripts: TranscriptStore, outputs: AssistantOutputStore):
        self.config = config
        self.state = state
        self.speaker = speaker
        self.transcripts = transcripts
        self.outputs = outputs

        self.lock = threading.RLock()
        self.active: Conversation | None = None
        self.worker: threading.Thread | None = None
        self.stopping = threading.Event()
        self.enabled = True
        self.wake = StandbyVoice(config, state, self.voice_eligible, self.accept_voice)
        self.tracker = None  # main() 在启用视觉跟随时设置
        self.weather = None  # main() 在启用早上天气播报时设置
        self.wake.on_quiet_request = self.handle_quiet_request
        self.wake.on_dock_request = self.handle_dock_request

        state.update(assistant_enabled=self.enabled)

    def handle_quiet_request(self):
        """用户让它闭嘴：结束当前对话；一段时间内只有唤醒词能叫醒它（仍然本地听写，但不发任何请求）。"""
        self.wake.quiet(self.config.quiet_seconds)
        with self.lock:
            if self.active:
                self.active.request_abort()

    def dock_and_rest(self) -> dict:
        """看板按钮：不在座上充电就回座；不论在不在，都开始冷却（调试频繁重启时防止它又跑出来）。"""
        # 不能用看板缓存的状态：它 10 秒才刷新一次，机器人刚开出去时还显示“在充电”。现查一次；
        # 刚动过的话 Engine 上报的 charging 也还没更新，宁可多发一次回充
        charging = False
        try:
            charging = str(self.speaker.ebo.robot_state().get('charging')).lower() == 'true'
        except Exception as exc:
            LOG.warning('Cannot read robot state before docking: %s', exc)
        if self.tracker is not None and time.time() - self.tracker.last_move_wall < 20:
            charging = False
        if not charging:
            try:
                self.speaker.ebo.command('dock')
                LOG.info('Dashboard: returning to dock and starting the cooldown')
            except Exception as exc:
                LOG.warning('Dock command failed: %s', exc)
        else:
            LOG.info('Dashboard: already charging; starting the cooldown only')
        if self.tracker is not None:
            self.tracker.rest(self.config.dock_rest_seconds)
        return {'accepted': True, 'docking': not charging,
                'rest_seconds': self.config.dock_rest_seconds if self.tracker is not None else 0}

    def handle_dock_request(self):
        """用户说让它回去充电：回充电座，并在一段时间内不追人（否则一看到人又开出来）。"""
        try:
            self.speaker.ebo.command('dock')
            LOG.info('Returning to dock on voice request')
        except Exception as exc:
            LOG.warning('Dock command failed: %s', exc)
        if self.tracker is not None:
            self.tracker.rest(self.config.dock_rest_seconds)
        self.state.update(last_voice_dock_at=time.time())

    def greet_homecoming(self, face_name: str) -> bool:
        """Frigate 认出家人刚回家：主动开口打招呼。正在聊天、暂停、安静模式时不打扰。"""
        profile = next((p for p in self.config.profiles if p.face_name == face_name), None)
        if profile is None or self.wake.quiet_active():
            return False
        name = profile.display_name
        # 家人的工作等背景属于隐私，放在 .env（EBO_GREET_CONTEXT），不进代码仓库
        background = f'关于{name}：{self.config.greet_context}\n' if self.config.greet_context else ''
        prompt = (
            f'{name}刚刚下班回到家，现在就在你面前。你要主动开口，像家里盼着她回来的小伙伴一样，'
            f'开心地迎接她，开启一段聊天，而不是打完招呼就结束。\n'
            f'{background}'
            f'这一轮先说两三句：欢迎{name}回家，心疼她上班辛苦（比如让她先坐下歇会儿、喝口水），'
            f'然后用一个轻松、具体、好回答的问题把话头递给她。每次只问一个问题，'
            f'也不要每天都问同一句。第一个问题要轻松、往好处问（累不累、今天顺不顺、想吃点什么），'
            f'别一进门就提糟心事；不要假设她今天发生了什么（比如嗓子疼、挨了气），她说了才算。\n'
            f'她回答以后，顺着她的话继续聊：她累了就多体贴、少提问；她想吐槽就认真听、站在她这边，'
            f'先共情、肯定她的辛苦，不要讲大道理、不要急着给建议、不要替别人说话，'
            f'也不要追问事情的细节和原因（那是让她再讲一遍糟心事）；'
            f'聊工作聊够了，就帮她把心思从工作上拉回来，聊点轻松的（吃什么、休息、开心的小事）；'
            f'她不想多说就安静陪着，告诉她有事随时叫你。'
            f'你是地上的小机器人，只能说话、跟着她走，不要主动说要帮她拿东西、做饭、看冰箱这类做不到的事。\n'
            f'语气口语、温暖、自然，每次两三句，不要长篇大论，不要提摄像头、人脸识别或“检测到”。'
        )
        with self.lock:
            if not self.voice_eligible():
                LOG.info('Skipped homecoming greeting for %s: busy or paused', name)
                return False
            try:
                return self.start(profile.id, 'greeting', opening_prompt=prompt)
            except ValueError as exc:
                LOG.info('Skipped homecoming greeting for %s: %s', name, exc)
                return False

    def play_prepared(self, pcm: bytes, label: str) -> bool:
        """播一段准备好的音频（早上的天气）。正在聊天、暂停、安静模式时不播，返回 False 稍后再试。"""
        with self.lock:
            if not self.voice_eligible() or self.wake.quiet_active():
                return False
            self.speaker.finish_stream(pcm, f'{label}_{int(time.time())}')
            return True

    def voice_eligible(self) -> bool:
        snapshot = self.state.snapshot()
        return (self.enabled and not self.stopping.is_set() and self.active is None
                and snapshot['listener_ready'] and snapshot['source_audio_ok']
                and snapshot['audio_streaming'] and not self.speaker.input_muted())

    def accept_voice(self, turn: VoiceTurn) -> bool:
        """唤醒句已验证 -> 以访客身份开启会话（当前版本没有人脸识别）"""
        with self.lock:
            if not self.voice_eligible():
                return False
            return self.start('guest', 'voice', turn)

    def start(self, user_id: str = 'mother', trigger: str = 'manual',
              initial_turn: VoiceTurn | None = None, opening_prompt: str = '') -> bool:
        """启动新会话"""
        profile = (GUEST_PROFILE if user_id == 'guest' and trigger == 'voice'
                   else self.config.profile(user_id))

        with self.lock:
            if not self.enabled:
                raise ValueError('assistant_disabled')

            if self.stopping.is_set() or self.active is not None:
                return False

            snapshot = self.state.snapshot()
            ready = (self.voice_eligible() if trigger == 'voice'
                     else snapshot['listener_ready'] and snapshot['audio_ready'])
            if not ready:
                raise ValueError('media_not_ready')

            conversation = Conversation(
                self.config, profile, self.speaker,
                self.state, self.transcripts, self.outputs, trigger
            )
            conversation.initial_turn = initial_turn
            conversation.opening_prompt = opening_prompt
            conversation.addressee = self.wake.addressee
            conversation.wake_words = self.wake.wake_words
            conversation.on_dock_request = self.handle_dock_request
            conversation.on_quiet_request = self.handle_quiet_request
            conversation.local_transcribe = self.wake.transcriber.transcribe

            # 唤醒句转录期间用户可能还在说：排队的后续语句和正在说的半句
            # 都交给会话，放在实时音频之前送达，不能被 wake.reset() 丢掉。
            for pcm in self.wake.take_buffered_audio():
                conversation.startup_audio.append(pcm)

            self.active = conversation
            self.state.update(session_state='connecting', active_user=user_id)

            def run():
                try:
                    asyncio.run(conversation.run())
                except Exception as exc:
                    self.state.update(last_error=f'session_failed:{type(exc).__name__}')
                    LOG.warning('Session failed: %s', type(exc).__name__)
                finally:
                    self.state.increment('sessions_completed')
                    with self.lock:
                        self.active = None
                        self.state.update(
                            session_state='standby',
                            active_user=None,
                            session_id=None,
                            realtime_connected=False
                        )

            self.worker = threading.Thread(target=run, name='conversation', daemon=True)
            self.worker.start()
            return True

    def close(self) -> bool:
        """关闭当前会话"""
        with self.lock:
            if self.active:
                self.active.request_close()
                return True
            return False

    def set_enabled(self, enabled: bool) -> bool:
        """启用/禁用助手"""
        with self.lock:
            self.enabled = enabled
            self.state.update(assistant_enabled=enabled)
            self.wake.reset()

            if not enabled:
                if self.active:
                    self.active.request_abort()
                self.speaker.suspend()
            else:
                self.speaker.resume()

            return True

    def offer_audio(self, pcm: bytes):
        """提供音频给活动会话"""
        with self.lock:
            if not self.enabled:
                return
            if self.active:
                self.active.offer_audio(pcm)
            else:
                self.wake.offer_audio(pcm)

    def stop(self):
        """停止监督器"""
        self.stopping.set()
        self.wake.stop()
        with self.lock:
            if self.active:
                self.active.request_abort()
        if self.worker:
            self.worker.join(timeout=30)


class MediaCapture:
    """EBO 音频捕获"""

    def __init__(self, config: Config, supervisor: Supervisor,
                 ebo: EboAPI, state: RuntimeState):
        self.config = config
        self.supervisor = supervisor
        self.ebo = ebo
        self.state = state
        self.stopping = threading.Event()
        self.socket = None

    def _audio_connection(self):
        """建立音频连接"""
        parts = urlsplit(self.config.talk_stream_url)
        url = self.config.audio_stream_url or urlunsplit(
            (parts.scheme, parts.netloc, '/listen', '', '')
        )
        return websocket.create_connection(url, timeout=10)

    def _receive_audio(self, connection):
        """接收音频流"""
        # 发送握手消息
        connection.send(json.dumps({
            'type': 'start',
            'token': self.config.ebo_api_token,
            'node': self.config.ebo_node,
            'rate': 24000,
            'channels': 1,
            'format': 'pcm16'
        }))

        # 等待就绪响应
        ready = json.loads(connection.recv())
        if ready != {'type': 'ready', 'rate': 24000, 'channels': 1, 'format': 'pcm16'}:
            raise ValueError('invalid_audio_stream_handshake')

        self.state.update(
            audio_capture_error='',
            audio_capture_transport='pcm_websocket'
        )
        LOG.info('Robot microphone stream connected')

        # 接收音频数据
        pending = bytearray()
        while not self.stopping.is_set():
            chunk = connection.recv()
            if not isinstance(chunk, bytes) or not chunk or len(chunk) % 2:
                raise ValueError('invalid_audio_stream_pcm')

            pending.extend(chunk)

            # 以 4800 字节（100ms @ 24kHz）为单位处理
            while len(pending) >= 4800:
                pcm = bytes(pending[:4800])
                del pending[:4800]
                self.state.mark_media('audio')
                self.supervisor.offer_audio(pcm)

    def audio_loop(self):
        """音频接收循环"""
        while not self.stopping.is_set():
            try:
                self.socket = self._audio_connection()
                self._receive_audio(self.socket)
            except Exception as exc:
                if not self.stopping.is_set():
                    self.state.update(audio_capture_error=type(exc).__name__)
                    LOG.warning('Audio stream unavailable: %s', type(exc).__name__)
            finally:
                if self.socket:
                    self.socket.close()
                    self.socket = None

            if not self.stopping.is_set():
                self.ebo.wake_if_due()
                self.stopping.wait(3)

    def audio_health_loop(self):
        """音频健康检查循环；每分钟顺带检查机器人状态（充电/桌面模式）"""
        next_robot_check = 0.0
        next_night_vision = 0.0
        while not self.stopping.is_set():
            if time.monotonic() >= next_robot_check:
                # 视觉跟随要尽快知道机器人是否在充电座上（在座上不能原地转）
                next_robot_check = time.monotonic() + (10 if self.config.tracking_enabled else 60)
                try:
                    robot = self.ebo.robot_state()
                    if self.config.floor_mode and self.ebo.ensure_floor_mode(robot):
                        robot['desktop_mode'] = 'false'
                    if self.config.night_vision_auto and time.monotonic() >= next_night_vision:
                        self.ebo.set_night_vision_auto()
                        next_night_vision = time.monotonic() + 6 * 3600
                    self.state.update(
                        robot_docked=str(robot.get('docked')).lower() == 'true',
                        robot_charging=str(robot.get('charging')).lower() == 'true',
                        robot_desktop_mode=str(robot.get('desktop_mode')).lower() == 'true',
                        robot_battery=robot.get('battery'),
                        robot_state_checked_at=time.time(),
                        robot_state_error='',
                    )
                except Exception as exc:
                    # 一起启动时 Engine 往往还没就绪，稍后就重试，不必等满一分钟
                    next_robot_check = time.monotonic() + 10
                    self.state.update(robot_state_error=type(exc).__name__)
                    LOG.warning('Robot state check failed: %s', exc)

            try:
                health = self.ebo.media_health()
                self.state.update(
                    engine_audio_health=health['audio'],
                    engine_audio_checked_at=time.time(),
                    engine_audio_error='',
                    engine_video_health=health['video'],
                    engine_video_checked_at=time.time(),
                    engine_video_error=''
                )
            except Exception as exc:
                self.state.update(
                    engine_audio_error=type(exc).__name__,
                    engine_video_error=type(exc).__name__
                )

            self.stopping.wait(5)

    def stop(self):
        """停止音频捕获"""
        self.stopping.set()
        if self.socket:
            self.socket.close()


def make_http_handler(store: AudioStore, state: RuntimeState,
                     supervisor: Supervisor, config: Config, log_buffer: LogBuffer):
    """创建 HTTP 请求处理器"""

    class Handler(BaseHTTPRequestHandler):
        def send_json(self, status: int, payload):
            # numpy 数字等非标准类型也要能输出，不能让一个字段让整个看板失效
            raw = json.dumps(payload, ensure_ascii=False,
                             default=lambda o: o.item() if hasattr(o, 'item') else str(o)).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            path, _, query = self.path.partition('?')
            if path in {'/', '/dashboard'}:
                raw = DASHBOARD_HTML.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(raw)
                return

            if path == '/health':
                return self.send_json(200, state.snapshot())

            if path == '/logs':
                params = parse_qs(query)
                try:
                    after = int(params.get('after', ['0'])[0])
                except ValueError:
                    after = 0
                return self.send_json(200, log_buffer.since(after))

            if path == '/conversation':
                return self.send_json(200, recent_conversation(
                    Path(config.transcript_path), Path(config.assistant_transcript_path)))

            if path == '/video/mjpeg':
                boxes = parse_qs(query).get('boxes', ['0'])[0] == '1'
                try:
                    upstream = urllib.request.urlopen(
                        mjpeg_url(config.frigate_url, config.camera_name, boxes), timeout=10)
                except Exception as exc:
                    LOG.warning('MJPEG source unavailable: %s', exc)
                    return self.send_error(502)
                self.close_connection = True
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', upstream.headers.get('Content-Type', 'multipart/x-mixed-replace;boundary=frame'))
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    while chunk := upstream.read1(65536):
                        self.wfile.write(chunk)
                except (OSError, ValueError):
                    pass  # 看板关闭/切换时浏览器断开
                finally:
                    upstream.close()
                return

            if self.path.startswith('/audio/'):
                name = self.path.removeprefix('/audio/').split('?', 1)[0]
                if (Path(name).name != name or
                        not name.startswith('reply-') or
                        not name.endswith('.wav')):
                    return self.send_error(404)

                path = store.directory / name
                if not path.is_file():
                    return self.send_error(404)

                raw = path.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'audio/wav')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(raw)
                return

            self.send_error(404)

        def proxy_whep(self):
            """把浏览器的 WebRTC 邀请转给 Engine 固定的 WHEP 地址（不是开放代理）。"""
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                length = 0
            if not 0 < length <= 64 * 1024:
                return self.send_error(413)
            req = urllib.request.Request(whep_url(config.rtsp_url), data=self.rfile.read(length),
                                         method='POST', headers={'Content-Type': 'application/sdp'})
            try:
                with urllib.request.urlopen(req, timeout=15) as response:
                    status, body = response.status, response.read()
                    ctype = response.headers.get('Content-Type', 'application/sdp')
            except urllib.error.HTTPError as exc:
                status, body = exc.code, exc.read()
                ctype = exc.headers.get('Content-Type', 'text/plain')
            except Exception as exc:
                LOG.warning('WebRTC video unavailable: %s', exc)
                return self.send_error(502)
            host = urlsplit('//' + self.headers.get('Host', '')).hostname or ''
            host = '127.0.0.1' if host == 'localhost' else host
            try:
                ipaddress.ip_address(host)
            except ValueError:
                host = ''
            if status in (200, 201) and host:
                body = add_ice_host(body.decode('utf-8'), host).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path == '/video/whep':
                return self.proxy_whep()
            if self.path == '/dashboard/weather-test':
                if self.headers.get('X-EBO-Dashboard') != '1':
                    return self.send_json(403, {'error': 'dashboard_only'})
                if supervisor.weather is None:
                    return self.send_json(200, {'accepted': False, 'error': 'weather_disabled'})
                try:
                    return self.send_json(200, {'accepted': True, 'script': supervisor.weather.test_now()})
                except Exception as exc:
                    LOG.warning('Weather test failed: %s', exc)
                    return self.send_json(200, {'accepted': False, 'error': str(exc)[:200]})
            if self.path == '/dashboard/end-rest':
                if self.headers.get('X-EBO-Dashboard') != '1':
                    return self.send_json(403, {'error': 'dashboard_only'})
                if supervisor.tracker is None:
                    return self.send_json(200, {'accepted': False, 'error': 'tracking_disabled'})
                return self.send_json(200, {'accepted': True, 'was_resting': supervisor.tracker.end_rest()})
            if self.path == '/dashboard/dock-rest':
                # 看板本机按钮，不要令牌；要求自定义请求头：别的网页跨站发不出这个头（浏览器会先预检，这里不应答）
                if self.headers.get('X-EBO-Dashboard') != '1':
                    return self.send_json(403, {'error': 'dashboard_only'})
                return self.send_json(202, supervisor.dock_and_rest())
            if self.path not in {'/session/start', '/session/close', '/robot/dock',
                                 '/assistant/pause', '/assistant/resume'}:
                return self.send_error(404)

            token = self.headers.get('X-EBO-Control-Token', '')
            if not token or not hmac.compare_digest(token, config.control_token):
                return self.send_json(403, {'error': 'invalid_control_token'})

            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1024:
                    return self.send_json(413, {'error': 'invalid_body_size'})

                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('invalid_body')

                if self.path == '/robot/dock':
                    # 和语音“回去充电”一样：回充，并让视觉跟随休息，免得看到人又开出来
                    supervisor.handle_dock_request()
                    return self.send_json(202, {'accepted': True})

                if self.path in {'/assistant/pause', '/assistant/resume'}:
                    supervisor.set_enabled(self.path.endswith('/resume'))
                    return self.send_json(200, {
                        'accepted': True,
                        'assistant_enabled': state.snapshot()['assistant_enabled']
                    })

                if self.path == '/session/start':
                    started = supervisor.start(body.get('user_id', 'mother'))
                    return self.send_json(
                        202 if started else 409,
                        {
                            'accepted': started,
                            'error': None if started else 'session_busy'
                        }
                    )

                return self.send_json(202, {'accepted': supervisor.close()})

            except (ValueError, TypeError) as exc:
                code = str(exc)
                return self.send_json(
                    409 if code == 'assistant_disabled' else 400,
                    {
                        'error': code if code in {
                            'unknown_user', 'media_not_ready', 'assistant_disabled'
                        } else 'invalid_body'
                    }
                )
            except OSError:
                return self.send_json(503, {'error': 'service_error'})

        def log_message(self, _format, *_args):
            pass

    return Handler


def main():
    """主入口"""
    # 加载配置
    config = Config.from_env()
    log_buffer = setup_logging(config.log_dir)
    LOG.info('Configuration loaded: Qwen region=%s, model=%s, engine=%s, frigate=%s, logs=%s',
             config.qwen_region, config.qwen_model, config.ebo_api_url, config.frigate_url,
             Path(config.log_dir).resolve())

    # 初始化状态
    state = RuntimeState()
    state.update(
        media_stale_after_seconds=config.media_stale_after_seconds,
        media_startup_grace_seconds=config.media_startup_grace_seconds,
        barge_in_enabled=config.barge_in_enabled,
        engine_audio_monitor_enabled=True,
        engine_video_monitor_enabled=True,
        interruption_control='local_echo_gate' if config.barge_in_enabled else 'half_duplex',
        playback_tail_ms=config.playback_tail_ms,
        engine_api_url=config.ebo_api_url,
        web_search_enabled=config.web_search_enabled,
        wake_words=config.wake_words,
        addressee_check=config.addressee_check,
        tracking_enabled=False,
        tracking_status='disabled',
        tracking_last_error='',
        audio_capture_error='',
        audio_capture_transport='',
        robot_docked=None,
        robot_charging=None,
        robot_desktop_mode=None,
        robot_state_error='',
    )

    # 初始化组件
    ebo = EboAPI(config, state)
    store = AudioStore(Path(config.output_audio_dir), state)
    transcripts = TranscriptStore(Path(config.transcript_path), state, source='gate')
    outputs = AssistantOutputStore(Path(config.assistant_transcript_path), store, state)
    speaker = Speaker(config, ebo, store, state)
    supervisor = Supervisor(config, state, speaker, transcripts, outputs)
    capture = MediaCapture(config, supervisor, ebo, state)

    # 启动 HTTP 服务器
    httpd = ThreadingHTTPServer(
        ('0.0.0.0', config.http_port),
        make_http_handler(store, state, supervisor, config, log_buffer)
    )

    # 启动工作线程
    threads = [
        threading.Thread(target=capture.audio_loop, daemon=True, name='audio'),
        threading.Thread(target=capture.audio_health_loop, daemon=True, name='audio-health'),
        threading.Thread(target=httpd.serve_forever, daemon=True, name='http'),
        threading.Thread(target=supervisor.wake.run, daemon=True, name='standby-voice'),
    ]

    tracker = None
    if config.tracking_enabled:
        # 暂停（隐私）时绝不移动；用户说话时不动，避免电机噪声干扰收音
        tracker = PersonTracker(
            config, state, ebo.command,
            allowed=lambda: supervisor.enabled and not state.snapshot()['input_speech_active'],
        )
        if config.tracking_scan_enabled:
            try:
                scanner = ScanDetector(config.scan_model_path,
                                       f'{config.frigate_url.rstrip("/")}/api/{config.camera_name}/latest.jpg',
                                       config.scan_min_score)
                tracker.scan = scanner.people
                LOG.info('Search scan enabled (min score %.2f)', config.scan_min_score)
            except Exception as exc:
                LOG.warning('Search scan unavailable, relying on Frigate only: %s', exc)
        tracker.start_mqtt()
        supervisor.tracker = tracker
        threads.append(threading.Thread(target=tracker.run, daemon=True, name='person-tracker'))
        LOG.info('Visual person following enabled')

    if config.weather_enabled:
        supervisor.weather = WeatherBrief(config, lambda pcm: supervisor.play_prepared(pcm, 'weather'))
        supervisor.weather.start()

    greeter = None
    if config.proactive_greeting:
        greeter = HomecomingGreeter(config, supervisor.greet_homecoming)
        greeter.start_mqtt()

    stop = threading.Event()

    def shutdown(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    for thread in threads:
        thread.start()

    state.update(listener_ready=True, session_state='standby')

    LOG.info('EBO Qwen Realtime Assistant ready on port %d', config.http_port)

    # 主循环
    while not stop.wait(1):
        if any(not t.is_alive() for t in threads):
            LOG.error('A service worker stopped unexpectedly')
            break

    # 清理
    if tracker:
        tracker.stop()
    if greeter is not None:
        greeter.stop()
    if supervisor.weather is not None:
        supervisor.weather.stop()
    capture.stop()
    supervisor.stop()
    httpd.shutdown()
    httpd.server_close()

    LOG.info('Shutdown complete')


if __name__ == '__main__':
    main()
