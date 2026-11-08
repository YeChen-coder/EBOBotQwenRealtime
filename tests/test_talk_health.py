"""“日志说播了、机器人没声音”要能查出来（11-08 续期后哑了 13 小时，日志全是 played）。"""
import sys, time
from pathlib import Path
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / 'engine'))

from talk_health import TalkHealth, tx_fault, SENDING_GRACE
from state import RuntimeState

now = 1_000_000.0
old_rtc, new_rtc = object(), object()
old_sender, new_sender = object(), object()
healthy = dict(tx_running=True, tx_alive=True, tx_rtc=new_rtc, tx_sender=new_sender, rtc=new_rtc,
               rtc_sender=new_sender, published_rtc=new_rtc, publish_rc=0, rtc_connected=True,
               tx_started_at=now - 60, last_sending_at=now - 1, now=now)


def check(**changes):
    return tx_fault(**dict(healthy, **changes))


assert check() == ''
assert check(tx_running=False, tx_rtc=old_rtc) == ''               # TX 没开，不算故障
# 11-08 原样：续期换了连接，推声音的线程还拿着旧连接的发送口
assert check(tx_rtc=old_rtc, tx_sender=old_sender, published_rtc=old_rtc) == 'stale_sender'
assert check(tx_sender=old_sender) == 'stale_sender'
assert check(rtc=None, rtc_sender=None) == 'stale_sender'           # 连接没了，线程还在推
assert check(tx_alive=False) == 'tx_loop_dead'
assert check(published_rtc=old_rtc) == 'not_published'              # 只在旧连接上发布过
assert check(publish_rc=-1000) == 'not_published'
assert check(publish_rc='exception: boom') == 'not_published'
assert check(publish_rc=None, last_sending_at=None) == ''           # 发布还在进行中
# 声网统计：发布后每 2 秒报一次 ~69 kbps（静音也是）；当前连接没报 = 没发出去
assert check(last_sending_at=None) == 'not_sending'
assert check(last_sending_at=now - SENDING_GRACE - 1) == 'not_sending'
assert check(tx_started_at=now - 3, last_sending_at=None) == ''      # 刚开始，统计还没来
assert check(rtc_connected=False, last_sending_at=None) == ''       # 断线另有报警，这里不重复

h = TalkHealth(fault_memory=1800)
h.local_stats(0, now=now)
assert h.last_sending_at == 0                                       # 0 kbps 不算在发
h.local_stats(69, now=now)
assert h.last_sending_at == now
h.new_connection()                                                  # 新连接：旧连接的统计不能作数
assert h.last_sending_at == 0 and h.sent_bitrate is None
snap = h.snapshot('', tx_running=False, now=now)
assert snap['status'] == 'idle' and snap['ok'] and not snap['last_fault']
snap = h.snapshot('stale_sender', tx_running=True, now=now)
assert snap['status'] == 'stale_sender' and not snap['ok'] and 'old RTC' in snap['fault_detail']
h.record_fault('stale_sender', healed=True, now=now)
snap = h.snapshot('', tx_running=True, now=now + 60)
assert snap['ok'] and snap['status'] == 'ok' and snap['last_fault'] == 'stale_sender'
assert snap['last_fault_healed'] and snap['faults'] == 1
assert h.snapshot('', tx_running=True, now=now + 1801)['last_fault'] == ''   # 30 分钟后不再列出


# 看板：Engine 报的故障要变成“最近错误”里的一行，当前故障还要让总状态变红
def dashboard(talk):
    s = RuntimeState()
    t = time.time()
    engine = dict(observed_at=t, status='receiving', source_audio_ok=True, last_packet_at=t, last_pcm_at=t)
    if talk is not None:
        engine['talk'] = dict(talk, observed_at=t)
    s.update(listener_ready=True, session_state='standby', last_audio_at=t,
             engine_audio_monitor_enabled=True, engine_audio_checked_at=t, engine_audio_health=engine)
    return s.snapshot()


d = dashboard(TalkHealth().snapshot('', tx_running=True))
assert d['ok'] and d['speaker_output_ok'] and d['speaker_output_error'] == ''
assert d['speaker_output_status'] == 'ok'

d = dashboard(TalkHealth().snapshot('stale_sender', tx_running=True))
assert not d['ok'] and not d['speaker_output_ok']
assert d['speaker_output_error'] == 'stale_sender' and d['speaker_output_fault_current']

h = TalkHealth()
h.record_fault('not_sending', healed=False)
d = dashboard(h.snapshot('', tx_running=True))
assert d['ok'] and d['speaker_output_error'] == 'not_sending' and not d['speaker_output_fault_current']
assert d['speaker_output_fault_at'] and not d['speaker_output_fault_healed']

d = dashboard(None)                                                 # 旧 Engine 没有 talk 字段
assert d['ok'] and d['speaker_output_status'] == 'not_monitored' and d['speaker_output_error'] == ''

print('talk health ok')
