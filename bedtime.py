"""睡前陪聊：晚上像小姐妹一样陪妈妈闲聊八卦，不催睡觉。

妈妈睡得晚（12 点、一两点），就睡在客厅沙发上。三件事：
1. 每晚 20:30 准备“今晚谈资”：千问文字模型联网搜最近几天的新鲜事——孙颖莎（她喜欢，顺带磕莎头 CP）、
   网上轻松的热门话题、应季生活小事——过滤掉坏消息和学校/学生/家长（她是老师，晚上不想听工作的事），
   存到 /data/bedtime/日期.json。
2. 陪聊时段（默认 21:30 到 0:30）里，客厅里最近看到过人、又没在聊天，就主动轻轻问一句要不要听八卦。
   每晚只问一次，不要就不再问。
3. 陪聊时段里开始的任何一场对话都带上夜聊的口吻和今晚的谈资：她自己叫醒它也一样。
"""
from __future__ import annotations
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import timers
from qwen_text import QwenText

LOG = logging.getLogger('ebo-bedtime')

WEEKDAYS = '一二三四五六日'
SEEN_RECENTLY = 600                    # 10 分钟内客厅里看到过人，才算她还醒着在沙发上
PREPARE_RETRY = 600

PACK_SYSTEM = """你在给家里的陪伴机器人准备今晚陪{name}睡前闲聊的“谈资”。只用联网搜索到的、最近三天内的内容。
{name}的喜好：喜欢乒乓球运动员孙颖莎；也顺带磕孙颖莎和王楚钦的“莎头”CP（王楚钦只是顺带，主角是孙颖莎）。
准备 3 到 5 条，只输出 JSON：{{"items": [{{"kind": "乒乓|热门|生活", "date": "搜到的报道日期", "topic": "一句话标题", "story": "..."}}]}}
- 乒乓 1~2 条：优先孙颖莎最近的比赛和动态；有莎头同框、混双的可以带上；没有新消息就挑一件她最近的趣事。
- 热门 1~2 条：今天网上真实在传的轻松话题（娱乐八卦、有趣的社会新闻、萌宠之类）。
- 生活 1 条：应季吃的、天气变化、天津本地的新鲜事之类。
- 只有“乒乓”那几条跟孙颖莎有关；其他条老老实实讲那件事本身，不要硬扯孙颖莎、莎头，不要自己加梗。
- 一切以搜索结果为准，搜不到的不要编；凑不够就少写几条。拿不准的细节（谁说了什么、谁转发了）不要写，
  热搜排名、播放量、点赞数这类数字不要写。
- story 两三句，用第三人称客观转述这件事（“孙颖莎在……”），不要写“我今早路过”“我刷到”这种第一人称经历。
- 不要：灾难、事故、犯罪、死亡、疾病和健康恐吓、政治、战争、国际冲突、吵架撕扯的负面新闻；
  不要学校、学生、家长、老师、考试、教育相关的（她是老师，晚上不想听工作的事）。"""

NIGHT_RULES = """
现在是晚上，{name}在客厅沙发上歇着，一天忙完了，她一般十二点到一两点在沙发上睡。这段时间你是陪她晚上闲聊的小伙伴，像小姐妹聊八卦一样轻松：
- 语气放轻、放慢，每次一两句，别长篇大论。
- 可以聊下面“今晚的谈资”（你今天听来的），一次只说一件，看她感不感兴趣：感兴趣就顺着多聊几句、问问她怎么看；
  不感兴趣就换一件，或者听她聊她想聊的。她喜欢孙颖莎、顺带磕莎头 CP，但别句句都是乒乓球。
- 谈资是网上看来的：说的时候用“我听说”“好像是”，不报具体数字和排名；她问细节你不知道，就说不太清楚。
- 不要主动提学校、学生、家长、上课这些工作上的事（就算你记得她工作上的事，晚上也不提）。
  她自己吐槽起学生、家长、学校：只回一句站在她这边的体贴话（比如“这么晚了还为他们闹心，不值当”），
  然后在同一句回答里直接讲一件今晚谈资里还没聊过的事，把话题换走（不要只说“跟你说个好玩的”就停下）。
  绝对不要问是谁、怎么回事、发生了什么，
  不要让她再讲一遍糟心事；她要是还想说，就安静听着、简单附和，不分析、不出主意。
- 不要催她睡觉，不要说“你该睡了”“早点休息”“别熬夜”。她回话越来越短、像是困了，你就少问问题、简单应和，让她安静下来。
- 她说要睡了，就轻轻道声晚安，不再开新话题。
"""

OFFER_PROMPT = (
    '现在是晚上，{name}在客厅沙发上歇着。你主动轻轻问她一句，要不要听你说说今天听来的八卦：'
    '挑今晚谈资里她最可能感兴趣的一件（比如孙颖莎的新消息），只提一句、吊个胃口，别展开。'
    '一两句话就好，语气轻，像小伙伴凑过去说悄悄话。'
    '她要是说不用、不想听、在忙，就说一声“好嘞，那我安静陪着你”，之后别再提、别再问；她想听就从那件开始讲。'
)


def _minutes(text: str) -> int:
    hour, _, minute = text.strip().partition(':')
    return int(hour) * 60 + int(minute or 0)


def parse_window(text: str) -> tuple[int, int]:
    """'21:30-0:30' -> 一天里的分钟数（开始, 结束），可以跨午夜；也认 '23-2'"""
    start, _, end = (text or '21:30-0:30').partition('-')
    return _minutes(start), _minutes(end)


class Bedtime:
    def __init__(self, config, offer: Callable[[str, str], bool],
                 someone_here: Callable[[float], bool] | None = None, client: QwenText | None = None):
        self.config = config
        self.offer = offer   # offer(开场提示, 夜聊说明) -> 是否开口了（忙、安静模式时返回 False）
        self.someone_here = someone_here   # someone_here(秒) -> 这么久内客厅里看到过人；None = 不看
        self.client = client or QwenText(config.qwen_api_key, config.qwen_region, timeout=120)
        self.tz = ZoneInfo(config.home_timezone) if config.home_timezone else None
        self.window_start, self.window_end = parse_window(config.bedtime_hours)
        self.prepare_at = _minutes(config.bedtime_prepare_time)
        self.dir = Path(config.bedtime_dir)
        self.name = config.bedtime_name
        self.next_prepare_try = 0.0
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    @staticmethod
    def evening(now: datetime) -> datetime:
        """凌晨 1 点还算“昨晚”"""
        return now - timedelta(days=1) if now.hour < 12 else now

    def in_hours(self, now: datetime | None = None) -> bool:
        now = now or self._now()
        minute = now.hour * 60 + now.minute
        if self.window_start <= self.window_end:
            return self.window_start <= minute < self.window_end
        return minute >= self.window_start or minute < self.window_end

    def _preparing(self, now: datetime) -> bool:
        """从准备时间到陪聊结束（可以跨午夜）"""
        minute = now.hour * 60 + now.minute
        if self.prepare_at <= self.window_end:
            return self.prepare_at <= minute < self.window_end
        return minute >= self.prepare_at or minute < self.window_end

    def _path(self, now: datetime) -> Path:
        return self.dir / (self.evening(now).strftime('%Y-%m-%d') + '.json')

    def items(self, now: datetime | None = None) -> list[dict]:
        try:
            return json.loads(self._path(now or self._now()).read_text(encoding='utf-8'))['items']
        except (OSError, ValueError, KeyError):
            return []

    def prepare(self, now: datetime | None = None) -> list[dict]:
        """联网搜今晚的谈资，存到 /data/bedtime/日期.json"""
        now = now or self._now()
        day = self.evening(now)
        model, result = self.client.chat_json_first(
            tuple(m.strip() for m in self.config.bedtime_model.split(',') if m.strip()),
            PACK_SYSTEM.format(name=self.name),
            f'今天是{now.year}年{now.month}月{now.day}日，星期{WEEKDAYS[now.weekday()]}。只要最近三天（含今天）的消息，旧闻不要。', 2000,
            temperature=0.3, search=True)
        items = [
            {'kind': str(i.get('kind', '')).strip(), 'date': str(i.get('date', '')).strip(),
             'topic': str(i.get('topic', '')).strip(), 'story': str(i.get('story', '')).strip()}
            for i in (result.get('items') or []) if isinstance(i, dict) and i.get('story')
        ][:6]
        if not items:
            raise RuntimeError('no items in the answer')
        self.dir.mkdir(parents=True, exist_ok=True)
        self._path(now).write_text(json.dumps({'model': model, 'items': items}, ensure_ascii=False, indent=1),
                                   encoding='utf-8')
        for old in sorted(self.dir.glob('*.json'))[:-14]:  # 只留两周
            old.unlink(missing_ok=True)
        LOG.info('Bedtime topics for %s (%s): %s', day.strftime('%Y-%m-%d'), model,
                 ' | '.join(f'[{i["kind"]}] {i["topic"]}' for i in items))
        return items

    def night_context(self, now: datetime | None = None) -> str:
        """加进整场对话说明里的夜聊口吻和今晚谈资"""
        items = self.items(now)
        topics = '\n'.join(f'{n}. 【{i["kind"]}】{i["topic"]}：{i["story"]}' for n, i in enumerate(items, 1))
        return NIGHT_RULES.format(name=self.name) + (
            f'今晚的谈资：\n{topics}\n' if topics else '今晚没准备到新鲜谈资：就陪她聊她想聊的，或者聊聊孙颖莎。\n')

    def offer_prompt(self) -> str:
        return OFFER_PROMPT.format(name=self.name)

    def try_now(self) -> dict:
        """看板“试试睡前聊天”：谈资没准备就先准备，然后马上主动问一次（不管几点、今晚问没问过）"""
        with self.lock:
            items = self.items() or self.prepare()
        if not self.offer(self.offer_prompt(), self.night_context()):
            raise RuntimeError('busy')
        return {'topics': [f'[{i["kind"]}] {i["topic"]}' for i in items]}

    def tick(self):
        now = self._now()
        evening = self.evening(now)
        if not self._preparing(now):
            return
        if not self._path(now).exists() and time.monotonic() >= self.next_prepare_try:
            try:
                with self.lock:
                    self.prepare(now)
            except Exception as exc:  # 搜不到也照样问、照样聊，只是没有新鲜谈资
                LOG.warning('Preparing bedtime topics failed (retrying in 10 min): %s', exc)
                self.next_prepare_try = time.monotonic() + PREPARE_RETRY
        key = 'bedtime_offered'
        if not self.in_hours(now) or timers.remaining(self.config.timers_path, key) > 0:
            return
        if self.someone_here is not None and not self.someone_here(SEEN_RECENTLY):
            return
        if self.offer(self.offer_prompt(), self.night_context(now)):
            LOG.info('Offered bedtime chat')
            noon = (evening + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
            timers.save(self.config.timers_path, key, noon.timestamp())

    def run(self):
        while not self.stop_event.wait(20):
            try:
                self.tick()
            except Exception:
                LOG.exception('Bedtime tick failed')

    def start(self):
        LOG.info('Bedtime chat: topics at %02d:%02d, chat window %02d:%02d-%02d:%02d (%s)',
                 *divmod(self.prepare_at, 60), *divmod(self.window_start, 60), *divmod(self.window_end, 60),
                 self.config.home_timezone or 'local time')
        self.thread = threading.Thread(target=self.run, name='bedtime', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
