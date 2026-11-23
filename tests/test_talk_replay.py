"""bridge 段错误重启那十几秒里发出的回复要补播一次（11-19 08:18 丢了一句）。
见 docs/incident-2026-11-23-bridge-segfault.md。"""
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ebo_transport  # noqa: E402
from ebo_transport import Speaker  # noqa: E402
from state import RuntimeState  # noqa: E402

ebo_transport.REPLAY_CHECK_DELAY = 0.01
ebo_transport.REPLAY_POLL = 0.01
ebo_transport.REPLAY_SETTLE = 0.01
ebo_transport.REPLAY_WINDOW = 0.5


class FakeEbo:
    def __init__(self, online):
        self.online = list(online)     # bridge_online() 依次返回的值，最后一个一直重复
        self.talks = []

    def bridge_online(self):
        return self.online.pop(0) if len(self.online) > 1 else self.online[0]

    def command(self, suffix, payload=''):
        self.talks.append((suffix, payload))


def speaker(ebo):
    config = types.SimpleNamespace(stream_prebuffer_ms=0, public_audio_base_url='http://a',
                                   playback_tail_ms=0)
    s = Speaker(config, ebo, store=None, state=RuntimeState())
    return s


def run(ebo, newer_reply=False):
    s = speaker(ebo)
    s._talk_seq = 1
    if newer_reply:
        s._talk_seq = 2
    s._replay_if_bridge_down(1, 'http://a/r.wav', 2.0)
    return ebo.talks, s


# bridge 一直在线：不补
talks, _ = run(FakeEbo([True]))
assert talks == [], talks
# 查不到状态（None）：不补，宁可漏也不重复说
talks, _ = run(FakeEbo([None]))
assert talks == [], talks
# 发出时 bridge 已死，过一会儿回来：补播一次
talks, s = run(FakeEbo([False, False, False, True]))
assert talks == [('talk', 'http://a/r.wav')], talks
assert s.state.snapshot()['replies_replayed_after_bridge_restart'] == 1
# 期间已经有更新的回复：不补旧的
talks, _ = run(FakeEbo([False, True]), newer_reply=True)
assert talks == [], talks
# 一直没回来：超时放弃
t0 = time.monotonic()
talks, _ = run(FakeEbo([False]))
assert talks == [] and time.monotonic() - t0 < 2, talks
print('test_talk_replay ok')
