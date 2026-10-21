"""对话管理模块 - 连接通义千问 API 和 EBO 传输层

手动轮次模式（turn_detection=None）：本地 VAD 判断一轮结束后，先由单独的识别连接转出文字、
判断是不是在跟机器人说，是才把这一轮音频送进对话、commit 并 response.create，否则丢掉
（电视声、噪声不进对话上下文）；机器人说话期间不送麦克风音频，避免自我打断。
"""
from __future__ import annotations
import asyncio
import re
import logging
import threading
import time
import uuid
from collections import deque

from asr_realtime import RealtimeTranscriber
from audio_utils import EndOfTurnDetector, resample_24k_to_16k
from config import Config, Profile
from qwen_realtime import QwenRealtimeClient, QwenConfig
from ebo_transport import Speaker, TranscriptStore, AssistantOutputStore
from addressee import may_be_dock_request, may_be_quiet_request
from standby_voice import has_wake_word, valid_wake_text
from state import RuntimeState

# 等服务端转出这一轮说了什么；超时就照常回复，不能因为转录慢把真人的话吞掉
# 通义千问：一个连接 300 秒没产生回复就断开；我们自己的空闲超时必须更早
QWEN_IDLE_LIMIT = 300
TRANSCRIPT_WAIT_SECONDS = 3
# 模型自己说“我这就回去 / 我去充电啦 / 马上回充电座”：按单句判断，问句和否定不算
ASSISTANT_GOING_TO_DOCK = re.compile(r'(我|这就|马上|立刻|立马|乖乖)[^，,]{0,4}(回去|去充电|回充电座|回窝)')
ASSISTANT_NOT_GOING = re.compile(r'(不|别|没)[^，,]{0,2}(回去|去充电|回窝)|等会|待会|晚点|过会|一会儿再|[吗呢？?]')


def says_going_to_dock(reply: str) -> bool:
    return any(ASSISTANT_GOING_TO_DOCK.search(sentence) and not ASSISTANT_NOT_GOING.search(sentence)
               for sentence in re.split(r'(?<=[。！？!?.])', reply))  # 服务端转录超过这个时间就改用本地 Whisper
DOCK_INSTRUCTION = ('\n你有自己的充电座。如果有人让你回去充电、回充电座或回去休息，你会自动回去，'
                    '只要简短答应一句（比如“好的，我去充电啦”），不要说自己做不到。')

LOG = logging.getLogger('conversation')


class Conversation:
    """管理单次对话会话"""

    def __init__(
        self,
        config: Config,
        profile: Profile,
        speaker: Speaker,
        state: RuntimeState,
        transcripts: TranscriptStore,
        outputs: AssistantOutputStore,
        trigger: str = 'manual',
    ):
        self.config = config
        self.profile = profile
        self.speaker = speaker
        self.state = state
        self.transcripts = transcripts
        self.outputs = outputs
        self.trigger = trigger

        # 唤醒句（standby_voice.VoiceTurn）与连接建立前到达的音频；
        # 两者都必须在实时麦克风音频之前送达模型，保证用户输入不丢。
        self.initial_turn = None
        self.opening_prompt = ''  # 非空时一连上就主动开口（家人回家打招呼）
        self.startup_audio: deque[bytes] = deque(maxlen=600)  # 100 ms 块 -> 最多 60 s
        self.startup_lock = threading.Lock()

        self.qwen: QwenRealtimeClient | None = None
        self.asr: RealtimeTranscriber | None = None

        self.session_id = f'session_{uuid.uuid4().hex[:12]}'
        self.session_started_at = time.time()
        self.last_activity = self.last_audio_at = time.monotonic()
        self.response_active = False
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop = asyncio.Event()
        self.close_requested = asyncio.Event()
        self.abort_requested = threading.Event()

        self.audio_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.turn_detector = EndOfTurnDetector(
            silence_frames=max(10, config.vad_silence_duration_ms // 20))
        self.uncommitted_audio = False
        self.preroll: deque[bytes] = deque(maxlen=3)
        self.turn_audio = bytearray()       # 本轮已送给模型的 24 kHz 音频，转录迟到时本地转写用
        self.local_transcribe = None        # 由 Supervisor 注入（StandbyVoice 的本地 Whisper）
        self.awaiting_transcript = False
        # 由 Supervisor 注入（StandbyVoice 里同一个判断器）；None 表示不判断，有话就回
        self.addressee = None
        self.wake_words: tuple[str, ...] = ()
        self.skip_addressee_once = False
        self.last_assistant_text = ''
        # 由 Supervisor 注入：用户让它回去充电时调用（发回充命令、暂停视觉跟随）
        self.on_dock_request = None
        self.on_quiet_request = None   # 由 Supervisor 注入：用户让它闭嘴时调用
        self.last_dock_request = -1e9
        self.heard: deque[tuple[float, str]] = deque(maxlen=4)

        self.current_response_id = ''
        self.current_audio_buffer = bytearray()
        self.current_transcript_buffer = ''

    async def run(self):
        """运行对话会话"""
        if self.abort_requested.is_set():
            return
        with self.startup_lock:
            self.loop = asyncio.get_running_loop()
            for pcm in self.startup_audio:
                self.audio_queue.put_nowait(pcm)
            self.startup_audio.clear()

        try:
            await self._init_qwen_client()

            self.state.update(
                session_id=self.session_id,
                session_state='active',
                session_trigger=self.trigger,
                session_started_at=self.session_started_at,
                realtime_connected=True,
            )

            LOG.info('Conversation session started: %s (user: %s, trigger: %s)',
                     self.session_id, self.profile.id, self.trigger)

            await self._opening()
            await self._conversation_loop()

        except Exception as exc:
            LOG.exception('Conversation failed: %s', exc)
            self.state.update(last_error=f'conversation_failed: {exc}'[:300])

        finally:
            await self._cleanup()

    async def _init_qwen_client(self):
        """初始化通义千问客户端"""
        qwen_config = QwenConfig(
            api_key=self.config.qwen_api_key,
            ws_url=self.config.qwen_ws_url,
            voice=self.config.qwen_voice,
            instructions=self._instructions(),
            input_sample_rate=16000,
            output_sample_rate=24000,
            vad_threshold=self.config.vad_threshold,
            vad_silence_duration_ms=self.config.vad_silence_duration_ms,
            vad_prefix_padding_ms=self.config.vad_prefix_padding_ms,
            server_vad=False,
            enable_search=self.config.web_search_enabled,
            transcribe_input=False,
        )

        self.qwen = QwenRealtimeClient(qwen_config)

        self.qwen.on_audio_delta = self._handle_audio_delta
        self.qwen.on_audio_done = self._handle_audio_done
        self.qwen.on_transcript_delta = self._handle_transcript_delta
        self.qwen.on_transcript_done = self._handle_transcript_done
        self.qwen.on_response_created = self._handle_response_created
        self.qwen.on_response_done = self._handle_response_done
        self.qwen.on_error = self._handle_error
        self.qwen.on_closed = self._handle_closed

        self.asr = RealtimeTranscriber(self.config.qwen_api_key, self.config.qwen_asr_ws_url)
        # 两条连接同时建，不多等；识别连接失败不影响聊天，判断改用本地 Whisper（慢 1~2 秒）
        qwen_result, asr_result = await asyncio.gather(self.qwen.connect(), self.asr.connect(),
                                                       return_exceptions=True)
        if isinstance(qwen_result, BaseException):
            raise qwen_result
        if isinstance(asr_result, BaseException):
            LOG.warning('ASR connection failed (%r); judging turns with local Whisper until it reconnects',
                        asr_result)
            self.asr.reconnect_later()

    def _instructions(self) -> str:
        name = self.profile.display_name
        text = f'{self.config.instructions}\n当前和你说话的是：{name}。' if name else self.config.instructions
        if self.opening_prompt:
            # response.create 里的 instructions 只管第一句；后面怎么接着聊要放进整场对话的指令里
            text += f'\n这场对话是你主动开始的：{self.opening_prompt}'
        # 回充由程序在听到请求时自动执行，模型只需要答应，不能说自己做不到
        return text + DOCK_INSTRUCTION

    async def _opening(self):
        """先送唤醒句音频，再由主循环送 startup_audio 与实时音频，最后统一 commit。"""
        turn, self.initial_turn = self.initial_turn, None
        if self.opening_prompt:
            LOG.info('Opening by speaking first: %s', self.trigger)
            # 我们先开口，对方接下来的第一句话一定是在回我们，不用再判断是不是在跟机器人说
            self.skip_addressee_once = True
            self.response_active = True
            await self.qwen.create_response(self.opening_prompt)
            return
        if turn is None:
            return
        # 唤醒句在待机时已经判断过是在对机器人说，第一轮不再重复判断
        self.skip_addressee_once = True
        LOG.info('Opening with wake utterance: %s', turn.text)
        if turn.text:
            self.transcripts.append(
                item_id=turn.item_id,
                transcript=turn.text,
                session_started_at=self.session_started_at,
                user_id=self.profile.id,
            )
        await self._send_turn_audio(turn.pcm)
        self.uncommitted_audio = True
        # 唤醒句本身已是完整语音；后续静音达到阈值即提交，期间的追加话语一并进入本轮
        self.turn_detector.mark_speech()

    async def _conversation_loop(self):
        """对话主循环"""
        audio_task = asyncio.create_task(self._process_audio_queue())
        try:
            while not self.stop.is_set():
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=1)
                except asyncio.TimeoutError:
                    await self._check_timeouts()
        finally:
            audio_task.cancel()
            try:
                await audio_task
            except asyncio.CancelledError:
                pass

    async def _check_timeouts(self):
        # 麦克风流中断（重连等）时没有静音帧可判断轮次结束，按时间兜底提交
        if (self.uncommitted_audio and not self.response_active and not self.awaiting_transcript
                and time.monotonic() - self.last_audio_at > 1.5):
            await self._finish_user_turn()
        if self.response_active or self.speaker.input_muted():
            self.last_activity = time.monotonic()
            return
        if time.time() - self.session_started_at > self.config.session_max_seconds:
            LOG.info('Session exceeded max duration, closing')
            self.stop.set()
        elif time.monotonic() - self.last_activity > min(self.config.session_idle_seconds, QWEN_IDLE_LIMIT - 30):
            LOG.info('Session idle for %.0fs, closing', min(self.config.session_idle_seconds, QWEN_IDLE_LIMIT - 30))
            self.stop.set()

    async def _process_audio_queue(self):
        """本地判断轮次：送音频 -> 静音达到阈值 -> commit + response.create"""
        while not self.stop.is_set():
            pcm = await self.audio_queue.get()
            self.last_audio_at = time.monotonic()
            if not self.qwen:
                continue

            # 半双工：机器人说话（含播放尾音）时丢弃麦克风输入，防止听到自己
            if self.response_active or self.speaker.input_muted():
                self.turn_detector.reset()
                self.preroll.clear()
                continue

            voiced, ended = self.turn_detector.observe(pcm)
            if voiced:
                self.state.update(input_speech_active=True)

            if voiced or self.turn_detector.has_speech or self.uncommitted_audio:
                # 语音起始前的 300 ms 一并送出，避免截掉第一个字
                while self.preroll:
                    await self._send_turn_audio(self.preroll.popleft())
                await self._send_turn_audio(pcm)
                self.uncommitted_audio = True
            else:
                self.preroll.append(pcm)

            # 队列里还有积压（唤醒期间排队的后续语句）时不能提交，
            # 否则后面的话会在机器人回复期间被半双工丢弃
            if ended and self.uncommitted_audio and self.audio_queue.empty() and not self.awaiting_transcript:
                self.state.update(input_speech_active=False)
                await self._finish_user_turn()

    async def _send_turn_audio(self, pcm: bytes):
        """这一轮的音频：只实时送给识别连接；对话模型等判断完、要回复时才一次性送（见 _settle_turn）。"""
        self.turn_audio.extend(pcm)
        if self.asr:
            self.asr.send_audio(resample_24k_to_16k(pcm))

    async def _finish_user_turn(self):
        if self.close_requested.is_set():
            self.stop.set()
            return
        LOG.info('User turn ended; transcribing before deciding whether to commit')
        self.uncommitted_audio = False
        self.turn_detector.reset()
        turn_pcm = bytes(self.turn_audio)
        self.turn_audio.clear()
        self.awaiting_transcript = True
        transcript = self.asr.commit() if self.asr else None
        asyncio.create_task(self._respond_if_speech(turn_pcm, transcript))

    async def _turn_transcript(self, turn_pcm: bytes, transcript: asyncio.Future | None) -> str:
        """这一轮说了什么。识别连接超时或断开时用本地 Whisper：实测电视噪声的转录常常 6 秒以上才回来
        而且是空的，以前超时就“照常回复”，模型只能反复说“我在听”。"""
        if transcript is not None:
            try:
                text = await asyncio.wait_for(transcript, TRANSCRIPT_WAIT_SECONDS)
            except asyncio.TimeoutError:
                text = None
            if text is not None:
                return text
        if self.local_transcribe is None:
            LOG.warning('No transcript within %ss and no local transcriber; ignoring turn', TRANSCRIPT_WAIT_SECONDS)
            return ''
        text = await asyncio.to_thread(self.local_transcribe, turn_pcm)
        LOG.info('ASR transcript late or unavailable; local Whisper heard: %r', text)
        return text

    async def _settle_turn(self, turn_pcm: bytes, respond: bool):
        """判断完：要回复才把这一轮音频送进对话并 commit；否则对话模型根本听不到（电视声、噪声不进上下文）。
        不能边说边送、不回复时再 clear：实测音频结尾带静音时服务端已经切好段，clear 清不掉，
        commit 进去的反而是被丢弃的那句。一次性送比边说边送晚 0.2~0.9 秒出声，识别连接快回来了。"""
        try:
            if respond and self.qwen and not self.stop.is_set():
                await self.qwen.send_audio(resample_24k_to_16k(turn_pcm))
                await self.qwen.commit_audio_buffer()
                self.last_activity = time.monotonic()
                self.response_active = True
                self.state.increment('manual_responses_requested')
                await self.qwen.create_response()
        finally:
            self.awaiting_transcript = False

    async def _respond_if_speech(self, turn_pcm: bytes, transcript: asyncio.Future | None):
        """只有这一轮真的有人说话才回复：噪声/电视声被切成一轮时转录为空，回复会变成反复道歉。"""
        respond = False
        try:
            text = await self._turn_transcript(turn_pcm, transcript)
            if not valid_wake_text(text):
                # 不记进对话记录和上下文：多半是履带声等噪音被识别成的“嗯。”
                LOG.info('Ignored turn without real speech: %r', text)
                self.state.increment('input_turns_ignored')
                return
            self._record_user_text(text)
            skip_check, self.skip_addressee_once = self.skip_addressee_once, False
            if (text and self.addressee is not None and not skip_check
                    and not has_wake_word(text, self.wake_words)):
                # heard 的最后一条就是当前这句
                recent = tuple(t for at, t in list(self.heard)[:-1] if at >= time.monotonic() - 60)
                addressed = await asyncio.to_thread(self.addressee.is_addressed, text,
                                                    self.last_assistant_text, recent)
                # 已经在对话中：判断失败时照常回复，避免把真人的话吞掉
                if addressed is False:
                    LOG.info('Ignored turn not addressed to the robot: %r', text)
                    self.state.increment('input_turns_ignored')
                    return
            if text and self.addressee is not None and self.on_quiet_request and may_be_quiet_request(text):
                if await asyncio.to_thread(self.addressee.wants_quiet, text):
                    # 不回答，直接结束对话；之后一段时间只认唤醒词
                    LOG.info('Asked to be quiet: %r', text)
                    await asyncio.to_thread(self.on_quiet_request)
                    return
            if text and self.addressee is not None and self.on_dock_request and may_be_dock_request(text):
                if await asyncio.to_thread(self.addressee.wants_to_dock, text, self.last_assistant_text):
                    LOG.info('Voice request to go charge: %r', text)
                    self.last_dock_request = time.monotonic()
                    await asyncio.to_thread(self.on_dock_request)
            respond = not (self.stop.is_set() or self.response_active or not self.qwen)
        finally:
            await self._settle_turn(turn_pcm, respond)

    def offer_audio(self, pcm: bytes):
        """接收来自 EBO 的 24 kHz 音频（可在会话连上之前调用）"""
        with self.startup_lock:
            if self.loop is None:
                self.startup_audio.append(pcm)
                return
        if self.stop.is_set():
            return
        try:
            self.loop.call_soon_threadsafe(self.audio_queue.put_nowait, pcm)
        except RuntimeError:
            pass

    def _handle_response_created(self, response_id: str):
        self.response_active = True
        self.current_response_id = response_id or uuid.uuid4().hex
        self.current_audio_buffer = bytearray()
        self.current_transcript_buffer = ''

    def _handle_audio_delta(self, pcm: bytes):
        """处理通义千问返回的音频片段"""
        try:
            self.current_audio_buffer.extend(pcm)
            self.speaker.stream_delta(pcm, self.current_response_id)
        except Exception as exc:
            LOG.error('Error handling audio delta: %s', exc)

    def _handle_audio_done(self, item_id: str):
        """处理音频完成"""
        LOG.info('Audio response completed: %s', item_id)
        if self.current_audio_buffer:
            self.speaker.finish_stream(bytes(self.current_audio_buffer), self.current_response_id)

    def _handle_transcript_delta(self, delta: str):
        self.current_transcript_buffer += delta

    def _handle_transcript_done(self, item_id: str, transcript: str):
        """处理助手转录完成"""
        LOG.info('Assistant said: %s', transcript[:100])
        if transcript:
            self.last_assistant_text = transcript[-200:]
            # 说到做到：模型答应了“我这就回去（充电）”，就真的回充（实测用户说“回去吧”时它答应了却没动）
            if (self.on_dock_request and says_going_to_dock(transcript)
                    and time.monotonic() - self.last_dock_request > 60):
                LOG.info('Assistant said it is going to charge; docking: %r', transcript[:60])
                self.last_dock_request = time.monotonic()
                self.loop.run_in_executor(None, self.on_dock_request)
        if transcript:
            self.outputs.append(
                output_id=self.current_response_id,
                response_id=self.current_response_id,
                item_id=item_id,
                transcript=transcript,
                metadata=self.speaker.output_metrics(self.current_response_id),
            )
        self.current_transcript_buffer = ''

    def _record_user_text(self, transcript: str):
        """这一轮听到的话：记日志和转录，并放进 heard（判断电视对白时连起来看）"""
        LOG.info('User said: %s', transcript)
        if transcript.strip():
            self.heard.append((time.monotonic(), transcript.strip()))
            self.transcripts.append(
                item_id=f'turn_{uuid.uuid4().hex[:24]}',
                transcript=transcript,
                session_started_at=self.session_started_at,
                user_id=self.profile.id,
            )

    def _handle_response_done(self, event: dict):
        """处理响应完成"""
        # 每次回复的计费 tokens（整场对话的上下文都会重新计一遍输入），方便对照账单
        usage = (event.get('response') or {}).get('usage') or {}
        details = usage.get('input_tokens_details') or {}
        LOG.info('Response completed; usage: input %s (text %s, audio %s), output %s',
                 usage.get('input_tokens'), details.get('text_tokens'), details.get('audio_tokens'),
                 usage.get('output_tokens'))
        self.response_active = False
        self.last_activity = time.monotonic()
        self.turn_detector.reset()
        if self.close_requested.is_set():
            LOG.info('Close requested, ending session')
            self.stop.set()

    def _handle_error(self, error: dict):
        error_msg = error.get('message', 'Unknown error')
        if error.get('code') == 'response_idle_timeout':
            # 服务端“300 秒没有回复就断开”：只是对话空闲结束，不是故障
            LOG.info('Qwen closed the idle session: %s', error_msg)
            self.response_active = False
            return
        LOG.error('Qwen API error: %s', error_msg)
        self.state.update(last_error=f'qwen_api_error:{error_msg}')
        # 出错的请求不会有 response.done，避免一直卡在“回复中”而丢弃麦克风音频
        self.response_active = False

    def _handle_closed(self):
        LOG.warning('Qwen connection closed by server; ending session %s', self.session_id)
        self.stop.set()

    def request_close(self):
        """请求关闭会话（当前回复播完后结束）"""
        LOG.info('Close requested for session %s', self.session_id)
        if self.loop is None:
            self.abort_requested.set()
            return
        self.loop.call_soon_threadsafe(self._request_close)

    def _request_close(self):
        self.close_requested.set()
        if not self.response_active:
            self.stop.set()

    def request_abort(self):
        """立即中止会话"""
        LOG.info('Abort requested for session %s', self.session_id)
        self.abort_requested.set()
        if self.loop:
            self.loop.call_soon_threadsafe(self.stop.set)

    async def _cleanup(self):
        LOG.info('Cleaning up conversation session')
        if self.asr:
            await self.asr.close()
            self.asr = None
        if self.qwen:
            await self.qwen.close()
            self.qwen = None
        self.state.update(
            session_state='standby',
            realtime_connected=False,
            input_speech_active=False,
        )
        LOG.info('Conversation session ended: %s', self.session_id)
