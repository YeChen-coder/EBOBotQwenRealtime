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
from datetime import datetime
from zoneinfo import ZoneInfo

from asr_realtime import RealtimeTranscriber
from audio_utils import EndOfTurnDetector, resample_24k_to_16k
from config import Config, Profile
from qwen_realtime import QwenRealtimeClient, QwenConfig
from ebo_transport import Speaker, TranscriptStore, AssistantOutputStore
from addressee import may_be_dock_request, may_be_quiet_request, may_promise_quiet
from standby_voice import collapse_repeats, has_wake_word, loose_wake_word, valid_reply_text
from state import RuntimeState

# 等服务端转出这一轮说了什么；超时就照常回复，不能因为转录慢把真人的话吞掉
# 通义千问：一个连接 300 秒没产生回复就断开；我们自己的空闲超时必须更早
QWEN_IDLE_LIMIT = 300
TRANSCRIPT_WAIT_SECONDS = 3
# 识别迟到时最多等这么久。实测（11-05）本地 Whisper 偶尔卡 120 秒，妈妈说完 124 秒才听到回答
TRANSCRIPT_DEADLINE_SECONDS = 8
# 猫猫说完后这么久之内开口的，默认就是在回它，不再请模型判断。实测判断器对“然后。”“天气不好。”
# “不是你刚才说我什么……”这类接话一律判“不是在跟机器人说”，妈妈接了话猫猫不理
REPLY_WINDOW_SECONDS = 20
# 上面这条免判断最多用几次，喊唤醒词才重新算。实测（11-05）它自己续自己：背景声一被回答，下一句又算接话，
# 连聊 29 分钟到时长上限，85 次回复里 69 次没过判断器。超过后交给判断器，妈妈真在聊时它基本都放行
MAX_FREE_REPLIES = 3
# 背景模式：连续这么多句被判“不是在跟机器人说”，多半开着电视或在打电话。实测（11-12）判断器能拦住长的电视对白，
# 却会放行夹在中间的“对”“嗯，行”“你疯了”，猫猫一句句接，家人听着就是在乱说。进入背景模式后：
# 不再免判断接话、3 个字以内的短应答不回、90 秒没有一句放行就安静结束会话；喊唤醒词或放行一句正经话就恢复
BACKGROUND_AFTER_IGNORED = 4
BACKGROUND_SHORT_CHARS = 3
BACKGROUND_CLOSE_SECONDS = 90
LOOP_STALL_SECONDS = 3   # 主循环每秒醒一次，隔这么久才醒说明被什么堵住了，记下来
# 对方只是让它等一下（实测“等我把桌子放到位了”），模型答应“安静等着”也不算要安静：进安静模式 18 分钟只认唤醒词，
# 她放好桌子回来喊它就不理了。不结束会话，等她回来接着聊
JUST_A_MOMENT = re.compile(r'等(我|一下|一会|会儿|等)|稍等|马上就')
# 一轮最长这么久就先交去识别。电视开着时 VAD 等不到静音，实测（11-04）一轮拖到 30~50 秒：
# 语音识别开始复读，还报 model repeat output 断线，人和电视混成一大段被判“不是在跟机器人说”
MAX_TURN_BYTES = 20 * 48000
# 对话中明确对着谁讲事：判断器实测一律判 no（“对，今天我跟你说……”），放宽提示词又会让它接电话、接闲聊
TELLING_YOU = re.compile(r'我[跟和给]你[说讲]|我告诉你')
# 模型自己说“我这就回去 / 我去充电啦 / 马上回充电座”：按单句判断，问句和否定不算
ASSISTANT_GOING_TO_DOCK = re.compile(r'(我|这就|马上|立刻|立马|乖乖)[^，,]{0,4}(回去|去充电|回充电座|回窝)')
ASSISTANT_NOT_GOING = re.compile(r'(不|别|没)[^，,]{0,2}(回去|去充电|回窝)|等会|待会|晚点|过会|一会儿再|[吗呢？?]')


def says_going_to_dock(reply: str) -> bool:
    return any(ASSISTANT_GOING_TO_DOCK.search(sentence) and not ASSISTANT_NOT_GOING.search(sentence)
               for sentence in re.split(r'(?<=[。！？!?.])', reply))  # 服务端转录超过这个时间就改用本地 Whisper
# 家人的话是以文字发给模型的（见 _settle_turn），来自语音识别
ASR_TEXT_INSTRUCTION = ('\n家人说的话会以文字形式给你，是语音识别出来的，常有同音错别字（比如“小人儿”写成“小人”），'
                        '请按读音和上下文理解，不要指出错字。'
                        '每句话开头方括号里是说这句话时家里的时间（程序加的，不是家人说的），问到几点就按它答，'
                        '别人说的时间和它对不上时以它为准；没人问就不要主动报时。')


def time_note(tz_name: str) -> str:
    """每句话带上家里的当前时间。实测（11-12）时间只在会话开头给一次：聊了 6 分钟还说“九点二十二”，
    有人说“六点十五”它也跟着说对（其实已经八点多）"""
    try:
        now = datetime.now(ZoneInfo(tz_name)) if tz_name else datetime.now()
    except Exception:
        now = datetime.now()
    return f'[家里现在 {now:%H:%M}] '
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
        # 开场前已经播出去的一段话（早上的天气录音）：放进上下文，对方接话或问“你刚才说啥”能接上
        self.spoken_first = ''
        self.spoken_first_note = ''   # 说那段话时还做了什么（如“开心地转了个圈”）
        self.night_context = ''       # 深夜的对话：夜聊口吻和今晚谈资（bedtime.py）
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
        self.free_replies = 0
        self.ignored_streak = 0             # 连续被判“不是在跟机器人说”的句数
        self.background_since = None        # 进入背景模式的时刻（monotonic），None = 不在背景模式
        self.homecoming_note = ''           # 会话中才认出有人回家：下一句回复时带给模型（note_homecoming）
        self.local_busy = False            # 上一次本地 Whisper 还没跑完（卡住时不再叠一个）
        self.last_assistant_text = ''
        self.turn_ended_at = time.monotonic()
        self.first_audio_logged = True      # 只记“这一轮之后的第一段回复声音”
        self.last_robot_audio_at = -1e9   # 机器人最后一次在说话（含播放尾音）的时刻
        self.turn_started_at = 0.0
        # 由 Supervisor 注入：用户让它回去充电时调用（发回充命令、暂停视觉跟随）
        self.on_dock_request = None
        self.on_quiet_request = None   # 由 Supervisor 注入：用户让它闭嘴时调用
        self.on_quiet_promised = None  # 由 Supervisor 注入：模型自己答应安静时调用（进入安静模式）
        self.last_dock_request = -1e9
        self.heard: deque[tuple[float, str]] = deque(maxlen=4)
        # 记忆（memory.py）：由 Supervisor 注入开场前已经知道的事，会话结束时把这场的逐句记录交回去
        self.memory_context = ''
        self.on_session_end = None
        self.memory_lines: list[tuple[str, str, float]] = []

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
            if self.abort_requested.is_set():   # 连接期间被叫停（比如进了安静模式）：别再开口
                return

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
        if self.memory_context:
            text += f'\n\n{self.memory_context}\n'
        if self.night_context:
            text += self.night_context
        if self.opening_prompt:
            # response.create 里的 instructions 只管第一句；后面怎么接着聊要放进整场对话的指令里
            text += f'\n这场对话是你主动开始的：{self.opening_prompt}'
        if self.spoken_first:
            text += (f'\n这场对话是你主动开始的：你刚刚已经对{name or "家人"}说了下面这段话（已经播完了）：'
                     f'「{self.spoken_first}」'
                     + (f'说的时候你还{self.spoken_first_note}（是你自己高兴才这么做的）。'
                        if self.spoken_first_note else '')
                     + f'\n对方接下来说的话多半是在回应它，或者问你刚才说了什么、'
                     f'哪句没听清。顺着接话，活泼、简短；没让你重说就别从头再念一遍，'
                     f'问到哪句就只说那句，数字照上面那段说，不要改。')
        # 回充由程序在听到请求时自动执行，模型只需要答应，不能说自己做不到
        return text + ASR_TEXT_INSTRUCTION + DOCK_INSTRUCTION

    async def _opening(self):
        """先送唤醒句音频，再由主循环送 startup_audio 与实时音频，最后统一 commit。"""
        turn, self.initial_turn = self.initial_turn, None
        if self.spoken_first:
            # 录音由 Supervisor 在连上之前就开始播了；这里只记成机器人说过的话，然后等对方开口
            LOG.info('Opening after a prepared announcement: %s', self.trigger)
            self.last_assistant_text = self.spoken_first[-200:]
            self.memory_lines.append(('assistant', self.spoken_first, time.time()))
            return
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
            self.memory_lines.append(('user', turn.text, time.time()))
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
            woke = time.monotonic()
            while not self.stop.is_set():
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=1)
                except asyncio.TimeoutError:
                    await self._check_timeouts()
                now = time.monotonic()
                if now - woke > LOOP_STALL_SECONDS:
                    LOG.warning('Event loop stalled for %.1fs', now - woke)
                woke = now
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
        elif (self.background_since is not None and time.monotonic()
              - max(self.background_since, self.last_activity) > BACKGROUND_CLOSE_SECONDS):
            LOG.info('Only background talk for %ds; closing quietly', BACKGROUND_CLOSE_SECONDS)
            self.stop.set()
        elif time.monotonic() - self.last_activity > min(self.config.session_idle_seconds, QWEN_IDLE_LIMIT - 30):
            LOG.info('Session idle for %.0fs, closing', min(self.config.session_idle_seconds, QWEN_IDLE_LIMIT - 30))
            self.stop.set()

    async def _process_audio_queue(self):
        """本地判断轮次：送音频 -> 静音达到阈值 -> commit + response.create"""
        while not self.stop.is_set():
            pcm = await self.audio_queue.get()
            await asyncio.sleep(0)  # 队列有积压时 get 不让出主循环，判断和回复都轮不到
            self.last_audio_at = time.monotonic()
            if not self.qwen:
                continue

            # 半双工：机器人说话（含播放尾音）时丢弃麦克风输入，防止听到自己
            if self.response_active or self.speaker.input_muted():
                self.last_robot_audio_at = time.monotonic()
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
            too_long = len(self.turn_audio) >= MAX_TURN_BYTES
            if ((ended or too_long) and self.uncommitted_audio and self.audio_queue.empty()
                    and not self.awaiting_transcript):
                self.state.update(input_speech_active=False)
                await self._finish_user_turn()

    async def _send_turn_audio(self, pcm: bytes):
        """这一轮的音频：只实时送给识别连接；对话模型等判断完、要回复时才一次性送（见 _settle_turn）。"""
        if not self.turn_audio:
            self.turn_started_at = time.monotonic()
        self.turn_audio.extend(pcm)
        if self.asr:
            self.asr.send_audio(resample_24k_to_16k(pcm))

    async def _finish_user_turn(self):
        if self.close_requested.is_set():
            self.stop.set()
            return
        LOG.info('User turn ended (%.1fs of audio); transcribing before deciding whether to commit',
                 len(self.turn_audio) / 48000)
        self.uncommitted_audio = False
        self.turn_ended_at = time.monotonic()
        self.turn_detector.reset()
        turn_pcm = bytes(self.turn_audio)
        self.turn_audio.clear()
        self.awaiting_transcript = True
        transcript = self.asr.commit() if self.asr else None
        is_reply = self.turn_started_at - self.last_robot_audio_at < REPLY_WINDOW_SECONDS
        asyncio.create_task(self._respond_if_speech(turn_pcm, transcript, is_reply))

    async def _turn_transcript(self, turn_pcm: bytes, transcript: asyncio.Future | None) -> str:
        """这一轮说了什么。识别连接超时或断开时用本地 Whisper：实测电视噪声的转录常常 6 秒以上才回来
        而且是空的，以前超时就“照常回复”，模型只能反复说“我在听”。"""
        if transcript is not None:
            try:
                # shield：超时后识别结果还要接着等，迟到的也比 Whisper 准
                text = await asyncio.wait_for(asyncio.shield(transcript), TRANSCRIPT_WAIT_SECONDS)
            except asyncio.TimeoutError:
                text = None
            if text is not None:
                return text
        waiting = {transcript} if transcript is not None and not transcript.done() else set()
        local = self._start_local_transcribe(turn_pcm)
        if local is not None:
            waiting.add(local)
        if not waiting:
            LOG.warning('No transcript within %ss and no local transcriber; ignoring turn', TRANSCRIPT_WAIT_SECONDS)
            return ''
        # 谁先出文字用谁；都没有就算没听到，不能一直等，等的时候下一轮也切不开
        deadline = time.monotonic() + TRANSCRIPT_DEADLINE_SECONDS - TRANSCRIPT_WAIT_SECONDS
        while waiting:
            done, waiting = await asyncio.wait(waiting, timeout=max(0.0, deadline - time.monotonic()),
                                               return_when=asyncio.FIRST_COMPLETED)
            if not done:
                LOG.warning('No transcript within %ss; ignoring turn', TRANSCRIPT_DEADLINE_SECONDS)
                if transcript in waiting:
                    transcript.add_done_callback(self._log_too_late)
                return ''
            for task in done:
                text = '' if task.cancelled() or task.exception() else task.result()
                LOG.info('ASR transcript late or unavailable; %s heard: %r',
                         'local Whisper' if task is local else 'late ASR', text)
                if text and text.strip():
                    return text
        return ''

    @staticmethod
    def _log_too_late(transcript: asyncio.Future):
        """过了上限才回来的识别：这一轮已经放弃了，记下来看有没有吞掉真话。"""
        if not transcript.cancelled():
            LOG.info('ASR arrived after the deadline: %r', transcript.result())

    def _start_local_transcribe(self, turn_pcm: bytes) -> asyncio.Task | None:
        if self.local_transcribe is None or self.local_busy:
            return None
        self.local_busy = True

        def finished(task: asyncio.Task):
            self.local_busy = False
            if not task.cancelled() and task.exception():
                LOG.warning('Local transcription failed: %r', task.exception())
        task = asyncio.create_task(asyncio.to_thread(self.local_transcribe, turn_pcm))
        task.add_done_callback(finished)
        return task

    async def _settle_turn(self, turn_pcm: bytes, respond: bool, text: str = ''):
        """判断完：要回复才把这一轮送进对话；否则对话模型根本听不到（电视声、噪声不进上下文）。

        送识别出的文字，不送音频。实测（10-23）：判断完一次性送音频，服务端要按音频时长“听”一遍才开始回答，
        2 秒的话要 2.5~5 秒才出声，夹着电视声的长句等 20 秒；送文字约 1 秒。
        也不能边说边送、不回复时再清掉：实测 input_audio_buffer.clear 和 conversation.item.delete
        都回“成功”，模型却照样回答被丢掉的那句。"""
        try:
            if respond and self.qwen and not self.stop.is_set():
                if text.strip():
                    note, self.homecoming_note = self.homecoming_note, ''
                    clock = time_note(getattr(self.config, 'home_timezone', ''))
                    await self.qwen.create_conversation_item(clock + note + text.strip(),
                                                             f'item_{uuid.uuid4().hex[:20]}')
                else:
                    await self.qwen.send_audio(resample_24k_to_16k(turn_pcm))
                    await self.qwen.commit_audio_buffer()
                LOG.info('Reply requested %.1fs after the turn ended', time.monotonic() - self.turn_ended_at)
                self.first_audio_logged = False
                self.last_activity = time.monotonic()
                self.response_active = True
                self.state.increment('manual_responses_requested')
                await self.qwen.create_response()
        finally:
            self.awaiting_transcript = False

    async def _respond_if_speech(self, turn_pcm: bytes, transcript: asyncio.Future | None, is_reply: bool = False):
        """只有这一轮真的有人说话才回复：噪声/电视声被切成一轮时转录为空，回复会变成反复道歉。"""
        respond = False
        text = ''
        try:
            text = await self._turn_transcript(turn_pcm, transcript)
            if not valid_reply_text(text):
                # 不记进对话记录和上下文：多半是履带声等噪音被识别成的“嗯。”
                LOG.info('Ignored turn without real speech: %r', text)
                self.state.increment('input_turns_ignored')
                return
            collapsed = collapse_repeats(text)
            if collapsed != text:
                LOG.info('Collapsed repeated ASR text: %d -> %d chars', len(text), len(collapsed))
                text = collapsed
            self._record_user_text(text)
            skip_check, self.skip_addressee_once = self.skip_addressee_once, False
            wake_word = has_wake_word(text, self.wake_words) and not loose_wake_word(text, self.wake_words)
            if wake_word:
                self.free_replies = 0
                self._leave_background('wake word')
            background = self.background_since is not None
            if background and not skip_check and len(re.findall(r'[一-鿿]', text)) <= BACKGROUND_SHORT_CHARS:
                LOG.info('Background mode: ignored a short reply-like turn: %r', text)
                self.state.increment('input_turns_ignored')
                return
            if is_reply and not skip_check and not background and self.free_replies < MAX_FREE_REPLIES:
                self.free_replies += 1
                LOG.info('Turn started within %ds of the robot speaking; treating it as a reply (%d/%d)',
                         REPLY_WINDOW_SECONDS, self.free_replies, MAX_FREE_REPLIES)
                skip_check = True
            if not skip_check and TELLING_YOU.search(text):
                LOG.info('Turn says it is telling the robot something; treating it as addressed')
                skip_check = True
            if text and self.addressee is not None and not skip_check and not wake_word:
                # heard 的最后一条就是当前这句
                recent = tuple(t for at, t in list(self.heard)[:-1] if at >= time.monotonic() - 60)
                addressed = await asyncio.to_thread(self.addressee.is_addressed, text,
                                                    self.last_assistant_text, recent)
                # 已经在对话中：判断失败时照常回复，避免把真人的话吞掉
                if addressed is False:
                    LOG.info('Ignored turn not addressed to the robot: %r', text)
                    self.state.increment('input_turns_ignored')
                    self.ignored_streak += 1
                    if self.background_since is None and self.ignored_streak >= BACKGROUND_AFTER_IGNORED:
                        LOG.info('%d turns in a row not addressed to the robot; background mode (TV or a call?)',
                                 self.ignored_streak)
                        self.background_since = time.monotonic()
                    return
                if addressed is True:
                    self._leave_background('the judge let a turn through')
            self.ignored_streak = 0
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
            await self._settle_turn(turn_pcm, respond, text)

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
            if not self.first_audio_logged:
                self.first_audio_logged = True
                LOG.info('First reply audio %.1fs after the turn ended', time.monotonic() - self.turn_ended_at)
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
            self.memory_lines.append(('assistant', transcript, time.time()))
            # 说到做到：模型答应了“我这就回去（充电）”，就真的回充（实测用户说“回去吧”时它答应了却没动）
            # 说了回去就结束这场对话（这句播完就断）：实测（11-12）它回了充电座，会话却还开着，
            # 背景里每个“对”都接一句，连说四遍“我去充电座待着啦”
            going_to_dock = self.on_dock_request is not None and says_going_to_dock(transcript)
            if going_to_dock and time.monotonic() - self.last_dock_request > 60:
                LOG.info('Assistant said it is going to charge; docking: %r', transcript[:60])
                self.last_dock_request = time.monotonic()
                self.loop.run_in_executor(None, self.on_dock_request)
            if going_to_dock:
                LOG.info('Assistant said it is going to charge; ending the session after this reply')
                self._request_close()
            elif self.addressee is not None and self.on_quiet_promised and may_promise_quiet(transcript):
                asyncio.create_task(self._honor_quiet_promise(transcript))
        if transcript:
            self.outputs.append(
                output_id=self.current_response_id,
                response_id=self.current_response_id,
                item_id=item_id,
                transcript=transcript,
                metadata=self.speaker.output_metrics(self.current_response_id),
            )
        self.current_transcript_buffer = ''

    async def _honor_quiet_promise(self, reply: str):
        """说到做到：模型答应安静，就真的进入安静模式并结束会话，这句播完就断。
        以前会话一直挂到空闲超时：答应了不说，之后的电视声、闲聊还在转写、判断，甚至回复。"""
        if self.heard and JUST_A_MOMENT.search(self.heard[-1][1]):
            LOG.info('Asked to wait a moment, not to be quiet; keeping the session: %r', self.heard[-1][1][:60])
            return
        if await asyncio.to_thread(self.addressee.promised_quiet, reply) is True and not self.stop.is_set():
            LOG.info('Assistant promised to stay quiet; ending the session: %r', reply[:60])
            self.on_quiet_promised()
            self._request_close()

    def _record_user_text(self, transcript: str):
        """这一轮听到的话：记日志和转录，并放进 heard（判断电视对白时连起来看）"""
        LOG.info('User said: %s', transcript)
        if transcript.strip():
            self.heard.append((time.monotonic(), transcript.strip()))
            self.memory_lines.append(('user', transcript.strip(), time.time()))
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

    def note_homecoming(self, name: str) -> bool:
        """会话已经开着时才认出有人回家（实测妈妈进门先开口，2 秒后才认出来，问候被跳过，她只好问“你咋不问候我”）。
        下一句回复时顺带问候。会话快结束了就返回 False，由调用方照常处理。"""
        if self.stop.is_set() or self.close_requested.is_set():
            return False
        self.homecoming_note = (f'（旁白，不要读出来：{name}刚回到家。这一句先欢迎{name}回家、心疼一句今天辛苦了，'
                                f'再接下面这句话。）')
        return True

    def request_close(self):
        """请求关闭会话（当前回复播完后结束）"""
        LOG.info('Close requested for session %s', self.session_id)
        if self.loop is None:
            self.abort_requested.set()
            return
        self.loop.call_soon_threadsafe(self._request_close)

    def _leave_background(self, why: str):
        self.ignored_streak = 0
        if self.background_since is not None:
            LOG.info('Leaving background mode: %s', why)
            self.background_since = None

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
        if self.on_session_end is not None:
            try:
                self.on_session_end(self)
            except Exception as exc:
                LOG.warning('Cannot hand the session to memory: %s', exc)
