"""配置管理模块 - 基于原项目的配置，适配通义千问 API"""
from __future__ import annotations
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value not in {'true', 'false', '1', '0', 'yes', 'no', 'on', 'off'}:
        raise ValueError(f'{name} must be a boolean')
    return value in {'true', '1', 'yes', 'on'}


@dataclass(frozen=True)
class Profile:
    id: str
    face_name: str
    display_name: str


@dataclass(frozen=True)
class Config:
    # 通义千问 API 配置
    qwen_api_key: str = field(default='', repr=False)
    qwen_workspace_id: str = ''
    qwen_region: str = 'cn'  # 'cn' for Beijing, 'intl' for Singapore
    qwen_model: str = 'qwen3.8-omni-flash-realtime'
    # 会话中先用它听清这一轮说了什么，判断完再决定音频要不要交给对话模型（见 asr_realtime.py）
    qwen_asr_model: str = 'qwen3-asr-flash-realtime'
    qwen_voice: str = 'longanlingxin'

    # EBO 机器人配置
    ebo_api_token: str = field(default='', repr=False)
    control_token: str = field(default='', repr=False)
    ebo_api_url: str = 'http://ebo-engine:8098'
    ebo_node: str = 'ebo'
    talk_stream_url: str = 'ws://ebo-engine:8200/talk'
    audio_stream_url: str = ''
    rtsp_url: str = 'rtsp://ebo-engine:8554/ebo'

    # 音频配置
    stream_prebuffer_ms: int = 200
    stream_connect_timeout_seconds: float = 10
    playback_tail_ms: int = 800

    # 服务配置
    http_port: int = 8099
    public_audio_base_url: str = 'http://realtime-assistant:8099/audio'
    auto_wake: bool = True

    # Frigate 配置
    frigate_url: str = 'http://frigate:5000'
    camera_name: str = 'ebo'
    mqtt_host: str = 'mosquitto'
    mqtt_port: int = 1883

    # 视觉配置
    visual_enabled: bool = True
    image_width: int = 768
    image_quality: int = 75

    # 会话配置
    session_idle_seconds: float = 300
    session_max_seconds: float = 2700
    cooldown_seconds: float = 90
    absence_seconds: float = 25
    # 家人回家主动打招呼（greeter.py）：认出这些人脸、且已经这么久没见到、且在这个时段内
    proactive_greeting: bool = True
    greet_names: str = '妈妈'
    greet_away_seconds: float = 10800
    greet_hours: str = '11-23'
    # greet_hours 按家里的时间算。程序跑在别的时区的电脑上时要写（如 Asia/Shanghai），空 = 这台电脑的时间
    home_timezone: str = ''
    # 每天早上定时播天气（weather_brief.py），时间按 home_timezone 算
    weather_enabled: bool = False
    weather_time: str = '06:00'
    weather_city: str = '天津'
    weather_lat: float = 39.1422
    weather_lon: float = 117.1767
    weather_name: str = '妈妈'   # 播报开头称呼谁
    weather_dir: str = '/data/weather'
    greet_context: str = ''  # 家人的背景（工作等），写进迎接提示词；隐私，只放 .env
    voice_wake_enabled: bool = True

    # Standby 唤醒（本地 Whisper）
    whisper_model: str = 'small'  # base 每句约 0.75 秒；small 约 2.6 秒但中文更准
    whisper_language: str = 'zh'
    wake_words: str = ''  # 逗号分隔；为空时任何中文语句都能唤醒
    standby_silence_ms: int = 900
    # 不说唤醒词时，由小模型判断这句话是不是在对机器人说（挡掉电视对白、家人之间的话）
    addressee_check: bool = True
    addressee_model: str = 'qwen-plus'

    # 联网搜索（通义千问内置 enable_search）
    web_search_enabled: bool = True

    # 视觉跟随
    tracking_enabled: bool = False
    dock_rest_seconds: float = 1080  # 用户说让它回去充电后，这么久（18 分钟）不追人
    quiet_seconds: float = 900      # 用户让它闭嘴后，这么久只认唤醒词
    timers_path: str = ''  # 上面两个倒计时跨重启保存的文件；空 = 不保存（docker-compose 里设为 /data/timers.json）
    # 找人转圈时每个方向自己识别一次（同一个 YOLOv9），门槛比 Frigate 低，能认出半身/趴着的人
    tracking_scan_enabled: bool = True
    scan_model_path: str = '/models/yolov9-s-320.onnx'
    scan_min_score: float = 0.5   # 0.35 时暗处噪点会被认成人
    tracking_dock_after_seconds: float = 300  # 跟随离开充电座后，这么久没看到人就回充（走远了先找一圈）

    # 机器人始终放在地上：发现桌面模式开着就自动关掉
    floor_mode: bool = True
    # 夜视保持 Auto：被设成 Day 时暗处看不见人
    night_vision_auto: bool = True

    # VAD 配置
    vad_threshold: float = .65
    vad_prefix_padding_ms: int = 300
    vad_silence_duration_ms: int = 650

    # 打断配置
    barge_in_enabled: bool = False
    barge_in_confirm_ms: int = 300
    barge_in_preroll_ms: int = 500
    barge_in_vad_mode: int = 2
    barge_in_echo_correlation: float = .65
    barge_in_residual_ratio: float = .45

    # AEC 配置
    aec_enabled: bool = True
    aec_delay_ms: int = 0
    aec_warmup_ms: int = 500

    # 人脸识别配置
    min_face_score: float = .90
    min_person_area: int = 40000

    # 数据存储路径
    transcript_path: str = '/data/transcripts.jsonl'
    output_audio_dir: str = '/data/replies'
    assistant_transcript_path: str = '/data/assistant_outputs.jsonl'
    # 全家一份的记忆（memory.py）：memory.json / notes.json / seed.json（seed 是家人自己写的固定事实）
    memory_dir: str = '/data/memory'
    assistant_control_path: str = ''

    # 其他配置
    memory_enabled: bool = True
    memory_note_model: str = 'qwen-plus'          # 第一层：每场对话后写笔记
    memory_model: str = 'qwen3-max,qwen-max'       # 第二层：整理，按顺序试（账号里没有的跳过）
    memory_consolidate_after: int = 4              # 攒够几条笔记就整理
    memory_context_chars: int = 2500               # 放进 instructions 的上限（每轮都重复计费）
    media_stale_after_seconds: float = 20
    media_startup_grace_seconds: float = 90
    instructions: str = '你是家里的 EBO 机器人助手。用自然、简洁、温暖的中文交谈。没有把握就明确说不知道。'
    trace_enabled: bool = False
    log_dir: str = 'logs'

    # 用户配置
    profiles: tuple[Profile, ...] = (
        Profile('father', '爸爸', '爸爸'),
        Profile('mother', '妈妈', '妈妈')
    )

    @property
    def qwen_ws_url(self) -> str:
        """生成通义千问 WebSocket URL"""
        # 工作空间专属域名（{workspace}.cn-beijing.maas.aliyuncs.com）对该 Key 返回
        # Endpoint.AccessDenied；标准 DashScope 域名可用
        hosts = {'cn': 'dashscope.aliyuncs.com', 'intl': 'dashscope-intl.aliyuncs.com'}
        if self.qwen_region not in hosts:
            raise ValueError(f'Invalid QWEN_REGION: {self.qwen_region}')
        return f'wss://{hosts[self.qwen_region]}/api-ws/v1/realtime?model={self.qwen_model}'

    @property
    def qwen_asr_ws_url(self) -> str:
        return self.qwen_ws_url.replace(f'model={self.qwen_model}', f'model={self.qwen_asr_model}')

    @classmethod
    def from_env(cls) -> Config:
        """从环境变量加载配置"""
        # 通义千问配置映射
        qwen_mapping = {
            'qwen_api_key': 'QWEN_API_KEY',
            'qwen_workspace_id': 'QWEN_WORKSPACE_ID',
            'qwen_region': 'QWEN_REGION',
            'qwen_model': 'QWEN_MODEL',
            'qwen_asr_model': 'QWEN_ASR_MODEL',
            'qwen_voice': 'EBO_VOICE',
        }

        # EBO 配置映射
        ebo_mapping = {
            'ebo_api_token': 'EBO_API_TOKEN',
            'ebo_api_url': 'EBO_API_URL',
            'ebo_node': 'EBO_NODE',
            'talk_stream_url': 'EBO_TALK_STREAM_URL',
            'audio_stream_url': 'EBO_AUDIO_STREAM_URL',
            'rtsp_url': 'EBO_RTSP_URL',
            'stream_prebuffer_ms': 'EBO_STREAM_PREBUFFER_MS',
            'stream_connect_timeout_seconds': 'EBO_STREAM_CONNECT_TIMEOUT_SECONDS',
            'playback_tail_ms': 'EBO_PLAYBACK_TAIL_MS',
            'http_port': 'EBO_ASSISTANT_PORT',
            'public_audio_base_url': 'EBO_ASSISTANT_AUDIO_URL',
            'auto_wake': 'EBO_AUTO_WAKE',
            'frigate_url': 'EBO_FRIGATE_URL',
            'camera_name': 'EBO_CAMERA_NAME',
            'mqtt_host': 'EBO_MQTT_HOST',
            'mqtt_port': 'EBO_MQTT_PORT',
            'visual_enabled': 'EBO_VISUAL_ENABLED',
            'image_width': 'REALTIME_IMAGE_WIDTH',
            'image_quality': 'REALTIME_IMAGE_QUALITY',
            'session_idle_seconds': 'EBO_SESSION_IDLE_SECONDS',
            'session_max_seconds': 'EBO_SESSION_MAX_SECONDS',
            'cooldown_seconds': 'EBO_COOLDOWN_SECONDS',
            'absence_seconds': 'EBO_ABSENCE_SECONDS',
            'proactive_greeting': 'EBO_PROACTIVE_GREETING_ENABLED',
            'greet_names': 'EBO_GREET_NAMES',
            'greet_away_seconds': 'EBO_GREET_AWAY_SECONDS',
            'greet_hours': 'EBO_GREET_HOURS',
            'home_timezone': 'EBO_HOME_TIMEZONE',
            'weather_enabled': 'EBO_WEATHER_ENABLED',
            'weather_time': 'EBO_WEATHER_TIME',
            'weather_city': 'EBO_WEATHER_CITY',
            'weather_lat': 'EBO_WEATHER_LAT',
            'weather_lon': 'EBO_WEATHER_LON',
            'weather_name': 'EBO_WEATHER_NAME',
            'greet_context': 'EBO_GREET_CONTEXT',
            'voice_wake_enabled': 'EBO_VOICE_WAKE_ENABLED',
            'whisper_model': 'EBO_WHISPER_MODEL',
            'whisper_language': 'EBO_WHISPER_LANGUAGE',
            'wake_words': 'EBO_WAKE_WORDS',
            'standby_silence_ms': 'EBO_STANDBY_SILENCE_MS',
            'addressee_check': 'EBO_ADDRESSEE_CHECK',
            'addressee_model': 'EBO_ADDRESSEE_MODEL',
            'web_search_enabled': 'EBO_WEB_SEARCH_ENABLED',
            'tracking_enabled': 'EBO_TRACKING_ENABLED',
            'tracking_dock_after_seconds': 'EBO_TRACKING_DOCK_AFTER_SECONDS',
            'tracking_scan_enabled': 'EBO_TRACKING_SCAN_ENABLED',
            'scan_model_path': 'EBO_SCAN_MODEL_PATH',
            'scan_min_score': 'EBO_SCAN_MIN_SCORE',
            'dock_rest_seconds': 'EBO_DOCK_REST_SECONDS',
            'quiet_seconds': 'EBO_QUIET_SECONDS',
            'timers_path': 'EBO_TIMERS_PATH',
            'floor_mode': 'EBO_FLOOR_MODE',
            'night_vision_auto': 'EBO_NIGHT_VISION_AUTO',
            'vad_threshold': 'REALTIME_VAD_THRESHOLD',
            'vad_prefix_padding_ms': 'EBO_VAD_PREFIX_PADDING_MS',
            'vad_silence_duration_ms': 'EBO_VAD_SILENCE_DURATION_MS',
            'barge_in_enabled': 'EBO_BARGE_IN_ENABLED',
            'barge_in_confirm_ms': 'EBO_BARGE_IN_CONFIRM_MS',
            'barge_in_preroll_ms': 'EBO_BARGE_IN_PREROLL_MS',
            'barge_in_vad_mode': 'EBO_BARGE_IN_VAD_MODE',
            'barge_in_echo_correlation': 'EBO_BARGE_IN_ECHO_CORRELATION',
            'barge_in_residual_ratio': 'EBO_BARGE_IN_RESIDUAL_RATIO',
            'aec_enabled': 'EBO_AEC_ENABLED',
            'aec_delay_ms': 'EBO_AEC_DELAY_MS',
            'aec_warmup_ms': 'EBO_AEC_WARMUP_MS',
            'min_face_score': 'EBO_MIN_FACE_SCORE',
            'min_person_area': 'EBO_MIN_PERSON_AREA',
            'transcript_path': 'EBO_TRANSCRIPT_PATH',
            'output_audio_dir': 'EBO_OUTPUT_AUDIO_DIR',
            'assistant_transcript_path': 'EBO_ASSISTANT_TRANSCRIPT_PATH',
            'memory_dir': 'EBO_MEMORY_DIR',
            'memory_enabled': 'EBO_MEMORY_ENABLED',
            'memory_note_model': 'EBO_MEMORY_NOTE_MODEL',
            'memory_model': 'EBO_MEMORY_MODEL',
            'memory_consolidate_after': 'EBO_MEMORY_CONSOLIDATE_AFTER',
            'assistant_control_path': 'EBO_ASSISTANT_CONTROL_PATH',
            'memory_context_chars': 'EBO_MEMORY_CONTEXT_CHARS',
            'media_stale_after_seconds': 'EBO_MEDIA_STALE_AFTER_SECONDS',
            'media_startup_grace_seconds': 'EBO_MEDIA_STARTUP_GRACE_SECONDS',
            'instructions': 'EBO_ASSISTANT_INSTRUCTIONS',
            'trace_enabled': 'EBO_TRACE_ENABLED',
            'log_dir': 'EBO_LOG_DIR',
        }

        defaults = cls()
        values = {}

        # 合并所有映射
        all_mapping = {**qwen_mapping, **ebo_mapping}

        for attribute, env_name in all_mapping.items():
            default = getattr(defaults, attribute)
            if isinstance(default, bool):
                values[attribute] = env_bool(env_name, default)
            else:
                raw = os.getenv(env_name)
                values[attribute] = type(default)(raw.strip()) if raw and raw.strip() else default

        # 处理 control_token
        values['control_token'] = os.getenv('EBO_ASSISTANT_CONTROL_TOKEN', '').strip() or values['ebo_api_token']

        # 处理用户配置
        raw_profiles = os.getenv('EBO_USERS_JSON')
        if raw_profiles:
            profiles = json.loads(raw_profiles)
            if not isinstance(profiles, list):
                raise ValueError('EBO_USERS_JSON must be an array')
            values['profiles'] = tuple(Profile(**profile) for profile in profiles)

        result = cls(**values)
        result.validate()
        return result

    def validate(self) -> None:
        """验证配置"""
        if not self.qwen_api_key:
            raise ValueError('QWEN_API_KEY is required')

        if not self.ebo_api_token or not self.control_token:
            raise ValueError('EBO_API_TOKEN is required')

        if self.qwen_region not in {'cn', 'intl'}:
            raise ValueError('QWEN_REGION must be "cn" or "intl"')

        if not self.profiles or len(self.profiles) > 10:
            raise ValueError('Configure between one and ten EBO users')

        for profile in self.profiles:
            if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', profile.id):
                raise ValueError('Profile IDs must be safe ASCII')
            if not profile.face_name.strip() or not profile.display_name.strip():
                raise ValueError('Profiles require face_name and display_name')

        if len({p.id for p in self.profiles}) != len(self.profiles):
            raise ValueError('User IDs must be unique')

        if len({p.face_name for p in self.profiles}) != len(self.profiles):
            raise ValueError('Face names must be unique')

        # 验证时间配置
        for attribute in ('session_idle_seconds', 'session_max_seconds',
                         'cooldown_seconds', 'absence_seconds', 'tracking_dock_after_seconds',
                         'media_stale_after_seconds', 'media_startup_grace_seconds'):
            value = getattr(self, attribute)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f'{attribute} must be finite and positive')

        if not 30 <= self.session_max_seconds <= 3300:
            raise ValueError('Session limit must be 30..3300 seconds')

        if self.session_idle_seconds > self.session_max_seconds:
            raise ValueError('Idle timeout must not exceed max session time')

        if not 0 < self.min_face_score <= 1:
            raise ValueError('Face score threshold out of range')

        if not 0 < self.vad_threshold < 1:
            raise ValueError('VAD threshold out of range')

        if not Path(self.memory_dir).is_absolute():
            raise ValueError('EBO_MEMORY_DIR must be absolute path')

    def profile(self, user_id: str) -> Profile:
        """获取用户配置"""
        for profile in self.profiles:
            if profile.id == user_id:
                return profile
        raise ValueError('unknown_user')

    @property
    def memory_models(self) -> tuple[str, ...]:
        return tuple(m.strip() for m in self.memory_model.split(',') if m.strip())
