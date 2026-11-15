"""靠判断器叫醒的会话：第一句按云端识别再判一次，免判断接话只给 1 次；喊了唤醒词照旧。
语音叫醒时摄像头最近只认出一位家人，就按这位家人开会话。在容器里跑。"""
import asyncio, sys, types
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))
for name in ('asr_realtime', 'audio_utils', 'config', 'qwen_realtime', 'ebo_transport', 'standby_voice', 'state'):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules['asr_realtime'].RealtimeTranscriber = object
sys.modules['audio_utils'].EndOfTurnDetector = lambda **k: None
sys.modules['audio_utils'].resample_24k_to_16k = lambda pcm: pcm
sys.modules['config'].Config = sys.modules['config'].Profile = object
sys.modules['qwen_realtime'].QwenRealtimeClient = sys.modules['qwen_realtime'].QwenConfig = object
sys.modules['ebo_transport'].Speaker = sys.modules['ebo_transport'].TranscriptStore = object
sys.modules['ebo_transport'].AssistantOutputStore = object
sys.modules['standby_voice'].has_wake_word = lambda text, words: '猫猫' in text
sys.modules['standby_voice'].loose_wake_word = lambda *a: False
sys.modules['standby_voice'].valid_reply_text = lambda t: bool(t)
sys.modules['standby_voice'].collapse_repeats = lambda t: t
sys.modules['state'].RuntimeState = object

import conversation
from conversation import GUEST_NOTE, JUDGE_WOKEN_FREE_REPLIES, MAX_FREE_REPLIES, Conversation


class Judge:
    def __init__(self, answers): self.answers, self.asked = list(answers), []
    def is_addressed(self, text, last='', recent=()):
        self.asked.append(text)
        return self.answers.pop(0)


class State:
    def increment(self, *a): pass


class Store:
    def append(self, **k): pass


class Turn:
    def __init__(self, text): self.text, self.pcm, self.item_id = text, b'', 'wake_x'


def make(judge):
    c = Conversation.__new__(Conversation)
    c.wake_words = ('猫猫',)
    c.addressee = judge
    c.state, c.transcripts = State(), Store()
    c.profile = types.SimpleNamespace(id='guest', display_name='家人')
    c.heard, c.memory_lines = conversation.deque(maxlen=4), []
    c.skip_addressee_once, c.free_replies, c.max_free_replies = False, 0, MAX_FREE_REPLIES
    c.ignored_streak, c.background_since = 0, None
    c.last_assistant_text, c.session_started_at, c.last_activity = '', 0, 0
    c.on_quiet_request = c.on_dock_request = None
    c.stop, c.response_active, c.qwen = asyncio.Event(), False, object()
    c.spoken_first = c.opening_prompt = ''
    c.answered = []

    async def transcript(pcm, fut): return pcm.decode()

    async def settle(pcm, respond, text):
        if respond:
            c.answered.append(text)

    async def send(pcm): pass
    c._turn_transcript, c._settle_turn, c._send_turn_audio = transcript, settle, send
    c.turn_detector = types.SimpleNamespace(mark_speech=lambda: None)
    return c


async def say(c, text, is_reply=False):
    await c._respond_if_speech(text.encode(), None, is_reply)


async def main():
    # 判断器叫醒、云端识别出来是电视（实测 11-08：Whisper 写“好的”，云端是“那也太抠了”）：不回，会话还开着
    judge = Judge([False, True, False])
    c = make(judge)
    c.initial_turn = Turn('好的')
    await c._opening()
    assert not c.skip_addressee_once and c.max_free_replies == JUDGE_WOKEN_FREE_REPLIES
    await say(c, '那也太抠了。')
    assert c.answered == [] and judge.asked == ['那也太抠了。'] and c.ignored_streak == 1
    # 下一句判断器认了就回；之后只给 1 次免判断，第 2 次交给判断器
    await say(c, '你在干嘛呢？')
    await say(c, '对。', is_reply=True)
    await say(c, '最实际的，最好的。', is_reply=True)
    assert c.answered == ['你在干嘛呢？', '对。'], c.answered
    assert judge.asked == ['那也太抠了。', '你在干嘛呢？', '最实际的，最好的。']
    # 中途喊了唤醒词：恢复成 3 次
    await say(c, '猫猫你听我说')
    assert c.max_free_replies == MAX_FREE_REPLIES and c.free_replies == 0

    # 喊唤醒词叫醒的：第一句不判断，照样 3 次免判断
    judge = Judge([])
    c = make(judge)
    c.initial_turn = Turn('猫猫你好')
    await c._opening()
    assert c.skip_addressee_once and c.max_free_replies == MAX_FREE_REPLIES
    await say(c, '猫猫你好')
    for said in ('对。', '嗯，行。', '你说呢？'):
        await say(c, said, is_reply=True)
    assert len(c.answered) == 4 and judge.asked == []

    # 判断失败（网络）时照常回，别把真人的话吞掉
    c = make(Judge([None]))
    c.initial_turn = Turn('今天好冷')
    await c._opening()
    await say(c, '今天好冷啊')
    assert c.answered == ['今天好冷啊']

asyncio.run(main())


# 不知道是谁时提示里讲清楚“妈妈”多半是她自己；认出妈妈时不加
def instructions(pid, name):
    c = Conversation.__new__(Conversation)
    c.config = types.SimpleNamespace(instructions='你是猫猫。')
    c.profile = types.SimpleNamespace(id=pid, display_name=name)
    c.memory_context = c.night_context = c.opening_prompt = c.spoken_first = ''
    return c._instructions()

assert GUEST_NOTE in instructions('guest', '家人')
assert GUEST_NOTE not in instructions('mother', '妈妈') and '当前和你说话的是：妈妈' in instructions('mother', '妈妈')


# 语音叫醒时按摄像头最近认出的人（runtime 依赖多，只拿出这个方法来测）
import importlib.util, pathlib
src = (pathlib.Path(__file__).resolve().parents[1] / 'runtime.py').read_text(encoding='utf-8')
ns = {'time': __import__('time'), 'LOG': types.SimpleNamespace(info=lambda *a: None)}
exec(src[src.index('VOICE_FACE_WINDOW ='):].split('\n', 1)[0], ns)
start = src.index('    def recent_speaker(')
body = src[start:src.index('\n    def ', start + 10)]
exec('class S:\n' + body, ns)
P = types.SimpleNamespace
sup = ns['S']()
sup.config = P(profiles=(P(id='mother', face_name='妈妈'), P(id='father', face_name='爸爸')))
now = 10_000.0
sup.greeter = None
assert sup.recent_speaker(now) == 'guest'
sup.greeter = P(seen={})
assert sup.recent_speaker(now) == 'guest'                       # 谁都没认出
sup.greeter = P(seen={'妈妈': now - 300})
assert sup.recent_speaker(now) == 'mother'                      # 5 分钟前认出妈妈
sup.greeter = P(seen={'妈妈': now - 3600})
assert sup.recent_speaker(now) == 'guest'                       # 一小时前的不算
sup.greeter = P(seen={'妈妈': now - 300, '爸爸': now - 60})
assert sup.recent_speaker(now) == 'guest'                       # 两位都在，分不清
print('ok')
