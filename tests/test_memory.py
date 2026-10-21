import json, sys, tempfile, time, types
from datetime import date, timedelta
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))
import memory
from memory import MemoryStore, MemoryManager, SessionRecord, clean_item, prune

class State:
    def __init__(s): s.d = {}
    def update(s, **k): s.d.update(k)

class FakeClient:
    def __init__(s): s.note = {}; s.items = None; s.fail = False; s.calls = []
    def chat_json(s, model, system, user, max_tokens=0):
        s.calls.append(('note', model, user)); return s.note
    def chat_json_first(s, models, system, user, max_tokens=0):
        s.calls.append(('cons', models, user))
        if s.fail: raise RuntimeError('boom')
        return models[-1], {'items': s.items, 'changes': 'test'}

def manager(tmp):
    cfg = types.SimpleNamespace(memory_dir=tmp, home_timezone='Asia/Shanghai', qwen_api_key='', qwen_region='cn',
                                memory_note_model='qwen-plus', memory_models=('qwen3-max', 'qwen-max'),
                                memory_consolidate_after=4, memory_context_chars=2500)
    c = FakeClient(); return MemoryManager(cfg, State(), c), c

today = date(2026, 10, 21)
# clean_item：默认过期、上限、未来日期
i = clean_item({'text': '妈妈嗓子哑了', 'kind': 'concern', 'tier': 'short', 'since': '2026-10-21'}, today)
assert i['expires_on'] == '2026-10-23' and i['status'] == 'open', i
i = clean_item({'text': 'x', 'tier': 'long', 'expires_on': '2027-12-01'}, today)
assert i['expires_on'] == (today + timedelta(days=90)).isoformat()
assert clean_item({'text': ''}, today) is None
# prune：过期删、resolved 两天后删、pinned 保留、上限
items = [clean_item({'text': f's{n}', 'tier': 'short', 'since': '2026-10-21'}, today) for n in range(12)]
items.append(dict(clean_item({'text': 'old', 'tier': 'short', 'since': '2026-10-15', 'expires_on': '2026-10-17'}, date(2026,10,15))))
items.append(clean_item({'text': 'done', 'kind': 'concern', 'status': 'resolved', 'resolved_on': '2026-10-18', 'since': '2026-10-17'}, today))
items.append(clean_item({'text': 'pin', 'expires_on': '2000-01-01'}, today, pinned=True))
kept = prune(items, today)
texts = {k['text'] for k in kept}
assert 'old' not in texts and 'done' not in texts and 'pin' in texts
assert sum(1 for k in kept if k['tier'] == 'short') == 8

with tempfile.TemporaryDirectory() as tmp:
    m, c = manager(tmp)
    # 时区：多伦多 2026-10-21 10:00 = 天津 22:00 同一天；多伦多 20:00 = 天津次日 08:00
    ts = 1792591200  # 2026-10-21 00:00 UTC +- ；直接比对
    assert m.store.today(ts).isoformat() == time.strftime('%Y-%m-%d', time.gmtime(ts + 8 * 3600))
    # seed 合并
    json.dump({'items': [{'about': '妈妈', 'kind': 'fact', 'text': '妈妈是九年级化学老师'}]}, open(f'{tmp}/seed.json', 'w', encoding='utf-8'), ensure_ascii=False)
    assert m.store.merge_seed() == 1
    # 第一层：笔记 + followups
    data = m.store.load(); concern = clean_item({'id': 'm_c1', 'text': '妈妈嗓子哑了', 'kind': 'concern', 'tier': 'short'}, m.store.today())
    data['items'].append(concern); m.store.save(data)
    c.note = {'talking_to_robot': True, 'speaker_guess': '妈妈',
              'notes': [{'about': '妈妈', 'kind': 'routine', 'text': '妈妈说今天限号，坐公交上班', 'evidence': '今天限号'}],
              'followups': [{'id': 'm_c1', 'asked': True, 'resolved': False}]}
    rec = SessionRecord('s1', time.time(), 'voice', [('assistant', '嗓子好些了吗', time.time()), ('user', '今天限号', time.time())])
    assert m.summarize(rec)
    st = {i['id']: i for i in m.store.items()}['m_c1']
    assert st['status'] == 'asked' and st['asked_on'] == m.store.today().isoformat()
    assert 'id=m_c1' in c.calls[-1][2]
    ctx = m.context()
    assert '已经问过的事' in ctx and '妈妈是九年级化学老师' in ctx and '可以关心一次' not in ctx, ctx
    # 第二层：pinned 不被改、状态不回退、失败不覆盖
    pin_id = [i for i in m.store.items() if i['pinned']][0]['id']
    c.items = [{'id': 'm_c1', 'about': '妈妈', 'kind': 'concern', 'tier': 'short', 'text': '妈妈嗓子哑了', 'status': 'open'},
               {'id': pin_id, 'about': '妈妈', 'kind': 'fact', 'tier': 'permanent', 'text': '妈妈是九年级化学老师'},
               {'about': '家里', 'kind': 'routine', 'tier': 'long', 'text': '家里车有限号日，那天妈妈坐公交',
                'support_dates': ['2026-10-14', '2026-10-17', '2026-10-20']},
               {'about': '妈妈', 'kind': 'preference', 'tier': 'long', 'text': '妈妈喜欢吃煎饺', 'support_dates': ['2026-10-18']}]
    before = open(f'{tmp}/memory.json', encoding='utf-8').read()
    c.fail = True
    assert not m.consolidate()
    assert open(f'{tmp}/memory.json', encoding='utf-8').read() == before and len(m.store.notes()) == 1
    c.fail = False
    assert m.consolidate()
    items = m.store.items()
    assert sum(1 for i in items if i['text'] == '妈妈是九年级化学老师') == 1
    assert [i for i in items if i['id'] == 'm_c1'][0]['status'] == 'asked'
    assert any('限号' in i['text'] and i['tier'] == 'long' for i in items)
    assert [i for i in items if i['text'] == '妈妈喜欢吃煎饺'][0]['tier'] == 'short'   # 一次不算习惯
    assert m.store.notes() == []
    # 整理时新写了一条相似的关心事项：沿用旧的“已问过”
    from memory import similar
    assert similar({'about': '妈妈', 'text': '妈妈嗓子哑了，需要休息'}, {'about': '妈妈', 'text': '妈妈嗓子哑了，可能是上课说话多导致的'})
    assert not similar({'about': '妈妈', 'text': '妈妈很累'}, {'about': '妈妈', 'text': '妈妈很开心'})
    c.items = [{'about': '妈妈', 'kind': 'concern', 'tier': 'short', 'text': '2026-10-21 妈妈嗓子哑了，需要休息', 'status': 'open'}]
    m.store.add_note({'id': 'n_x', 'date': '2026-10-21', 'time': '06:00', 'notes': [{'about': '妈妈', 'kind': 'event', 'text': 'x'}]})
    assert m.consolidate()
    again = [i for i in m.store.items() if not i['pinned']]
    assert len(again) == 1 and again[0]['id'] == 'm_c1' and again[0]['status'] == 'asked', again
    # 第一层：依据只在猫猫的话里 -> 丢掉
    c.note = {'notes': [{'about': '妈妈', 'kind': 'concern', 'text': '妈妈很疲惫', 'evidence': '看你一脸疲惫'}], 'followups': []}
    rec = SessionRecord('s2', time.time(), 'voice', [('assistant', '看你一脸疲惫，今天累坏了吧', time.time()), ('user', '嗯', time.time())])
    assert m.summarize(rec) is None
    # forget：pinned 不能删
    assert not m.store.forget(pin_id) and m.store.forget('m_c1')
    # 上下文长度上限
    data = m.store.load()
    data['items'] += [clean_item({'text': '很长的一件事' * 15, 'tier': 'long'}, m.store.today()) for _ in range(15)]
    m.store.save(data)
    assert len(m.store.context(800)) <= 800
print('all memory tests passed')
