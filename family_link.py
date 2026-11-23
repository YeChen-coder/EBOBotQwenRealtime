"""姐姐在外地通过 MCP（Claude Code → engine/ebo_mcp.py → 这里的 /mcp/* 接口）照看家里。

托问：姐姐说"问问妈妈晚饭吃了没"，就往记忆里加一条"关心的事"（concern，状态 open，3 天有效）。
    新对话开始时它出现在"可以关心一次的事"里，猫猫找机会自然地问一次；对话结束后第一层记忆
    判断问过没有、说好了没有（open → asked → resolved）。这里另记一本账（asks.json）：
    说好了的事两天后会被记忆清掉、每晚整理也可能改写条目，账本里还能查到托过什么。
    查结果时，问过的事附上那天托问之后的对话原文，Claude 读了告诉姐姐妈妈怎么说的。

见到记录：Frigate 没开录像和快照，不存事件历史（实测 /api/events 一条都没有），
    人脸识别结果只通过 MQTT 实时推一次。greeter 每次认出家人就记一行到 sightings.jsonl，
    同一个人一分钟最多记一次，只留 14 天。
"""
from __future__ import annotations
import json
import logging
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from memory import MemoryStore, clean_item, new_id

LOG = logging.getLogger('ebo-family')

ASK_DAYS = 3                 # 托问有效期（记忆里"短期"那一档最多也是 3 天）
ASK_KEEP_DAYS = 14           # 账本留多久
ASK_MAX_CHARS = 80
CONVERSATION_MAX_LINES = 80
SIGHTING_GAP = 60            # 同一个人一分钟最多记一次
SIGHTING_KEEP_DAYS = 14


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    tmp.replace(path)


def _jsonl_since(path: Path, start: float, end: float) -> list[dict]:
    """整份读（对话记录一两 MB，几天的量），只要 received_at 落在 [start, end) 的。"""
    rows = []
    try:
        with path.open(encoding='utf-8', errors='replace') as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                at = row.get('received_at') or 0
                if isinstance(at, (int, float)) and start <= at < end:
                    rows.append(row)
    except OSError:
        pass
    return rows


class FamilyLink:
    def __init__(self, asks_path: str, sightings_path: str, home_timezone: str = '',
                 transcript_path: str = '', assistant_transcript_path: str = ''):
        self.asks_path = Path(asks_path)
        self.sightings_path = Path(sightings_path)
        self.tz = ZoneInfo(home_timezone) if home_timezone else None
        self.transcript_path = Path(transcript_path) if transcript_path else None
        self.assistant_path = Path(assistant_transcript_path) if assistant_transcript_path else None
        self.lock = threading.Lock()
        self.last_sighting: dict[str, float] = {}
        self._prune_sightings()

    # ---- 时间（家里时区）----
    def _now(self, ts: float | None = None) -> datetime:
        return datetime.fromtimestamp(time.time() if ts is None else ts, self.tz)

    def _day_bounds(self, day: date) -> tuple[float, float]:
        start = datetime(day.year, day.month, day.day, tzinfo=self.tz)
        return start.timestamp(), (start + timedelta(days=1)).timestamp()

    def parse_day(self, text: str) -> date:
        text = (text or 'today').strip().lower()
        today = self._now().date()
        if text in ('today', '今天'):
            return today
        if text in ('yesterday', '昨天'):
            return today - timedelta(days=1)
        return date.fromisoformat(text)

    # ---- 托问 ----
    def add_ask(self, store: MemoryStore, question: str, now: float | None = None) -> dict:
        question = ' '.join((question or '').split())[:ASK_MAX_CHARS]
        if not question:
            raise ValueError('empty_question')
        now = time.time() if now is None else now
        today = store.today(now)
        item = clean_item({
            'id': new_id(), 'about': '妈妈', 'kind': 'concern', 'tier': 'short',
            'text': f'姐姐托猫猫问妈妈：{question}',
            'expires_on': (today + timedelta(days=ASK_DAYS)).isoformat(),
            'evidence': '姐姐通过 MCP 托问',
        }, today)
        store.add_item(item)
        entry = {'id': item['id'], 'question': question, 'created_at': now,
                 'expires_on': item['expires_on']}
        with self.lock:
            data = _read_json(self.asks_path, {'asks': []})
            cutoff = now - ASK_KEEP_DAYS * 86400
            data['asks'] = [a for a in data.get('asks', []) if a.get('created_at', 0) >= cutoff] + [entry]
            _write_json(self.asks_path, data)
        LOG.info('Ask from big sister queued for 妈妈 (%s, until %s)', item['id'], item['expires_on'])
        return entry

    def asks(self, store: MemoryStore, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        today = store.today(now)
        items = {i['id']: i for i in store.items()}
        with self.lock:
            entries = list(_read_json(self.asks_path, {'asks': []}).get('asks', []))
        result = []
        for entry in sorted(entries, key=lambda a: a.get('created_at', 0), reverse=True):
            item = items.get(entry.get('id'))
            status = item.get('status') if item else None
            expired = date.fromisoformat(entry['expires_on']) < today
            if status not in ('open', 'asked', 'resolved'):
                # 条目没了：要么过期被清，要么说好了两天后被清，要么每晚整理时被改写
                status = 'expired' if expired else 'gone'
            elif status == 'open' and expired:
                status = 'expired'
            row = {
                'question': entry.get('question', ''),
                'asked_at': self._now(entry.get('created_at')).strftime('%Y-%m-%d %H:%M'),
                'expires_on': entry.get('expires_on'),
                'status': status,
                'robot_asked_on': item.get('asked_on') if item else None,
                'resolved_on': item.get('resolved_on') if item else None,
            }
            if item and status in ('asked', 'resolved') and item.get('asked_on'):
                row['conversation'] = self._conversation_on(
                    date.fromisoformat(item['asked_on']), after=entry.get('created_at', 0))
            result.append(row)
        return result

    def _conversation_on(self, day: date, after: float) -> list[dict]:
        start, end = self._day_bounds(day)
        start = max(start, after)
        lines = []
        if self.transcript_path:
            lines += [('妈妈那边', r) for r in _jsonl_since(self.transcript_path, start, end)]
        if self.assistant_path:
            lines += [('猫猫', r) for r in _jsonl_since(self.assistant_path, start, end)]
        lines.sort(key=lambda pair: pair[1].get('received_at', 0))
        return [{'time': self._now(r.get('received_at')).strftime('%H:%M'), 'who': who,
                 'text': r.get('transcript', '')}
                for who, r in lines[:CONVERSATION_MAX_LINES] if r.get('transcript')]

    # ---- 见到记录 ----
    def record_sighting(self, name: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self.lock:
            if now - self.last_sighting.get(name, -1e18) < SIGHTING_GAP:
                return False
            self.last_sighting[name] = now
            try:
                self.sightings_path.parent.mkdir(parents=True, exist_ok=True)
                with self.sightings_path.open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps({'name': name, 'at': round(now, 1)}, ensure_ascii=False) + '\n')
            except OSError as exc:
                LOG.warning('Cannot record sighting: %s', exc)
                return False
        return True

    def _sightings(self) -> list[dict]:
        rows = []
        try:
            with self.sightings_path.open(encoding='utf-8') as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row.get('at'), (int, float)) and isinstance(row.get('name'), str):
                        rows.append(row)
        except OSError:
            pass
        return rows

    def _prune_sightings(self):
        cutoff = time.time() - SIGHTING_KEEP_DAYS * 86400
        rows = self._sightings()
        kept = [r for r in rows if r['at'] >= cutoff]
        if len(kept) == len(rows):
            return
        try:
            tmp = self.sightings_path.with_suffix('.tmp')
            tmp.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in kept), encoding='utf-8')
            tmp.replace(self.sightings_path)
        except OSError as exc:
            LOG.warning('Cannot prune sightings: %s', exc)

    def seen(self, name: str, day: date) -> dict:
        start, end = self._day_bounds(day)
        everyone = self._sightings()
        rows = [r for r in everyone if r['name'] == name]
        today_rows = [r for r in rows if start <= r['at'] < end]
        hours: dict[str, int] = {}
        for r in today_rows:
            hour = f"{self._now(r['at']).hour:02d}"
            hours[hour] = hours.get(hour, 0) + 1
        result = {'name': name, 'day': day.isoformat(), 'times_seen': len(today_rows),
                  'by_hour': hours, 'first': None, 'last': None, 'last_seen_before': None,
                  'known_names': sorted({r['name'] for r in everyone}),
                  'recording_since': (self._now(min(r['at'] for r in everyone)).strftime('%Y-%m-%d %H:%M')
                                      if everyone else None)}
        if today_rows:
            result['first'] = self._now(today_rows[0]['at']).strftime('%H:%M')
            result['last'] = self._now(today_rows[-1]['at']).strftime('%H:%M')
        earlier = [r for r in rows if r['at'] < start]
        if earlier:
            result['last_seen_before'] = self._now(earlier[-1]['at']).strftime('%Y-%m-%d %H:%M')
        return result
