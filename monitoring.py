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
</style>
</head>
<body>
<header>
  <h1>EBO 助手看板</h1>
  <span id="overall" class="badge idle">连接中…</span>
  <span id="session" class="badge idle">—</span>
  <span class="meta" id="meta"></span>
  <a class="meta" href="http://127.0.0.1:8101/" target="_blank" rel="noopener" style="margin-left:auto">机器人控制面板 ↗</a>
</header>
<main>
  <div class="top">
    <section class="card"><h2>实时画面 <span id="vstate" class="badge idle">连接中…</span>
      <span class="tools">
        <button id="sound" type="button">开声音</button>
        <label id="boxesl" hidden><input type="checkbox" id="boxes"> 识别框</label>
        <select id="vmode"><option value="auto">自动</option><option value="webrtc">WebRTC（低延迟+声音）</option><option value="mjpeg">图片流（兼容）</option></select>
        <button id="vfull" type="button">全屏</button>
      </span></h2>
      <div class="screen" id="screen">
        <video id="vid" autoplay muted playsinline></video>
        <img id="mjpg" alt="实时画面" hidden>
        <div class="vmsg" id="vmsg">正在连接机器人摄像头…</div>
      </div>
    </section>
    <section class="card"><h2>最近对话</h2><div class="chat" id="chat"><div class="empty">还没有对话</div></div></section>
  </div>
  <div class="grid" id="cards"></div>
  <section class="card" style="margin-top:12px"><h2>日志
    <span class="tools"><select id="level"><option value="INFO">全部</option><option value="WARNING">警告及以上</option><option value="ERROR">仅错误</option></select>
    <label><input type="checkbox" id="follow" checked> 自动滚动</label></span></h2>
    <div class="logs" id="logs"></div></section>
</main>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const yes = v => v ? '<span class="badge ok">是</span>' : '<span class="badge bad">否</span>';
const age = s => s == null ? '—' : s < 60 ? `${s.toFixed(1)} 秒前` : s < 3600 ? `${Math.round(s/60)} 分钟前` : `${(s/3600).toFixed(1)} 小时前`;
const ago = ts => ts ? age(Date.now()/1000 - ts) : '从未';
const clock = ts => new Date(ts*1000).toLocaleTimeString('zh-CN', {hour12:false});
const STATE = {standby:['待机','ok'], connecting:['连接中','info'], active:['对话中','info'], paused:['已暂停','warn'], starting:['启动中','idle'], saving_memory:['保存中','info']};
let lastSeq = 0, logs = [], failures = 0;
const HINTS = {
  TimeoutError: '连接超时：这台电脑访问不到 Engine（检查 IP 地址 / 是否同一网络 / VPN）',
  URLError: '访问不到 Engine 的 HTTP 接口（检查 IP 地址 / 网络）',
  ConnectionRefusedError: 'Engine 主机可达，但端口没开（Engine 没在运行？）',
  ConnectionResetError: 'Engine 断开了连接',
  WebSocketBadStatusException: 'Engine 拒绝了音频连接（检查 EBO_API_TOKEN）',
  HTTPError: 'Engine 返回错误（检查 EBO_API_TOKEN / 机器人节点名）',
  ValueError: '数据格式不对（Engine 版本不匹配？）',
  monitor_error: '读不到 Engine 的音频状态',
  monitor_stale: 'Engine 音频状态过时',
  no_source_packets: '机器人没发来音频（可能在休眠）',
  source_stale: '机器人音频中断',
};
const explain = v => { const k = String(v).split(':')[0]; return HINTS[k] ? `${HINTS[k]} <small>(${esc(v)})</small>` : esc(v); };

function card(title, badge, rows) {
  return `<div class="card"><h2>${title}${badge || ''}</h2>${rows.map(([k, v]) => `<div class="row"><span>${k}</span><span>${v}</span></div>`).join('')}</div>`;
}
function tag(text, cls) { return `<span class="badge ${cls}">${esc(text)}</span>`; }

function render(h) {
  $('overall').className = 'badge ' + (h.ok ? 'ok' : h.media_starting ? 'warn' : 'bad');
  $('overall').textContent = h.ok ? '运行正常' : h.media_starting ? '启动中' : '有问题';
  const [label, cls] = STATE[h.session_state] || [h.session_state, 'idle'];
  $('session').className = 'badge ' + cls; $('session').textContent = label;
  $('meta').textContent = `已运行 ${Math.floor(h.uptime_seconds/3600)} 小时 ${Math.floor(h.uptime_seconds%3600/60)} 分 · 完成会话 ${h.sessions_completed} 次`;

  const audioOk = h.audio_streaming && h.source_audio_ok;
  const appBusy = ((h.engine_audio_health || {}).other_viewers || 0) > 0;
  const micTag = appBusy ? tag(audioOk ? '正常（EBO App 在线）' : '被 EBO App 占用', 'warn') : tag(audioOk ? '正常' : '异常', audioOk ? 'ok' : 'bad');
  const errors = [['助手', h.last_error], ['音频采集', h.audio_capture_error], ['Engine 音频', h.engine_audio_error],
                  ['唤醒', h.voice_wake_last_error], ['视觉跟随', h.tracking_last_error],
                  ['机器人状态', h.robot_state_error]].filter(([, v]) => v);
  const wakeState = !h.voice_wake_enabled ? tag('关闭','idle') : h.voice_wake_transcribing ? tag('识别中','info')
                    : h.voice_wake_listening ? tag('监听中','ok') : tag('未监听','idle');
  const tracking = h.tracking_enabled
      ? ({following:tag('跟随中','info'), centered:tag('已对准','ok'), too_close:tag('太近，停住','warn'), paused:tag('暂停','idle'),
          waiting:tag('没看到人，原地等','idle'), searching:tag('转圈找人','warn'), returning_to_dock:tag('回充电座','info'), resting:tag('休息中（让它去充电了）','info'), leaving_dock:tag('驶离充电座','info')}[h.tracking_status] || tag('等待看到人','idle'))
      : tag('未启用','idle');

  $('cards').innerHTML = [
    card('机器人麦克风', micTag, [
      ['EBO App', appBusy ? tag('正在用 App 看机器人' + (audioOk ? '' : '，关掉 App 后自动恢复'), 'warn') : '没有'],
      ['Engine 地址', esc(h.engine_api_url || '—')],
      ['音频流到达', yes(h.audio_streaming)], ['上次收到音频', age(h.last_audio_age_seconds)],
      ['Engine 音源状态', explain(h.source_audio_status)], ['传输方式', esc(h.audio_capture_transport || '—')],
      ['自动唤醒机器人次数', h.media_recovery_attempts]]),
    card('通义千问', tag(h.realtime_connected ? '已连接' : '未连接', h.realtime_connected ? 'ok' : 'idle'), [
      ['会话状态', esc(label)], ['当前用户', esc(h.active_user || '—')], ['触发方式', esc(h.session_trigger || '—')],
      ['本次会话时长', h.realtime_session_age_seconds == null ? '—' : Math.round(h.realtime_session_age_seconds) + ' 秒'],
      ['用户正在说话', h.input_speech_active ? tag('是','info') : '否'], ['联网搜索', h.web_search_enabled ? '开' : '关']]),
    card('待机唤醒', wakeState, [
      ['唤醒方式', esc((h.wake_words ? '说“' + h.wake_words.split(',').join('”/“') + '”马上醒' : '')
          + (h.addressee_check ? (h.wake_words ? '；' : '') + '其他话由 AI 判断是不是在跟它说' : (h.wake_words ? '' : '任何中文语句')))], ['候选语句', h.voice_wake_candidates],
      ['唤醒成功', h.voice_wake_accepted], ['被拒绝', h.voice_wake_rejected], ['上次唤醒', ago(h.last_voice_wake_at)]]),
    card('播放 / 回复', tag(h.speaker_stream_status, h.speaker_stream_failures ? 'warn' : 'idle'), [
      ['已保存回复', h.assistant_outputs_persisted], ['上次回复', ago(h.last_assistant_output_at)],
      ['用户转录条数', h.user_transcripts_received], ['流式失败', h.speaker_stream_failures], ['改用 URL 播放', h.speaker_stream_fallbacks],
      ['请求回复次数', h.manual_responses_requested]]),
    // 机器人上报的 docked 在离开充电座后很久都不复位，只能按是否在充电判断
    card('机器人状态', h.robot_charging == null ? tag('未知','idle') : h.robot_charging ? tag('在充电座上充电','info') : tag('不在充电座','ok'), [
      ['电量', (h.robot_battery ?? '—') + (h.robot_battery != null ? '%' : '')],
      ['桌面模式', h.robot_desktop_mode == null ? '—' : h.robot_desktop_mode ? tag('开（会自动关闭）','warn') : '关']]),
    card('视觉跟随', tracking, [
      ['MQTT', h.tracking_enabled ? yes(h.mqtt_connected) : '—'], ['人相对中心', h.tracking_offset ?? '—'],
      ['人的远近', h.tracking_distance ? ({far:'远（会靠近）', ok:'合适', close:'太近'}[h.tracking_distance] + '，框高 ' + Math.round(h.tracking_height_ratio * 100) + '%') : '—'],
      ['多久没看到人', h.tracking_enabled && h.tracking_absent_seconds != null ? age(h.tracking_absent_seconds).replace('前', '') : '—'],
      ['自动回充', h.tracking_enabled ? `离开充电座后 ${Math.round((h.tracking_dock_after_seconds || 600) / 60)} 分钟没人` : '—']]),
    `<div class="card errors"><h2>最近错误${errors.length ? tag(errors.length + ' 项','bad') : tag('无','ok')}</h2>${
      errors.length ? errors.map(([k, v]) => `<div class="row"><span>${k}</span><span>${explain(v)}</span></div>`).join('') : '<div class="empty">一切正常</div>'}</div>`,
  ].join('');
}

function renderChat(items) {
  const box = $('chat');
  if (!items.length) { box.innerHTML = '<div class="empty">还没有对话</div>'; return; }
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.innerHTML = items.map(m => `<div class="msg ${m.role}"><small>${m.role === 'user' ? '用户' : '助手'} · ${clock(m.at)}${
    m.interrupted ? ' · 被打断' : ''}</small>${esc(m.text)}</div>`).join('');
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

async function tick() {
  try {
    const [h, l, c] = await Promise.all([fetch('/health'), fetch('/logs?after=' + lastSeq), fetch('/conversation')]
      .map(p => p.then(r => r.json())));
    failures = 0;
    render(h);
    if (l.length) { logs = logs.concat(l).slice(-800); lastSeq = l[l.length - 1].seq; renderLogs(); }
    renderChat(c);
  } catch (e) {
    if (++failures >= 2) { $('overall').className = 'badge bad'; $('overall').textContent = '看板连不上助手程序'; }
  }
}
tick(); setInterval(tick, 2000);

// ---- 实时画面：优先 WebRTC（低延迟、带声音），连不上自动改用 Frigate 图片流 ----
const vid = $('vid'), img = $('mjpg');
let pc = null, retryTimer = null, generation = 0;
function vstate(text, cls, msg) {
  $('vstate').className = 'badge ' + cls; $('vstate').textContent = text;
  $('vmsg').hidden = !msg; if (msg) $('vmsg').textContent = msg;
}
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
  if (!resp.ok) throw new Error(resp.status === 404 ? '摄像头还没出画面（机器人可能在休眠）' : 'Engine 返回 ' + resp.status);
  await pc.setRemoteDescription({type: 'answer', sdp: await resp.text()});
  const state = await new Promise(res => {
    const t = setTimeout(() => res('timeout'), 7000);
    pc.addEventListener('connectionstatechange', () => {
      if (pc.connectionState === 'connected') { clearTimeout(t); res('connected'); }
      else if (pc.connectionState === 'failed') { clearTimeout(t); res('failed'); }
    });
  });
  if (gen !== generation) return;
  if (state !== 'connected') throw new Error('WebRTC 连接' + (state === 'failed' ? '失败' : '超时'));
  vid.hidden = false;
  $('boxesl').hidden = true; $('sound').hidden = false;
  vstate('WebRTC 直播', 'ok', null);
  mine.addEventListener('connectionstatechange', () => {
    if (gen === generation && ['failed', 'disconnected', 'closed'].includes(mine.connectionState)) {
      vstate('已断开', 'warn', '画面断开，正在重连…'); retry(2000);
    }
  });
}

function startMJPEG(gen) {
  img.hidden = false;
  img.onload = () => { if (gen === generation) vstate('图片流', 'ok', null); };
  img.onerror = () => { if (gen === generation) { vstate('无画面', 'bad', '取不到画面（Frigate 未运行？），5 秒后重试'); retry(5000); } };
  img.src = '/video/mjpeg?boxes=' + ($('boxes').checked ? 1 : 0) + '&t=' + Date.now();
}

async function startVideo() {
  stopVideo();
  const gen = generation, mode = $('vmode').value;
  $('boxesl').hidden = true;
  $('sound').hidden = true;
  vstate('连接中…', 'idle', '正在连接机器人摄像头…');
  if (mode !== 'mjpeg') {
    try { await startWebRTC(gen); return; }
    catch (e) {
      if (gen !== generation) return;
      if (pc) { try { pc.close(); } catch (x) {} pc = null; }
      if (mode === 'webrtc') { vstate('无画面', 'bad', e.message + '，5 秒后重试'); return retry(5000); }
    }
  }
  $('boxesl').hidden = false; $('sound').hidden = true;
  startMJPEG(gen);
}

$('vmode').onchange = startVideo;
$('boxes').onchange = () => { if (!img.hidden) startMJPEG(generation); };
$('sound').onclick = () => { vid.muted = !vid.muted; if (!vid.muted) vid.play().catch(() => {}); $('sound').textContent = vid.muted ? '开声音' : '静音'; };
$('vfull').onclick = () => { const s = $('screen'); document.fullscreenElement ? document.exitFullscreen() : s.requestFullscreen?.(); };
startVideo();
</script>
</body>
</html>
"""
