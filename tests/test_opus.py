"""会话中的识别连接改送 Ogg Opus：一条连接一个流、提交前补齐半帧、提交失败不让结果错位。在容器里跑。"""
import asyncio, base64, io, json, sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))
import av
import numpy as np
import ogg_opus
from ogg_opus import OggOpusStream

assert ogg_opus._crc(b'123456789') == 0x89A1897F   # Ogg 用的 CRC-32（不反转、初值 0、不取反）

rng = np.random.default_rng(0)
t = np.arange(16000 * 3) / 16000
pcm = ((np.sin(2 * np.pi * 220 * t) * 6000) + rng.normal(0, 800, t.size)).astype(np.int16).tobytes()

# 一个流跨三轮：每轮 100 ms 一块地送，块大小故意不是 20 ms 的整数倍，提交前 flush
stream, data, first = OggOpusStream(), b'', b''
for turn in range(3):
    part = pcm[turn * 32000:(turn + 1) * 32000]
    for offset in range(0, len(part), 3300):
        chunk = stream.feed(part[offset:offset + 3300])
        first = first or chunk
        data += chunk
    data += stream.flush()
    assert not stream._pending, '提交前尾巴要编出去，不能算进下一轮'
assert first.startswith(b'OggS') and b'OpusHead' in first and b'OpusTags' in first, '第一块带头'
assert data.count(b'OpusHead') == 1, '整条连接只能有一个流'
assert stream.flush() == b''
container = av.open(io.BytesIO(data), format='ogg')
seconds = sum(frame.samples for frame in container.decode(audio=0)) / container.streams.audio[0].rate
assert 2.95 < seconds < 3.1, seconds
assert len(data) < len(pcm) * 0.12, f'跨境上传要省：{len(data)} vs PCM {len(pcm)}'


class Socket:
    def __init__(self): self.sent = []
    def send(self, message): self.sent.append(json.loads(message))
    def close(self): pass


async def transcriber():
    from asr_realtime import RealtimeTranscriber
    asr = RealtimeTranscriber('key', 'wss://example')
    asr.loop = asyncio.get_running_loop()
    asr.ready, asr.gone = asyncio.Event(), asyncio.Event()
    asr.ready.set()
    asr.closed, asr.stopped = False, True
    ws = asr.ws = Socket()
    asr._send = lambda message: ws.send(json.dumps(message))   # 不起发送线程，直接看发了什么
    asr._on_open(ws)
    assert ws.sent[0]['session']['input_audio_format'] == 'opus'

    def audio(message): return base64.b64decode(message['audio'])
    asr.send_audio(pcm[:3300])
    asr.send_audio(pcm[3300:6600])
    one = asr.commit()
    appends = [m for m in ws.sent if m['type'] == 'input_audio_buffer.append']
    assert b'OpusHead' in audio(appends[0]) and b'OpusHead' not in audio(appends[1])
    assert ws.sent[-2]['type'] == 'input_audio_buffer.append' and ws.sent[-1]['type'] == 'input_audio_buffer.commit', \
        '提交前先把半帧补齐送出'

    # 提交失败（服务端没有 committed，只回 error）：这一轮改用本地，下一轮的结果不能对到它头上
    asr._handle(ws, json.dumps({'type': 'error', 'error': {
        'type': 'invalid_request_error', 'message': 'Error committing input audio buffer, maybe no invalid audio stream.'}}))
    assert await one is None
    asr.send_audio(pcm[6600:9900])
    two = asr.commit()
    asr._handle(ws, json.dumps({'type': 'input_audio_buffer.committed', 'item_id': 'b'}))
    asr._handle(ws, json.dumps({'type': 'conversation.item.input_audio_transcription.completed',
                                'item_id': 'b', 'transcript': '第二句'}))
    assert await two == '第二句'
    # 其他错误不动队列
    three = asr.commit()
    asr._handle(ws, json.dumps({'type': 'error', 'error': {'message': 'something else'}}))
    assert not three.done()

    # 重连后是新连接：要重新带头
    asr._ogg = None   # connect() 里做的
    ws.sent.clear()
    asr.send_audio(pcm[:3300])
    assert b'OpusHead' in audio(ws.sent[0])

asyncio.run(transcriber())
print('ok')
