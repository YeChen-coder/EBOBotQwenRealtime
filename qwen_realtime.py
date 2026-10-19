"""通义千问 Realtime API 客户端实现"""
from __future__ import annotations
import asyncio
import base64
import json
import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Any

import websocket

LOG = logging.getLogger('qwen-realtime')

# 1 秒 16 kHz s16le = 32 KB，base64 后约 43 KB，远小于服务端 256 KB 单帧上限
AUDIO_CHUNK_BYTES = 32000


class EventType(str, Enum):
    """通义千问 Realtime API 事件类型"""
    # Session events
    SESSION_CREATED = 'session.created'
    SESSION_UPDATE = 'session.update'
    SESSION_UPDATED = 'session.updated'

    # Input audio buffer events
    INPUT_AUDIO_BUFFER_APPEND = 'input_audio_buffer.append'
    INPUT_AUDIO_BUFFER_COMMIT = 'input_audio_buffer.commit'
    INPUT_AUDIO_BUFFER_COMMITTED = 'input_audio_buffer.committed'
    INPUT_AUDIO_BUFFER_CLEAR = 'input_audio_buffer.clear'
    INPUT_AUDIO_BUFFER_CLEARED = 'input_audio_buffer.cleared'
    INPUT_AUDIO_BUFFER_SPEECH_STARTED = 'input_audio_buffer.speech_started'
    INPUT_AUDIO_BUFFER_SPEECH_STOPPED = 'input_audio_buffer.speech_stopped'

    # Conversation events
    CONVERSATION_ITEM_CREATE = 'conversation.item.create'
    CONVERSATION_ITEM_CREATED = 'conversation.item.created'
    CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED = 'conversation.item.input_audio_transcription.completed'
    CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_FAILED = 'conversation.item.input_audio_transcription.failed'

    # Response events
    RESPONSE_CREATE = 'response.create'
    RESPONSE_CREATED = 'response.created'
    RESPONSE_DONE = 'response.done'
    RESPONSE_AUDIO_DELTA = 'response.audio.delta'
    RESPONSE_AUDIO_DONE = 'response.audio.done'
    RESPONSE_AUDIO_TRANSCRIPT_DELTA = 'response.audio_transcript.delta'
    RESPONSE_AUDIO_TRANSCRIPT_DONE = 'response.audio_transcript.done'
    RESPONSE_TEXT_DELTA = 'response.text.delta'
    RESPONSE_TEXT_DONE = 'response.text.done'

    # Error events
    ERROR = 'error'


@dataclass
class QwenConfig:
    """通义千问 API 配置"""
    api_key: str
    ws_url: str
    voice: str = 'longanlingxin'
    instructions: str = '你是一个智能助手'
    input_sample_rate: int = 16000
    output_sample_rate: int = 24000
    vad_threshold: float = 0.65
    vad_silence_duration_ms: int = 650
    vad_prefix_padding_ms: int = 300
    server_vad: bool = False
    enable_search: bool = False


class QwenRealtimeClient:
    """通义千问 Realtime API WebSocket 客户端"""

    def __init__(self, config: QwenConfig):
        self.config = config
        self.ws: websocket.WebSocketApp | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.connected = asyncio.Event()
        self.session_configured = asyncio.Event()
        self.stopped = False

        # 事件回调
        self.on_audio_delta: Callable[[bytes], None] | None = None
        self.on_audio_done: Callable[[str], None] | None = None
        self.on_transcript_delta: Callable[[str], None] | None = None
        self.on_transcript_done: Callable[[str, str], None] | None = None
        self.on_user_transcript: Callable[[str, str], None] | None = None
        self.on_committed: Callable[[str], None] | None = None
        self.on_speech_started: Callable[[], None] | None = None
        self.on_speech_stopped: Callable[[], None] | None = None
        self.on_error: Callable[[dict], None] | None = None
        self.on_response_done: Callable[[dict], None] | None = None
        self.on_response_created: Callable[[str], None] | None = None
        self.on_closed: Callable[[], None] | None = None
        self._thread: threading.Thread | None = None
        self.closed = asyncio.Event()
        self.last_error = ''
        self._send_lock = threading.Lock()

        # 状态跟踪
        self.current_response_id: str = ''
        self.current_item_id: str = ''
        self.audio_buffer: bytearray = bytearray()
        self.transcript_buffer: list[str] = []

    async def connect(self):
        """建立 WebSocket 连接"""
        LOG.info('Connecting to Qwen Realtime API: %s', self.config.ws_url)
        self.loop = asyncio.get_running_loop()
        started = time.monotonic()

        # run_forever 会阻塞到连接关闭，必须放在独立线程里而不是 await
        self._thread = threading.Thread(target=self._run_websocket, name='qwen-ws', daemon=True)
        self._thread.start()

        # 等待连接建立；被拒绝（403 等）时立即失败，不必等超时。从海外连北京实测握手约 6 秒
        opened = asyncio.create_task(self.connected.wait())
        closed = asyncio.create_task(self.closed.wait())
        await asyncio.wait({opened, closed}, timeout=20, return_when=asyncio.FIRST_COMPLETED)
        opened.cancel()
        closed.cancel()
        if not self.connected.is_set():
            raise RuntimeError(f'Qwen connect failed: {self.last_error or "timeout"}')
        LOG.info('Connected to Qwen Realtime API in %.1fs', time.monotonic() - started)

        # 配置会话
        await self._configure_session()

        # 等待会话配置完成
        try:
            await asyncio.wait_for(self.session_configured.wait(), timeout=10)
            LOG.info('Qwen Realtime session configured')
        except asyncio.TimeoutError:
            raise RuntimeError('Failed to configure Qwen Realtime session')

    def _run_websocket(self):
        """运行 WebSocket 客户端（在单独线程中）"""
        self.ws = websocket.WebSocketApp(
            self.config.ws_url,
            header=[f'Authorization: Bearer {self.config.api_key}'],
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self.ws.run_forever()

    def _on_open(self, ws):
        """WebSocket 连接打开"""
        LOG.info('WebSocket connection opened')
        if self.loop:
            self.loop.call_soon_threadsafe(self.connected.set)

    def _on_message(self, ws, message):
        """接收 WebSocket 消息"""
        if self.loop:
            self.loop.call_soon_threadsafe(self._handle_message, message)

    def _on_error(self, ws, error):
        """WebSocket 错误"""
        LOG.error('WebSocket error: %s', error)
        text = str(error)
        # 握手失败时服务端的 JSON 原因在最后，如 Endpoint.AccessDenied / InvalidApiKey
        self.last_error = text[text.rfind("b'{"):] if "b'{" in text else text[:200]

    def _on_close(self, ws, close_status_code, close_msg):
        """WebSocket 连接关闭"""
        LOG.info('WebSocket connection closed: %s %s', close_status_code, close_msg)
        if self.loop:
            self.loop.call_soon_threadsafe(self.closed.set)
        if self.loop and not self.stopped and self.on_closed:
            self.loop.call_soon_threadsafe(self.on_closed)

    def _handle_message(self, message: str):
        """处理接收到的消息"""
        try:
            event = json.loads(message)
            event_type = event.get('type', '')

            LOG.debug('Received event: %s', event_type)

            if event_type == EventType.SESSION_CREATED:
                self._handle_session_created(event)
            elif event_type == EventType.SESSION_UPDATED:
                self._handle_session_updated(event)
            elif event_type == EventType.RESPONSE_AUDIO_DELTA:
                self._handle_audio_delta(event)
            elif event_type == EventType.RESPONSE_AUDIO_DONE:
                self._handle_audio_done(event)
            elif event_type == EventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA:
                self._handle_transcript_delta(event)
            elif event_type == EventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
                self._handle_transcript_done(event)
            elif event_type == EventType.INPUT_AUDIO_BUFFER_COMMITTED:
                if self.on_committed:
                    self.on_committed(event.get('item_id', ''))
            elif event_type == EventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
                self._handle_user_transcript(event)
            elif event_type == EventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_FAILED:
                LOG.warning('Input transcription failed: %s', event.get('error'))
                if self.on_user_transcript:
                    self.on_user_transcript(event.get('item_id', ''), '')
            elif event_type == EventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
                self._handle_speech_started(event)
            elif event_type == EventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
                self._handle_speech_stopped(event)
            elif event_type == EventType.RESPONSE_CREATED:
                response_id = event.get('response', {}).get('id', '')
                self.current_response_id = response_id
                if self.on_response_created:
                    self.on_response_created(response_id)
            elif event_type == EventType.RESPONSE_DONE:
                self._handle_response_done(event)
            elif event_type == EventType.ERROR:
                self._handle_error_event(event)

        except json.JSONDecodeError as exc:
            LOG.error('Failed to parse message: %s', exc)
        except Exception as exc:
            LOG.exception('Error handling message: %s', exc)

    def _handle_session_created(self, event: dict):
        """处理 session.created 事件"""
        LOG.info('Session created: %s', event.get('session', {}).get('id', ''))

    def _handle_session_updated(self, event: dict):
        """处理 session.updated 事件"""
        LOG.info('Session updated')
        self.session_configured.set()

    def _handle_audio_delta(self, event: dict):
        """处理音频数据块"""
        audio_b64 = event.get('delta', '')
        if audio_b64 and self.on_audio_delta:
            try:
                audio_pcm = base64.b64decode(audio_b64)
                self.audio_buffer.extend(audio_pcm)
                self.on_audio_delta(audio_pcm)
            except Exception as exc:
                LOG.error('Failed to decode audio delta: %s', exc)

    def _handle_audio_done(self, event: dict):
        """处理音频完成"""
        item_id = event.get('item_id', '')
        LOG.info('Audio response done: %s', item_id)
        if self.on_audio_done:
            self.on_audio_done(item_id)

    def _handle_transcript_delta(self, event: dict):
        """处理转录文本片段"""
        delta = event.get('delta', '')
        if delta:
            self.transcript_buffer.append(delta)
            if self.on_transcript_delta:
                self.on_transcript_delta(delta)

    def _handle_transcript_done(self, event: dict):
        """处理转录文本完成"""
        item_id = event.get('item_id', '')
        transcript = event.get('transcript', '') or ''.join(self.transcript_buffer)
        LOG.info('Transcript done: %s', transcript[:50])
        self.transcript_buffer.clear()
        if self.on_transcript_done:
            self.on_transcript_done(item_id, transcript)

    def _handle_user_transcript(self, event: dict):
        """处理用户语音转录"""
        item_id = event.get('item_id', '')
        transcript = event.get('transcript', '')
        LOG.info('User transcript: %s', transcript)
        if self.on_user_transcript:
            self.on_user_transcript(item_id, transcript)

    def _handle_speech_started(self, event: dict):
        """处理语音开始"""
        LOG.debug('Speech started')
        if self.on_speech_started:
            self.on_speech_started()

    def _handle_speech_stopped(self, event: dict):
        """处理语音停止"""
        LOG.debug('Speech stopped')
        if self.on_speech_stopped:
            self.on_speech_stopped()

    def _handle_response_done(self, event: dict):
        """处理响应完成"""
        LOG.info('Response done')
        if self.on_response_done:
            self.on_response_done(event)

    def _handle_error_event(self, event: dict):
        """处理错误事件"""
        error = event.get('error', {})
        LOG.error('Qwen API error: %s', error)
        if self.on_error:
            self.on_error(error)

    async def _configure_session(self):
        """配置会话参数"""
        config_message = {
            'type': EventType.SESSION_UPDATE,
            'session': {
                'modalities': ['text', 'audio'],
                'instructions': self.config.instructions,
                'voice': self.config.voice,
                'audio': {
                    'input': {
                        'format': {
                            'type': 'pcm',
                            'sample_rate': self.config.input_sample_rate,
                            'sample_format': 's16le',
                            'channels': 1,
                        },
                    },
                    'output': {
                        'voice': self.config.voice,
                        'format': {
                            'type': 'pcm',
                            'sample_rate': self.config.output_sample_rate,
                            'sample_format': 's16le',
                            'channels': 1,
                        },
                    },
                },
                'input_audio_transcription': {'model': 'qwen3-asr-flash-realtime'},
                # 模型自行决定何时联网搜索；与 tools 互斥
                'enable_search': self.config.enable_search,
                'turn_detection': ({
                    'type': 'server_vad',
                    'threshold': self.config.vad_threshold,
                    'prefix_padding_ms': self.config.vad_prefix_padding_ms,
                    'silence_duration_ms': self.config.vad_silence_duration_ms,
                } if self.config.server_vad else None),
            },
        }

        await self._send(config_message)

    async def create_conversation_item(self, text: str, item_id: str, role: str = 'user'):
        """发送文本消息（用于 initial_turn，保证第一段话不丢失）"""
        message = {
            'type': EventType.CONVERSATION_ITEM_CREATE,
            'item': {
                'id': item_id,
                'type': 'message',
                'role': role,
                'content': [{'type': 'input_text', 'text': text}],
            },
        }
        await self._send(message)

    async def send_audio(self, pcm_data: bytes):
        """发送 16 kHz 音频；服务端单条消息上限 256 KB，长的唤醒句/积压音频必须分块"""
        if not self.ws or not self.connected.is_set():
            LOG.warning('Cannot send audio: not connected')
            return
        for offset in range(0, len(pcm_data), AUDIO_CHUNK_BYTES):
            await self._send({
                'type': EventType.INPUT_AUDIO_BUFFER_APPEND,
                'audio': base64.b64encode(pcm_data[offset:offset + AUDIO_CHUNK_BYTES]).decode('ascii'),
            })

    async def commit_audio_buffer(self):
        """提交音频缓冲区（手动模式下，语音结束后调用）"""
        message = {'type': EventType.INPUT_AUDIO_BUFFER_COMMIT}
        await self._send(message)

    # 向后兼容别名
    commit_audio = commit_audio_buffer

    async def create_response(self):
        """请求生成响应（手动模式）"""
        message = {'type': EventType.RESPONSE_CREATE}
        await self._send(message)

    async def clear_audio_buffer(self):
        """清空音频缓冲区"""
        message = {'type': EventType.INPUT_AUDIO_BUFFER_CLEAR}
        await self._send(message)

    async def _send(self, message: dict):
        """发送消息到 WebSocket"""
        if not self.ws:
            LOG.warning('Cannot send message: WebSocket not initialized')
            return
        if self.closed.is_set():
            return  # 服务端已断开：不再逐条报“发送失败”刷日志

        try:
            json_str = json.dumps(message, ensure_ascii=False, separators=(',', ':'))
            # 同步发送 + 锁：线程池并发 send 可能打乱 item.create 与音频的顺序
            with self._send_lock:
                self.ws.send(json_str)
        except Exception as exc:
            LOG.error('Failed to send message: %s', exc)

    async def close(self):
        """关闭连接"""
        self.stopped = True
        if self.ws:
            self.ws.close()
            self.ws = None
        if self._thread:
            await asyncio.to_thread(self._thread.join, 5)
            self._thread = None
        LOG.info('Qwen Realtime client closed')
