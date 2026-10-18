"""找人转圈时自己看一眼：用和 Frigate 相同的 YOLOv9 模型直接识别一帧，门槛放低。

Frigate 只在画面有变化的区域识别，且要连续几帧分数中位数达到门槛才算“人”；
沙发上趴着、坐着、只露半身的人分数偏低，常常被漏掉。这里每转到一个方向停稳后识别一次。
"""
from __future__ import annotations
import io
import logging
import time
from urllib import request

import numpy as np
from PIL import Image

LOG = logging.getLogger('ebo-scan')
INPUT = 320
PERSON = 0


def _letterbox(img: Image.Image) -> tuple[np.ndarray, float]:
    scale = INPUT / max(img.size)
    resized = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))))
    canvas = Image.new('RGB', (INPUT, INPUT), (114, 114, 114))
    canvas.paste(resized, (0, 0))
    return np.asarray(canvas, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0, scale


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


class ScanDetector:
    def __init__(self, model_path: str, frame_url: str, min_score: float):
        import onnxruntime as ort
        self.session = ort.InferenceSession(model_path, providers=['CPUExecutionProvider'])
        self.input_name = self.session.get_inputs()[0].name
        self.frame_url = frame_url
        self.min_score = min_score

    def _detect(self, img: Image.Image, dx: float = 0.0) -> list[tuple[float, tuple]]:
        x, scale = _letterbox(img)
        out = self.session.run(None, {self.input_name: x})[0][0]   # (84, N): cx, cy, w, h, 80 类分数
        scores = out[4 + PERSON]
        found = []
        for i in np.where(scores >= self.min_score)[0]:
            cx, cy, w, h = out[:4, i] / scale
            found.append((float(scores[i]), (cx - w / 2 + dx, cy - h / 2, cx + w / 2 + dx, cy + h / 2)))
        return found

    def people(self, image: Image.Image | None = None) -> tuple[list[tuple[float, tuple]], tuple[int, int]]:
        """返回 ([(分数, 框)], 画面尺寸)。整幅 + 左右两半各识别一次（远处的人在整幅缩小后太小）。"""
        if image is None:
            with request.urlopen(self.frame_url, timeout=5) as response:
                image = Image.open(io.BytesIO(response.read())).convert('RGB')
        started = time.monotonic()
        width, height = image.size
        half = width // 2
        candidates = self._detect(image)
        candidates += self._detect(image.crop((0, 0, half, height)))
        candidates += self._detect(image.crop((half, 0, width, height)), dx=half)
        kept: list[tuple[float, tuple]] = []
        for score, box in sorted(candidates, key=lambda c: -c[0]):
            if all(_iou(box, other) < 0.45 for _, other in kept):
                kept.append((score, box))
        LOG.info('scan: %d person(s) %s (%.0f ms)', len(kept),
                 [round(s, 2) for s, _ in kept], (time.monotonic() - started) * 1000)
        return kept, (width, height)
