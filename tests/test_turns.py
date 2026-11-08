"""语音识别复读要折叠；一轮不能无限长（电视开着时等不到静音）；“我跟你说”直接算在对机器人说。在容器里跑。"""
import asyncio, re, sys, threading, time, types
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))
import conversation
from conversation import MAX_FREE_REPLIES, MAX_TURN_BYTES, TELLING_YOU, Conversation
from standby_voice import collapse_repeats, valid_reply_text

# 实测的复读
looped = '对，今天我跟你说，这个盘要提前铺开，然后你就可以' + '，你就可以' * 150
assert collapse_repeats(looped) == '对，今天我跟你说，这个盘要提前铺开，然后你就可以'
assert collapse_repeats('他怎么' + '怎么' * 60) == '他怎么'
assert collapse_repeats('好，真的有，真的有，真的有，真的有，真的有，这个') == '好，真的有，这个'
# 正常说话不动
for same in ('对对对，转发的。', '真的有，真的有，真的有，真的有', '我跟你说，我跟你说，今天下雨', '花了2000000块', '谢谢谢谢', '今天天气不错，我们出去走走吧'):
    assert collapse_repeats(same) == same, same
# 整句都是复读的，仍然先被当成没说话
assert not valid_reply_text('你就可以你就可以你就可以你就可以')
started = time.monotonic()
collapse_repeats(''.join(chr(0x4e00 + i * 7 % 900) for i in range(5000)))
assert time.monotonic() - started < .5

for said in ('对，今天我跟你说，我今天做了什么', '我跟你讲，这个不行', '我告诉你一件事', '我给你说啊'):
    assert TELLING_YOU.search(said), said
for said in ('他跟你说了吗', '你跟我说说', '今天天气不错', '他应该以前就有吧'):
    assert not TELLING_YOU.search(said), said


class Detector:
    has_speech = True
    def observe(self, pcm): return True, False   # 一直有声音，等不到静音
    def reset(self): pass


class Speaker:
    def input_muted(self): return False


class State:
    def update(self, **kw): pass


async def long_turn():
    c = Conversation.__new__(Conversation)
    c.stop = asyncio.Event()
    c.audio_queue = asyncio.Queue()
    c.turn_detector, c.speaker, c.state, c.preroll = Detector(), Speaker(), State(), []
    c.qwen, c.asr = object(), None
    c.turn_audio, c.uncommitted_audio, c.awaiting_transcript = bytearray(), False, False
    c.response_active = False
    c.last_audio_at = c.turn_started_at = 0.0
    finished = []

    async def finish():
        finished.append(len(c.turn_audio))
        c.turn_audio.clear()
    c._finish_user_turn = finish
    task = asyncio.create_task(c._process_audio_queue())
    chunk = bytes(4800)   # 100 ms
    for _ in range(450):  # 45 秒，一块一块来，和麦克风一样
        c.audio_queue.put_nowait(chunk)
        for _ in range(3):
            await asyncio.sleep(0)
    task.cancel()
    assert len(finished) == 2 and all(n == MAX_TURN_BYTES for n in finished), finished

asyncio.run(long_turn())


def transcriber(whisper=None):
    c = Conversation.__new__(Conversation)
    c.local_transcribe, c.local_busy = whisper, False
    return c


async def transcripts():
    conversation.TRANSCRIPT_WAIT_SECONDS, conversation.TRANSCRIPT_DEADLINE_SECONDS = .05, .3
    loop = asyncio.get_running_loop()

    # 识别准时
    ready = loop.create_future()
    ready.set_result('你好')
    assert await transcriber()._turn_transcript(b'', ready) == '你好'
    # 识别迟到但比 Whisper 先出：用识别的
    late = loop.create_future()
    loop.call_later(.1, late.set_result, '迟到的')
    assert await transcriber(lambda pcm: time.sleep(.2) or '本地')._turn_transcript(b'', late) == '迟到的'
    # Whisper 没听到，还要等迟到的识别
    late = loop.create_future()
    loop.call_later(.15, late.set_result, '还是听到了')
    assert await transcriber(lambda pcm: '')._turn_transcript(b'', late) == '还是听到了'
    # 识别不来，用 Whisper
    assert await transcriber(lambda pcm: '本地')._turn_transcript(b'', loop.create_future()) == '本地'
    # 连接断了（None），用 Whisper
    gone = loop.create_future()
    gone.set_result(None)
    assert await transcriber(lambda pcm: '本地')._turn_transcript(b'', gone) == '本地'
    # 实测的卡死：Whisper 不回来也不能一直等；卡着时不再叠一个
    c = transcriber(lambda pcm: time.sleep(.6) or '太晚了')
    started = time.monotonic()
    assert await c._turn_transcript(b'', loop.create_future()) == ''
    assert time.monotonic() - started < .5 and c.local_busy
    assert await c._turn_transcript(b'', loop.create_future()) == ''
    await asyncio.sleep(.5)
    assert not c.local_busy

    # Whisper 报错不能把这一轮弄崩
    def broken(pcm):
        raise RuntimeError('boom')
    assert await transcriber(broken)._turn_transcript(b'', loop.create_future()) == ''

asyncio.run(transcripts())


class Addressee:
    def __init__(self):
        self.asked = []

    def is_addressed(self, text, last, recent):
        self.asked.append(text)
        return False


async def free_replies():
    c = Conversation.__new__(Conversation)
    c.stop, c.addressee = asyncio.Event(), Addressee()
    c.wake_words, c.heard, c.last_assistant_text = ('猫猫',), [], ''
    c.skip_addressee_once, c.free_replies = False, 0
    c.on_quiet_request = c.on_dock_request = None
    c.response_active, c.qwen = False, object()
    c.state = type('S', (), {'increment': lambda self, name: None})()
    c._record_user_text = lambda text: None
    answered = []

    async def settle(pcm, respond, text=''):
        answered.append(respond)
    c._settle_turn = settle

    async def said(text):
        future = asyncio.get_running_loop().create_future()
        future.set_result(text)
        await c._respond_if_speech(b'', future, True)
    for _ in range(MAX_FREE_REPLIES):
        await said('今天天气不错')
    assert answered == [True] * MAX_FREE_REPLIES and not c.addressee.asked
    await said('背景里的话')             # 免判断用完：判断器说不是，不回
    assert answered[-1] is False and c.addressee.asked == ['背景里的话']
    await said('猫猫你在吗')             # 唤醒词：回答，重新算
    assert answered[-1] is True and c.free_replies == 1

asyncio.run(free_replies())


class StuckSocket:
    """实测的网络卡死：send 一直不返回。"""
    def __init__(self):
        self.stuck, self.release, self.closed, self.sent = False, threading.Event(), False, 0

    def send(self, message):
        self.sent += 1
        if self.stuck:
            self.release.wait(5)

    def close(self):
        self.closed = True


async def stuck_network_keeps_the_loop_alive():
    from asr_realtime import SEND_BACKLOG, RealtimeTranscriber
    asr = RealtimeTranscriber('key', 'wss://example')
    asr.loop = asyncio.get_running_loop()
    asr.ready, asr.gone = asyncio.Event(), asyncio.Event()
    asr.ready.set()
    asr.closed, asr.stopped = False, True   # stopped：这里不测重连
    ws = asr.ws = StuckSocket()
    asr._on_open(ws)
    ws.stuck = True
    waiting = asr.commit()                  # 卡住前提交的一轮
    await asyncio.sleep(.05)
    started = time.monotonic()
    for _ in range(SEND_BACKLOG * 2):
        asr.send_audio(bytes(3200))
    assert time.monotonic() - started < .2, '主循环上不能等网络'
    assert not asr.available and await waiting is None, '积压满了：算断线，等着的这一轮马上改用本地'
    await asyncio.sleep(.05)
    assert ws.closed
    assert asr.commit().result() is None and not asr.available
    ws.release.set()
    await asr.close()


async def stuck_reply_send_keeps_the_loop_alive():
    from qwen_realtime import QwenConfig, QwenRealtimeClient
    client = QwenRealtimeClient(QwenConfig('key', 'wss://example'))
    client.loop = asyncio.get_running_loop()
    ws = client.ws = StuckSocket()
    ws.stuck = True
    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(.01)
    ticker = asyncio.create_task(tick())
    send = asyncio.create_task(client.create_response())
    await asyncio.sleep(.2)
    assert ticks > 10 and not send.done()
    ws.release.set()
    await send
    ticker.cancel()
    await client.close()

asyncio.run(stuck_network_keeps_the_loop_alive())
asyncio.run(stuck_reply_send_keeps_the_loop_alive())


async def homecoming_joins_the_conversation():
    c = Conversation.__new__(Conversation)
    c.stop, c.close_requested = asyncio.Event(), asyncio.Event()
    c.state = types.SimpleNamespace(increment=lambda name: None)
    c.turn_ended_at, c.awaiting_transcript, c.homecoming_note = time.monotonic(), True, ''
    items = []

    class Qwen:
        async def create_conversation_item(self, text, item_id): items.append(text)
        async def create_response(self): items.append('reply')
    c.qwen = Qwen()
    assert c.note_homecoming('妈妈')
    await c._settle_turn(b'', False, '不回答的话')       # 这句不回答：旁白留给下一句
    assert not items and c.homecoming_note
    await c._settle_turn(b'', True, '你咋不问候我')
    assert '妈妈刚回到家' in items[0] and items[0].endswith('你咋不问候我') and not c.homecoming_note
    items.clear()
    await c._settle_turn(b'', True, '第二句')
    assert items[0] == '第二句', '只带一次'
    c.stop.set()
    assert not c.note_homecoming('妈妈'), '会话要结束了，交给调用方'

asyncio.run(homecoming_joins_the_conversation())
print('ok')
