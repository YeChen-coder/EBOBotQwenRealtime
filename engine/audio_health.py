"""Observe upstream audio, independently of RTSP silence padding and speech volume."""
import threading
import time


class AudioHealth:
    def __init__(self):
        self._lock = threading.Lock()
        self.last_packet_at = 0.0
        self.last_pcm_at = 0.0
        self.received_bytes = 0
        self.received_bitrate = 0

    def statistics(self, received_bytes, received_bitrate):
        with self._lock:
            # Counters can reset after a track replacement. New positive bytes count too.
            if received_bytes > 0 and received_bytes != self.received_bytes:
                self.last_packet_at = time.time()
            self.received_bytes = received_bytes
            self.received_bitrate = received_bitrate

    def pcm(self):
        with self._lock:
            self.last_pcm_at = time.time()

    def snapshot(self, listen_on, connected, enabled=True, max_age=20):
        now = time.time()
        with self._lock:
            packet_age = now - self.last_packet_at if self.last_packet_at else None
            pcm_age = now - self.last_pcm_at if self.last_pcm_at else None
            if not enabled:
                status = "disabled"
            elif not listen_on:
                status = "muted"
            elif not connected:
                status = "disconnected"
            elif packet_age is None or packet_age > max_age:
                status = "no_source_packets"
            elif pcm_age is None or pcm_age > max_age:
                status = "no_decoded_pcm"
            else:
                status = "receiving"
            return {
                "observed_at": now, "status": status, "listen_enabled": listen_on,
                "source_audio_ok": status == "receiving",
                "last_packet_at": self.last_packet_at or None,
                "last_pcm_at": self.last_pcm_at or None,
                "packet_age_seconds": packet_age, "pcm_age_seconds": pcm_age,
                "received_bytes": self.received_bytes,
                "received_bitrate": self.received_bitrate,
            }
