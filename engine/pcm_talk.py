"""Token-guarded PCM WebSocket transport for low-latency EBO talkback."""

from __future__ import annotations

import audioop
import hmac
import json
import queue
import threading
import time

from websockets.sync.server import serve


INPUT_RATE = 24000
OUTPUT_RATE = 8000
FRAME_MS = 20
FRAME_BYTES = OUTPUT_RATE * FRAME_MS // 1000 * 2


class PcmStream:
    """One 24 kHz PCM16 input stream converted into paced 8 kHz Agora frames."""

    def __init__(self, stream_id: str):
        self.stream_id = stream_id
        self.frames: queue.Queue[bytes] = queue.Queue(maxsize=500)
        self.input_done = threading.Event()
        self.done = threading.Event()
        self.stopped = threading.Event()
        self.stop_reason = ""
        self.played_frames = 0
        self._rate_state = None
        self._partial = bytearray()
        self._lock = threading.Lock()

    @property
    def played_ms(self) -> int:
        with self._lock:
            return self.played_frames * FRAME_MS

    def push(self, pcm24: bytes) -> None:
        if self.input_done.is_set() or self.stopped.is_set() or not pcm24:
            return
        converted, self._rate_state = audioop.ratecv(
            pcm24, 2, 1, INPUT_RATE, OUTPUT_RATE, self._rate_state
        )
        self._partial.extend(converted)
        while len(self._partial) >= FRAME_BYTES:
            frame = bytes(self._partial[:FRAME_BYTES])
            del self._partial[:FRAME_BYTES]
            self.frames.put(frame, timeout=2)

    def finish_input(self) -> None:
        if self.input_done.is_set():
            return
        if self._partial:
            frame = bytes(self._partial).ljust(FRAME_BYTES, b"\0")
            self._partial.clear()
            self.frames.put(frame, timeout=2)
        self.input_done.set()

    def pop_frame(self) -> bytes | None:
        if self.stopped.is_set():
            return None
        try:
            return self.frames.get_nowait()
        except queue.Empty:
            return None

    def mark_played(self) -> None:
        with self._lock:
            self.played_frames += 1

    def drained(self) -> bool:
        return self.input_done.is_set() and self.frames.empty()

    def complete(self) -> None:
        self.done.set()

    def stop(self, reason: str = "client") -> None:
        self.stop_reason = reason
        self.stopped.set()
        self.input_done.set()
        with self.frames.mutex:
            self.frames.queue.clear()


class PcmTalkServer:
    """Small internal-only WebSocket server; Docker networking controls reachability."""

    def __init__(self, host, port, token, node, on_activate, on_release, can_listen=None):
        self.host = host
        self.port = int(port)
        self.token = token
        self.node = node
        self.on_activate = on_activate
        self.on_release = on_release
        self.can_listen = can_listen or (lambda: True)
        self._active: PcmStream | None = None
        self._lock = threading.Lock()
        self._server = None
        self._listeners = {}
        self._stopping = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, name="pcm-talk-ws", daemon=True).start()

    def _run(self) -> None:
        with serve(self._handle, self.host, self.port, close_timeout=1) as server:
            self._server = server
            server.serve_forever()

    def stop(self) -> None:
        self._stopping.set()
        with self._lock:
            active = self._active
            listeners = list(self._listeners)
        for websocket in listeners:
            websocket.close(code=1001, reason="server shutdown")
        if active:
            active.stop("server_shutdown")
        if self._server:
            self._server.shutdown()

    @staticmethod
    def _send(websocket, kind: str, stream: PcmStream, **extra) -> None:
        payload = {
            "type": kind,
            "stream_id": stream.stream_id,
            "played_ms": stream.played_ms,
        }
        payload.update(extra)
        websocket.send(json.dumps(payload, separators=(",", ":")))

    def _authenticate(self, first) -> tuple[bool, dict]:
        if not isinstance(first, str):
            return False, {}
        try:
            message = json.loads(first)
        except (TypeError, json.JSONDecodeError):
            return False, {}
        if not isinstance(message, dict):
            return False, {}
        supplied = str(message.get("token", ""))
        valid = bool(self.token) and hmac.compare_digest(supplied, self.token)
        valid = valid and message.get("type") == "start"
        valid = valid and str(message.get("node", self.node)) == self.node
        valid = valid and message.get("rate") == INPUT_RATE
        valid = valid and message.get("channels") == 1
        valid = valid and str(message.get("format", "")) == "pcm16"
        return valid, message

    def broadcast_audio(self, pcm: bytes) -> None:
        """Fan out real microphone PCM without blocking the Agora callback.

        A slow reader loses old audio; it never stalls another reader or video.
        """
        if not pcm or len(pcm) % 2:
            return
        item = (time.monotonic(), bytes(pcm))
        with self._lock:
            listeners = list(self._listeners.values())
        for pending in listeners:
            try:
                pending.put_nowait(item)
            except queue.Full:
                try:
                    pending.get_nowait()
                except queue.Empty:
                    pass
                try:
                    pending.put_nowait(item)
                except queue.Full:
                    pass

    def _listen(self, websocket) -> None:
        pending = queue.Queue(maxsize=20)
        try:
            valid, _ = self._authenticate(websocket.recv(timeout=10))
            if not valid:
                websocket.close(code=1008, reason="unauthorized")
                return
            with self._lock:
                self._listeners[websocket] = pending
            websocket.send(json.dumps({"type": "ready", "rate": INPUT_RATE,
                                       "channels": 1, "format": "pcm16"}))
            rate_state = None
            while not self._stopping.is_set():
                # Detect closed/idle clients even while the robot mic is muted.
                try:
                    control = websocket.recv(timeout=0)
                    if control is not None:
                        break
                except TimeoutError:
                    pass
                try:
                    stamp, pcm = pending.get(timeout=.5)
                except queue.Empty:
                    continue
                if not self.can_listen() or time.monotonic() - stamp > .2:
                    rate_state = None
                    continue
                converted, rate_state = audioop.ratecv(pcm, 2, 1, OUTPUT_RATE, INPUT_RATE, rate_state)
                websocket.send(converted)
        finally:
            with self._lock:
                self._listeners.pop(websocket, None)

    def _handle(self, websocket) -> None:
        stream = None
        try:
            request = getattr(websocket, "request", None)
            if request is not None and getattr(request, "path", "") == "/listen":
                self._listen(websocket)
                return
            if request is not None and getattr(request, "path", "") != "/talk":
                websocket.close(code=1008, reason="unknown endpoint")
                return
            first = websocket.recv(timeout=10)
            valid, message = self._authenticate(first)
            if not valid:
                websocket.send(json.dumps({"type": "error", "code": "unauthorized"}))
                websocket.close(code=1008, reason="unauthorized")
                return
            stream_id = str(message.get("stream_id", ""))[:96]
            if not stream_id:
                websocket.send(json.dumps({"type": "error", "code": "missing_stream_id"}))
                websocket.close(code=1008, reason="missing stream id")
                return
            stream = PcmStream(stream_id)
            with self._lock:
                previous = self._active
                self._active = stream
            if previous:
                previous.stop("replaced")
            self.on_activate(stream)
            self._send(websocket, "ready", stream, output_rate=OUTPUT_RATE, frame_ms=FRAME_MS)
            last_progress = 0
            while not stream.done.is_set() and not stream.stopped.is_set():
                try:
                    incoming = websocket.recv(timeout=0.05)
                except TimeoutError:
                    incoming = None
                if isinstance(incoming, bytes):
                    stream.push(incoming)
                elif isinstance(incoming, str):
                    try:
                        control = json.loads(incoming)
                    except json.JSONDecodeError:
                        control = {}
                    if control.get("type") == "end":
                        stream.finish_input()
                    elif control.get("type") == "stop":
                        stream.stop("client")
                played_ms = stream.played_ms
                if played_ms - last_progress >= 100:
                    self._send(websocket, "progress", stream)
                    last_progress = played_ms
            if stream.stopped.is_set():
                self._send(websocket, "stopped", stream, reason=stream.stop_reason)
            else:
                self._send(websocket, "done", stream)
        except Exception as exc:
            if stream:
                stream.stop("connection_lost")
            try:
                websocket.send(json.dumps({"type": "error", "message": str(exc)}))
            except Exception:
                pass
        finally:
            if stream:
                with self._lock:
                    if self._active is stream:
                        self._active = None
                self.on_release(stream)
