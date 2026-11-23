"""bridge 重连不能再段错误（docs/incident-2026-11-23-bridge-segfault.md）。

37 次 rc=139 全出在“重建 Agora 会话”：
- 同一进程里第二次 initialize Agora 服务（续期后又重连）→ 崩在 agora_service_initialize；
- 换下来的 RTM 客户端只 logout 不留引用，回调表被 Python 回收后原生层再回调 → 崩在 RTM 回调。

要用 Agora SDK，所以在 engine 镜像里跑（stderr 输出，ebo_log 会吞掉 stdout）：
  docker run --rm -v "$PWD/tests:/t" --entrypoint python ebo-qwen-realtime-ebo-engine /t/test_bridge_reconnect.py
"""
import sys
import threading
from pathlib import Path

for p in ('/app', str(Path(__file__).resolve().parents[1] / 'engine')):
    sys.path.insert(0, p)

import ebo_bridge  # noqa: E402

calls = []


class FakeSvc:
    def __init__(self):
        calls.append('svc.new')

    def initialize(self, cfg):
        calls.append('svc.initialize')
        return 0

    def release(self):
        calls.append('svc.release')


class FakeRtc:
    def __init__(self, name):
        self.name = name

    def disconnect(self):
        calls.append(f'{self.name}.disconnect')

    def release(self):
        calls.append(f'{self.name}.release')


class FakeRtm:
    def __init__(self, name):
        self.name = name

    def logout(self):
        calls.append(f'{self.name}.logout')
        return 0, 1

    def release(self):
        calls.append(f'{self.name}.release')


ebo_bridge.AgoraService = FakeSvc
b = object.__new__(ebo_bridge.Bridge)
b.s = {'app_id': 'x'}
b.svc = None
b.video_enabled = False
b.agora_apm_enabled = False
b.agora_aec_enabled = b.agora_noise_suppression_enabled = b.agora_agc_enabled = False
b._retired_rtm = []
b._rtm_gen = None

# 1. 服务每个进程只初始化一次
svc = b._agora_service()
assert b._agora_service() is svc
assert calls.count('svc.initialize') == 1, calls

# 2. 重连：旧 RTC 断开并释放，服务不动，旧 RTM 登出且一直留着引用
b.rtc, b.rtm, b.rtc_state, b._observers_registered = FakeRtc('rtc1'), FakeRtm('rtm1'), 'connected', True
b._last_rejoin = 0
b._rejoin_lock = threading.Lock()
b.provider = None
b.robot_uid, b.other_viewers, b.connected, b.account = '1', set(), True, 'me'
b._stop_audio_tx = lambda: calls.append('tx.stop')
b.send = lambda *a, **k: None
reconnects = []


def fake_connect():
    reconnects.append(b._agora_service())
    b.rtc, b.rtm = FakeRtc('rtc2'), FakeRtm('rtm2')


b.connect_agora = fake_connect
calls.clear()
b._force_rejoin()
assert 'svc.release' not in calls and 'svc.initialize' not in calls, calls
assert calls.index('rtc1.disconnect') < calls.index('rtc1.release'), calls
assert 'rtm1.logout' in calls and 'rtm1.release' not in calls, calls
assert [r.name for r in b._retired_rtm] == ['rtm1']
assert reconnects == [svc] and b.rtc_state is None and not b._observers_registered

# 3. 待机断开（set_connected(False)）也一样：释放 RTC，RTM 留引用
b.video = None
b.mqtt = None
b._publish_camera_state = lambda: None
calls.clear()
b.set_connected(False)
assert calls[-3:] == ['rtc2.disconnect', 'rtc2.release', 'rtm2.logout'], calls
assert [r.name for r in b._retired_rtm] == ['rtm1', 'rtm2'] and b.rtm is None and b.rtc is None

print('test_bridge_reconnect ok', file=sys.stderr)
