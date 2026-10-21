"""Standby 唤醒：本地 VAD 分段，只有完整语句才交给本地 Whisper 转录。"""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass
import logging
import queue
import re
import threading
import time
import uuid

import webrtcvad

import timers
import wake_match
from addressee import AddresseeClassifier, may_be_dock_request, may_be_quiet_request
from audio_utils import FRAME_BYTES_24K, MotionNoiseGate, is_speech_24k, pcm16k_to_float, resample_24k_to_16k

LOG = logging.getLogger('ebo-standby-voice')

# Whisper 在噪声/静音上常编出的固定句子（训练数据里的视频字幕）
HALLUCINATIONS = ('字幕', '订阅', '点赞', '打赏', '转发', 'Amara', '谢谢观看', '感谢观看', '请不吝')
# 整句只有这些客套话时不唤醒：Whisper small 在噪声上会编出“谢谢”（视频结尾字幕），
# 实测 10-20 把机器人唤醒了三次，Qwen 对同一段音频转出来是空的。只拦整句，“谢谢你猫猫”照常唤醒
STOCK_PHRASES = {'谢谢', '谢谢谢谢', '谢谢你', '谢谢大家', '谢谢收看', '谢谢收听', '感谢收看', '感谢收听'}

RECENT_CONTEXT_SECONDS = 60
REPEATED = re.compile(r'(.{1,10}?)\1{2,}')

FILLER_WORDS = {'嗯嗯', '啊啊', '哦哦', '呃呃', '喵喵', '哈哈', '呵呵'}
# 只有语气词（“嗯。”“嗯嗯嗯”“啊”“呃……”）：多半是履带声或别的噪音被识别出来的，一律当没听到
INTERJECTIONS = set('嗯恩啊阿哦噢喔呃额唔哼嘿诶欸哎唉呀嗷哈呵')


def is_filler(text: str) -> bool:
    compact = compact_text(text)
    return bool(compact) and all(c in INTERJECTIONS for c in compact)


@dataclass(frozen=True)
class VoiceTurn:
    item_id: str
    pcm: bytes  # 24 kHz s16le
    captured_at: float
    epoch: int
    text: str = ''
    languages: object = None


def actionable_transcript(transcript: str) -> bool:
    """中文家庭场景：含中文或数字即可；纯英文短语（多为误识别）丢弃。"""
    normalized = ' '.join(transcript.split())
    if not normalized:
        return False
    if re.search(r'[㐀-䶿一-鿿]', normalized) or any(c.isdigit() for c in normalized):
        return True
    words = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", normalized)
    return not (words and len(words) <= 3 and len(normalized) <= 24)


def compact_text(text: str) -> str:
    return ''.join(re.findall(r'[\w一-鿿]', text or '')).lower()


def valid_wake_text(text: str, wake_words: tuple[str, ...] = ()) -> bool:
    compact = compact_text(text)
    if len(compact) < 2 or compact in FILLER_WORDS or is_filler(text) or not actionable_transcript(text):
        return False
    # Whisper 典型幻觉：同一短语重复好几遍（实测“做一件事做一件事做一件事”把机器人唤醒了）
    if REPEATED.fullmatch(compact):
        return False
    return not wake_words or has_wake_word(text, wake_words)


def has_wake_word(text: str, wake_words: tuple[str, ...]) -> bool:
    """按读音容错匹配（“毛毛”“小盆友”也算），见 wake_match.py"""
    return bool(wake_words) and wake_match.matches(text, wake_words)


class SpeechSegmenter:
    """24 kHz PCM -> 完整语句；静音与短噪声被丢弃。"""

    def __init__(self, vad=None, silence_frames: int = 45):
        self.vad = vad or webrtcvad.Vad(2)
        self.silence_frames = silence_frames  # 45 帧 x 20 ms = 900 ms
        self.gate = MotionNoiseGate()
        self.reset()

    def reset(self):
        self.preroll = deque(maxlen=15)  # 300 ms
        self.chunks: list[bytes] = []
        self.voiced = self.silence = 0
        self.too_long = False
        self._pending = bytearray()

    def in_progress(self) -> bytes:
        """正在说、尚未结束的那一段（唤醒被接受时要交给会话，不能丢）。"""
        return b''.join(self.chunks) + bytes(self._pending) if self.chunks else b''

    def observe(self, pcm: bytes) -> list[bytes]:
        results = []
        self._pending.extend(pcm)
        while len(self._pending) >= FRAME_BYTES_24K:
            frame = bytes(self._pending[:FRAME_BYTES_24K])
            del self._pending[:FRAME_BYTES_24K]
            voiced = self.gate.voiced(is_speech_24k(self.vad, frame), frame)
            if not self.chunks:
                self.preroll.append(frame)
                if not voiced:
                    continue
                self.chunks = list(self.preroll)
                self.preroll.clear()
            elif not self.too_long:
                self.chunks.append(frame)
            self.voiced += int(voiced)
            self.silence = 0 if voiced else self.silence + 1
            # 超长语句（>30 s）不在中途回答：丢弃，静音后重新监听
            if len(self.chunks) >= 1500:
                self.too_long = True
            if self.silence >= self.silence_frames:
                if self.voiced >= 15 and not self.too_long:
                    results.append(b''.join(self.chunks))
                pending = bytes(self._pending)
                self.reset()
                self._pending.extend(pending)
        return results


class WhisperTranscriber:
    """本地 faster-whisper，首次使用时加载（CPU int8）。"""

    def __init__(self, model_name: str, language: str = 'zh'):
        self.model_name = model_name
        self.language = language
        self.model = None
        # 不用 initial_prompt：实测 Whisper 会把提示词改写后当成听到的话（“以下是简体的字”等），
        # 繁体输出改由 OpenCC 转成简体
        from opencc import OpenCC
        self.to_simplified = OpenCC('t2s')

    def load(self):
        if self.model is None:
            from faster_whisper import WhisperModel
            started = time.monotonic()
            self.model = WhisperModel(self.model_name, device='cpu', compute_type='int8')
            LOG.info('Whisper model %s loaded in %.1fs', self.model_name, time.monotonic() - started)
        return self.model

    def transcribe(self, pcm24k: bytes) -> str:
        audio = pcm16k_to_float(resample_24k_to_16k(pcm24k))
        segments, _info = self.load().transcribe(
            audio,
            language=self.language,
            beam_size=1,
            condition_on_previous_text=False,
            vad_filter=True,  # 先用 Silero VAD 去掉非语音，Whisper 对噪声会编出句子
        )
        kept = [s.text for s in segments if s.no_speech_prob < 0.6 and s.avg_logprob > -1.0]
        text = self.to_simplified.convert(''.join(kept).strip())
        if any(phrase in text for phrase in HALLUCINATIONS):
            LOG.info('standby ignored Whisper hallucination: %r', text)
            return ''
        return text


class StandbyVoice:
    def __init__(self, config, state, eligible, accept):
        self.config, self.state = config, state
        self.eligible, self.accept = eligible, accept
        self.segmenter = SpeechSegmenter(silence_frames=max(10, config.standby_silence_ms // 20))
        self.lock = threading.Lock()
        self.epoch = 0
        self.pending: queue.Queue[VoiceTurn] = queue.Queue(maxsize=4)
        self.stopping = threading.Event()
        self.wake_words = tuple(w.strip().lower() for w in config.wake_words.split(',') if w.strip())
        self.transcriber = WhisperTranscriber(config.whisper_model, config.whisper_language)
        # 不说唤醒词也能自然接话：其他话交给小模型判断是不是在对机器人说（挡掉电视对白）
        self.addressee = (AddresseeClassifier(config.qwen_api_key, config.qwen_region, config.addressee_model,
                                              self.wake_words) if config.addressee_check else None)
        # 最近听到的话：电视对白是连续的，连起来看更容易认出来
        self.heard: deque[tuple[float, str]] = deque(maxlen=4)
        self.quiet_until = 0.0
        left = timers.remaining(config.timers_path, 'quiet')
        if left > 0:
            self.quiet_until = time.monotonic() + left
            state.update(quiet_until=time.time() + left)
            LOG.info('Restored quiet mode after restart: %.0f s left', left)
        self.on_quiet_request = None  # 由 Supervisor 设置
        self.on_dock_request = None   # 由 Supervisor 设置
        state.update(voice_wake_enabled=config.voice_wake_enabled)

    def reset(self):
        with self.lock:
            self.epoch += 1
            self.segmenter.reset()
        while True:
            try:
                self.pending.get_nowait()
            except queue.Empty:
                break
        self.state.update(voice_wake_listening=False)

    def take_buffered_audio(self) -> list[bytes]:
        """取出已排队的后续语句和正在说的半句，然后清空（唤醒被接受时调用）。"""
        with self.lock:
            audio = []
            while True:
                try:
                    turn = self.pending.get_nowait()
                except queue.Empty:
                    break
                if turn.epoch == self.epoch:
                    audio.append(turn.pcm)
            partial = self.segmenter.in_progress()
            if partial:
                audio.append(partial)
            self.epoch += 1
            self.segmenter.reset()
        self.state.update(voice_wake_listening=False)
        return audio

    def offer_audio(self, pcm: bytes):
        if not self.config.voice_wake_enabled or not self.eligible():
            self.reset()
            return
        self.state.update(voice_wake_listening=True)
        with self.lock:
            for audio in self.segmenter.observe(pcm):
                turn = VoiceTurn('wake_' + uuid.uuid4().hex[:24], audio, time.monotonic(), self.epoch)
                try:
                    self.pending.put_nowait(turn)
                    self.state.increment('voice_wake_candidates')
                except queue.Full:
                    self.state.increment('voice_wake_rejected')

    def _stale(self, turn: VoiceTurn) -> bool:
        return turn.epoch != self.epoch or not self.eligible() or time.monotonic() - turn.captured_at > 20

    def recent_heard(self) -> tuple[str, ...]:
        cutoff = time.monotonic() - RECENT_CONTEXT_SECONDS
        return tuple(text for at, text in self.heard if at >= cutoff)

    def quiet(self, seconds: float):
        """用户让它闭嘴：这段时间只认唤醒词，其他话照常本地听写但不唤醒、不调用任何模型。"""
        self.quiet_until = time.monotonic() + seconds
        self.state.update(quiet_until=time.time() + seconds)
        timers.save(self.config.timers_path, 'quiet', time.time() + seconds)
        LOG.info('Quiet mode for %.0f min: only wake words wake the robot', seconds / 60)

    def quiet_active(self) -> bool:
        return time.monotonic() < self.quiet_until

    def end_quiet(self, reason: str):
        if self.quiet_until:
            self.quiet_until = 0.0
            self.state.update(quiet_until=0)
            timers.save(self.config.timers_path, 'quiet', 0)
            LOG.info('Quiet mode ended: %s', reason)

    def should_wake(self, text: str) -> bool:
        if not valid_wake_text(text):
            return False
        wake_word = bool(self.wake_words) and has_wake_word(text, self.wake_words)
        if not wake_word and compact_text(text) in STOCK_PHRASES:
            LOG.info('standby ignored a stock phrase Whisper makes up on noise: %r', text)
            return False
        recent = self.recent_heard()
        self.heard.append((time.monotonic(), text))
        if self.quiet_active() and not wake_word:
            return False
        # 待机时直接说“闭嘴”：不唤醒，进入安静（含唤醒词的“猫猫闭嘴”同理）
        if (self.addressee is not None and self.on_quiet_request and may_be_quiet_request(text)
                and (wake_word or self.addressee.is_addressed(text, recent=recent) is True)
                and self.addressee.wants_quiet(text) is True):
            self.on_quiet_request()
            return False
        # 待机时直接说“回去充电”：不唤醒，直接回充（实测以前待机只判断要不要唤醒，这句话被当成乱码丢掉）
        if (self.addressee is not None and self.on_dock_request and may_be_dock_request(text)
                and self.addressee.wants_to_dock(text) is True):
            LOG.info('Standby heard a request to go charge: %r', text)
            self.on_dock_request()
            return False
        if wake_word:
            self.end_quiet('wake word')
            return True
        if self.addressee is not None:
            # 判断失败（网络等）时不唤醒：宁可要求说唤醒词，也不能和电视聊起来
            return self.addressee.is_addressed(text, recent=recent) is True
        return not self.wake_words

    def process(self, turn: VoiceTurn) -> bool:
        if self._stale(turn):
            self.state.increment('voice_wake_rejected')
            return False
        self.state.update(voice_wake_transcribing=True)
        try:
            text = self.transcriber.transcribe(turn.pcm)
            self.state.update(voice_wake_last_error='')
            LOG.info('standby heard: %r', text)
            if self._stale(turn) or not self.should_wake(text):
                self.state.increment('voice_wake_rejected')
                return False
            verified = VoiceTurn(turn.item_id, turn.pcm, turn.captured_at, turn.epoch, text)
            if self.accept(verified):
                self.state.increment('voice_wake_accepted')
                self.state.update(last_voice_wake_at=time.time())
                LOG.info('validated standby voice opened a conversation: item=%s', turn.item_id)
                return True
            self.state.increment('voice_wake_rejected')
        except Exception as exc:
            self.state.update(voice_wake_last_error=type(exc).__name__)
            LOG.warning('standby transcription failed: %s', exc)
        finally:
            self.state.update(voice_wake_transcribing=False)
        return False

    def run(self):
        if self.config.voice_wake_enabled:
            try:
                self.transcriber.load()  # 启动时预加载，避免首次唤醒多等几秒
            except Exception as exc:
                self.state.update(voice_wake_last_error=type(exc).__name__)
                LOG.error('Failed to load Whisper model: %s', exc)
        while not self.stopping.is_set():
            try:
                turn = self.pending.get(timeout=.2)
            except queue.Empty:
                continue
            self.process(turn)

    def stop(self):
        self.stopping.set()
        self.reset()
