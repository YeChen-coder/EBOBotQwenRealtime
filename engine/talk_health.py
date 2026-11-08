"""Can a talk reply actually reach the robot speaker, or only the Agora sender?

"N ms played" counts frames handed to an Agora PCM sender, nothing more. On 11-08 that sender
belonged to a connection the token renewal had already closed: no error, every reply "played",
and the robot was mute for 13 h. This module answers the question the logs could not: is the
TX loop sending into the CURRENT connection, with our track published there?
"""
import threading
import time

# Readable on the panel / dashboard; keep the codes stable, the assistant maps them to text.
FAULTS = {
    "tx_loop_dead": "talk TX thread died while marked running",
    "stale_sender": "talk TX loop sends into an old RTC connection",
    "not_published": "our audio track is not published on the current RTC connection",
    "not_sending": "Agora reports nothing sent while a reply plays",
}


# Measured 11-08 on SDK 2.4.9: once published, Agora reports sent_bitrate ~69 kbps every 2 s,
# silence included (G.711, no DTX); a connection we never published on reports no stats at all.
SENDING_GRACE = 8.0


def tx_fault(*, tx_running, tx_alive, tx_rtc, tx_sender, rtc, rtc_sender, published_rtc, publish_rc,
             rtc_connected=True, tx_started_at=0.0, last_sending_at=None, now=None,
             grace=SENDING_GRACE):
    """Why audio pushed by the TX loop would not reach the current connection, or ''.

    last_sending_at must come from the CURRENT connection's local audio stats.
    """
    if not tx_running:
        return ""
    if not tx_alive:
        return "tx_loop_dead"
    if rtc is None or tx_rtc is not rtc or tx_sender is None or tx_sender is not rtc_sender:
        return "stale_sender"
    if published_rtc is not rtc or publish_rc not in (0, None):  # None = publish in flight
        return "not_published"
    now = now or time.time()
    if (rtc_connected and publish_rc == 0 and now - tx_started_at > grace
            and now - (last_sending_at or 0) > grace):
        return "not_sending"
    return ""


class TalkHealth:
    """Survives RTC reconnects on purpose: a fault seen before a renewal must stay visible."""

    def __init__(self, fault_memory=1800, stats_max_age=10):
        self._lock = threading.Lock()
        self.fault_memory = fault_memory
        self.stats_max_age = stats_max_age
        self.faults = 0
        self.last_fault = ""
        self.last_fault_detail = ""
        self.last_fault_at = 0.0
        self.last_fault_healed = False
        self._reset_connection()

    def _reset_connection(self):
        self.sent_bitrate = None
        self.stats_at = 0.0
        self.last_sending_at = 0.0
        self.publish_state = None

    def new_connection(self):
        with self._lock:
            self._reset_connection()

    def local_stats(self, sent_bitrate, now=None):
        with self._lock:
            self.sent_bitrate = int(sent_bitrate)
            self.stats_at = now or time.time()
            if self.sent_bitrate > 0:
                self.last_sending_at = self.stats_at

    def set_publish_state(self, state):
        with self._lock:
            self.publish_state = state

    def record_fault(self, code, detail="", healed=False, now=None):
        with self._lock:
            self.faults += 1
            self.last_fault = code
            self.last_fault_detail = detail or FAULTS.get(code, code)
            self.last_fault_at = now or time.time()
            self.last_fault_healed = bool(healed)

    def snapshot(self, fault, tx_running, now=None):
        now = now or time.time()
        with self._lock:
            stats_age = now - self.stats_at if self.stats_at else None
            if fault:
                status = fault
            elif not tx_running:
                status = "idle"
            else:
                status = "ok"
            recent = bool(self.last_fault_at) and now - self.last_fault_at < self.fault_memory
            return {
                "observed_at": now, "status": status, "ok": not fault,
                "fault_detail": FAULTS.get(fault, fault) if fault else "",
                "tx_running": bool(tx_running),
                "faults": self.faults,
                "last_fault": self.last_fault if recent else "",
                "last_fault_detail": self.last_fault_detail if recent else "",
                "last_fault_at": self.last_fault_at or None,
                "last_fault_healed": self.last_fault_healed,
                "sent_bitrate": self.sent_bitrate if stats_age is not None and stats_age < self.stats_max_age else None,
                "stats_age_seconds": stats_age,
                "last_sending_at": self.last_sending_at or None,
                "publish_state": self.publish_state,
            }
