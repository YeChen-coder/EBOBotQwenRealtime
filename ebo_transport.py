"""EBO Engine transport and durable diagnostic output, retained from the EBO project.

Conversation decisions belong to conversation.py; this module only transports PCM.
"""
from __future__ import annotations
import json
import logging
import math
import queue
import threading
import time
import uuid
import wave
from dataclasses import dataclass, field
from pathlib import Path
from urllib import request
import websocket
from config import Config
from state import RuntimeState
LOG = logging.getLogger("ebo-transport")

STEERING_LEVELS = {"Low": 0, "Medium": 1, "High": 2, "Max": 3}
REPLY_RETENTION_DAYS = 7
RECENT_METRICS_LIMIT = 50


def _flag(value: object) -> bool:
    return value is True or str(value).lower() == "true"


class EboAPI:
    def __init__(self, config: Config, state: RuntimeState) -> None:
        self.config = config
        self.state = state
        self._last_wake = 0.0

    def audio_health(self) -> dict[str, object]:
        return self.media_health()["audio"]

    def media_health(self) -> dict[str, object]:
        req = request.Request(
            self.config.ebo_api_url + "/api/robots",
            headers={"X-Enabot-Token": self.config.ebo_api_token},
        )
        with request.urlopen(req, timeout=5) as response:
            robots = json.load(response)
        for robot in robots:
            if robot.get("node") == self.config.ebo_node:
                health = robot.get("audio_health")
                if not isinstance(health, dict):
                    raise ValueError("Engine audio health unavailable; update Engine too")
                for key in ("observed_at", "last_packet_at", "last_pcm_at"):
                    stamp = float(health.get(key) or 0)
                    if not math.isfinite(stamp):
                        raise ValueError("Invalid Engine audio health timestamp")
                video = robot.get("video_health")
                if not isinstance(video, dict):
                    # Missing video telemetry must not turn verified mic audio
                    # into a monitor failure or disable voice-only wakeup.
                    video = dict(observed_at=time.time(), status='monitor_error',
                                 source_video_ok=False, last_frame_at=0)
                return {"audio": health, "video": video}
        raise ValueError("Robot missing from Engine audio health")

    def command(self, suffix: str, payload: str = "") -> None:
        body = json.dumps(
            {"node": self.config.ebo_node, "suffix": suffix, "payload": str(payload)}
        ).encode()
        req = request.Request(
            self.config.ebo_api_url + "/api/cmd",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Enabot-Token": self.config.ebo_api_token,
            },
        )
        with request.urlopen(req, timeout=10) as response:
            if response.status >= 400:
                raise RuntimeError(f"EBO command {suffix} returned HTTP {response.status}")

    def robot_state(self) -> dict[str, object]:
        req = request.Request(
            self.config.ebo_api_url + "/api/robots",
            headers={"X-Enabot-Token": self.config.ebo_api_token},
        )
        with request.urlopen(req, timeout=5) as response:
            robots = json.load(response)
        for robot in robots:
            if robot.get("node") == self.config.ebo_node:
                return dict(robot.get("state") or {})
        raise ValueError("Robot missing from Engine")

    def ensure_floor_mode(self, state: dict[str, object]) -> bool:
        """机器人始终放在地上：桌面模式开着就关掉。返回是否发送了关闭命令。"""
        if not _flag(state.get("desktop_mode")):
            return False
        # 103023 必须整包发送；其余字段按机器人当前值原样回填，只改桌面模式
        data = {
            "status": 0,
            "pickUpCheck": _flag(state.get("pickup_check")),
            "autoDesktopMode": False,
            "avoidobstacle": _flag(state.get("avoid_obstacle")),
            "steeringSensitivity": STEERING_LEVELS.get(str(state.get("steering")), 0),
            "abnormalExerciseReminder": _flag(state.get("abnormal_reminder")),
        }
        self.command("cmd", json.dumps({"id": 103023, "data": data}))
        LOG.warning("Robot desk mode was on; turned it off (robot lives on the floor)")
        return True

    def set_night_vision_auto(self) -> None:
        """夜视设成 Auto（暗了自动切夜视）。机器人上报的 night_vision 一直是 Day、从不刷新，
        不能按上报值判断，所以由调用方定期发送。"""
        self.command("night_vision/set", "Auto")
        LOG.info("Robot night vision set to Auto")

    def wake_if_due(self) -> None:
        snapshot = self.state.snapshot()
        # A video failure is never a reason to rejoin a working microphone.
        if snapshot.get('source_audio_ok') is True:
            return
        # A failed reader is not proof the robot is asleep. Do not interrupt a
        # healthy source, or override an intentional camera/privacy setting.
        if snapshot.get('engine_audio_monitor_enabled') and snapshot.get('source_audio_status') not in {
            'no_source_packets', 'no_decoded_pcm', 'disconnected', 'source_stale'
        }:
            return
        if not self.config.auto_wake or time.monotonic() - self._last_wake < 60:
            return
        self._last_wake = time.monotonic()
        self.state.update(
            media_recovery_attempts=int(snapshot["media_recovery_attempts"]) + 1,
            last_media_recovery_at=time.time(),
        )
        try:
            LOG.info("Microphone source unavailable; asking EBO to wake")
            self.command("wake")
        except Exception as exc:  # noqa: BLE001
            self.state.update(last_error=f"auto-wake failed: {exc}")
            LOG.warning("auto-wake failed: %s", exc)


class AudioStore:
    def __init__(self, directory: Path, state: RuntimeState) -> None:
        self.directory = directory
        self.state = state
        self.directory.mkdir(parents=True, exist_ok=True)
        self.state.update(
            output_audio_dir=str(self.directory),
            output_audio_files_persisted=len(
                list(self.directory.rglob("reply-*.wav"))
            ),
        )

    @staticmethod
    def _safe_id(output_id: str) -> str:
        safe = "".join(
            character
            for character in output_id
            if character.isascii() and (character.isalnum() or character in "-_")
        )
        return safe[:96] or uuid.uuid4().hex

    def filename_for(self, output_id: str) -> str:
        return f"reply-{self._safe_id(output_id)}.wav"

    def write_pcm24_wav(self, pcm: bytes, output_id: str = "") -> tuple[str, float]:
        name = self.filename_for(output_id)
        path = self.directory / name
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24000)
            output.writeframes(pcm)
        duration = len(pcm) / (24000 * 2)
        self._prune()
        return name, duration

    def _prune(self) -> None:
        """24 小时运行时回复录音会一直累积；只保留最近 REPLY_RETENTION_DAYS 天。"""
        cutoff = time.time() - REPLY_RETENTION_DAYS * 86400
        kept = 0
        for wav in self.directory.glob("reply-*.wav"):
            try:
                if wav.stat().st_mtime < cutoff:
                    wav.unlink()
                    wav.with_suffix(".txt").unlink(missing_ok=True)
                else:
                    kept += 1
            except OSError:
                kept += 1
        self.state.update(output_audio_files_persisted=kept)


class TranscriptStore:
    """Append final user speech transcripts to durable, host-mounted JSONL."""

    def __init__(self, path: Path, state: RuntimeState, source: str = "realtime") -> None:
        self.path = path
        self.state = state
        if source not in {"realtime", "gate", "live"}:
            raise ValueError("transcript source must be realtime, gate or live")
        self.source = source
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self.state.update(**{
            "live_transcript_path" if source == "live" else "transcript_path": str(self.path)
        })

    def append(
        self,
        item_id: str,
        transcript: str,
        languages: object = None,
        *,
        session_started_at: float | None = None,
        user_id: str | None = None,
    ) -> None:
        snapshot = self.state.snapshot()
        now = time.time()
        record = {
            "received_at": now,
            "received_at_local": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "session_started_at": snapshot["realtime_session_started_at"] if session_started_at is None else session_started_at,
            "item_id": item_id,
            "transcript": transcript,
            "source": self.source,
            "user_id": snapshot.get("active_user") if session_started_at is None else user_id,
            "languages": languages if isinstance(languages, list) else [],
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as output:
                output.write(line)
        if self.source != "live":
            self.state.update(
                last_user_transcript_at=now,
                user_transcripts_received=int(snapshot["user_transcripts_received"]) + 1,
            )
        else:
            self.state.update(
                last_live_user_transcript_at=now,
                live_user_transcripts_received=int(snapshot["live_user_transcripts_received"]) + 1,
            )
        if transcript.strip():
            print(json.dumps({
                "event": "conversation.user.live_transcript" if self.source == "live" else "conversation.user.transcript",
                "received_at": now, "session_started_at": record["session_started_at"],
                "item_id": item_id, "transcript": transcript,
                "source": self.source, "user_id": record["user_id"],
            }, ensure_ascii=False, separators=(",", ":")), flush=True)


class AssistantOutputStore:
    """Persist final assistant transcripts beside their matching model-output WAV."""

    def __init__(self, path: Path, audio_store: AudioStore, state: RuntimeState) -> None:
        self.path = path
        self.audio_store = audio_store
        self.state = state
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        with self.path.open("r", encoding="utf-8") as existing:
            persisted = sum(1 for line in existing if line.strip())
        self.state.update(
            assistant_transcript_path=str(self.path),
            assistant_outputs_persisted=persisted,
            last_assistant_output_at=(
                self.path.stat().st_mtime if persisted else 0.0
            ),
        )

    def append(
        self,
        output_id: str,
        response_id: str,
        item_id: str,
        transcript: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        snapshot = self.state.snapshot()
        now = time.time()
        audio_name = self.audio_store.filename_for(output_id)
        text_path = (self.audio_store.directory / audio_name).with_suffix(".txt")
        metadata = metadata or {}
        record = {
            "received_at": now,
            "received_at_local": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "session_started_at": snapshot["realtime_session_started_at"],
            "response_id": response_id,
            "user_id": snapshot.get("active_user"),
            "item_id": item_id,
            "transcript": transcript,
            "audio_file": str(self.audio_store.directory / audio_name),
            "text_file": str(text_path),
            "streamed": bool(metadata.get("streamed", False)),
            "interrupted": bool(metadata.get("interrupted", False)),
            "generated_ms": int(metadata.get("generated_ms", 0) or 0),
            "played_ms": int(metadata.get("played_ms", 0) or 0),
            "stream_id": str(metadata.get("stream_id", "") or ""),
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            text_path.write_text(transcript + "\n", encoding="utf-8")
            with self.path.open("a", encoding="utf-8") as output:
                output.write(line)
        self.state.update(
            last_assistant_output_at=now,
            assistant_outputs_persisted=int(snapshot["assistant_outputs_persisted"]) + 1,
        )
        print(json.dumps({"event": "conversation.assistant.output", **record},
            ensure_ascii=False, separators=(",", ":")), flush=True)

    def update_stream_metadata(
        self, output_id: str, metadata: dict[str, object]
    ) -> bool:
        """Update an already-persisted response when playback is interrupted later."""
        changed = False
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            rewritten: list[str] = []
            for line in lines:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    rewritten.append(line)
                    continue
                audio_name = Path(str(record.get("audio_file", ""))).name
                matches = (
                    record.get("response_id") == output_id
                    or audio_name == self.audio_store.filename_for(output_id)
                )
                if matches:
                    record.update(
                        {
                            "streamed": bool(metadata.get("streamed", False)),
                            "interrupted": bool(metadata.get("interrupted", False)),
                            "generated_ms": int(
                                metadata.get("generated_ms", 0) or 0
                            ),
                            "played_ms": int(metadata.get("played_ms", 0) or 0),
                            "stream_id": str(metadata.get("stream_id", "") or ""),
                        }
                    )
                    changed = True
                rewritten.append(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                )
            if changed:
                temporary = self.path.with_suffix(self.path.suffix + ".tmp")
                temporary.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
                temporary.replace(self.path)
        return changed


@dataclass
class _SpeakerStream:
    stream_id: str
    output_id: str
    pending: bytearray = field(default_factory=bytearray)
    generated_pcm: bytearray = field(default_factory=bytearray)
    chunks: queue.Queue[bytes | None] = field(default_factory=queue.Queue)
    stop_requested: threading.Event = field(default_factory=threading.Event)
    stop_sent: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    worker: threading.Thread | None = None
    socket: object = None
    ready: bool = False
    failed: str = ""
    played_ms: int = 0
    generated_ms: int = 0
    interrupted: bool = False
    generation: int = 0


class Speaker:
    """Stream model PCM immediately while retaining WAV URL playback as a fallback."""

    def __init__(self, config: Config, ebo: EboAPI, store: AudioStore, state: RuntimeState) -> None:
        self.config = config
        self.ebo = ebo
        self.store = store
        self.state = state
        self._mute_lock = threading.Lock()
        self._mute_until = 0.0
        self._stream_active = False
        self._stream_lock = threading.RLock()
        self._enabled = True
        self._generation = 0
        self._stream: _SpeakerStream | None = None
        self._prebuffer_bytes = config.stream_prebuffer_ms * 24000 * 2 // 1000
        self._recent_metrics: dict[str, dict[str, object]] = {}

    def input_muted(self) -> bool:
        with self._mute_lock:
            return self._stream_active or time.monotonic() < self._mute_until

    def suspend(self):
        with self._stream_lock:
            self._enabled = False
            self._generation += 1
            if self._stream:
                self._stream.interrupted = True
                self._stream.stop_requested.set()
            # Stop only assistant-owned URL playback. An idle pause must not
            # send a global talk command into a family's separate call.
            if self.state.snapshot()['speaker_stream_status'] in {'fallback', 'url'} and self.input_muted():
                self._stop_url_playback()
            self._stream = None
            with self._mute_lock:
                self._stream_active = False
                self._mute_until = time.monotonic()+self.config.playback_tail_ms/1000
            self.state.update(speaker_stream_status='paused')

    def resume(self):
        with self._stream_lock:
            self._enabled = True

    def _new_stream(self, output_id: str) -> _SpeakerStream:
        stream = _SpeakerStream(
            stream_id=f"stream_{uuid.uuid4().hex}",
            output_id=output_id or uuid.uuid4().hex,
            generation=self._generation,
        )
        self._stream = stream
        with self._mute_lock:
            self._stream_active = True
        self.state.update(
            speaker_stream_status="prebuffering",
            speaker_stream_id=stream.stream_id,
            speaker_stream_played_ms=0,
        )
        return stream

    def _start_worker(self, stream: _SpeakerStream) -> None:
        if stream.worker is not None:
            return
        stream.worker = threading.Thread(
            target=self._stream_worker,
            args=(stream,),
            name=f"speaker-{stream.stream_id[-8:]}",
            daemon=True,
        )
        stream.worker.start()

    def stream_delta(self, pcm: bytes, output_id: str = "") -> None:
        if not pcm:
            return
        start = False
        queued = b""
        with self._stream_lock:
            if not self._enabled:
                return
            stream = self._stream
            if stream is None or stream.output_id != output_id:
                if stream is not None:
                    stream.stop_requested.set()
                stream = self._new_stream(output_id)
            stream.generated_pcm.extend(pcm)
            stream.generated_ms = round(
                len(stream.generated_pcm) / (24000 * 2) * 1000
            )
            if stream.worker is None:
                stream.pending.extend(pcm)
                if len(stream.pending) >= self._prebuffer_bytes:
                    queued = bytes(stream.pending)
                    stream.pending.clear()
                    start = True
            else:
                queued = pcm
        if start:
            self._start_worker(stream)
        if queued:
            stream.chunks.put(queued)

    def _handle_stream_status(self, stream: _SpeakerStream, raw: str) -> str:
        try:
            status = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return ""
        kind = str(status.get("type", ""))
        try:
            stream.played_ms = max(stream.played_ms, int(status.get("played_ms", 0)))
        except (TypeError, ValueError):
            pass
        self.state.update(
            speaker_stream_status=kind or "streaming",
            speaker_stream_id=stream.stream_id,
            speaker_stream_played_ms=stream.played_ms,
        )
        return kind

    def _stream_worker(self, stream: _SpeakerStream) -> None:
        ws = None
        try:
            if not self.config.talk_stream_url:
                raise RuntimeError("EBO_TALK_STREAM_URL is empty")
            self.state.update(speaker_stream_status="connecting")
            ws = websocket.create_connection(
                self.config.talk_stream_url,
                timeout=self.config.stream_connect_timeout_seconds,
                http_proxy_host=None,
                http_proxy_port=None,
            )
            stream.socket = ws
            ws.send(
                json.dumps(
                    {
                        "type": "start",
                        "token": self.config.ebo_api_token,
                        "node": self.config.ebo_node,
                        "stream_id": stream.stream_id,
                        "rate": 24000,
                        "channels": 1,
                        "format": "pcm16",
                    },
                    separators=(",", ":"),
                )
            )
            ready = ws.recv()
            if self._handle_stream_status(stream, ready) != "ready":
                raise RuntimeError(f"stream did not become ready: {ready}")
            stream.ready = True
            self.state.update(last_reply_at=time.time())
            ws.settimeout(0.02)
            ending = False
            while not ending:
                if stream.stop_requested.is_set():
                    if not stream.stop_sent.is_set():
                        ws.send(json.dumps({"type": "stop"}))
                        stream.stop_sent.set()
                    ending = True
                else:
                    try:
                        chunk = stream.chunks.get(timeout=0.02)
                    except queue.Empty:
                        chunk = b""
                    if chunk is None:
                        ws.send(json.dumps({"type": "end"}))
                        ending = True
                    elif chunk:
                        ws.send_binary(chunk)
                try:
                    if self._handle_stream_status(stream, ws.recv()) in {"done", "stopped"}:
                        return
                except websocket.WebSocketTimeoutException:
                    pass
            deadline = time.monotonic() + max(30, stream.generated_ms / 1000 + 10)
            while time.monotonic() < deadline:
                if stream.stop_requested.is_set() and not stream.stop_sent.is_set():
                    ws.send(json.dumps({"type": "stop"}))
                    stream.stop_sent.set()
                try:
                    kind = self._handle_stream_status(stream, ws.recv())
                except websocket.WebSocketTimeoutException:
                    continue
                if kind in {"done", "stopped"}:
                    break
            else:
                raise TimeoutError("timed out waiting for stream completion")
        except Exception as exc:  # noqa: BLE001
            stream.failed = str(exc)
            snapshot = self.state.snapshot()
            self.state.update(
                speaker_stream_status="failed",
                speaker_stream_failures=int(snapshot["speaker_stream_failures"]) + 1,
                last_error=f"speaker stream failed: {exc}",
            )
            LOG.warning("speaker stream failed (%s): %s", stream.stream_id, exc)
        finally:
            if ws:
                try:
                    ws.close()
                except Exception:
                    pass
            stream.finished.set()

    def finish_stream(self, pcm: bytes, output_id: str = "") -> None:
        if not pcm:
            self.abort_stream()
            return
        with self._stream_lock:
            if not self._enabled:
                return
            generation = self._generation
        try:
            name, duration = self.store.write_pcm24_wav(pcm, output_id)
        except Exception as exc:  # noqa: BLE001
            self.state.update(last_error=f"audio persistence failed: {exc}")
            LOG.exception("audio persistence failed")
            return
        with self._stream_lock:
            if not self._enabled or generation != self._generation:
                return
            stream = self._stream
            if stream is None or stream.output_id != output_id:
                stream = self._new_stream(output_id)
                stream.pending.extend(pcm)
                stream.generated_pcm.extend(pcm)
            stream.generated_ms = round(len(pcm) / (24000 * 2) * 1000)
            if stream.worker is None:
                queued = bytes(stream.pending)
                stream.pending.clear()
                self._start_worker(stream)
                if queued:
                    stream.chunks.put(queued)
            stream.chunks.put(None)
        threading.Thread(
            target=self._finalize_stream,
            args=(stream, name, duration),
            name=f"speaker-finalize-{stream.stream_id[-8:]}",
            daemon=True,
        ).start()

    def _finalize_stream(self, stream: _SpeakerStream, name: str, duration: float) -> None:
        timeout = duration + self.config.stream_connect_timeout_seconds + 35
        stream.finished.wait(timeout)
        if not stream.finished.is_set():
            stream.failed = "stream worker did not finish"
            stream.stop_requested.set()
        if stream.interrupted:
            pass
        elif stream.failed:
            try:
                self._play_url(name, duration, fallback=True, generation=stream.generation)
            except Exception as exc:  # noqa: BLE001
                self.state.update(last_error=f"speaker fallback failed: {exc}")
                LOG.exception("speaker WAV fallback failed")
        else:
            self.state.update(
                speaker_stream_status="done",
                speaker_stream_played_ms=stream.played_ms,
                last_reply_at=time.time(),
            )
            LOG.info(
                "streamed %.1fs reply to EBO speaker (%d ms played)",
                duration,
                stream.played_ms,
            )
        self._recent_metrics[stream.output_id] = {
            "streamed": not bool(stream.failed),
            "interrupted": stream.interrupted,
            "generated_ms": stream.generated_ms,
            "played_ms": stream.played_ms,
            "stream_id": stream.stream_id,
        }
        self._trim_metrics()
        with self._stream_lock:
            if self._stream is stream:
                self._stream = None
                with self._mute_lock:
                    self._stream_active = False
                    self._mute_until = max(self._mute_until, time.monotonic()+self.config.playback_tail_ms/1000)

    def abort_stream(self) -> None:
        with self._stream_lock:
            stream = self._stream
        if stream:
            stream.stop_requested.set()
            threading.Thread(
                target=self._cleanup_aborted_stream,
                args=(stream,),
                daemon=True,
            ).start()

    def _cleanup_aborted_stream(self, stream: _SpeakerStream) -> None:
        stream.finished.wait(0.75)
        with self._stream_lock:
            if self._stream is stream:
                self._stream = None
                with self._mute_lock:
                    self._stream_active = False
                    self._mute_until = max(self._mute_until, time.monotonic()+self.config.playback_tail_ms/1000)

    def echo_reference(self) -> tuple[bytes, int]:
        with self._stream_lock:
            stream = self._stream
            if stream is None:
                return b"", 0
            return bytes(stream.generated_pcm), stream.played_ms

    def output_metrics(self, output_id: str) -> dict[str, object]:
        with self._stream_lock:
            stream = self._stream
            if stream is not None and stream.output_id == output_id:
                return {
                    "streamed": stream.ready and not bool(stream.failed),
                    "interrupted": False,
                    "generated_ms": stream.generated_ms,
                    "played_ms": stream.played_ms,
                    "stream_id": stream.stream_id,
                }
        return dict(self._recent_metrics.get(output_id, {}))

    def _trim_metrics(self) -> None:
        # 只用于给刚结束的回复补指标；24 小时运行时不能无限增长
        while len(self._recent_metrics) > RECENT_METRICS_LIMIT:
            self._recent_metrics.pop(next(iter(self._recent_metrics)))

    def active_output_id(self) -> str:
        with self._stream_lock:
            return self._stream.output_id if self._stream is not None else ""

    def interrupt(self, pcm: bytes, output_id: str) -> dict[str, object]:
        """Stop audible output now and persist the generated prefix for diagnostics."""
        with self._stream_lock:
            stream = self._stream
        generated_ms = round(len(pcm) / (24000 * 2) * 1000)
        stream_id = ""
        played_ms = 0
        if stream is not None:
            stream.interrupted = True
            stream_id = stream.stream_id
            output_id = stream.output_id
            if not pcm:
                pcm = bytes(stream.generated_pcm)
                generated_ms = round(len(pcm) / (24000 * 2) * 1000)
            stream.generated_ms = max(stream.generated_ms, generated_ms)
            stream.stop_requested.set()
            ws = stream.socket
            if ws is not None and not stream.stop_sent.is_set():
                try:
                    ws.send(json.dumps({"type": "stop"}))
                    stream.stop_sent.set()
                except Exception:
                    pass
            stream.finished.wait(0.25)
            played_ms = min(stream.played_ms, generated_ms)
        # Wait for the URL stop before allowing a new utterance to start, so a
        # delayed command cannot stop the next reply. The caller runs in a thread.
        self._stop_url_playback()
        if pcm:
            try:
                self.store.write_pcm24_wav(pcm, output_id)
            except OSError as exc:
                self.state.update(last_error=f"interrupted audio persistence failed: {exc}")
        metrics = {
            "output_id": output_id,
            "streamed": bool(stream and stream.ready),
            "interrupted": True,
            "generated_ms": generated_ms,
            "played_ms": played_ms,
            "stream_id": stream_id,
        }
        self._recent_metrics[output_id] = metrics
        self._trim_metrics()
        self.state.update(
            speaker_stream_status="interrupted",
            speaker_stream_played_ms=played_ms,
        )
        with self._stream_lock:
            if self._stream is stream:
                self._stream = None
                with self._mute_lock:
                    self._stream_active = False
                    self._mute_until = max(self._mute_until, time.monotonic()+self.config.playback_tail_ms/1000)
        return metrics

    def _stop_url_playback(self) -> None:
        try:
            self.ebo.command("talk/stop")
        except Exception as exc:  # noqa: BLE001
            LOG.warning("talk/stop fallback failed during barge-in: %s", exc)

    def _play_url(self, name: str, duration: float, fallback: bool = False, generation=None) -> None:
        with self._stream_lock:
            if not self._enabled or (generation is not None and generation != self._generation):
                return
            self._play_url_enabled(name, duration, fallback)

    def _play_url_enabled(self, name, duration, fallback):
        with self._mute_lock:
            self._mute_until = time.monotonic() + duration + max(1.5, self.config.playback_tail_ms/1000)
        url = f"{self.config.public_audio_base_url}/{name}"
        self.ebo.command("talk", url)
        snapshot = self.state.snapshot()
        values = {
            "last_reply_at": time.time(),
            "speaker_stream_status": "fallback" if fallback else "url",
        }
        if fallback:
            values["speaker_stream_fallbacks"] = int(
                snapshot["speaker_stream_fallbacks"]
            ) + 1
        self.state.update(**values)
        LOG.info("sent %.1fs reply to EBO speaker by WAV URL", duration)

    def play(self, pcm: bytes, output_id: str = "") -> None:
        if not pcm:
            return
        with self._stream_lock:
            if not self._enabled:
                return
            generation = self._generation
        try:
            name, duration = self.store.write_pcm24_wav(pcm, output_id)
            self._play_url(name, duration, generation=generation)
        except Exception as exc:  # noqa: BLE001
            self.state.update(last_error=f"speaker failed: {exc}")
            LOG.exception("speaker playback failed")
