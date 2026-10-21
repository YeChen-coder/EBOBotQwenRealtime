"""每天早上定时播报天气（默认 6:00，天津）。

妈妈早上 6 点肯定已经起了。提前 15 分钟准备好：从 Open-Meteo（免费、不用 Key）取今天的天气和空气质量，
按固定模板写成播报稿（数字不经过大模型，不会编），再让对话用的同一个千问实时模型、同一个音色
把稿子念出来，录成音频存到 /data/weather/。到点直接播，不用等模型现场生成；重启也不丢。
正在聊天、暂停、安静模式时先等着，30 分钟内找到空当再播，过了就不播了。
"""
from __future__ import annotations
import asyncio
import difflib
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib import parse, request
from zoneinfo import ZoneInfo

import timers
from qwen_realtime import QwenConfig, QwenRealtimeClient

LOG = logging.getLogger('ebo-weather')

PREPARE_AHEAD = timedelta(minutes=15)
PLAY_WINDOW = timedelta(minutes=30)
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
    '你是播音员。用户发来的是一段写好的天气播报稿，你要用温柔、亲切、自然的语气，'
    '把它一字不改地读出来。不要加任何开场白或结束语，不要回答、解释或改写，只读稿子。'
)


def _get_json(url: str) -> dict:
    with request.urlopen(url, timeout=15) as response:
        return json.load(response)


def fetch_weather(lat: float, lon: float, tz: str) -> dict:
    query = parse.urlencode({
        'latitude': lat, 'longitude': lon, 'timezone': tz, 'forecast_days': 1,
        'daily': 'weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max',
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


def compose(weather: dict, city: str, name: str, day: datetime) -> str:
    hi, lo = round(weather['temperature_2m_max']), round(weather['temperature_2m_min'])
    sky = WEATHER.get(weather.get('weather_code'), '')
    parts = [f'{name}早上好！今天是{day.month}月{day.day}日，星期{WEEKDAYS[day.weekday()]}。',
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
    parts.append('祝你今天顺顺利利！')
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
    def __init__(self, config, play: Callable[[bytes], bool]):
        self.config = config
        self.play = play  # play(pcm24k) -> 是否播出去了（忙的时候返回 False，稍后再试）
        self.tz = ZoneInfo(config.home_timezone) if config.home_timezone else None
        hour, minute = (int(x) for x in config.weather_time.split(':'))
        self.at = (hour, minute)
        self.dir = Path(config.weather_dir)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.next_prepare_try = 0.0
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
        script = compose(weather, self.config.weather_city, self.config.weather_name, day)
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
        if not self.play(audio):
            raise RuntimeError('busy')
        return script

    def tick(self):
        now = self._now()
        start = now.replace(hour=self.at[0], minute=self.at[1], second=0, microsecond=0)
        if not (start - PREPARE_AHEAD <= now < start + PLAY_WINDOW):
            return
        key = 'weather_played'
        if timers.remaining(self.config.timers_path, key) > 0:
            return
        pcm_path, _ = self._paths(now)
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
        if self.play(pcm_path.read_bytes()):
            LOG.info('Played the morning weather brief')
            tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            timers.save(self.config.timers_path, key, tomorrow.timestamp())

    def run(self):
        while not self.stop_event.wait(20):
            try:
                self.tick()
            except Exception:
                LOG.exception('Weather brief tick failed')

    def start(self):
        LOG.info('Morning weather brief for %s at %02d:%02d (%s)', self.config.weather_city, *self.at,
                 self.config.home_timezone or 'local time')
        self.thread = threading.Thread(target=self.run, name='weather-brief', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
