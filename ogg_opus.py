"""把 16 kHz 单声道 PCM 边说边编码成 Ogg Opus，给会话中的语音识别连接用。

实测（11-08）国内晚高峰时多伦多到阿里云北京的上传只剩约 35 KB/s，PCM 要 43 KB/s，识别结果越拖越晚，
每一轮都超时被丢。Opus 24 kbps 只有 PCM 的 9%，识别文字一字不差。
服务端只认 Ogg 封装的 Opus（裸 Opus 包会报 “Error committing input audio buffer”）。
一条识别连接从头到尾只能是一个 Ogg 流：服务端按连接解析，实测每轮另起一个流（重新带头、EOS 收尾），
第二轮起提交全部报同样的错。所以只在连接建立后的第一块带 OpusHead/OpusTags，提交前 flush() 补齐半帧。
"""
from __future__ import annotations
import random
import struct

import av
import numpy as np

SAMPLE_RATE = 16000
FRAME_SAMPLES = 320          # 20 ms
GRANULE_PER_FRAME = 960      # Ogg Opus 的 granule 永远按 48 kHz 计
PRE_SKIP = 312               # libopus 编码延迟（48 kHz 采样数）
BITRATE = 24000


def _crc_table() -> list[int]:
    table = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = ((r << 1) ^ 0x04C11DB7) if r & 0x80000000 else r << 1
        table.append(r & 0xFFFFFFFF)
    return table


_CRC = _crc_table()


def _crc(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC[((crc >> 24) & 0xFF) ^ byte]
    return crc


class OggOpusStream:
    """一条连接一个流：feed() 返回这次能发出去的 Ogg 数据（第一次会带上头两页），每轮提交前 flush()。"""

    def __init__(self, bitrate: int = BITRATE):
        self.serial = random.getrandbits(32)
        self.sequence = 0
        self.granule = 0
        self._pending = bytearray()      # 不够 20 ms 一帧的尾巴
        self._codec = av.CodecContext.create('libopus', 'w')
        self._codec.sample_rate = SAMPLE_RATE
        self._codec.layout = 'mono'
        self._codec.format = 's16'
        self._codec.bit_rate = bitrate
        self._codec.open()
        self._header = self._page([struct.pack('<8sBBHIhB', b'OpusHead', 1, 1, PRE_SKIP, SAMPLE_RATE, 0, 0)],
                                  0, bos=True)
        vendor = b'ebo-qwen'
        self._header += self._page([b'OpusTags' + struct.pack('<I', len(vendor)) + vendor + struct.pack('<I', 0)], 0)

    def feed(self, pcm16k: bytes) -> bytes:
        self._pending.extend(pcm16k)
        packets = []
        usable = len(self._pending) // (FRAME_SAMPLES * 2) * FRAME_SAMPLES * 2
        if usable:
            packets = self._encode(bytes(self._pending[:usable]))
            del self._pending[:usable]
        return self._take_header() + self._audio_page(packets)

    def flush(self) -> bytes:
        """这一轮说完、提交之前：不满 20 ms 的尾巴补静音编出去，免得它算进下一轮。"""
        if not self._pending:
            return b''
        tail = bytes(self._pending) + bytes(FRAME_SAMPLES * 2 - len(self._pending))
        self._pending.clear()
        return self._take_header() + self._audio_page(self._encode(tail))

    def _take_header(self) -> bytes:
        header, self._header = self._header, b''
        return header

    def _encode(self, pcm: bytes) -> list[bytes]:
        samples = np.frombuffer(pcm, dtype=np.int16)
        packets = []
        for offset in range(0, len(samples), FRAME_SAMPLES):
            frame = av.AudioFrame.from_ndarray(samples[offset:offset + FRAME_SAMPLES].reshape(1, -1),
                                               format='s16', layout='mono')
            frame.sample_rate = SAMPLE_RATE
            packets += [bytes(p) for p in self._codec.encode(frame)]
        return packets

    def _audio_page(self, packets: list[bytes]) -> bytes:
        if not packets:
            return b''
        self.granule += GRANULE_PER_FRAME * len(packets)
        return self._page(packets, self.granule)

    def _page(self, packets: list[bytes], granule: int, bos: bool = False) -> bytes:
        lacing = bytearray()
        for packet in packets:
            lacing += b'\xff' * (len(packet) // 255) + bytes([len(packet) % 255])
        if len(lacing) > 255:   # 100 ms 才 5 个包，到不了；真到了就拆成两页
            half = len(packets) // 2
            first = self._page(packets[:half], granule - GRANULE_PER_FRAME * (len(packets) - half), bos=bos)
            return first + self._page(packets[half:], granule)
        flags = 0x02 if bos else 0
        header = struct.pack('<4sBBqIIIB', b'OggS', 0, flags, granule, self.serial, self.sequence, 0, len(lacing))
        self.sequence += 1
        page = bytearray(header + lacing + b''.join(packets))
        page[22:26] = struct.pack('<I', _crc(bytes(page)))
        return bytes(page)
