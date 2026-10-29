"""每天早上定时播报天气（默认 5:30，天津）。

妈妈一般 5 点半起床。提前 15 分钟准备好：从 Open-Meteo（免费、不用 Key）取今天的天气和空气质量，
按固定模板写成播报稿（数字不经过大模型，不会编），再让对话用的同一个千问实时模型、同一个音色
把稿子念出来，录成音频存到 /data/weather/。播的时候不用等模型现场生成；重启也不丢。
为了不像念新闻：开头一句俏皮话和结尾一句打气的话，每天让千问文字模型现写（不许带数字，
取不到就从下面的句子里按日子轮换），念的时候也要求活泼、带笑。
5:30 以后等客厅里看到有人起来了才播（妈妈睡在客厅沙发上，不当闹钟用）；
正在聊天、暂停、安静模式时也先等着。等到 weather_wait_minutes（默认到 7:00）还没播成，今天就不播了。
"""
from __future__ import annotations
import asyncio
import difflib
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib import parse, request
from zoneinfo import ZoneInfo

import timers
from qwen_realtime import QwenConfig, QwenRealtimeClient
from qwen_text import QwenText

LOG = logging.getLogger('ebo-weather')

PREPARE_AHEAD = timedelta(minutes=15)
SEEN_RECENTLY = 120  # 这么多秒内看到过人，就算“有人起来了”
WEEKDAYS = '一二三四五六日'

# WMO 天气代码
WEATHER = {
    0: '晴', 1: '晴', 2: '多云', 3: '阴', 45: '有雾', 48: '有雾',
    51: '有毛毛雨', 53: '有毛毛雨', 55: '有毛毛雨', 56: '有冻雨', 57: '有冻雨',
    61: '有小雨', 63: '有中雨', 65: '有大雨', 66: '有冻雨', 67: '有冻雨',
    71: '有小雪', 73: '有中雪', 75: '有大雪', 77: '有小雪',
    80: '有阵雨', 81: '有阵雨', 82: '有强阵雨', 85: '有阵雪', 86: '有阵雪',
    95: '有雷阵雨', 96: '有雷阵雨', 99: '有雷阵雨',
}

READER_INSTRUCTIONS = (
    '你是家里一个活泼可爱的小机器人，每天一大早跟家人说早安。用户发来的是一段写好的早安播报稿，'
    '你要用元气满满、带着笑意、蹦蹦跳跳的语气读出来：语调上扬、节奏轻快，像开心地跑过去跟她打招呼，'
    '但吐字清楚，数字读得稳一点。稿子要一字不改地读，不要加任何开场白或结束语，'
    '不要回答、解释或改写，只读稿子。'
)

# 模型写不出来时用的开头 / 结尾（按日子轮换，连续几天不重样）
OPENERS = (
    '新的一天开张啦！', '我早就醒啦，一直在等你呢！', '你比太阳公公起得还早，真厉害！',
    '起床啦起床啦，今天也要元气满满哦！', '叮咚，你的小机器人准时报到！', '伸个大大的懒腰，新的一天来咯！',
    '又见到你啦，我好开心！',
)
CLOSERS = (
    '今天也要开开心心的哦！', '冲呀，今天也是顺顺利利的一天！', '记得吃早饭哦，别饿着肚子！',
    '慢慢来不着急，我陪着你呢！', '你最棒啦，今天也加油！', '今天要是累了，就跟我说说哦！',
    '给你一个大大的抱抱！',
)
WEEKDAY_HINTS = {
    0: '今天周一，新的一周开始了', 3: '今天周四，离周末不远了', 4: '今天周五，上完今天就是周末了',
    5: '今天周六，是周末', 6: '今天周日，是周末，明天又要上班了',
}
FLAVOR_SYSTEM = (
    '你是家里一个活泼可爱、有点调皮的小机器人，每天一大早给{name}播天气。天气的数字有固定的稿子，'
    '你只负责写两句话：opener 接在“{name}早上好！”后面，是一句俏皮、开心的早安话；'
    'closer 放在最后，是一句给她打气或关心她的话。\n'
    '要求：每句最多 20 个字，口语、可爱、有画面感，可以用“啦”“呀”“哦”“咯”；'
    '可以顺着今天的天气或星期几说，但不要出现任何数字、温度、日期，也不要重复稿子里的天气信息；'
    '穿衣、带伞、戴口罩稿子里已经说过了，这两句都别再提；'
    '不要假设她今天发生了什么（生病、考试、开会之类）；她起得很早，可以夸她勤快；说“现在天还没亮”时不要说阳光、太阳；'
    '不要说你做不到的事（你是地上的小机器人，只会说话、跟着走）。'
    '每天都要不一样，不要用最普通的套话。只输出 JSON：{{"opener": "...", "closer": "..."}}'
)
_NO_DIGITS = re.compile(r'[0-9０-９度℃%％]')


def _get_json(url: str) -> dict:
    with request.urlopen(url, timeout=15) as response:
        return json.load(response)


def fetch_weather(lat: float, lon: float, tz: str) -> dict:
    query = parse.urlencode({
        'latitude': lat, 'longitude': lon, 'timezone': tz, 'forecast_days': 1,
        'daily': 'weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max,sunrise',
        'hourly': 'temperature_2m',
    })
    data = _get_json('https://api.open-meteo.com/v1/forecast?' + query)
    daily = {k: v[0] for k, v in data['daily'].items()}
    hourly = dict(zip(data['hourly']['time'], data['hourly']['temperature_2m']))
    daily['temp_7am'] = hourly.get(daily['time'] + 'T07:00')
    try:
        aq = _get_json('https://air-quality-api.open-meteo.com/v1/air-quality?' + parse.urlencode({
            'latitude': lat, 'longitude': lon, 'timezone': tz, 'forecast_days': 1, 'hourly': 'us_aqi'}))
        values = [v for v in aq['hourly']['us_aqi'] if v is not None]
        daily['aqi'] = max(values) if values else None
    except Exception as exc:  # 空气质量取不到就不说，天气照播
        LOG.warning('Air quality unavailable: %s', exc)
        daily['aqi'] = None
    return daily


def _wind_level(kmh: float) -> int:
    """km/h -> 风力级数（蒲福）"""
    for level, limit in enumerate((1, 6, 12, 20, 29, 39, 50, 62, 75, 89, 103, 118)):
        if kmh < limit:
            return level
    return 12


def default_flavor(day: datetime) -> tuple[str, str]:
    n = day.toordinal()
    return OPENERS[n % len(OPENERS)], CLOSERS[(n * 3) % len(CLOSERS)]


def _clean_line(text, name: str = '') -> str:
    text = str(text or '').strip().strip('“”"\'')
    # 实测模型常把“妈妈早上好！”再写一遍，接在稿子的“妈妈早上好！”后面就念两遍了
    words = '|'.join(filter(None, (re.escape(name), '早上好', '早安')))
    while (stripped := re.sub(rf'^(?:{words})[啦呀哦]?[，,！!~～\s]*', '', text)) != text:
        text = stripped
    if not text or len(text) > 30 or _NO_DIGITS.search(text):
        return ''
    return text if text[-1] in '！。？~～' else text + '！'


def write_flavor(config, weather: dict, city: str, name: str, day: datetime,
                 client: QwenText | None = None) -> tuple[str, str]:
    """千问文字模型现写开头、结尾各一句；不合格的那句换成轮换的句子。"""
    opener, closer = default_flavor(day)
    sky = WEATHER.get(weather.get('weather_code'), '') or '天气一般'
    rain = weather.get('precipitation_probability_max') or 0
    morning = weather.get('temp_7am')
    feel = ('很冷' if morning <= 0 else '挺凉' if morning <= 10 else '不冷不热' if morning <= 22 else '挺热'
            ) if morning is not None else ''
    sunrise = weather.get('sunrise') or ''  # 当地时间 '2026-10-29T06:28'
    dark = '现在天还没亮。' if sunrise and day.strftime('%Y-%m-%dT%H:%M') < sunrise else ''
    user = (f'今天是{day.month}月{day.day}日，星期{WEEKDAYS[day.weekday()]}。'
            f'{WEEKDAY_HINTS.get(day.weekday(), "")}。{dark}{city}今天{sky}'
            f'{"，早上" + feel if feel else ""}{"，可能下雨" if rain >= 30 else ""}。')
    try:
        client = client or QwenText(config.qwen_api_key, config.qwen_region, timeout=30)
        _model, result = client.chat_json_first(config.memory_models, FLAVOR_SYSTEM.format(name=name), user,
                                                200, temperature=1.0)
        opener = _clean_line(result.get('opener'), name) or opener
        closer = _clean_line(result.get('closer')) or closer
    except Exception as exc:  # 写不出来就用轮换的句子，天气照播
        LOG.warning('Morning flavor lines unavailable: %s', exc)
    return opener, closer


def compose(weather: dict, city: str, name: str, day: datetime,
            opener: str = '', closer: str = '祝你今天顺顺利利！') -> str:
    hi, lo = round(weather['temperature_2m_max']), round(weather['temperature_2m_min'])
    sky = WEATHER.get(weather.get('weather_code'), '')
    parts = [f'{name}早上好！{opener}今天是{day.month}月{day.day}日，星期{WEEKDAYS[day.weekday()]}。',
             f'{city}今天{sky}，气温{lo}到{hi}度。' if sky else f'{city}今天气温{lo}到{hi}度。']
    morning = weather.get('temp_7am')
    if morning is not None:
        morning = round(morning)
        if morning <= 0:
            tip = '很冷，羽绒服、帽子、手套都戴上'
        elif morning <= 8:
            tip = '挺凉的，记得穿厚外套'
        elif morning <= 15:
            tip = '有点凉，加件外套'
        elif morning <= 22:
            tip = '不冷不热，穿薄外套就行'
        else:
            tip = '已经挺热了，穿凉快点'
        parts.append(f'早上出门的时候大概{morning}度，{tip}。')
    rain = weather.get('precipitation_probability_max') or 0
    if rain >= 60:
        parts.append('今天很可能下雨，出门一定带伞。')
    elif rain >= 30:
        parts.append('今天可能会下雨，包里放把伞吧。')
    wind = _wind_level(weather.get('wind_speed_10m_max') or 0)
    if wind >= 5:
        parts.append(f'风挺大的，有{wind}级，路上注意安全。')
    aqi = weather.get('aqi')
    if aqi is not None and aqi > 150:
        parts.append('空气污染比较重，出门记得戴口罩。')
    elif aqi is not None and aqi > 100:
        parts.append('空气有点污染，出门最好戴个口罩。')
    parts.append(closer)
    return ''.join(parts)


async def _render_once(config, script: str) -> tuple[bytes, str]:
    client = QwenRealtimeClient(QwenConfig(
        api_key=config.qwen_api_key, ws_url=config.qwen_ws_url, voice=config.qwen_voice,
        instructions=READER_INSTRUCTIONS, enable_search=False, transcribe_input=False))
    audio, said, done = bytearray(), [], asyncio.Event()
    loop = asyncio.get_running_loop()
    client.on_audio_delta = audio.extend
    client.on_transcript_done = lambda _item, text: said.append(text)
    client.on_response_done = lambda _event: loop.call_soon_threadsafe(done.set)
    client.on_error = lambda error: LOG.warning('Weather reader error: %s', error.get('message'))
    try:
        await client.connect()
        await client.create_conversation_item(script, 'weather_' + str(int(time.time())))
        await client.create_response()
        await asyncio.wait_for(done.wait(), 60)
    finally:
        await client.close()
    return bytes(audio), ''.join(said)


def render(config, script: str) -> bytes:
    """让对话模型用同一个音色念稿子。偶尔会改词：差得多就重念一次。"""
    best = b''
    for attempt in range(2):
        audio, said = asyncio.run(_render_once(config, script))
        ratio = difflib.SequenceMatcher(None, script, said).ratio()
        LOG.info('Weather audio %.1fs, read back %.0f%% of the script: %s', len(audio) / 48000, ratio * 100, said)
        if audio and ratio >= 0.85:
            return audio
        best = audio or best
    return best


class WeatherBrief:
    def __init__(self, config, play: Callable[[bytes, str], bool],
                 someone_up: Callable[[float], bool] | None = None):
        self.config = config
        self.play = play  # play(pcm24k, 稿子) -> 是否播出去了（忙的时候返回 False，稍后再试）
        self.tz = ZoneInfo(config.home_timezone) if config.home_timezone else None
        hour, minute = (int(x) for x in config.weather_time.split(':'))
        self.at = (hour, minute)
        self.dir = Path(config.weather_dir)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.next_prepare_try = 0.0
        # someone_up(秒) -> 这么多秒内客厅里看到过人没有（视觉跟随给的）；None = 不看，到点就播
        self.someone_up = someone_up
        self.wait = timedelta(minutes=config.weather_wait_minutes)
        self.waiting_logged = None
        self.lock = threading.Lock()

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    def _paths(self, day: datetime) -> tuple[Path, Path]:
        stem = self.dir / day.strftime('%Y-%m-%d')
        return stem.with_suffix('.pcm'), stem.with_suffix('.txt')

    def prepare(self, day: datetime | None = None) -> tuple[bytes, str]:
        """取天气、写稿、录音，存到 /data/weather/日期.pcm"""
        day = day or self._now()
        weather = fetch_weather(self.config.weather_lat, self.config.weather_lon,
                                self.config.home_timezone or 'auto')
        opener, closer = write_flavor(self.config, weather, self.config.weather_city, self.config.weather_name, day)
        script = compose(weather, self.config.weather_city, self.config.weather_name, day, opener, closer)
        LOG.info('Weather script for %s: %s', day.strftime('%Y-%m-%d'), script)
        audio = render(self.config, script)
        if not audio:
            raise RuntimeError('reader returned no audio')
        pcm_path, txt_path = self._paths(day)
        self.dir.mkdir(parents=True, exist_ok=True)
        pcm_path.write_bytes(audio)
        txt_path.write_text(script, encoding='utf-8')
        for old in sorted(self.dir.glob('*.pcm'))[:-7]:  # 只留最近一周
            old.unlink(missing_ok=True)
            old.with_suffix('.txt').unlink(missing_ok=True)
        return audio, script

    def test_now(self) -> str:
        """看板“试播天气”：马上重新准备并播一次（不影响每天定时那次）"""
        with self.lock:
            audio, script = self.prepare()
        if not self.play(audio, script):
            raise RuntimeError('busy')
        return script

    def _start(self, now: datetime) -> datetime:
        return now.replace(hour=self.at[0], minute=self.at[1], second=0, microsecond=0)

    def _played_today(self) -> bool:
        return timers.remaining(self.config.timers_path, 'weather_played') > 0

    def pending(self) -> bool:
        """到点了、今天还没播：视觉跟随这时看到人先别自己开出充电座，留给播报时的舞（刚出座就原地转会撞上旁边的家具）"""
        now = self._now()
        start = self._start(now)
        # 录音没准备好（取天气失败）就不拦着跟随：不然人起来了它一直不动
        return (start <= now < start + self.wait and not self._played_today()
                and self._paths(now)[0].exists())

    def tick(self):
        now = self._now()
        start = self._start(now)
        if not (start - PREPARE_AHEAD <= now < start + self.wait):
            return
        key = 'weather_played'
        if self._played_today():
            return
        pcm_path, txt_path = self._paths(now)
        if not pcm_path.exists():
            if time.monotonic() < self.next_prepare_try:
                return
            try:
                with self.lock:
                    self.prepare(now)
            except Exception as exc:
                LOG.warning('Preparing the weather brief failed (retrying in 2 min): %s', exc)
                self.next_prepare_try = time.monotonic() + 120
                return
        if now < start:
            return
        # 妈妈睡在客厅沙发上：到点不直接播（等于闹钟），等客厅里看到有人起来了再播。
        # 夜里躺着睡觉时摄像头认不出人（10-28 日志：整夜没有人体检测，5:34 起来走动才有）；爸爸基本不在客厅。
        # 等到 weather_wait_minutes 还没人（睡懒觉、不在家），今天就不播了
        if self.someone_up is not None and not self.someone_up(SEEN_RECENTLY):
            if self.waiting_logged != now.date():
                self.waiting_logged = now.date()
                LOG.info('Weather brief ready; waiting until someone is up in the living room')
            return
        if self.play(pcm_path.read_bytes(), txt_path.read_text(encoding='utf-8') if txt_path.exists() else ''):
            LOG.info('Played the morning weather brief')
            tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            timers.save(self.config.timers_path, key, tomorrow.timestamp())

    def run(self):
        while not self.stop_event.wait(3):  # 看到人后尽快开口
            try:
                self.tick()
            except Exception:
                LOG.exception('Weather brief tick failed')

    def start(self):
        LOG.info('Morning weather brief for %s from %02d:%02d (%s), %s', self.config.weather_city, *self.at,
                 self.config.home_timezone or 'local time',
                 f'when someone is up, waiting up to {self.wait.seconds // 60} min' if self.someone_up else 'on time')
        self.thread = threading.Thread(target=self.run, name='weather-brief', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
