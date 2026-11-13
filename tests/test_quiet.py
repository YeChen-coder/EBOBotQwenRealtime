"""安静模式要真的结束会话：模型自己答应安静、连接途中被叫停、说到一半被看板叫停。"""
import asyncio, sys, time, types
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
sys.modules['standby_voice'].has_wake_word = lambda *a: False
sys.modules['standby_voice'].loose_wake_word = lambda *a: False
sys.modules['standby_voice'].valid_reply_text = lambda t: bool(t)
sys.modules['standby_voice'].collapse_repeats = lambda t: t
sys.modules['state'].RuntimeState = object

from addressee import may_be_quiet_request, may_promise_quiet
from conversation import Conversation


# 实测漏掉的说法（“甭”不在关键词里），旧的说法也不能丢
for said in ('哎呀,甭说话了,这乱着了啊。一会儿你再说啊。', '说什么的甭说了啊。', '你别吭声', '别再说了', '不要说话', '闭嘴',
             '吵死了', '你好烦', '不想听'):
    assert may_be_quiet_request(said), said
assert not may_be_quiet_request('今天天气不错')
assert may_promise_quiet('好，那我就在这儿安静待着。') and may_promise_quiet('好嘞，我不打扰你。快睡吧')
assert not may_promise_quiet('今天降温了，你裹暖和点')


class Stream:
    def append(self, **k): pass


class Speaker:
    def output_metrics(self, output_id): return {}


class State:
    def update(self, **k): pass


class Judge:
    def __init__(self, answer): self.answer, self.asked = answer, []
    def promised_quiet(self, reply):
        self.asked.append(reply)
        return self.answer


def conversation(answer):
    config = types.SimpleNamespace(vad_silence_duration_ms=800, session_idle_seconds=300, session_max_seconds=2700)
    c = Conversation.__new__(Conversation)
    c.__dict__.update(config=config, state=State(), speaker=Speaker(), outputs=Stream(), stop=asyncio.Event(),
                      close_requested=asyncio.Event(), response_active=True, on_dock_request=None,
                      turn_detector=types.SimpleNamespace(reset=lambda: None),
                      addressee=Judge(answer), current_response_id='r', current_transcript_buffer='',
                      last_assistant_text='', memory_lines=[], session_id='s', last_dock_request=-1e9, quiet=0,
                      heard=[])
    c.on_quiet_promised = lambda: c.__dict__.update(quiet=c.quiet + 1)
    c.loop = asyncio.get_running_loop()
    return c


async def settled():
    for task in asyncio.all_tasks() - {asyncio.current_task()}:
        await task


async def promise_ends_the_session():
    c = conversation(True)
    c._handle_transcript_done('i', '好，那我就在这儿安静待着。')
    await settled()
    assert c.quiet == 1 and c.close_requested.is_set(), '进入安静模式并请求结束'
    assert not c.stop.is_set(), '这句还在回复中：等 response.done 再断'
    c._handle_response_done({})
    assert c.stop.is_set(), '这句回复完就结束会话'


async def going_to_dock_ends_the_session():
    """11-12 实测：它回了充电座，会话还开着，连说四遍“我去充电座待着啦”"""
    c = conversation(True)
    docked = []
    c.on_dock_request = lambda: docked.append(1)
    c._handle_transcript_done('i', '既然你也觉得对，那就赶紧去拿瓶水喝吧。身体要紧，我去充电座待着啦。')
    await settled()
    await asyncio.sleep(.05)
    assert docked == [1] and c.close_requested.is_set() and c.quiet == 0, '回充、结束会话，但不进安静模式'
    c._handle_response_done({})
    assert c.stop.is_set()
    c = conversation(True)
    c.on_dock_request = lambda: None
    c._handle_transcript_done('i', '要我回去充电吗？')
    await settled()
    assert not c.close_requested.is_set(), '问句不算'


async def asked_to_wait_is_not_asked_to_be_quiet():
    c = conversation(True)
    c.heard.append((0, '等我把桌子放到位了。'))
    c._handle_transcript_done('i', '好嘞，你慢慢弄，不着急。我就在这儿安静等着，等你放好了咱们再聊。')
    await settled()
    assert c.quiet == 0 and not c.close_requested.is_set() and not c.addressee.asked, '不进安静模式，也不用问模型'


async def promise_after_the_reply_finished():
    c = conversation(True)
    c.response_active = False
    c._handle_transcript_done('i', '行行行，我闭嘴。')
    await settled()
    assert c.quiet == 1 and c.stop.is_set()


async def hypothetical_keeps_chatting():
    c = conversation(False)
    c._handle_transcript_done('i', '要是您嫌吵，我就安静点。')
    await settled()
    assert c.addressee.asked and c.quiet == 0 and not c.close_requested.is_set()


async def judge_failure_keeps_chatting():
    c = conversation(None)
    c._handle_transcript_done('i', '好，我不打扰你。')
    await settled()
    assert c.quiet == 0 and not c.close_requested.is_set()


async def plain_reply_costs_no_check():
    c = conversation(True)
    c._handle_transcript_done('i', '今天降温了，你裹暖和点。')
    await settled()
    assert not c.addressee.asked and c.quiet == 0


async def session_already_over():
    c = conversation(True)
    c._handle_transcript_done('i', '好，我不打扰你。')
    c.stop.set()
    await settled()
    assert c.quiet == 0


async def abort_while_connecting_never_speaks():
    c = conversation(True)
    c.abort_requested = __import__('threading').Event()
    c.startup_lock = __import__('threading').Lock()
    c.startup_audio = []
    c.session_started_at = time.time()
    c.trigger = 'greeting'
    c.opening = []

    async def connect(): c.abort_requested.set()
    async def opening(): c.opening.append(1)
    async def loop_(): c.opening.append(2)
    async def cleanup(): c.opening.append('cleanup')
    c._init_qwen_client, c._opening, c._conversation_loop, c._cleanup = connect, opening, loop_, cleanup
    await c.run()
    assert c.opening == ['cleanup'], c.opening


for test in (promise_ends_the_session, going_to_dock_ends_the_session, asked_to_wait_is_not_asked_to_be_quiet, promise_after_the_reply_finished, hypothetical_keeps_chatting,
             judge_failure_keeps_chatting, plain_reply_costs_no_check, session_already_over,
             abort_while_connecting_never_speaks):
    asyncio.run(test())
    print('ok', test.__name__)
