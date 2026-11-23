"""姐姐的 MCP：托问（记忆里的关心的事）和见到记录。运行：python tests/test_family_link.py"""
import json, sys, tempfile, time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from family_link import FamilyLink, SIGHTING_GAP
from memory import MemoryStore

TZ = 'Asia/Shanghai'


def at(day: str, hm: str) -> float:
    return datetime.fromisoformat(f'{day} {hm}').replace(tzinfo=ZoneInfo(TZ)).timestamp()


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    store = MemoryStore(str(tmp / 'memory'), TZ)
    user_t, bot_t = tmp / 'transcripts.jsonl', tmp / 'assistant_outputs.jsonl'
    link = FamilyLink(str(tmp / 'memory' / 'asks.json'), str(tmp / 'sightings.jsonl'), TZ, str(user_t), str(bot_t))

    # ---- 托问：进记忆，成为"可以关心一次的事" ----
    now = time.time()
    entry = link.add_ask(store, '  晚饭吃了 什么 ')
    assert entry['question'] == '晚饭吃了 什么'
    item = [i for i in store.items() if i['id'] == entry['id']][0]
    assert item['kind'] == 'concern' and item['status'] == 'open' and item['about'] == '妈妈'
    assert item['tier'] == 'short' and item['expires_on'] == (store.today() + timedelta(days=3)).isoformat()
    ctx = store.context()
    assert '可以关心一次的事' in ctx and '姐姐托猫猫问妈妈：晚饭吃了 什么' in ctx, ctx
    try:
        link.add_ask(store, '   ')
        raise AssertionError('empty question accepted')
    except ValueError:
        pass
    [row] = link.asks(store)
    assert row['status'] == 'open' and 'conversation' not in row

    # 猫猫问过了：附上托问之后当天的对话（托问之前的不要）
    today = store.today()
    user_t.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in [
        {'received_at': now - 5, 'transcript': '托问之前说的话'},
        {'received_at': now + 60, 'transcript': '吃了，包的饺子'},
    ]) + '\n', encoding='utf-8')
    bot_t.write_text(json.dumps({'received_at': now + 30, 'transcript': '妈妈，姐姐让我问问您晚饭吃了啥？'},
                                ensure_ascii=False) + '\n', encoding='utf-8')
    assert store.apply_followups([{'id': entry['id'], 'asked': True}], today)
    [row] = link.asks(store)
    assert row['status'] == 'asked' and row['robot_asked_on'] == today.isoformat()
    texts = [(c['who'], c['text']) for c in row['conversation']]
    assert texts == [('猫猫', '妈妈，姐姐让我问问您晚饭吃了啥？'), ('妈妈那边', '吃了，包的饺子')], texts
    # 问过的事在上下文里换到"已经问过"那一节
    assert '已经问过的事' in store.context()

    # 条目被每晚整理改没了：账本还在，标成 gone；过了有效期标成 expired
    store.forget(entry['id'])
    assert link.asks(store)[0]['status'] == 'gone'
    later = now + 4 * 86400
    assert link.asks(store, now=later)[0]['status'] == 'expired'
    # 账本只留 14 天
    link.add_ask(store, '腰好点没', now=now + 15 * 86400)
    assert [a['question'] for a in json.loads((tmp / 'memory' / 'asks.json').read_text(encoding='utf-8'))['asks']] == ['腰好点没']

    # ---- 见到记录 ----
    assert link.record_sighting('妈妈', at('2026-11-19', '22:00'))
    t0 = at('2026-11-20', '05:40')
    assert link.record_sighting('妈妈', t0)
    assert not link.record_sighting('妈妈', t0 + SIGHTING_GAP - 1)       # 一分钟内只记一次
    assert link.record_sighting('爸爸', t0 + 5)                          # 别人不受影响
    assert link.record_sighting('妈妈', at('2026-11-20', '21:15'))
    seen = link.seen('妈妈', link.parse_day('2026-11-20'))
    assert seen['times_seen'] == 2 and seen['first'] == '05:40' and seen['last'] == '21:15', seen
    assert seen['by_hour'] == {'05': 1, '21': 1} and seen['last_seen_before'] == '2026-11-19 22:00'
    assert seen['known_names'] == ['妈妈', '爸爸']
    none = link.seen('妈妈', link.parse_day('2026-11-21'))
    assert none['times_seen'] == 0 and none['first'] is None and none['last_seen_before'] == '2026-11-20 21:15'
    assert link.parse_day('昨天') == link.parse_day('today') - timedelta(days=1)
    # 启动时清掉 14 天前的
    old = time.time() - 20 * 86400
    with (tmp / 'sightings.jsonl').open('a', encoding='utf-8') as f:
        f.write(json.dumps({'name': '妈妈', 'at': old}) + '\n')
    FamilyLink(str(tmp / 'memory' / 'asks.json'), str(tmp / 'sightings.jsonl'), TZ)
    assert all(json.loads(l)['at'] > old for l in (tmp / 'sightings.jsonl').read_text(encoding='utf-8').splitlines())

# greeter 认出人脸时记一笔（分数不够的不记）
import types
from greeter import HomecomingGreeter
cfg = types.SimpleNamespace(greet_names='妈妈', greet_hours='11-23', home_timezone=TZ, timers_path='',
                            greet_away_seconds=10800, min_face_score=0.9)
g = HomecomingGreeter(cfg, greet=lambda name: False)
calls = []
g.on_seen = lambda name, now: calls.append(name)
g.observe({'after': {'label': 'person', 'sub_label': ['妈妈', 0.95]}})
g.observe({'after': {'label': 'person', 'sub_label': ['妈妈', 0.5]}})
assert calls == ['妈妈'], calls
print('all family link tests passed')
