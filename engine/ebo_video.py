"""
ebo_video.py — receive the robot's Agora video as DECODED YUV (the SDK decodes H.265),
re-encode to H.264 and republish as RTSP so Home Assistant can show it as a camera.

Pipeline:  Agora video-frame observer (I420 YUV)  ->  ffmpeg (libx264)  ->  RTSP (mediamtx)

The SDK's *encoded* frame path segfaults for H.265, but it CAN decode H.265 to raw YUV via
the decoded video-frame observer — that's what we use here. The RTSP stream is served at
rtsp://<add-on host>:8554/ebo.
"""
import os
import select
import subprocess
import tempfile
import threading
import time

from agora.rtc.video_frame_observer import IVideoFrameObserver

from ebo_log import log


def _pack_plane(buf, stride, width, height):
    """Return a tightly-packed plane (strip any stride padding)."""
    b = bytes(buf)
    if stride == width:
        return b[:width * height]
    out = bytearray(width * height)
    for row in range(height):
        src = row * stride
        out[row * width:(row + 1) * width] = b[src:src + width]
    return bytes(out)


class VideoPipeline(IVideoFrameObserver):
    def __init__(self, rtsp_port=8554, path="ebo", fps=None):
        super().__init__()
        self.rtsp_port = rtsp_port
        self.rtsp_url = f"rtsp://127.0.0.1:{rtsp_port}/{path}"
        # Pace raw inputs in real time. Frame-count timestamps only work if the
        # number of frames written actually matches the declared input rate.
        self.fps = max(1, min(30, int(fps or os.environ.get("EBO_VIDEO_FPS", "20") or "20")))
        # bitrate cap in kbps (VBV) so busy scenes can't spike bandwidth/CPU (0 = uncapped)
        self.bitrate = int(os.environ.get("EBO_VIDEO_BITRATE", "2500") or "0")
        # downscale to cut CPU on the re-encode (0 = keep the robot's native resolution)
        self.max_h = int(os.environ.get("EBO_VIDEO_MAX_HEIGHT", "720") or "0")
        self.preset = os.environ.get("EBO_VIDEO_PRESET", "ultrafast")
        # Optional mic PCM, muxed as Opus for RTSP / WebRTC.
        self.audio = os.environ.get("EBO_AUDIO", "0") == "1"
        # robot mic is 8 kHz mono (measured on the real app); must match the SDK PCM rate
        self.audio_rate = int(os.environ.get("EBO_AUDIO_RATE", "8000"))
        self._a_w = None              # write end of the audio pipe to ffmpeg
        self._audio_lock = threading.Lock()
        self._audio_pending = bytearray()
        self._last_audio = 0.0        # last time real PCM arrived
        self.ff = None
        self.w = 0
        self.h = 0
        self.frames = 0
        self._last_frame = 0.0        # wall-clock of the last decoded frame (liveness: robot awake?)
        # RTC callbacks only replace a single latest-frame slot. A clocked worker
        # feeds FFmpeg independently, holding the previous frame across gaps so
        # a missing video packet cannot stop otherwise healthy audio transport.
        # _last_frame always describes an actual decoded source frame, not a hold.
        self._pending = None          # (y,u,v,w,h) latest frame awaiting encode; overwrite=drop
        self._pending_evt = threading.Event()
        self._writer = None
        self._stopping = threading.Event()
        self._last_item = None
        self._last_output = 0.0
        self.encoder_restarts = 0
        self.server_restarts = 0
        self.remote_stats = {}
        self._restart_after = 0.0
        self._dropped = 0
        self._src_count = 0           # source/encoded frame counters for the fps diagnostic
        self._enc_count = 0
        self._src_t0 = 0.0
        self.feeding = False          # only pipe to ffmpeg while the camera switch is on
        self.lock = threading.Lock()
        self._start_mediamtx()

    # ---- RTSP server ----
    def _start_mediamtx(self):
        # per-instance temp file: a fixed /tmp path would (a) be a predictable-path smell and
        # (b) COLLIDE when the add-on runs one bridge per robot (multi-robot). Key it by port.
        cfg = os.path.join(tempfile.gettempdir(), "ebo_mediamtx_%d.yml" % self.rtsp_port)
        # Low-Latency HLS for a FLUID browser preview (the snapshot path is choppy). HTTP-based, so
        # it works straight through the add-on's mapped port — no WebRTC/ICE finickiness. ~0.5-1s.
        self.hls_port = 8888 + (self.rtsp_port - 8554)
        # WebRTC (WHEP): the panel's fullscreen 'drive' view plays this for a TRULY fluid, ~200 ms
        # preview (much better than HLS for actually driving). The robot's H.265 is already
        # re-encoded to H.264 here, which the browser CAN decode over WebRTC. ICE candidates use the
        # host's real LAN IPs (below) so the browser reaches us even across NIC/VLAN boundaries.
        self.webrtc_port = 8189 + (self.rtsp_port - 8554)
        ice_hosts = [ip.strip() for ip in
                     os.environ.get("EBO_HOST_IPS", "").split(",") if ip.strip()]
        # de-dup, keep order; a YAML flow list "[a, b]" (empty -> mediamtx auto-detects interfaces)
        seen, hosts = set(), []
        for ip in ice_hosts:
            if ip not in seen:
                seen.add(ip)
                hosts.append(ip)
        additional_hosts = ("[" + ", ".join(hosts) + "]") if hosts else "[]"
        with open(cfg, "w") as f:
            f.write("logLevel: info\n"
                    f"rtspAddress: :{self.rtsp_port}\n"
                    # Each robot owns a server; default UDP / SRT ports otherwise
                    # collide and make the second instance exit immediately.
                    f"rtpAddress: :{8000 + 2 * (self.rtsp_port - 8554)}\n"
                    f"rtcpAddress: :{8001 + 2 * (self.rtsp_port - 8554)}\n"
                    "srt: no\n"
                    "hls: yes\n"
                    f"hlsAddress: :{self.hls_port}\n"
                    "hlsVariant: lowLatency\n"
                    # only mux HLS when a client actually asks for it (fallback). Always-remux would
                    # burn CPU generating HLS even while everyone is on the fluid WebRTC path.
                    "hlsAlwaysRemux: no\n"
                    "hlsSegmentCount: 7\n"
                    "hlsSegmentDuration: 1s\n"
                    "hlsPartDuration: 200ms\n"
                    "hlsAllowOrigin: '*'\n"
                    "webrtc: yes\n"
                    f"webrtcAddress: :{self.webrtc_port}\n"
                    f"webrtcLocalUDPAddress: :{self.webrtc_port}\n"
                    f"webrtcAdditionalHosts: {additional_hosts}\n"
                    "webrtcAllowOrigin: '*'\n"
                    "webrtcICEServers2: []\n"
                    "rtmp: no\n"
                    "paths:\n  all_others:\n")
        log("[video] WebRTC(WHEP) on :%d — ICE hosts %s" % (self.webrtc_port, additional_hosts))
        try:
            self.mediamtx = subprocess.Popen(
                ["/usr/local/bin/mediamtx", cfg],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.server_restarts += 1
            threading.Thread(target=self._server_logs, args=(self.mediamtx,), daemon=True).start()
            time.sleep(1)
            log("[video] mediamtx RTSP server on :%d" % self.rtsp_port)
        except FileNotFoundError:
            log("[video] mediamtx not found — video disabled")
            self.mediamtx = None

    def _server_logs(self, process):
        # Preserve the reason the server closes a publisher, rather than hiding
        # it and leaving consumers with only the resulting RTSP 404.
        for line in process.stdout:
            log("[mediamtx]", line.decode("utf-8", "replace").strip())
        process.stdout.close()

    # ---- ffmpeg: raw I420 in -> H.264 RTSP out ----
    def _start_ffmpeg(self, w, h):
        self._stop_ffmpeg()
        gop = self.fps                  # a keyframe every real second
        scale = []
        if self.max_h and h > self.max_h:
            scale = ["-vf", "scale=-2:%d" % self.max_h]   # keep aspect, even width
            log("[video] starting ffmpeg %dx%d -> ~%dp H.264/RTSP (preset %s)"
                % (w, h, self.max_h, self.preset))
        else:
            log("[video] starting ffmpeg %dx%d -> H.264/RTSP (preset %s)"
                % (w, h, self.preset))
        # optional audio input via a dedicated pipe (fd inherited by ffmpeg)
        audio_in, audio_out, pass_fds = [], ["-an"], ()
        a_r = None
        if self.audio:
            a_r, self._a_w = os.pipe()
            os.set_inheritable(a_r, True)
            # A default pipe holds 64 KB = ~4 s of 8 kHz mono PCM. That reservoir is exactly how the
            # audio ended up seconds behind the video. Shrink it so a backlog simply can't build:
            # when it's full we drop the newest chunk (see write_audio) and stay near real time.
            try:
                import fcntl
                fcntl.fcntl(self._a_w, 1031, 8192)      # F_SETPIPE_SZ = 1031, 8 KB ~= 0.5 s
            except Exception:
                pass
            # Small queue + no buffering: the video path already drops stale frames to bound
            # latency; audio had no such control, so it queued up and arrived seconds late.
            audio_in = ["-thread_queue_size", "64",
                        "-probesize", "32", "-analyzeduration", "0",
                        "-f", "s16le",
                        "-ar", str(self.audio_rate), "-ac", "1", "-i", "pipe:%d" % a_r]
            # Opus, NOT AAC. WebRTC only carries Opus / G.711 / G.722 — with AAC the browser gets
            # no audio track at all in the drive view (the stream had sound, WebRTC just dropped it).
            # Opus also works in mediamtx's fMP4 HLS. 48 kHz mono, low bitrate: the source is an
            # 8 kHz telephony mic, so there is nothing to gain from more.
            audio_out = ["-af", "aresample=async=1:first_pts=0",
                         "-c:a", "libopus", "-ar", "48000", "-ac", "1",
                         "-b:a", "24k", "-application", "lowdelay",
                         "-frame_duration", "10"]
            pass_fds = (a_r,)
        _nullout = os.environ.get("EBO_VIDEO_NULLOUT") == "1"   # DIAG: encode to null (isolate mediamtx)
        self.ff = subprocess.Popen([
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            # The writer supplies a steady cadence, including held frames during
            # source gaps. This keeps video and PCM on the same monotonic clock
            # without bursty wall-clock timestamps or dependence on source fps.
            "-probesize", "32", "-analyzeduration", "0",
            "-f", "rawvideo", "-pixel_format", "yuv420p",
            "-video_size", "%dx%d" % (w, h), "-framerate", str(self.fps),
            "-i", "pipe:0",
        ] + audio_in + scale + [
            "-c:v", "libx264", "-preset", self.preset, "-tune", "zerolatency",
            "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0", "-bf", "0",
            "-pix_fmt", "yuv420p",
        ] + (
            # VBV cap: keep bitrate bounded so motion can't spike bandwidth/CPU
            ["-maxrate", "%dk" % self.bitrate, "-bufsize", "%dk" % (self.bitrate * 2)]
            if self.bitrate > 0 else []
        ) + audio_out + [
            # low muxer delay for latency. NB: do NOT add -flush_packets 1 here — over RTSP/TCP it
            # forces per-packet writes that stall ffmpeg's output and starve the encoder (throughput
            # collapsed to ~5 fps). muxdelay/muxpreload 0 is enough.
            "-muxdelay", "0", "-muxpreload", "0",
        ] + (["-f", "null", "-"] if _nullout else
             ["-f", "rtsp", "-rtsp_transport", "tcp", self.rtsp_url]),
            stdin=subprocess.PIPE, pass_fds=pass_fds, bufsize=0)
        self.encoder_restarts += 1
        os.set_blocking(self.ff.stdin.fileno(), False)
        if _nullout:
            log("[video] DIAG: ffmpeg output = NULL (mediamtx bypassed)")
        if a_r is not None:
            os.close(a_r)             # parent drops the read end (ffmpeg owns it)
            os.set_blocking(self._a_w, False)   # never block the SDK audio thread
            with self._audio_lock:
                self._audio_pending.clear()
            threading.Thread(target=self._audio_loop, args=(self.ff, self._a_w),
                             daemon=True).start()

    def _audio_loop(self, ff, descriptor):
        """Clock PCM at its declared rate; conceal missing samples with silence.

        This is transport padding, never evidence that the robot mic is live.
        The independent AudioHealth observer remains the source of that fact.
        """
        size = int(self.audio_rate * 2 * 0.02)
        deadline = time.monotonic()
        while not self._stopping.is_set():
            with self._audio_lock:
                if self.ff is not ff or self._a_w != descriptor:
                    return
                chunk = bytes(self._audio_pending[:size])
                del self._audio_pending[:size]
                try:
                    os.write(descriptor, chunk.ljust(size, b"\x00"))
                except (BlockingIOError, BrokenPipeError, OSError):
                    pass
            deadline = max(deadline + 0.02, time.monotonic())
            self._stopping.wait(max(0, deadline - time.monotonic()))

    def _stop_ffmpeg(self):
        # Kill before closing stdin: close can otherwise wait on a writer blocked
        # in FFmpeg and deadlock the Agora callback / camera-off command.
        ff, self.ff = self.ff, None
        with self._audio_lock:
            if self._a_w is not None:
                try:
                    os.close(self._a_w)
                except OSError:
                    pass
                self._a_w = None
            self._audio_pending.clear()
        if ff:
            try:
                ff.kill()
                ff.wait(timeout=2)
            except Exception:
                pass
            try:
                if ff.stdin:
                    ff.stdin.close()
            except Exception:
                pass

    def write_audio(self, pcm):
        """Keep at most 200ms of fresh PCM; never block an SDK callback."""
        if not self.feeding:
            return
        with self._audio_lock:
            self._audio_pending.extend(pcm)
            limit = int(self.audio_rate * 2 * 0.2)
            if len(self._audio_pending) > limit:
                del self._audio_pending[:len(self._audio_pending) - limit]
            self._last_audio = time.time()

    def health_snapshot(self, connected=True, enabled=True):
        now = time.time()
        with self.lock:
            age = now - self._last_frame if self._last_frame else None
            status = ("disabled" if not enabled else "disconnected" if not connected
                      else "camera_off" if not self.feeding else "no_source_frames" if age is None
                      else "source_stale" if age >= 20 else "receiving")
            return dict(observed_at=now, status=status, source_video_ok=status == "receiving",
                        last_frame_at=self._last_frame, last_frame_age_seconds=age,
                        source_frames_received=self.frames, output_fps=self.fps,
                        last_output_at=self._last_output, encoder_restarts=self.encoder_restarts,
                        server_restarts=self.server_restarts, remote_stats=dict(self.remote_stats))

    def is_streaming(self, max_age=3.0):
        """True when live frames are actually arriving — i.e. the robot is awake and publishing.
        Used to decide whether turning the camera on needs a fresh RTC re-join to WAKE the robot."""
        return self.feeding and self.frames > 0 and (time.time() - self._last_frame) < max_age

    # ---- camera switch ----
    def start_feed(self):
        with self.lock:
            self.feeding = True
            self._ensure_writer()
        self._pending_evt.set()

    def stop_feed(self):
        with self.lock:
            self.feeding = False
            self._stop_ffmpeg()
            self.w = self.h = 0
            self._pending = self._last_item = None
            self._last_frame = 0.0
            self.frames = 0
        self._pending_evt.set()

    def _ensure_writer(self):
        if self._writer is None or not self._writer.is_alive():
            self._writer = threading.Thread(target=self._writer_loop, daemon=True)
            self._writer.start()

    def _writer_loop(self):
        """Write the newest image on a stable clock; recover stalled publishers."""
        deadline = time.monotonic()
        while not self._stopping.is_set():
            try:
                with self.lock:
                    if self._pending is not None:
                        self._last_item, self._pending = self._pending, None
                    item = self._last_item if self.feeding else None
                if item is None:
                    self._pending_evt.wait(0.5)
                    self._pending_evt.clear()
                    deadline = time.monotonic()
                    continue
                if self._stopping.wait(max(0, deadline - time.monotonic())):
                    return
                with self.lock:
                    if not self.feeding:
                        continue
                    server = getattr(self, "mediamtx", None)
                    if server is None or server.poll() is not None:
                        if time.monotonic() < self._restart_after:
                            self._pending_evt.wait(0.1)
                            continue
                        self._restart_after = time.monotonic() + 5
                        self._stop_ffmpeg()
                        self._start_mediamtx()
                    if self.ff is None or self.ff.poll() is not None or (item[3], item[4]) != (self.w, self.h):
                        self._start_ffmpeg(item[3], item[4])
                        self.w, self.h = item[3], item[4]
                    ff = self.ff
                try:
                    # Nonblocking partial writes with a deadline prevent a stuck
                    # encoder from trapping this worker indefinitely.
                    pending = memoryview(item[0] + item[1] + item[2])
                    limit = time.monotonic() + 2
                    while pending:
                        if self.ff is not ff or self._stopping.is_set():
                            raise BrokenPipeError("publisher replaced")
                        if time.monotonic() >= limit:
                            raise TimeoutError("raw video write stalled")
                        try:
                            written = os.write(ff.stdin.fileno(), pending)
                            pending = pending[written:]
                        except BlockingIOError:
                            select.select([], [ff.stdin.fileno()], [], 0.05)
                    self._last_output = time.time()
                    self._enc_count += 1
                except (BrokenPipeError, ValueError, OSError) as exc:
                    log("[video] publisher recovery:", exc)
                    with self.lock:
                        if self.ff is ff:
                            self._stop_ffmpeg()
                    self._stopping.wait(0.5)
                deadline = max(deadline + 1 / self.fps, time.monotonic())
            except Exception as e:
                log("[video] writer error:", e)
                self._stopping.wait(1)

    # ---- Agora callback: one decoded YUV frame ----
    def on_frame(self, channel_id, remote_uid, frame):
        try:
            with self.lock:
                if not self.feeding:
                    return 0
                w, h = frame.width, frame.height
                if not w or not h or frame.y_buffer is None:
                    return 0
                self._src_count += 1              # every decoded frame (source rate diagnostic)
                now = time.time()
                if self._src_t0 == 0.0:
                    self._src_t0 = now
                elif now - self._src_t0 >= 30.0:
                    # every 30 s, not every 5: at 5 s this one line drowned out everything else in
                    # the add-on log (audio diagnostics especially) within a couple of minutes.
                    log("[video] source ~%.1f fps, output ~%.1f fps, %d superseded source frames"
                        % (self._src_count / (now - self._src_t0),
                           self._enc_count / (now - self._src_t0), self._dropped))
                    self._src_t0 = now
                    self._src_count = 0
                    self._enc_count = 0
                    self._dropped = 0
                # Prefer the latest image; never queue old scenes behind a slow encoder.
                if self._pending is not None:
                    self._dropped += 1
                y = _pack_plane(frame.y_buffer, frame.y_stride or w, w, h)
                u = _pack_plane(frame.u_buffer, frame.u_stride or (w // 2), w // 2, h // 2)
                v = _pack_plane(frame.v_buffer, frame.v_stride or (w // 2), w // 2, h // 2)
                self._pending = (y, u, v, w, h)
                self.frames += 1
                self._last_frame = time.time()
                first = self.frames == 1
                nframes = self.frames
            self._pending_evt.set()
            if first:
                log("[video] first decoded frame %dx%d (pix_type=%s) strides y=%s u=%s v=%s "
                    "(w=%d → %s) — encoding to RTSP"
                    % (w, h, getattr(frame, "type", "?"), frame.y_stride, frame.u_stride,
                       frame.v_stride, w, "PADDED (slow unpack!)" if (frame.y_stride or w) != w
                       else "no padding"))
            elif nframes % 4500 == 0:         # light heartbeat (~every few minutes)
                log("[video] streaming — %d frames (%dx%d), %d dropped for latency"
                    % (nframes, w, h, self._dropped))
        except Exception as e:
            log("[video] frame error:", e)
        return 0

    def stop(self):
        self._stopping.set()
        self._pending_evt.set()
        self.stop_feed()
        if self._writer:
            self._writer.join(timeout=3)
        for p in (getattr(self, "mediamtx", None),):
            try:
                if p:
                    p.terminate()
            except Exception:
                pass
