"""EBO 麦克风是 24 kHz s16le；Qwen3.8 输入和 Whisper 都要求 16 kHz。"""
from __future__ import annotations

import numpy as np
import webrtcvad

FRAME_BYTES_24K = 960  # 20 ms @ 24 kHz s16le


def resample_24k_to_16k(pcm: bytes) -> bytes:
    """3 个样本 -> 2 个样本；输入长度为 3 样本整数倍时无块边界误差。"""
    samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 6], dtype='<i2').astype(np.int32)
    if samples.size == 0:
        return b''
    groups = samples.reshape(-1, 3)
    out = np.empty((groups.shape[0], 2), dtype=np.int32)
    out[:, 0] = (2 * groups[:, 0] + groups[:, 1]) // 3
    out[:, 1] = (groups[:, 1] + 2 * groups[:, 2]) // 3
    return out.reshape(-1).astype('<i2').tobytes()


def pcm16k_to_float(pcm16k: bytes) -> np.ndarray:
    return np.frombuffer(pcm16k, dtype='<i2').astype(np.float32) / 32768.0


def is_speech_24k(vad: webrtcvad.Vad, frame: bytes) -> bool:
    downsampled = np.frombuffer(frame, dtype='<i2')[::3].tobytes()
    return vad.is_speech(downsampled, 8000)


class EndOfTurnDetector:
    """会话中的本地 VAD：说过足够长的话、随后静音达到阈值 -> 一轮结束。"""

    def __init__(self, silence_frames: int = 35, min_voiced_frames: int = 15, vad_mode: int = 2):
        self.vad = webrtcvad.Vad(vad_mode)
        self.silence_frames = silence_frames
        self.min_voiced_frames = min_voiced_frames
        self._pending = bytearray()
        self.reset()

    def reset(self):
        self._pending.clear()
        self.voiced = 0
        self.silence = 0

    def mark_speech(self):
        """外部已确认有完整语音（例如唤醒句），直接进入“等待静音”状态。"""
        self.voiced = max(self.voiced, self.min_voiced_frames)
        self.silence = 0

    @property
    def has_speech(self) -> bool:
        return self.voiced >= self.min_voiced_frames

    def observe(self, pcm: bytes) -> tuple[bool, bool]:
        """返回 (本块是否含语音, 当前是否处于“说完了”状态)。

        “说完了”状态会一直保持到调用方 reset()，这样调用方可以推迟提交
        （例如积压音频还没送完），之后再有语音会自动取消该状态。
        """
        self._pending.extend(pcm)
        any_voiced = False
        while len(self._pending) >= FRAME_BYTES_24K:
            frame = bytes(self._pending[:FRAME_BYTES_24K])
            del self._pending[:FRAME_BYTES_24K]
            voiced = is_speech_24k(self.vad, frame)
            any_voiced |= voiced
            if voiced:
                self.voiced += 1
                self.silence = 0
            else:
                self.silence += 1
        return any_voiced, self.has_speech and self.silence >= self.silence_frames
