"""全家一份的两层记忆（计划见 docs/memory-plan.md）。

第一层（每场对话结束）：qwen-plus 把这场对话写成几条笔记，先分清是在跟猫猫说话还是电视、直播、电话的声音，
    背景声一律不记；顺带判断"关心的事"这次问过没有、说好了没有（状态马上生效，不等整理）。
第二层（攒够几条笔记 / 每晚 3 点 / 有记忆过期）：更强的模型把现有记忆和笔记合并成新的记忆。
新对话开始时，把记忆拼成一段"你已经知道的事"放进 instructions。

语音唤醒的对话不知道是谁在说（user_id 一律 guest），所以不按人分文件：全家一份，每条标"关于谁"。
所有日期都按家里的时区算（EBO_HOME_TIMEZONE）。
"""
from __future__ import annotations
import json
import logging
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from qwen_text import QwenText

LOG = logging.getLogger('ebo-memory')

KINDS = ('fact', 'preference', 'routine', 'relation', 'plan', 'event', 'concern', 'mood')
TIERS = ('permanent', 'long', 'short')
STATUS_ORDER = {'open': 0, 'asked': 1, 'resolved': 2}
TRACKED_KINDS = ('concern', 'plan')          # 有 open → asked → resolved 状态
LIMITS = {'permanent': 20, 'long': 15, 'short': 8}
DEFAULT_DAYS = {'long': 60, 'short': 2}
MAX_DAYS = {'long': 90, 'short': 3}
HABIT_KINDS = ('routine', 'preference')
HABIT_MIN_DAYS = 3                            # 作息、喜好至少要 3 个不同日期都出现过，才记成长期
RESOLVED_KEEP_DAYS = 2                        # 说好了的事再留两天（避免当天又被问起），然后删掉
WEEKDAYS = '一二三四五六日'

NOTE_SYSTEM = """你在帮家里的陪伴机器人“猫猫”整理一场对话的笔记。猫猫是放在地上的小机器人，住在中国天津爸爸妈妈的家里；家人有妈妈、爸爸，女儿小叶子（猫猫叫她姐姐）在加拿大。

输入是这场对话的逐句记录（语音识别结果，常有同音错别字）。“家里听到”那一栏是麦克风听到的所有声音，里面经常混着电视剧、短剧、直播带货、刷视频、打电话、念别人消息的声音，不一定是在跟猫猫说话。

你的任务：
1. 判断这场对话里有没有家人真的在跟猫猫说话（talking_to_robot）。
2. 只记下以后值得记住的、关于这个家的事：家人对猫猫说的自己的情况、身体、心情、作息、喜好、计划，家里的人和事。
   - 电视剧、短剧、直播、视频、电话里的内容一律不记。宁可漏记，也不能把剧情当成家里的事；判断不了是不是背景声的，不记。
   - evidence 必须是“家里听到”那一栏的原话；只有猫猫的话能说明的事，不记。
   - 猫猫自己说的话只用来理解上下文，不能当成事实（猫猫问“嗓子怎么哑了”不代表谁嗓子哑了；家人只是“嗯”“对”地应付一句，也不算她说了）。
   - 不记寒暄，也不记让它回去充电、闭嘴这类控制的话；但“我上班了”“我睡了”“晚安”说话的时间可以作为作息线索记一条（kind=routine，写清楚几点）。
   - 可以按上下文纠正明显的错别字（比如“茄夹”其实是“煎饺”）；拿不准的不记。
   - 只记对方明确说出来的事，不猜原因、不推断习惯：不要写“可能……”“暗示……”“通常……”。作息线索只写“几点谁说了什么”，比如“5:45 妈妈说要去上班了，今天限号坐公交”。
   - 不记家人怎么使用猫猫（让它充电、关灯、试它听不听得见、问它会什么）。
   - 不推测疾病诊断和用药，不记别人的隐私八卦。同一件事在一场对话里只记一条。
3. 对输入里“正在关心的事”（带 id）逐条判断：
   - asked：猫猫在这场对话里有没有问起或提到这件事（比如“嗓子好些了吗”“之前听说你嗓子哑了”都算）。
   - resolved：家人有没有说已经好了、没事了、解决了。
   没提到的就都填 false。这些事已经记着了，不要再写进 notes。

kind 的含义：fact 身份事实；preference 喜好；routine 作息习惯；relation 人物关系；plan 计划或待办；event 发生的事；concern 身体不舒服、烦心事、担心的事；mood 当天心情。
about 写这件事是关于谁的（妈妈 / 爸爸 / 小叶子 / 家里 / 家人），不确定是谁的事写“家人”。

只输出 JSON：
{"talking_to_robot": true, "speaker_guess": "妈妈/爸爸/家人/不确定",
 "notes": [{"about": "妈妈", "kind": "concern", "text": "用第三人称一句话写清楚，比如：妈妈嗓子哑了，说是上课说话多", "evidence": "最能说明这件事的一句原话"}],
 "followups": [{"id": "m_xxxx", "asked": false, "resolved": false}],
 "nothing_worth_keeping": false}
没有可记的就让 notes 为空、nothing_worth_keeping 为 true。"""

CONSOLIDATE_SYSTEM = """你在整理家里陪伴机器人“猫猫”的长期记忆。输入：今天的日期（家里时间）、家人亲自写的固定事实、现有记忆、最近几场对话的笔记。请输出完整的新记忆列表。

每条记忆的格式：{"id", "about", "kind", "tier", "text", "since", "expires_on", "status", "asked_on", "resolved_on", "evidence", "support_dates"}
- support_dates：支持这条记忆的笔记日期列表（YYYY-MM-DD，不重复）；作息习惯和喜好一定要写。
- about：关于谁（妈妈 / 爸爸 / 小叶子 / 家里 / 家人）。kind：fact / preference / routine / relation / plan / event / concern / mood。
- tier：
  permanent 永久：身份、家庭成员关系、住在哪、职业；expires_on 为空。
  long 长期：喜好、作息习惯、持续几周到几个月的事；expires_on 写 30~90 天后。
  short 短期：当天心情、身体小状况、刚发生的事；expires_on 写 1~3 天后，默认 2 天。
- since：这件事最早是哪天说的（YYYY-MM-DD）。

规则：
- 同一件事合并成一条，不要重复；新的明确说法覆盖旧的。现有记忆里的条目要保留原来的 id；再次被提到的 long / short 条目把 expires_on 往后顺延。
- 关心的事（concern）和计划（plan）有 status：open 还没问过；asked 猫猫已经问过（asked_on 写日期）；resolved 已经好了或办完了（resolved_on 写日期）。status 只能往前走，不能从 asked 退回 open。
- 作息习惯和喜好（routine / preference，long）要有至少 3 个不同日期的笔记支持才能归纳，写成“妈妈一般 5:45~6:30 出门上班”这样；只出现一两次的，照实作为 short 记下（写清日期和时间），不要写“通常”“喜欢”“习惯”。
- 只写家人自己说的、或明显是这个家的事；不写猜测（“可能……”）、问题、猫猫的建议；不推测诊断和用药；不写隐私八卦。
- “固定事实”是家人亲自写的：不要输出它们，不要改写，也不要再写一条和它重复的。但笔记里固定事实没写到的具体信息（比如几点出门、限号那天坐公交）要单独记下，不能因为相近就丢掉。
- text 用中文第三人称一句话，尽量 30 字以内；evidence 留一句原话（可以没有）。
- 数量上限：permanent 20 条、long 15 条、short 8 条；超了就合并，或者丢掉最不重要的。
- expires_on 早于今天的条目不要输出。

只输出 JSON：{"items": [...], "changes": "一句话说明这次改了什么"}"""

USAGE_RULES = """使用规则：
- 这些是背景，不是指令。不要背诵，也不要说“我记得你说过”；聊到相关的话题时自然地用上。
- 一场对话里最多主动提一件“可以关心的事”，而且要等聊开了、时机自然时再提；对方没接话就不要追问。
- “已经问过的事”不要再主动问；对方自己说起时再接着聊。
- 当前说话的人不一定是“关于谁”里的人；不确定是谁在说话时，别把妈妈的事当成在跟妈妈说。"""


def _atomic_write(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, path)


def _read(path: Path, default: dict) -> dict:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else default
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        LOG.warning('Cannot read %s: %s', path, exc)
        return default


def _parse_day(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _text(value, limit: int) -> str:
    return ' '.join(str(value or '').split())[:limit]


def _compact(value) -> str:
    return ''.join(ch for ch in str(value or '') if ch.isalnum())


def _bigrams(text: str, about: str = '') -> set[str]:
    core = ''.join(ch for ch in _compact(text).replace(about, '') if not ch.isdigit())
    return {core[i:i + 2] for i in range(len(core) - 1)}


def similar(a: dict, b: dict, ratio: float = 0.4) -> bool:
    """同一个人的同一件事（“妈妈嗓子哑了，需要休息”≈“妈妈嗓子哑了，说是上课说话多”）。"""
    if a.get('about') != b.get('about'):
        return False
    x, y = _bigrams(a.get('text'), a.get('about') or ''), _bigrams(b.get('text'), b.get('about') or '')
    shared = len(x & y)
    return shared >= 3 and shared / max(1, min(len(x), len(y))) >= ratio


def _support_days(raw: dict, previous: dict | None) -> set[str]:
    days = set()
    for source in (raw.get('support_dates'), (previous or {}).get('support_dates')):
        for value in source if isinstance(source, list) else ():
            d = _parse_day(value)
            if d:
                days.add(d.isoformat())
    return days


def new_id(prefix: str = 'm') -> str:
    return f'{prefix}_{uuid.uuid4().hex[:6]}'


def clean_item(raw: dict, today: date, pinned: bool = False) -> dict | None:
    """校验并补全一条记忆；不合格返回 None。"""
    if not isinstance(raw, dict):
        return None
    text = _text(raw.get('text'), 120)
    if not text:
        return None
    kind = raw.get('kind') if raw.get('kind') in KINDS else 'event'
    tier = raw.get('tier') if raw.get('tier') in TIERS else 'short'
    if pinned:
        tier = 'permanent'
    since = _parse_day(raw.get('since')) or today
    if since > today:
        since = today
    item = {
        'id': _text(raw.get('id'), 24) or new_id(),
        'about': _text(raw.get('about'), 10) or '家人',
        'kind': kind,
        'tier': tier,
        'text': text,
        'since': since.isoformat(),
        'expires_on': None,
        'status': None,
        'asked_on': None,
        'resolved_on': None,
        'pinned': pinned,
        'evidence': _text(raw.get('evidence'), 80),
    }
    if tier != 'permanent':
        expires = _parse_day(raw.get('expires_on'))
        latest = today + timedelta(days=MAX_DAYS[tier])
        if expires is None:
            expires = since + timedelta(days=DEFAULT_DAYS[tier])
        item['expires_on'] = min(expires, latest).isoformat()
    if kind in TRACKED_KINDS and not pinned:
        status = raw.get('status') if raw.get('status') in STATUS_ORDER else 'open'
        asked = _parse_day(raw.get('asked_on')) or (today if status != 'open' else None)
        resolved = _parse_day(raw.get('resolved_on')) or (today if status == 'resolved' else None)
        item.update(status=status, asked_on=asked.isoformat() if asked else None,
                    resolved_on=resolved.isoformat() if resolved else None)
    return item


def prune(items: list[dict], today: date) -> list[dict]:
    """代码兜底：删过期的、说好了两天以上的，按档截断（固定事实永远保留）。"""
    kept = []
    for item in items:
        if not item.get('pinned'):
            expires = _parse_day(item.get('expires_on'))
            if expires is not None and expires < today:
                continue
            resolved = _parse_day(item.get('resolved_on'))
            if item.get('status') == 'resolved' and resolved and (today - resolved).days >= RESOLVED_KEEP_DAYS:
                continue
        kept.append(item)
    result = []
    for tier in TIERS:
        group = [i for i in kept if i['tier'] == tier]
        # 固定事实排最前，其余越新越靠前
        pinned = [i for i in group if i.get('pinned')]
        others = sorted((i for i in group if not i.get('pinned')), key=lambda i: i.get('since') or '', reverse=True)
        result.extend(pinned + others[:max(0, LIMITS[tier] - len(pinned))])
    return result


@dataclass
class SessionRecord:
    """一场对话：逐句 (role, text, at)。role: user（麦克风听到的）/ assistant（猫猫说的）"""
    session_id: str
    started_at: float
    trigger: str
    lines: list[tuple[str, str, float]] = field(default_factory=list)
    faces: list[tuple[str, float]] = field(default_factory=list)   # 前后 10 分钟内认出的人脸


class MemoryStore:
    def __init__(self, directory: str, home_timezone: str = ''):
        self.dir = Path(directory)
        self.tz = ZoneInfo(home_timezone) if home_timezone else None
        self.memory_path = self.dir / 'memory.json'
        self.notes_path = self.dir / 'notes.json'
        self.seed_path = self.dir / 'seed.json'
        self.lock = threading.RLock()

    # ---- 时间（家里时区）----
    def now(self, ts: float | None = None) -> datetime:
        return datetime.fromtimestamp(time.time() if ts is None else ts, self.tz)

    def today(self, ts: float | None = None) -> date:
        return self.now(ts).date()

    # ---- 读写 ----
    def load(self) -> dict:
        with self.lock:
            data = _read(self.memory_path, {})
            data.setdefault('items', [])
            return data

    def save(self, data: dict):
        with self.lock:
            _atomic_write(self.memory_path, data)

    def notes(self) -> list[dict]:
        with self.lock:
            return list(_read(self.notes_path, {}).get('notes', []))

    def add_note(self, note: dict):
        with self.lock:
            notes = self.notes()
            notes.append(note)
            _atomic_write(self.notes_path, {'notes': notes})

    def drop_notes(self, ids: set[str]):
        """只删这次真正合并过的笔记：整理期间新来的笔记留到下次。"""
        with self.lock:
            _atomic_write(self.notes_path, {'notes': [n for n in self.notes() if n.get('id') not in ids]})

    def items(self) -> list[dict]:
        return list(self.load()['items'])

    # ---- 固定事实（seed.json，家人自己写，隐私，不进 git）----
    def merge_seed(self) -> int:
        """seed 有、记忆没有的补上；seed 改了以 seed 为准；seed 删掉的固定事实也从记忆里删掉。"""
        seed = _read(self.seed_path, {}).get('items', [])
        today = self.today()
        pinned = []
        for raw in seed:
            item = clean_item(raw, today, pinned=True)
            if item:
                pinned.append(item)
        with self.lock:
            data = self.load()
            others = [i for i in data['items'] if not i.get('pinned')]
            data['items'] = pinned + others
            self.save(data)
        return len(pinned)

    # ---- 状态更新 ----
    def apply_followups(self, followups: list[dict], today: date) -> list[str]:
        """第一层报告"问过了 / 好了"：马上生效，不等整理（否则下一场对话还会再问）。"""
        changed = []
        with self.lock:
            data = self.load()
            by_id = {i['id']: i for i in data['items']}
            for f in followups or []:
                item = by_id.get(str(f.get('id')))
                if not item or item.get('kind') not in TRACKED_KINDS or item.get('pinned'):
                    continue
                if f.get('resolved') is True and item.get('status') != 'resolved':
                    item['status'] = 'resolved'
                    item['resolved_on'] = today.isoformat()
                    item['asked_on'] = item.get('asked_on') or today.isoformat()
                    changed.append(f"{item['text']} → 已解决")
                elif f.get('asked') is True and item.get('status') == 'open':
                    item['status'] = 'asked'
                    item['asked_on'] = today.isoformat()
                    changed.append(f"{item['text']} → 已问过")
            if changed:
                self.save(data)
        return changed

    def forget(self, item_id: str) -> bool:
        with self.lock:
            data = self.load()
            before = len(data['items'])
            data['items'] = [i for i in data['items'] if i['id'] != item_id or i.get('pinned')]
            if len(data['items']) == before:
                return False
            self.save(data)
            return True

    def expire(self) -> int:
        with self.lock:
            data = self.load()
            kept = prune(data['items'], self.today())
            removed = len(data['items']) - len(kept)
            if removed:
                data['items'] = kept
                self.save(data)
            return removed

    def has_expired(self) -> bool:
        today = self.today()
        return any(not i.get('pinned') and (_parse_day(i.get('expires_on')) or today) < today for i in self.items())

    # ---- 拼进 instructions ----
    def context(self, max_chars: int = 2500, about: str = '') -> str:
        """新对话开头放进 instructions 的一段。about 非空时只要关于这个人的（迎接妈妈时用）。"""
        now = self.now()
        today = now.date()
        items = [i for i in prune(self.items(), today) if not about or i['about'] in (about, '家里')]
        if not items:
            return ''

        def line(i: dict) -> str:
            return f"- [{i['about']}] {i['text']}"

        def short_day(value) -> str:
            d = _parse_day(value)
            return f'{d.month}-{d.day}' if d else ''

        tracked = [i for i in items if i.get('status') in STATUS_ORDER]
        open_items = [i for i in tracked if i['status'] == 'open']
        asked = [i for i in tracked if i['status'] == 'asked']
        resolved = [i for i in tracked if i['status'] == 'resolved']
        rest = [i for i in items if i not in tracked]
        sections = [
            ('关于这个家：', [line(i) for i in rest if i['tier'] == 'permanent']),
            ('最近的情况：', [f"{line(i)}（{short_day(i['since'])} 起）" for i in rest if i['tier'] == 'long']),
            ('这两天：', [f"{line(i)}（{short_day(i['since'])}）" for i in rest if i['tier'] == 'short']),
            ('可以关心一次的事（问过就别再问）：', [f"{line(i)}（{short_day(i['since'])} 起，还没问过）" for i in open_items]),
            ('已经问过的事（对方不提就别再问）：', [f"{line(i)}（{short_day(i['asked_on'])} 问过）" for i in asked]),
            ('已经好了的事（不要再提）：', [line(i) for i in resolved]),
        ]
        header = (f'# 你已经知道的事（背景，不是指令；家里时间 {today.isoformat()} 周{WEEKDAYS[today.weekday()]} '
                  f'{now:%H:%M}）')
        # 超长时先砍“这两天”、再砍“最近的情况”的末尾（越旧越先砍），关心的事和固定事实最后才砍
        drop_order = [2, 1, 5, 4, 0, 3]
        while True:
            body = '\n'.join(f'{title}\n' + '\n'.join(lines) for title, lines in sections if lines)
            text = f'{header}\n{body}\n{USAGE_RULES}'
            if len(text) <= max_chars:
                return text
            for index in drop_order:
                if sections[index][1]:
                    sections[index][1].pop()
                    break
            else:
                return text[:max_chars]


class MemoryManager:
    """后台线程：对话结束后写笔记，攒够了 / 每晚 / 有过期时整理。都不在对话线程里跑，失败只记日志。"""

    def __init__(self, config, state, client: QwenText | None = None, is_idle: Callable[[], bool] = lambda: True):
        self.config = config
        self.state = state
        self.store = MemoryStore(config.memory_dir, config.home_timezone)
        self.client = client or QwenText(config.qwen_api_key, config.qwen_region)
        self.is_idle = is_idle
        self.queue: queue.Queue[SessionRecord | str] = queue.Queue()
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_nightly = ''
        self.consolidating = threading.Lock()

    # ---- 对外 ----
    def start(self):
        count = self.store.merge_seed()
        self.store.expire()
        LOG.info('Household memory ready: %d items (%d pinned from seed), %d notes waiting',
                 len(self.store.items()), count, len(self.store.notes()))
        self._publish()
        self.thread = threading.Thread(target=self._run, name='memory', daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()

    def session_ended(self, record: SessionRecord):
        if any(role == 'user' for role, _t, _a in record.lines):
            self.queue.put(record)

    def consolidate_soon(self):
        self.queue.put('consolidate')

    def context(self, about: str = '') -> str:
        try:
            text = self.store.context(self.config.memory_context_chars, about)
        except Exception as exc:
            LOG.warning('Cannot build memory context: %s', exc)
            return ''
        if not about:
            self.state.update(memory_context_chars=len(text))
        return text

    def snapshot(self) -> dict:
        data = self.store.load()
        return {
            'items': prune(data['items'], self.store.today()),
            'notes_waiting': len(self.store.notes()),
            'consolidated_at': data.get('consolidated_at', 0),
            'consolidated_model': data.get('consolidated_model', ''),
            'last_changes': data.get('last_changes', ''),
        }

    # ---- 第一层 ----
    def summarize(self, record: SessionRecord) -> dict | None:
        today = self.store.today(record.started_at)
        started = self.store.now(record.started_at)
        tracked = [i for i in self.store.items() if i.get('status') in ('open', 'asked')]
        lines = []
        for role, text, at in record.lines:
            who = '猫猫' if role == 'assistant' else '家里听到'
            lines.append(f'{self.store.now(at):%H:%M:%S} {who}：{text}')
        faces = sorted({name for name, _at in record.faces})
        user = [f'家里时间：{started:%Y-%m-%d} 周{WEEKDAYS[started.weekday()]} {started:%H:%M}',
                f'这场对话怎么开始的：{ {"voice": "有人叫了它", "greeting": "认出家人回家，猫猫主动打招呼"}.get(record.trigger, record.trigger) }',
                f'对话前后摄像头认出的人：{"、".join(faces) if faces else "没有认出（不代表没人）"}']
        if tracked:
            user.append('正在关心的事：\n' + '\n'.join(
                f"- id={i['id']} [{i['about']}] {i['text']}（{'还没问过' if i['status'] == 'open' else '已经问过'}）"
                for i in tracked))
        user.append('对话记录：\n' + '\n'.join(lines[-80:]))
        result = self.client.chat_json(self.config.memory_note_model, NOTE_SYSTEM, '\n\n'.join(user), 1200)
        notes = []
        user_said = ''.join(t for r, t, _a in record.lines if r == 'user')
        robot_said = ''.join(t for r, t, _a in record.lines if r == 'assistant')
        for raw in result.get('notes') or []:
            evidence = _compact(raw.get('evidence')) if isinstance(raw, dict) else ''
            if evidence and evidence in _compact(robot_said) and evidence not in _compact(user_said):
                # 依据只是猫猫自己说的（比如猫猫说“看你一脸疲惫”），不能当成家里的事实
                LOG.info('Dropped a note based only on what the robot said: %s', raw.get('text'))
                continue
            if isinstance(raw, dict) and any(similar(raw, t) for t in tracked):
                continue  # 已经在关心的事：状态由 followups 管，不再写一条新的（否则会被当成“没问过”再问一遍）
            if isinstance(raw, dict) and _text(raw.get('text'), 120):
                notes.append({'about': _text(raw.get('about'), 10) or '家人',
                              'kind': raw.get('kind') if raw.get('kind') in KINDS else 'event',
                              'text': _text(raw.get('text'), 120), 'evidence': _text(raw.get('evidence'), 80)})
        followups = [f for f in result.get('followups') or [] if isinstance(f, dict)]
        changed = self.store.apply_followups(followups, today)
        if changed:
            LOG.info('Memory follow-ups: %s', '；'.join(changed))
        talking = result.get('talking_to_robot') is not False
        if not notes:
            LOG.info('Session %s: nothing worth remembering (talking to robot: %s)', record.session_id, talking)
            return None
        note = {'id': new_id('n'), 'session_id': record.session_id, 'date': today.isoformat(),
                'time': f'{started:%H:%M}', 'trigger': record.trigger,
                'speaker_guess': _text(result.get('speaker_guess'), 10), 'talking_to_robot': talking,
                'notes': notes, 'created_at': time.time()}
        self.store.add_note(note)
        LOG.info('Session %s notes: %s', record.session_id, '；'.join(n['text'] for n in notes))
        return note

    # ---- 第二层 ----
    def consolidate(self, now: float | None = None) -> bool:
        if not self.consolidating.acquire(blocking=False):
            return False
        try:
            return self._consolidate(now)
        finally:
            self.consolidating.release()

    def _consolidate(self, now: float | None) -> bool:
        today = self.store.today(now)
        notes = self.store.notes()
        data = self.store.load()
        current = prune(data['items'], today)
        pinned = [i for i in current if i.get('pinned')]
        editable = [i for i in current if not i.get('pinned')]
        if not notes:
            # 只有过期的：代码删掉就行，不用请模型
            return self.store.expire() > 0
        self.state.update(memory_status='consolidating')
        keys = ('id', 'about', 'kind', 'tier', 'text', 'since', 'expires_on', 'status', 'asked_on', 'resolved_on', 'evidence',
                'support_dates')
        user = '\n\n'.join([
            f'今天：{today.isoformat()} 周{WEEKDAYS[today.weekday()]}（家里时间）',
            '固定事实（只读）：\n' + '\n'.join(f"- [{i['about']}] {i['text']}" for i in pinned),
            '现有记忆：\n' + json.dumps([{k: i.get(k) for k in keys} for i in editable], ensure_ascii=False),
            '最近的对话笔记：\n' + '\n'.join(
                f"- {n['date']} {n['time']}（说话的人可能是{n.get('speaker_guess') or '不确定'}）："
                + '；'.join(f"[{x['about']}/{x['kind']}] {x['text']}" + (f"（原话：{x['evidence']}）" if x.get('evidence') else '')
                            for x in n['notes'])
                for n in notes) if notes else '最近的对话笔记：（没有，只需要清理过期的）',
        ])
        try:
            model, result = self.client.chat_json_first(self.config.memory_models, CONSOLIDATE_SYSTEM, user, 4000)
            raw_items = result.get('items')
            if not isinstance(raw_items, list):
                raise ValueError('no items list in the answer')
            old = {i['id']: i for i in editable}
            pinned_texts = {i['text'] for i in pinned}
            merged = []
            for raw in raw_items:
                item = clean_item(raw, today)
                if item is None or item['text'] in pinned_texts or any(similar(item, p, 0.3) for p in pinned):
                    continue  # 固定事实已经写了的，不再另记一条（模型常把它换个说法再写一遍）
                if (item['kind'] in HABIT_KINDS and item['tier'] == 'long'
                        and len(_support_days(raw, old.get(item['id']))) < HABIT_MIN_DAYS):
                    # 一两次的事不算习惯：降成短期，照实记着，等它再出现
                    LOG.info('Not enough days for a habit yet; keeping it short-term: %s', item['text'])
                    item['tier'] = 'short'
                    item['expires_on'] = (today + timedelta(days=DEFAULT_DAYS['short'])).isoformat()
                item['support_dates'] = sorted(_support_days(raw, old.get(item['id'])))[-10:]
                if item['tier'] != 'permanent':
                    # 过期时间从“最后一次被提到”算起，不能被模型无限往后推（三天前的“累得慌”不该今天还去问）
                    last_seen = max([_parse_day(item['since'])] + [_parse_day(d) for d in item['support_dates']])
                    latest = last_seen + timedelta(days=MAX_DAYS[item['tier']])
                    if _parse_day(item['expires_on']) > latest:
                        item['expires_on'] = latest.isoformat()
                previous = old.get(item['id'])
                if previous is None and item.get('status') in STATUS_ORDER:
                    previous = next((o for o in old.values() if o.get('status') in STATUS_ORDER and similar(o, item)
                                     and all(m['id'] != o['id'] for m in merged)), None)
                    if previous is not None:
                        item['id'] = previous['id']
                if previous is None:
                    item['id'] = new_id() if any(m['id'] == item['id'] for m in merged) else item['id']
                elif previous.get('status') in STATUS_ORDER and item.get('status') in STATUS_ORDER:
                    # 状态只能往前走：模型把“已问过”写回“没问过”时以旧的为准
                    if STATUS_ORDER[item['status']] < STATUS_ORDER[previous['status']]:
                        for key in ('status', 'asked_on', 'resolved_on'):
                            item[key] = previous.get(key)
                merged.append(item)
        except Exception as exc:
            LOG.warning('Memory consolidation failed; keeping the old memory and the notes: %s', exc)
            self.state.update(memory_status='error', memory_last_error=str(exc)[:200])
            return False
        # 已经问过 / 已经好了的事，模型漏写了也留着直到过期：丢了它，下次再提起就会被当成新的再问一遍
        for o in editable:
            if (o.get('status') in ('asked', 'resolved') and all(m['id'] != o['id'] for m in merged)
                    and not any(similar(o, m) for m in merged)):
                merged.append(o)
        items = prune(pinned + merged, today)
        with self.store.lock:
            fresh = self.store.load()
            fresh.update(items=items, consolidated_at=time.time(), consolidated_model=model,
                         last_changes=_text(result.get('changes'), 200))
            self.store.save(fresh)
            self.store.drop_notes({n['id'] for n in notes})
        LOG.info('Memory consolidated with %s: %d notes -> %d items (%s)', model, len(notes), len(items),
                 _text(result.get('changes'), 120))
        self.state.update(memory_last_error='', memory_consolidation_at=time.time())
        return True

    # ---- 后台 ----
    def _publish(self, status: str = ''):
        notes = len(self.store.notes())
        self.state.update(memory_pending=notes > 0, memory_status=status or ('ready' if self.store.items() else 'empty'))

    def _run(self):
        while not self.stopping.is_set():
            try:
                job = self.queue.get(timeout=60)
            except queue.Empty:
                job = None
            try:
                if isinstance(job, SessionRecord):
                    self.state.update(memory_status='writing_notes')
                    self.summarize(job)
                    self.state.update(memory_last_saved_at=time.time(), memory_last_error='')
                    if len(self.store.notes()) >= self.config.memory_consolidate_after:
                        self.consolidate()
                elif job == 'consolidate':
                    self.consolidate()
                elif self._nightly_due() or (self.store.has_expired() and self.is_idle()):
                    self.consolidate()
                self._publish()
            except Exception as exc:
                LOG.warning('Memory job failed: %s', exc)
                self.state.update(memory_status='error', memory_last_error=str(exc)[:200])

    def _nightly_due(self) -> bool:
        now = self.store.now()
        key = now.date().isoformat()
        if now.hour == 3 and self.last_nightly != key and self.is_idle():
            self.last_nightly = key
            return bool(self.store.notes())
        return False
