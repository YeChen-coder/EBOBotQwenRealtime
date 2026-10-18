#!/usr/bin/env python3
"""Run the EBO entrypoint while mirroring its combined output to a rotating file.

Docker Desktop keeps container stdout inside its Linux VM.  This wrapper preserves that
stdout stream and additionally writes it under /data, which is bind-mounted from the host.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys


DEFAULT_PATH = "/data/logs/ebo-engine.log"
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5


def _positive_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise SystemExit(f"{name} must be at least {minimum}, got {value}")
    return value


class RotatingByteLog:
    """Small binary rotating writer that keeps at most N size-limited backups."""

    def __init__(self, path: Path, max_bytes: int, backup_count: int) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._output = self.path.open("ab", buffering=0)

    def _size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def _rotate(self) -> None:
        self._output.close()
        oldest = self.path.with_name(f"{self.path.name}.{self.backup_count}")
        try:
            oldest.unlink()
        except FileNotFoundError:
            pass
        for index in range(self.backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            target = self.path.with_name(f"{self.path.name}.{index + 1}")
            if source.exists():
                source.replace(target)
        if self.path.exists():
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))
        self._output = self.path.open("ab", buffering=0)

    def write(self, data: bytes) -> None:
        if not data:
            return
        size = self._size()
        if size and size + len(data) > self.max_bytes:
            self._rotate()
        self._output.write(data)

    def close(self) -> None:
        self._output.close()


def _console(data: bytes) -> None:
    try:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    except BrokenPipeError:
        pass


def main() -> int:
    path = Path(os.environ.get("EBO_HOST_LOG_PATH", DEFAULT_PATH).strip() or DEFAULT_PATH)
    max_bytes = _positive_int("EBO_HOST_LOG_MAX_BYTES", DEFAULT_MAX_BYTES, 1024)
    backup_count = _positive_int(
        "EBO_HOST_LOG_BACKUP_COUNT", DEFAULT_BACKUP_COUNT, 1
    )
    durable: RotatingByteLog | None
    try:
        durable = RotatingByteLog(path, max_bytes, backup_count)
    except OSError as exc:
        durable = None
        _console(
            f"[host-log] WARNING: cannot open {path}: {exc}; "
            "continuing with Docker logs only\n".encode("utf-8", "replace")
        )
    child: subprocess.Popen[bytes] | None = None

    def mirror(data: bytes) -> None:
        nonlocal durable
        _console(data)
        if durable is None:
            return
        try:
            durable.write(data)
        except OSError as exc:
            try:
                durable.close()
            except OSError:
                pass
            durable = None
            _console(
                f"[host-log] WARNING: file logging disabled after error: {exc}\n".encode(
                    "utf-8", "replace"
                )
            )

    def forward(signum: int, _frame: object) -> None:
        if child is None or child.poll() is not None:
            return
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)

    banner = (
        f"[host-log] mirroring EBO Engine output to {path} "
        f"(rotate {max_bytes} bytes, {backup_count} backups)\n"
    ).encode("utf-8")
    mirror(banner)

    try:
        child = subprocess.Popen(
            ["/app/run.sh"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            bufsize=0,
        )
        assert child.stdout is not None
        while True:
            chunk = child.stdout.read(8192)
            if not chunk:
                break
            mirror(chunk)
        return child.wait()
    finally:
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if durable is not None:
            durable.close()


if __name__ == "__main__":
    raise SystemExit(main())
