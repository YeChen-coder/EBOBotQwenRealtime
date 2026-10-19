"""日志（滚动文件 + 内存环形缓冲）与健康看板。"""
from __future__ import annotations
from collections import deque
import json
import logging
import logging.handlers
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

LOG_FORMAT = '%(asctime)s %(levelname)s %(name)s: %(message)s'


class LogBuffer(logging.Handler):
    """保留最近的日志供看板增量拉取。"""

    def __init__(self, capacity: int = 1000):
        super().__init__()
        self.records: deque[dict] = deque(maxlen=capacity)
        self.seq = 0
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord):
        try:
            message = record.getMessage()
            if record.exc_info:
                message += '\n' + logging.Formatter().formatException(record.exc_info)
        except Exception:
            message = str(record.msg)
        with self._lock:
            self.seq += 1
            self.records.append({'seq': self.seq, 'ts': record.created, 'level': record.levelname,
                                 'name': record.name, 'msg': message})

    def since(self, after: int, limit: int = 500) -> list[dict]:
        with self._lock:
            items = [r for r in self.records if r['seq'] > after]
        return items[-limit:]


def setup_logging(log_dir: str) -> LogBuffer:
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    file_handler = logging.handlers.RotatingFileHandler(
        directory / 'assistant.log', maxBytes=5 * 1024 * 1024, backupCount=5, encoding='utf-8')
    file_handler.setFormatter(formatter)
    buffer = LogBuffer()

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in (console, file_handler, buffer):
        root.addHandler(handler)
    # 第三方库的逐条连接/请求日志会淹没有用信息
    for noisy in ('websocket', 'httpx', 'httpx2', 'urllib3', 'faster_whisper', 'huggingface_hub'):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    def thread_crash(args):
        logging.getLogger('ebo-qwen-runtime').error(
            'Thread %s crashed', args.thread.name if args.thread else '?',
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    threading.excepthook = thread_crash
    return buffer


def whep_url(rtsp_url: str) -> str:
    """Engine 的 mediamtx：RTSP 8554 对应 WebRTC(WHEP) 8189，路径相同。"""
    parts = urlsplit(rtsp_url)
    port = 8189 + ((parts.port or 8554) - 8554)
    return f'http://{parts.hostname}:{port}{parts.path}/whep'


def add_ice_host(sdp: str, host_ip: str) -> str:
    """mediamtx 只通告容器内网 IP 和 Engine 配置里的主机 IP，浏览器所在机器都连不上；
    它的 WebRTC UDP 端口已映射到宿主机，所以按浏览器访问看板用的地址补一个候选。"""
    out, added = [], set()
    for line in sdp.splitlines():
        if line.startswith('m='):
            added = set()
        out.append(line)
        parts = line.split()
        if (line.startswith('a=candidate:') and len(parts) >= 8 and parts[2].lower() == 'udp'
                and parts[7] == 'host' and parts[4] != host_ip and (parts[1], parts[5]) not in added):
            added.add((parts[1], parts[5]))
            out.append(f'a=candidate:9{parts[1]}{parts[5]} {parts[1]} udp 2130706175 {host_ip} {parts[5]} typ host')
    return '\r\n'.join(out) + '\r\n'


def mjpeg_url(frigate_url: str, camera: str, boxes: bool) -> str:
    url = f'{frigate_url.rstrip("/")}/api/{quote(camera)}?fps=10&h=480'
    return url + '&bbox=1' if boxes else url


def tail_jsonl(path: Path, limit: int) -> list[dict]:
    """读取 JSONL 文件末尾若干条（只读最后 256 KB，文件再大也不慢）。"""
    try:
        with path.open('rb') as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 256 * 1024))
            lines = handle.read().decode('utf-8', errors='replace').splitlines()
    except OSError:
        return []
    if size > 256 * 1024:
        lines = lines[1:]  # 第一行可能被截断
    records = []
    for line in lines[-limit:]:
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def recent_conversation(user_path: Path, assistant_path: Path, limit: int = 40) -> list[dict]:
    items = [{'role': 'user', 'at': r.get('received_at', 0), 'text': r.get('transcript', ''),
              'user_id': r.get('user_id')} for r in tail_jsonl(user_path, limit)]
    items += [{'role': 'assistant', 'at': r.get('received_at', 0), 'text': r.get('transcript', ''),
               'played_ms': r.get('played_ms', 0), 'interrupted': r.get('interrupted', False)}
              for r in tail_jsonl(assistant_path, limit)]
    items.sort(key=lambda r: r['at'])
    return items[-limit:]


DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EBO 助手看板</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2129;--muted:#6b7280;--line:#e5e7eb;
--ok:#15803d;--ok-bg:#dcfce7;--warn:#b45309;--warn-bg:#fef3c7;--bad:#b91c1c;--bad-bg:#fee2e2;
--info:#1d4ed8;--info-bg:#dbeafe;--user:#eef2ff;--bot:#f0fdf4;--mono:ui-monospace,SFMono-Regular,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--text:#e5e7eb;--muted:#9ca3af;--line:#2a2f3a;
--ok:#4ade80;--ok-bg:#12301d;--warn:#fbbf24;--warn-bg:#3a2c0b;--bad:#f87171;--bad-bg:#3b1414;
--info:#93c5fd;--info-bg:#172554;--user:#1e2240;--bot:#13261b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header{display:flex;flex-wrap:wrap;align-items:center;gap:12px;padding:14px 16px;border-bottom:1px solid var(--line);background:var(--card);position:sticky;top:0;z-index:1}
h1{font-size:17px;margin:0;font-weight:600}
.meta{color:var(--muted);font-size:13px}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-weight:600;font-size:13px}
.ok{color:var(--ok);background:var(--ok-bg)}.warn{color:var(--warn);background:var(--warn-bg)}
.bad{color:var(--bad);background:var(--bad-bg)}.info{color:var(--info);background:var(--info-bg)}
.idle{color:var(--muted);background:var(--line)}
main{padding:16px;max-width:1400px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;min-width:0}
.card h2{font-size:14px;margin:0 0 8px;display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:8px;white-space:nowrap}
.row{display:flex;justify-content:space-between;gap:8px;padding:3px 0;border-top:1px dashed var(--line)}
.row:first-of-type{border-top:0}
.row span:first-child{color:var(--muted);flex-shrink:0;white-space:nowrap}
.row span:last-child{text-align:right;overflow-wrap:anywhere}
.top{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(0,1fr);gap:12px;margin-bottom:12px}
@media (max-width:900px){.top{grid-template-columns:1fr}}
.screen{position:relative;background:#000;border-radius:8px;overflow:hidden;aspect-ratio:16/9}
.screen video,.screen img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:#000}
.screen:fullscreen{border-radius:0}
.vmsg{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#fff;background:#000a;font-size:15px;text-align:center;padding:16px}
.vmsg[hidden]{display:none}
button{font:inherit;font-size:12px;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:1px 8px;cursor:pointer}
.tools{flex-wrap:wrap;justify-content:flex-end}
.chat{max-height:min(52vw,520px);min-height:200px;overflow:auto;display:flex;flex-direction:column;gap:6px}
@media (max-width:900px){.chat{max-height:360px}}
.msg{padding:6px 10px;border-radius:8px;max-width:92%;overflow-wrap:anywhere}
.msg.user{background:var(--user);align-self:flex-start}.msg.assistant{background:var(--bot);align-self:flex-end}
.msg small{display:block;color:var(--muted);font-size:11px}
.logs{font:12px/1.45 var(--mono);max-height:520px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}
.logs div{padding:1px 0;border-bottom:1px solid var(--line)}
.lv-WARNING{color:var(--warn)}.lv-ERROR,.lv-CRITICAL{color:var(--bad)}
.tools{display:flex;gap:8px;align-items:center;font-size:12px;font-weight:400}
select,label{font:inherit;color:inherit}
select{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:1px 4px}
.empty{color:var(--muted);font-size:13px}
.errors .row span:last-child{color:var(--bad)}
#lang{font-weight:600}
</style>
</head>
<body>
<header>
  <h1 data-en="EBO Assistant Dashboard">EBO 助手看板</h1>
  <span id="overall" class="badge idle">…</span>
  <span id="session" class="badge idle">—</span>
  <span class="meta" id="meta"></span>
  <span style="margin-left:auto;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
    <span class="meta" id="dockmsg"></span>
    <button id="dockrest" type="button" data-en="Dock &amp; cool down"
      title="不在充电座就回去；已在充电就只开始冷却。冷却期间看到人也不追，重启程序也不会丢"
      data-en-title="Returns to the dock if it isn't charging; if it already is, just starts the cooldown. It won't follow people during the cooldown, and a restart keeps it.">回充电座并冷却</button>
    <button id="endrest" type="button" data-en="End cooldown"
      title="马上结束冷却，恢复正常跟随" data-en-title="End the cooldown now and resume normal following">结束冷却</button>
    <a class="meta" href="http://127.0.0.1:8101/" target="_blank" rel="noopener" data-en="Robot control panel ↗">机器人控制面板 ↗</a>
    <button id="lang" type="button" title="切换语言 / Switch language">English</button>
  </span>
</header>
<main>
  <div class="top">
    <section class="card"><h2><span data-en="Live view">实时画面</span> <span id="vstate" class="badge idle">…</span>
      <span class="tools">
        <button id="sound" type="button"></button>
        <label id="boxesl" hidden><input type="checkbox" id="boxes"> <span data-en="Detection boxes">识别框</span></label>
        <select id="vmode"><option value="auto" data-en="Auto">自动</option><option value="webrtc" data-en="WebRTC (low latency + audio)">WebRTC（低延迟+声音）</option><option value="mjpeg" data-en="Image stream (compatible)">图片流（兼容）</option></select>
        <button id="vfull" type="button" data-en="Fullscreen">全屏</button>
      </span></h2>
      <div class="screen" id="screen">
        <video id="vid" autoplay muted playsinline></video>
        <img id="mjpg" alt="" hidden>
        <div class="vmsg" id="vmsg"></div>
      </div>
    </section>
    <section class="card"><h2 data-en="Recent conversation">最近对话</h2><div class="chat" id="chat"></div></section>
  </div>
  <div class="grid" id="cards"></div>
  <section class="card" style="margin-top:12px"><h2><span data-en="Logs">日志</span>
    <span class="tools"><select id="level"><option value="INFO" data-en="All">全部</option><option value="WARNING" data-en="Warnings and above">警告及以上</option><option value="ERROR" data-en="Errors only">仅错误</option></select>
    <label><input type="checkbox" id="follow" checked> <span data-en="Auto-scroll">自动滚动</span></label></span></h2>
    <div class="logs" id="logs"></div></section>
</main>
<script>
const $ = id => document.getElementById(id);
let lang = 'zh';
try { lang = localStorage.getItem('ebo-dashboard-lang') === 'en' ? 'en' : 'zh'; } catch (e) {}
const T = (zh, en) => lang === 'en' ? en : zh;
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const yes = v => v ? `<span class="badge ok">${T('是','Yes')}</span>` : `<span class="badge bad">${T('否','No')}</span>`;
const dur = s => s < 60 ? T(`${s.toFixed(1)} 秒`, `${s.toFixed(1)} s`) : s < 3600 ? T(`${Math.round(s/60)} 分钟`, `${Math.round(s/60)} min`) : T(`${(s/3600).toFixed(1)} 小时`, `${(s/3600).toFixed(1)} h`);
const age = s => s == null ? '—' : T(dur(s) + '前', dur(s) + ' ago');
const ago = ts => ts ? age(Date.now()/1000 - ts) : T('从未', 'never');
const clock = ts => new Date(ts*1000).toLocaleTimeString(lang === 'en' ? 'en-GB' : 'zh-CN', {hour12:false});
const mmss = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
const STATE = () => ({standby:[T('待机','Standby'),'ok'], connecting:[T('连接中','Connecting'),'info'], active:[T('对话中','In conversation'),'info'],
  paused:[T('已暂停','Paused'),'warn'], starting:[T('启动中','Starting'),'idle'], saving_memory:[T('保存中','Saving memory'),'info']});
let lastSeq = 0, logs = [], failures = 0, lastHealth = null, lastChat = null, dockNote = null, wasResting = false;
const HINTS = () => ({
  TimeoutError: T('连接超时：这台电脑访问不到 Engine（检查 IP 地址 / 是否同一网络 / VPN）', 'Connection timed out: this computer cannot reach the Engine (check the IP address / same network / VPN)'),
  URLError: T('访问不到 Engine 的 HTTP 接口（检查 IP 地址 / 网络）', "Cannot reach the Engine's HTTP API (check the IP address / network)"),
  ConnectionRefusedError: T('Engine 主机可达，但端口没开（Engine 没在运行？）', 'Engine host is reachable but the port is closed (is the Engine running?)'),
  ConnectionResetError: T('Engine 断开了连接', 'The Engine closed the connection'),
  WebSocketBadStatusException: T('Engine 拒绝了音频连接（检查 EBO_API_TOKEN）', 'The Engine rejected the audio connection (check EBO_API_TOKEN)'),
  HTTPError: T('Engine 返回错误（检查 EBO_API_TOKEN / 机器人节点名）', 'The Engine returned an error (check EBO_API_TOKEN / robot node name)'),
  ValueError: T('数据格式不对（Engine 版本不匹配？）', 'Unexpected data format (Engine version mismatch?)'),
  monitor_error: T('读不到 Engine 的音频状态', "Cannot read the Engine's audio status"),
  monitor_stale: T('Engine 音频状态过时', 'Engine audio status is stale'),
  no_source_packets: T('机器人没发来音频（可能在休眠）', 'No audio from the robot (it may be asleep)'),
  source_stale: T('机器人音频中断', 'Robot audio stopped'),
});
const explain = v => { const k = String(v).split(':')[0], h = HINTS(); return h[k] ? `${h[k]} <small>(${esc(v)})</small>` : esc(v); };

function card(title, badge, rows) {
  return `<div class="card"><h2>${title}${badge || ''}</h2>${rows.map(([k, v]) => `<div class="row"><span>${k}</span><span>${v}</span></div>`).join('')}</div>`;
}
function tag(text, cls) { return `<span class="badge ${cls}">${esc(text)}</span>`; }

function render(h) {
  $('overall').className = 'badge ' + (h.ok ? 'ok' : h.media_starting ? 'warn' : 'bad');
  $('overall').textContent = h.ok ? T('运行正常','Running normally') : h.media_starting ? T('启动中','Starting') : T('有问题','Problem');
  const [label, cls] = STATE()[h.session_state] || [h.session_state, 'idle'];
  $('session').className = 'badge ' + cls; $('session').textContent = label;
  const hrs = Math.floor(h.uptime_seconds/3600), mins = Math.floor(h.uptime_seconds%3600/60);
  $('meta').textContent = T(`已运行 ${hrs} 小时 ${mins} 分 · 完成会话 ${h.sessions_completed} 次`,
                            `Up ${hrs} h ${mins} min · ${h.sessions_completed} sessions completed`);

  // 冷却倒计时：跟着看板每 2 秒刷新
  const resting = h.tracking_status === 'resting' && h.tracking_rest_seconds > 0;
  if (wasResting && !resting) dockNote = null;   // 冷却自己结束了，旧提示不再显示
  wasResting = resting;
  $('dockmsg').textContent = resting ? T(`冷却中，还剩 ${mmss(h.tracking_rest_seconds)}`, `Cooling down, ${mmss(h.tracking_rest_seconds)} left`)
                                     : dockNote ? dockNote() : '';

  const audioOk = h.audio_streaming && h.source_audio_ok;
  const appBusy = ((h.engine_audio_health || {}).other_viewers || 0) > 0;
  const micTag = appBusy ? tag(audioOk ? T('正常（EBO App 在线）','OK (EBO App online)') : T('被 EBO App 占用','In use by the EBO App'), 'warn')
                         : tag(audioOk ? T('正常','OK') : T('异常','Error'), audioOk ? 'ok' : 'bad');
  const errors = [[T('助手','Assistant'), h.last_error], [T('音频采集','Audio capture'), h.audio_capture_error], [T('Engine 音频','Engine audio'), h.engine_audio_error],
                  [T('唤醒','Wake'), h.voice_wake_last_error], [T('视觉跟随','Person following'), h.tracking_last_error],
                  [T('机器人状态','Robot status'), h.robot_state_error]].filter(([, v]) => v);
  const wakeState = !h.voice_wake_enabled ? tag(T('关闭','Off'),'idle') : h.voice_wake_transcribing ? tag(T('识别中','Transcribing'),'info')
                    : h.voice_wake_listening ? tag(T('监听中','Listening'),'ok') : tag(T('未监听','Not listening'),'idle');
  const quietLeft = h.quiet_until ? Math.ceil((h.quiet_until - Date.now() / 1000) / 60) : 0;
  const quietTag = quietLeft > 0 ? tag(T(`安静中，还剩 ${quietLeft} 分钟（只认唤醒词）`, `Quiet, ${quietLeft} min left (wake words only)`), 'warn') : null;
  const tracking = h.tracking_enabled
      ? ({following:tag(T('跟随中','Following'),'info'), centered:tag(T('已对准','Centered'),'ok'), too_close:tag(T('太近，停住','Too close, holding'),'warn'),
          paused:tag(T('暂停','Paused'),'idle'), waiting:tag(T('没看到人，原地等','No one seen, waiting'),'idle'), searching:tag(T('转圈找人','Turning to search'),'warn'),
          returning_to_dock:tag(T('回充电座','Returning to dock'),'info'), resting:tag(T('休息中（让它去充电了）','Resting (sent to charge)'),'info'),
          leaving_dock:tag(T('驶离充电座','Leaving the dock'),'info')}[h.tracking_status] || tag(T('等待看到人','Waiting to see someone'),'idle'))
      : tag(T('未启用','Disabled'),'idle');
  const words = h.wake_words ? h.wake_words.split(',') : [];
  const wakeHow = (words.length ? T('说“' + words.join('”/“') + '”马上醒', 'Say "' + words.join('" / "') + '" to wake instantly') : '')
      + (h.addressee_check ? (words.length ? T('；', '; ') : '') + T('其他话由 AI 判断是不是在跟它说', 'AI decides whether other speech is meant for it')
                           : (words.length ? '' : T('任何中文语句', 'Any Chinese sentence')));
  const distance = {far:T('远（会靠近）','Far (will approach)'), ok:T('合适','Good'), close:T('太近','Too close')};

  $('cards').innerHTML = [
    card(T('机器人麦克风','Robot microphone'), micTag, [
      ['EBO App', appBusy ? tag(T('正在用 App 看机器人','Someone is watching via the App') + (audioOk ? '' : T('，关掉 App 后自动恢复','; recovers when the App closes')), 'warn') : T('没有','None')],
      [T('Engine 地址','Engine address'), esc(h.engine_api_url || '—')],
      [T('音频流到达','Audio stream arriving'), yes(h.audio_streaming)], [T('上次收到音频','Last audio received'), age(h.last_audio_age_seconds)],
      [T('Engine 音源状态','Engine audio source'), explain(h.source_audio_status)], [T('传输方式','Transport'), esc(h.audio_capture_transport || '—')],
      [T('自动唤醒机器人次数','Robot auto-wake count'), h.media_recovery_attempts]]),
    card(T('通义千问','Qwen (Tongyi Qianwen)'), tag(h.realtime_connected ? T('已连接','Connected') : T('未连接','Not connected'), h.realtime_connected ? 'ok' : 'idle'), [
      [T('会话状态','Session state'), esc(label)], [T('当前用户','Current user'), esc(h.active_user || '—')], [T('触发方式','Trigger'), esc(h.session_trigger || '—')],
      [T('本次会话时长','Session length'), h.realtime_session_age_seconds == null ? '—' : Math.round(h.realtime_session_age_seconds) + T(' 秒',' s')],
      [T('用户正在说话','User speaking'), h.input_speech_active ? tag(T('是','Yes'),'info') : T('否','No')], [T('联网搜索','Web search'), h.web_search_enabled ? T('开','On') : T('关','Off')]]),
    card(T('待机唤醒','Standby wake'), quietTag || wakeState, [
      [T('唤醒方式','How to wake'), esc(wakeHow)], [T('候选语句','Candidate phrases'), h.voice_wake_candidates],
      [T('唤醒成功','Woken'), h.voice_wake_accepted], [T('被拒绝','Rejected'), h.voice_wake_rejected], [T('上次唤醒','Last wake'), ago(h.last_voice_wake_at)]]),
    card(T('播放 / 回复','Playback / replies'), tag(h.speaker_stream_status, h.speaker_stream_failures ? 'warn' : 'idle'), [
      [T('已保存回复','Replies saved'), h.assistant_outputs_persisted], [T('上次回复','Last reply'), ago(h.last_assistant_output_at)],
      [T('用户转录条数','User transcripts'), h.user_transcripts_received], [T('流式失败','Stream failures'), h.speaker_stream_failures],
      [T('改用 URL 播放','URL playback fallbacks'), h.speaker_stream_fallbacks], [T('请求回复次数','Responses requested'), h.manual_responses_requested]]),
    // 机器人上报的 docked 在离开充电座后很久都不复位，只能按是否在充电判断
    card(T('机器人状态','Robot status'), h.robot_charging == null ? tag(T('未知','Unknown'),'idle') : h.robot_charging ? tag(T('在充电座上充电','Charging on the dock'),'info') : tag(T('不在充电座','Off the dock'),'ok'), [
      [T('电量','Battery'), (h.robot_battery ?? '—') + (h.robot_battery != null ? '%' : '')],
      [T('桌面模式','Desk mode'), h.robot_desktop_mode == null ? '—' : h.robot_desktop_mode ? tag(T('开（会自动关闭）','On (will be turned off)'),'warn') : T('关','Off')]]),
    card(T('视觉跟随','Person following'), tracking, [
      ['MQTT', h.tracking_enabled ? yes(h.mqtt_connected) : '—'], [T('人相对中心','Offset from center'), h.tracking_offset ?? '—'],
      [T('人的远近','Distance'), h.tracking_distance ? (distance[h.tracking_distance] + T('，框高 ',', box height ') + Math.round(h.tracking_height_ratio * 100) + '%') : '—'],
      [T('多久没看到人','Time since last seen'), h.tracking_enabled && h.tracking_absent_seconds != null ? dur(h.tracking_absent_seconds) : '—'],
      [T('自动回充','Auto-dock'), h.tracking_enabled ? T(`离开充电座后 ${Math.round((h.tracking_dock_after_seconds || 600) / 60)} 分钟没人`,
                                                        `After ${Math.round((h.tracking_dock_after_seconds || 600) / 60)} min off the dock with no one around`) : '—'],
      [T('冷却剩余','Cooldown left'), resting ? T(`${mmss(h.tracking_rest_seconds)}（这段时间不追人）`, `${mmss(h.tracking_rest_seconds)} (won't follow people)`) : '—']]),
    `<div class="card errors"><h2>${T('最近错误','Recent errors')}${errors.length ? tag(errors.length + T(' 项',''),'bad') : tag(T('无','None'),'ok')}</h2>${
      errors.length ? errors.map(([k, v]) => `<div class="row"><span>${k}</span><span>${explain(v)}</span></div>`).join('') : `<div class="empty">${T('一切正常','All good')}</div>`}</div>`,
  ].join('');
}

function renderChat(items) {
  const box = $('chat');
  if (!items.length) { box.innerHTML = `<div class="empty">${T('还没有对话','No conversation yet')}</div>`; return; }
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.innerHTML = items.map(m => `<div class="msg ${m.role}"><small>${m.role === 'user' ? T('用户','User') : T('助手','Assistant')} · ${clock(m.at)}${
    m.interrupted ? T(' · 被打断',' · interrupted') : ''}</small>${esc(m.text)}</div>`).join('');
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function renderLogs() {
  const order = {INFO:1, WARNING:2, ERROR:3, CRITICAL:3};
  const min = order[$('level').value];
  const box = $('logs');
  box.innerHTML = logs.filter(l => (order[l.level] || 0) >= min).map(l =>
    `<div class="lv-${l.level}">${clock(l.ts)} ${l.level[0]} ${esc(l.name)}: ${esc(l.msg)}</div>`).join('');
  if ($('follow').checked) box.scrollTop = box.scrollHeight;
}
$('level').onchange = renderLogs;

// 按钮结果提示：存成函数，切换语言后也能重新显示；冷却中时被倒计时盖住
function note(fn) { dockNote = fn; $('dockmsg').textContent = fn(); }
$('endrest').onclick = async () => {
  const btn = $('endrest');
  btn.disabled = true; note(() => T('发送中…','Sending…'));
  try {
    const r = await fetch('/dashboard/end-rest', {method: 'POST', headers: {'X-EBO-Dashboard': '1'}});
    const j = await r.json();
    note(() => j.error === 'tracking_disabled' ? T('视觉跟随没开，没有冷却','Person following is off; there is no cooldown')
      : j.was_resting ? T('冷却已结束，恢复正常跟随','Cooldown ended; normal following resumes') : T('本来就不在冷却中，正常工作','Not cooling down; working normally'));
    wasResting = false;
    if (lastHealth) { lastHealth.tracking_status = 'idle'; lastHealth.tracking_rest_seconds = 0; render(lastHealth); }
  } catch (e) { note(() => T('失败：','Failed: ') + e.message); }
  setTimeout(() => { btn.disabled = false; }, 2000);
};
$('dockrest').onclick = async () => {
  const btn = $('dockrest');
  btn.disabled = true; note(() => T('发送中…','Sending…'));
  try {
    const r = await fetch('/dashboard/dock-rest', {method: 'POST', headers: {'X-EBO-Dashboard': '1'}});
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.status);
    const mins = Math.round(j.rest_seconds / 60);
    note(() => !j.rest_seconds ? T('已发回充命令（视觉跟随没开，不需要冷却）','Dock command sent (person following is off, no cooldown needed)')
      : j.docking ? T(`正在回充电座，冷却 ${mins} 分钟`, `Returning to the dock; ${mins} min cooldown`)
                  : T(`已经在充电，开始冷却 ${mins} 分钟`, `Already charging; ${mins} min cooldown started`));
  } catch (e) { note(() => T('失败：','Failed: ') + e.message); }
  setTimeout(() => { btn.disabled = false; }, 3000);
};

async function tick() {
  try {
    const [h, l, c] = await Promise.all([fetch('/health'), fetch('/logs?after=' + lastSeq), fetch('/conversation')]
      .map(p => p.then(r => r.json())));
    failures = 0;
    lastHealth = h; lastChat = c;
    render(h);
    if (l.length) { logs = logs.concat(l).slice(-800); lastSeq = l[l.length - 1].seq; renderLogs(); }
    renderChat(c);
  } catch (e) {
    if (++failures >= 2) { $('overall').className = 'badge bad'; $('overall').textContent = T('看板连不上助手程序','Dashboard cannot reach the assistant'); }
  }
}

// ---- 实时画面：优先 WebRTC（低延迟、带声音），连不上自动改用 Frigate 图片流 ----
const vid = $('vid'), img = $('mjpg');
let pc = null, retryTimer = null, generation = 0, lastV = null;
// text / msg 是返回当前语言文字的函数，切换语言时重新显示
function vstate(text, cls, msg) {
  lastV = [text, cls, msg];
  $('vstate').className = 'badge ' + cls; $('vstate').textContent = text();
  $('vmsg').hidden = !msg; if (msg) $('vmsg').textContent = msg();
}
const fail = (zh, en) => { const e = new Error(en); e.zh = zh; return e; };
const errText = e => e.zh ? T(e.zh, e.message) : e.message;
function stopVideo() {
  generation++;
  clearTimeout(retryTimer);
  if (pc) { try { pc.close(); } catch (e) {} pc = null; }
  vid.srcObject = null; vid.hidden = true;
  img.onerror = img.onload = null; img.removeAttribute('src'); img.hidden = true;
}
function retry(ms) { clearTimeout(retryTimer); retryTimer = setTimeout(startVideo, ms); }

async function startWebRTC(gen) {
  pc = new RTCPeerConnection({iceServers: []});
  const mine = pc;
  pc.addTransceiver('video', {direction: 'recvonly'});
  pc.addTransceiver('audio', {direction: 'recvonly'});
  pc.ontrack = e => { if (e.streams[0] && vid.srcObject !== e.streams[0]) { vid.srcObject = e.streams[0]; vid.play().catch(() => {}); } };
  await pc.setLocalDescription(await pc.createOffer());
  await new Promise(res => {
    if (pc.iceGatheringState === 'complete') return res();
    const t = setTimeout(res, 1200);
    pc.addEventListener('icegatheringstatechange', () => { if (pc.iceGatheringState === 'complete') { clearTimeout(t); res(); } });
  });
  const resp = await fetch('/video/whep', {method: 'POST', headers: {'Content-Type': 'application/sdp'}, body: pc.localDescription.sdp});
  if (!resp.ok) throw resp.status === 404 ? fail('摄像头还没出画面（机器人可能在休眠）', 'No camera picture yet (the robot may be asleep)')
                                          : fail('Engine 返回 ' + resp.status, 'Engine returned ' + resp.status);
  await pc.setRemoteDescription({type: 'answer', sdp: await resp.text()});
  const state = await new Promise(res => {
    const t = setTimeout(() => res('timeout'), 7000);
    pc.addEventListener('connectionstatechange', () => {
      if (pc.connectionState === 'connected') { clearTimeout(t); res('connected'); }
      else if (pc.connectionState === 'failed') { clearTimeout(t); res('failed'); }
    });
  });
  if (gen !== generation) return;
  if (state !== 'connected') throw state === 'failed' ? fail('WebRTC 连接失败', 'WebRTC connection failed') : fail('WebRTC 连接超时', 'WebRTC connection timed out');
  vid.hidden = false;
  $('boxesl').hidden = true; $('sound').hidden = false;
  vstate(() => T('WebRTC 直播','WebRTC live'), 'ok', null);
  mine.addEventListener('connectionstatechange', () => {
    if (gen === generation && ['failed', 'disconnected', 'closed'].includes(mine.connectionState)) {
      vstate(() => T('已断开','Disconnected'), 'warn', () => T('画面断开，正在重连…','Video dropped, reconnecting…')); retry(2000);
    }
  });
}

function startMJPEG(gen) {
  img.hidden = false;
  img.onload = () => { if (gen === generation) vstate(() => T('图片流','Image stream'), 'ok', null); };
  img.onerror = () => { if (gen === generation) { vstate(() => T('无画面','No video'), 'bad', () => T('取不到画面（Frigate 未运行？），5 秒后重试','Cannot get video (is Frigate running?), retrying in 5 s')); retry(5000); } };
  img.src = '/video/mjpeg?boxes=' + ($('boxes').checked ? 1 : 0) + '&t=' + Date.now();
}

async function startVideo() {
  stopVideo();
  const gen = generation, mode = $('vmode').value;
  $('boxesl').hidden = true;
  $('sound').hidden = true;
  vstate(() => T('连接中…','Connecting…'), 'idle', () => T('正在连接机器人摄像头…','Connecting to the robot camera…'));
  if (mode !== 'mjpeg') {
    try { await startWebRTC(gen); return; }
    catch (e) {
      if (gen !== generation) return;
      if (pc) { try { pc.close(); } catch (x) {} pc = null; }
      if (mode === 'webrtc') { vstate(() => T('无画面','No video'), 'bad', () => errText(e) + T('，5 秒后重试',', retrying in 5 s')); return retry(5000); }
    }
  }
  $('boxesl').hidden = false; $('sound').hidden = true;
  startMJPEG(gen);
}

const soundLabel = () => { $('sound').textContent = vid.muted ? T('开声音','Sound on') : T('静音','Mute'); };
$('vmode').onchange = startVideo;
$('boxes').onchange = () => { if (!img.hidden) startMJPEG(generation); };
$('sound').onclick = () => { vid.muted = !vid.muted; if (!vid.muted) vid.play().catch(() => {}); soundLabel(); };
$('vfull').onclick = () => { const s = $('screen'); document.fullscreenElement ? document.exitFullscreen() : s.requestFullscreen?.(); };

// ---- 中英切换：静态文字带 data-en，动态内容按当前语言重新渲染 ----
function applyLang() {
  document.documentElement.lang = lang === 'en' ? 'en' : 'zh-CN';
  document.title = T('EBO 助手看板', 'EBO Assistant Dashboard');
  document.querySelectorAll('[data-en]').forEach(el => {
    if (el.dataset.zh == null) el.dataset.zh = el.textContent;
    el.textContent = lang === 'en' ? el.dataset.en : el.dataset.zh;
  });
  document.querySelectorAll('[data-en-title]').forEach(el => {
    if (el.dataset.zhTitle == null) el.dataset.zhTitle = el.title;
    el.title = lang === 'en' ? el.dataset.enTitle : el.dataset.zhTitle;
  });
  $('lang').textContent = lang === 'en' ? '中文' : 'English';
  $('mjpg').alt = T('实时画面', 'Live view');
  soundLabel();
  if (lastV) vstate(...lastV);
  if (lastHealth) render(lastHealth); else $('overall').textContent = T('连接中…','Connecting…');
  if (lastChat) renderChat(lastChat); else $('chat').innerHTML = `<div class="empty">${T('还没有对话','No conversation yet')}</div>`;
  if (dockNote && !(lastHealth && lastHealth.tracking_status === 'resting')) $('dockmsg').textContent = dockNote();
  renderLogs();
}
$('lang').onclick = () => {
  lang = lang === 'en' ? 'zh' : 'en';
  try { localStorage.setItem('ebo-dashboard-lang', lang); } catch (e) {}
  applyLang();
};
applyLang();
tick(); setInterval(tick, 2000);
startVideo();
</script>
</body>
</html>
"""
