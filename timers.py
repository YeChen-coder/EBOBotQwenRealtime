"""跨重启保留的倒计时（回充后不追人、闭嘴后的安静模式）。

实测：回充冷却只记在内存里，程序一重启（部署、崩溃自动重启、电脑重启）就丢了，
机器人 3 分钟后又开出了充电座。这里按墙上时间存结束时刻，启动时读回来接着算。
"""
from __future__ import annotations
import json
import logging
import threading
import time
from pathlib import Path

LOG = logging.getLogger('ebo-timers')
_lock = threading.Lock()


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save(path: str, name: str, until_wall: float) -> None:
    if not path:
        return
    file = Path(path)
    with _lock:
        data = _read(file)
        data[name] = until_wall
        try:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(json.dumps(data), encoding='utf-8')
        except OSError as exc:
            LOG.warning('Cannot save %s timer: %s', name, exc)


def remaining(path: str, name: str) -> float:
    """还剩多少秒（已过期或没有记录返回 0）。"""
    if not path:
        return 0.0
    with _lock:
        until = _read(Path(path)).get(name, 0)
    try:
        return max(0.0, float(until) - time.time())
    except (TypeError, ValueError):
        return 0.0
