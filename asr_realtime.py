"""会话中单独的语音识别连接（qwen3-asr-flash-realtime，手动模式）。

音频边说边送给它；一轮说完先在这里 commit 拿到文字，判断是不是在跟机器人说，
是才把这一轮音频送进对话模型并回复，否则丢掉。以前是先 commit 进对话再判断，
电视声、音乐、噪声全留在上下文里：实测妈妈问“今天多少？”，模型答“我听着像是《小幸运》”。
实测：commit 后 0.5~1.4 秒出结果，比对话模型自带的转写（中位数 1.6 秒）快。
"""
from __future__ import annotations
import asyncio
import base64
import json
import logging
import threading
import time
from collections import deque

import websocket

LOG = logging.getLogger('qwen-asr')

AUDIO_CHUNK_BYTES = 32000  # 1 秒 16 kHz s16le
RETRY_SECONDS = 30


class RealtimeTranscriber:
    def __init__(self, api_key: str, ws_url: str, language: str = 'zh'):
        self.api_key = api_key
        self.ws_url = ws_url
        self.language = language
        self.loop: asyncio.AbstractEventLoop | None = None
        self.ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self.ready: asyncio.Event | None = None
        self.gone: asyncio.Event | None = None
        self.closed = True
        self.stopped = False       # close() 之后不再重连
        self._reconnecting = False
        # commit 之后先等 committed 拿到 item_id，再按 item_id 对上识别结果
        self._awaiting_commit: deque[asyncio.Future] = deque()
        self._by_item: dict[str, asyncio.Future] = {}

    @property
    def available(self) -> bool:
        return not self.closed and self.ready is not None and self.ready.is_set()

    async def connect(self, timeout: float = 5):
        self.loop = asyncio.get_running_loop()
        self.ready, self.gone = asyncio.Event(), asyncio.Event()
        self.closed = False
        self.ws = websocket.WebSocketApp(
            self.ws_url,
            header=[f'Authorization: Bearer {self.api_key}'],
            on_open=self._on_open,
            on_message=lambda ws, message: self.loop.call_soon_threadsafe(self._handle, ws, message),
            on_error=lambda _ws, error: LOG.warning('ASR WebSocket error: %s', str(error)[:200]),
            on_close=lambda ws, code, msg: self.loop.call_soon_threadsafe(self._handle_closed, ws, code, msg),
        )
        self._thread = threading.Thread(target=self.ws.run_forever, name='qwen-asr-ws', daemon=True)
        self._thread.start()
        # 被拒绝（模型名、Key 等）时服务端立刻断开：马上失败，不必等超时拖慢开场
        waits = {asyncio.ensure_future(self.ready.wait()), asyncio.ensure_future(self.gone.wait())}
        await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        for task in waits:
            task.cancel()
        if not self.ready.is_set():
            if self.ws is not None:
                self.ws.close()  # 没连上（或会话已结束）的连接别留在后台
            raise ConnectionError('ASR session not ready')
        LOG.info('ASR session ready (%s)', self.ws_url.rsplit('model=', 1)[-1])

    def _on_open(self, _ws):
        self._send({'type': 'session.update', 'session': {
            'input_audio_format': 'pcm',
            'sample_rate': 16000,
            'input_audio_transcription': {'language': self.language},
            'turn_detection': None,  # 手动模式：轮次由本地 VAD 决定
        }})

    def _handle(self, ws, message: str):
        if ws is not self.ws:
            return  # 重连前那条旧连接
        try:
            event = json.loads(message)
        except ValueError:
            return
        kind = event.get('type', '')
        if kind == 'session.updated':
            self.ready.set()
        elif kind == 'input_audio_buffer.committed':
            if self._awaiting_commit:
                self._by_item[event.get('item_id', '')] = self._awaiting_commit.popleft()
        elif kind == 'conversation.item.input_audio_transcription.completed':
            self._resolve(self._by_item.pop(event.get('item_id', ''), None), event.get('transcript', ''))
        elif kind == 'conversation.item.input_audio_transcription.failed':
            LOG.warning('ASR failed: %s', event.get('error'))
            self._resolve(self._by_item.pop(event.get('item_id', ''), None), '')
        elif kind == 'error':
            LOG.warning('ASR error: %s', event.get('error'))

    def _handle_closed(self, ws, code, msg):
        if ws is self.ws:
            self._lost(f'closed: {code} {msg}')

    def _lost(self, reason: str):
        """连接断了：还在等结果的轮次立刻改用本地 Whisper（不必等超时），后台重连，下一轮起照常"""
        if not self.closed:
            LOG.warning('ASR connection lost (%s)', reason)
        self.closed = True
        if self.gone:
            self.gone.set()
        for future in list(self._awaiting_commit) + list(self._by_item.values()):
            self._resolve(future, None)
        self._awaiting_commit.clear()
        self._by_item.clear()
        self.reconnect_later()

    def reconnect_later(self):
        if self.stopped or self._reconnecting or self.loop is None:
            return
        self._reconnecting = True

        async def retry():
            try:
                while not self.stopped and not self.available:
                    started = time.monotonic()
                    try:
                        await self.connect()
                    except Exception as exc:
                        LOG.warning('ASR reconnect failed: %r', exc)
                        await asyncio.sleep(max(0.0, RETRY_SECONDS - (time.monotonic() - started)))
            finally:
                self._reconnecting = False
        self.loop.create_task(retry())

    @staticmethod
    def _resolve(future: asyncio.Future | None, text: str | None):
        if future is not None and not future.done():
            future.set_result(text)

    def _send(self, message: dict):
        ws = self.ws
        try:
            with self._send_lock:
                ws.send(json.dumps(message, ensure_ascii=False))
        except Exception as exc:
            if ws is self.ws and not self.closed:
                self.closed = True  # 立刻停发，免得每块音频都报一次错
                self.loop.call_soon_threadsafe(self._lost, f'send failed: {exc}')

    def send_audio(self, pcm16k: bytes):
        if not self.available:
            return
        for offset in range(0, len(pcm16k), AUDIO_CHUNK_BYTES):
            self._send({'type': 'input_audio_buffer.append',
                        'audio': base64.b64encode(pcm16k[offset:offset + AUDIO_CHUNK_BYTES]).decode('ascii')})

    def commit(self) -> asyncio.Future:
        """提交这一轮；返回的 future 得到识别文字，连接不可用时得到 None（调用方改用本地 Whisper）。"""
        future = self.loop.create_future() if self.loop else asyncio.get_running_loop().create_future()
        if not self.available:
            future.set_result(None)
            return future
        self._awaiting_commit.append(future)
        self._send({'type': 'input_audio_buffer.commit'})
        return future

    async def close(self):
        self.stopped = True
        if self.ws is None:
            return
        if self.available:
            self._send({'type': 'session.finish'})
        self.closed = True
        self.ws.close()
        self.ws = None
        if self._thread:
            await asyncio.to_thread(self._thread.join, 5)
            self._thread = None
