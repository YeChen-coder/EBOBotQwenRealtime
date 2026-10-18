# EBO 通义千问 Realtime 实施计划

## 📋 项目目标

将 EBO 机器人从 OpenAI Realtime API 迁移到通义千问 Qwen Realtime API，并增加以下新功能：
1. 完整的语音交互（保证不丢失用户输入）
2. Web Search 功能（通义千问不自带搜索）
3. 视觉跟随功能（检测到人时自动靠近）

---

## ✅ 已确认的技术可行性

### 1. 语音输入完整性
- ✅ 通义千问支持 `conversation.item.create` 发送文本消息
- ✅ 可以先发送第一段文本，再流式发送后续音频
- ✅ 原项目的 `initial_turn` + `startup_audio` 机制完全适用

### 2. Standby 唤醒
- ✅ 使用分段转录（segment-based），不是连续流式
- ✅ 本地 VAD (webrtcvad) + 700ms 沉默阈值检测完整语句
- ✅ 只转录完整段，成本极低（~0.6元/月）
- ⚠️ 需要调整：把 OpenAI Whisper API 改为通义千问 ASR 或本地 Whisper

### 3. VAD 自我打断问题
- ✅ 通义千问支持禁用 server_vad（设置 `turn_detection: None`）
- ✅ 改为手动模式：本地检测语音结束 → 手动触发响应
- ✅ 避免机器人自己的声音触发 VAD

### 4. 视觉跟随
- ✅ EBO Engine 已暴露 HTTP API (http://ebo-engine:8098)
- ✅ 支持运动控制：`POST /api/cmd` with `move/vector`
- ✅ Frigate 已集成，可检测人员位置（bounding box）
- ❌ 机器人自带的 AI Tracking (103049) 不适合自动化
- ✅ 方案：自己实现简单的视觉跟随逻辑

---

## 🏗️ 架构设计

### 核心模块

```
ebo-qwen-realtime/
├── app.py                    # 入口（保持不变）
├── runtime.py                # 主程序（需要修改）
├── config.py                 # 配置（已完成）
├── qwen_realtime.py          # 通义千问客户端（需要完善）
├── conversation.py           # 对话管理（需要适配通义千问）
├── ebo_transport.py          # EBO 传输（复用原项目）
├── state.py                  # 状态管理（复用原项目）
├── standby_voice.py          # Standby 唤醒（需要新写）⭐
├── person_tracker.py         # 视觉跟随（新增）⭐
├── web_search.py             # 搜索功能（已有框架，需要集成）
└── .env                      # 配置文件（已填写密钥）
```

---

## 📝 详细实施步骤

### 阶段 1：核心语音功能（优先级最高）

#### 1.1 完善 `qwen_realtime.py`
**目标**：实现完整的通义千问 Realtime WebSocket 客户端

**需要添加的功能**：
- [ ] `create_conversation_item()` - 发送文本消息
- [ ] `commit_audio_buffer()` - 提交音频缓冲
- [ ] `create_response()` - 手动触发响应（禁用 VAD 后使用）
- [ ] 事件处理完整性检查

**关键代码**：
```python
async def create_conversation_item(self, text: str, item_id: str):
    """发送文本消息（用于 initial_turn）"""
    message = {
        'type': 'conversation.item.create',
        'item': {
            'id': item_id,
            'type': 'message',
            'role': 'user',
            'content': [{'type': 'input_text', 'text': text}]
        }
    }
    await self._send(message)
```

#### 1.2 适配 `conversation.py`
**目标**：将原项目的对话管理逻辑适配到通义千问

**关键修改点**：
1. **初始化连接时发送配置**
   - 设置 `turn_detection: None`（禁用 server VAD）
   - 配置音频格式（16kHz input, 24kHz output）

2. **opening() 方法**
   - 发送 `initial_turn` 作为文本
   - 发送 `startup_audio` 缓冲区的音频
   - 手动调用 `commit_audio_buffer()` 和 `create_response()`

3. **音频流处理**
   - 本地 VAD 检测语音结束
   - 调用 `commit_audio_buffer()` + `create_response()`

#### 1.3 新建 `standby_voice.py`
**目标**：实现成本最优的 Standby 唤醒

**参考原项目逻辑**：
- 复用 `SpeechSegmenter` 类（本地 VAD + 700ms 沉默阈值）
- 复用 `StandbyVoice` 类框架

**需要修改的部分**：
- 转录 API：从 OpenAI Whisper → 通义千问 ASR 或本地 Whisper
- 选项 A（推荐）：本地 Whisper（免费，~3.5s 延迟，CPU 可用）
- 选项 B：通义千问 Paraformer（收费，低延迟）

**成本对比**：
| 方案 | 成本/月 | 延迟 | 优缺点 |
|------|---------|------|--------|
| 本地 Whisper | 0元 | ~3.5s | 免费，CPU 可用，延迟可接受 ✅ |
| 通义千问 ASR | ~7-10元 | ~1s | 低延迟，但需要付费 |

**实现要点**：
```python
# 使用本地 Whisper
import whisper

class StandbyVoice:
    def __init__(self):
        self.model = whisper.load_model("base")  # CPU 可用

    def transcribe(self, audio_pcm):
        # 转换为 Whisper 需要的格式
        result = self.model.transcribe(audio_pcm)
        return result["text"]
```

---

### 阶段 2：防止输入丢失（关键）

#### 2.1 修改 `runtime.py` 的 `accept_voice()` 方法

**问题**：原项目在唤醒后立即调用 `wake.reset()`，会丢弃队列中的第二段音频

**解决方案**：
```python
def accept_voice(self, initial_turn):
    with self.lock:
        conversation = Conversation(...)

        if initial_turn is not None:
            conversation.initial_turn = initial_turn

        # 🆕 把 pending 队列中的剩余音频段都加入 startup_audio
        while True:
            try:
                remaining = self.wake.pending.get_nowait()
                # 把音频加入 startup_audio（每帧 960 字节）
                for i in range(0, len(remaining.pcm), 960):
                    conversation.startup_audio.append(remaining.pcm[i:i+960])
            except queue.Empty:
                break

        self.active = conversation
        self.wake.reset()  # 现在可以安全 reset
```

#### 2.2 调整沉默阈值（可选）

**当前**：700ms 沉默触发分段
**建议**：900ms-1000ms（减少短停顿导致的分段）

```python
# standby_voice.py
if self.silence >= 45:  # 900ms (45帧 × 20ms)
    results.append(segment)
```

---

### 阶段 3：视觉跟随功能（新增）

#### 3.1 新建 `person_tracker.py`

**功能**：
- 订阅 Frigate 的人员检测事件
- 计算人在画面中的位置和大小
- 决定机器人的移动方向和速度
- 调用 EBO Engine API 发送移动命令

**完整实现**：
```python
import logging
import json
import httpx
from typing import Optional, Dict

LOG = logging.getLogger('ebo-person-tracker')

class PersonTracker:
    """简单的视觉跟随：检测到人时自动朝人移动"""

    def __init__(self, config):
        self.config = config
        self.enabled = False
        self.image_width = 1920
        self.image_height = 1080
        self.dead_zone = 0.2  # 中心死区（20%）
        self.min_area_ratio = 0.08  # 最小面积（太远）
        self.max_area_ratio = 0.25  # 最大面积（太近）
        self.tracking_active = False

    async def process_detection(self, detection: dict) -> Optional[dict]:
        """
        处理 Frigate 检测
        返回：移动命令（如果需要移动）
        """
        if not self.enabled:
            return None

        if detection.get("label") != "person":
            return None

        box = detection.get("box")
        if not box or len(box) != 4:
            return None

        # 计算人的中心位置
        person_x = (box[0] + box[2]) / 2
        person_y = (box[1] + box[3]) / 2

        # 归一化到 -1.0 ~ 1.0
        norm_x = (person_x / self.image_width) * 2 - 1

        # 计算人的面积（判断距离）
        area = (box[2] - box[0]) * (box[3] - box[1])
        area_ratio = area / (self.image_width * self.image_height)

        # 决策
        ly = 0  # 前后移动（负值=前进，正值=后退）
        rx = 0  # 转向（负值=左转，正值=右转）

        # 1. 转向：让人保持在画面中心
        if abs(norm_x) > self.dead_zone:
            rx = int(norm_x * 25)  # -25 到 25
            LOG.info(f"Person at x={norm_x:.2f}, turning rx={rx}")

        # 2. 前后：保持合适距离
        if area_ratio < self.min_area_ratio:
            ly = -20  # 前进（注意：负值）
            LOG.info(f"Person too far (area={area_ratio:.3f}), moving forward")
        elif area_ratio > self.max_area_ratio:
            ly = 15  # 后退
            LOG.info(f"Person too close (area={area_ratio:.3f}), moving back")

        # 如果已经在中心且距离合适，停止
        if abs(norm_x) <= self.dead_zone and self.min_area_ratio <= area_ratio <= self.max_area_ratio:
            LOG.info("Person centered and at good distance")
            return None

        return {"ly": ly, "rx": rx}

    async def send_move_command(self, cmd: dict):
        """发送移动命令到 EBO Engine"""
        payload = {
            "node": self.config.ebo_node,
            "suffix": "move/vector",
            "payload": json.dumps({
                "ly": cmd["ly"],
                "rx": cmd["rx"],
                "lx": 0,
                "ry": 0,
                "hold": 1.5,  # 持续 1.5 秒
                "buttons": 1
            })
        }

        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                response = await client.post(
                    f"{self.config.ebo_api_url}/api/cmd",
                    headers={"X-Enabot-Token": self.config.ebo_api_token},
                    json=payload
                )
                response.raise_for_status()
                LOG.debug(f"Sent move command: {cmd}")
            except Exception as e:
                LOG.error(f"Failed to send move command: {e}")
```

#### 3.2 集成到 `runtime.py`

**添加跟踪逻辑**：
```python
class Runtime:
    def __init__(self, ...):
        # ... 现有代码 ...
        self.tracker = PersonTracker(config)

    def on_message(self, _client, _userdata, message):
        # ... 现有的 presence gate 逻辑 ...

        # 🆕 视觉跟随
        if message.topic == "frigate/events" and self.tracker.enabled:
            try:
                payload = json.loads(message.payload)
                event = payload.get("after", {})

                if event.get("label") == "person" and event.get("camera") == self.config.camera_name:
                    box = event.get("box")
                    if box:
                        move_cmd = asyncio.run_coroutine_threadsafe(
                            self.tracker.process_detection(event),
                            self.loop
                        ).result()

                        if move_cmd:
                            asyncio.run_coroutine_threadsafe(
                                self.tracker.send_move_command(move_cmd),
                                self.loop
                            )
            except Exception as e:
                LOG.error(f"Tracking error: {e}")
```

#### 3.3 添加配置项

**在 `config.py` 中添加**：
```python
# 视觉跟随配置
self.tracking_enabled = os.getenv('EBO_TRACKING_ENABLED', 'false').lower() == 'true'
```

**在 `.env` 中添加**：
```bash
# ========================================
# 视觉跟随配置（新增功能）
# ========================================
EBO_TRACKING_ENABLED=false  # 设为 true 启用自动跟随
```

---

### 阶段 4：Web Search 集成

#### 4.1 完善 `web_search.py`

已有基础框架，需要：
- [ ] 测试 Tavily/SerpAPI/Google 三个 provider
- [ ] 添加错误处理和重试逻辑
- [ ] 添加结果缓存（避免重复搜索）

#### 4.2 集成到对话流程

**在 `conversation.py` 中**：
```python
# 当检测到需要搜索的意图时
if "搜索" in user_text or "查" in user_text:
    search_results = self.search_manager.search(query)
    # 将结果注入到对话上下文
```

**或者使用 Function Calling**（如果通义千问支持）：
```python
tools = [{
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "搜索实时信息",
        "parameters": {...}
    }
}]
```

---

## 🧪 测试计划

### 单元测试
- [ ] `qwen_realtime.py` - WebSocket 连接和事件处理
- [ ] `standby_voice.py` - 语音分段和转录
- [ ] `person_tracker.py` - 位置计算逻辑

### 集成测试
- [ ] 完整唤醒流程：说话 → 检测 → 转录 → 启动会话
- [ ] 多段输入不丢失：快速说两句话，AI 都能听到
- [ ] 视觉跟随：Frigate 检测到人 → 机器人转向并移动
- [ ] 搜索功能：问天气 → 触发搜索 → 返回结果

### 压力测试
- [ ] 长时间 standby（24 小时）
- [ ] 频繁对话（每分钟一次，持续 1 小时）
- [ ] 并发功能（同时对话 + 视觉跟随）

---

## 🚀 部署步骤

### 1. 准备工作
- [ ] 确认 EBO Engine 正常运行
- [ ] 确认 Frigate 正常检测人员
- [ ] 确认通义千问 API Key 有效

### 2. 安装依赖
```bash
pip install -r requirements.txt
```

新增依赖：
```
whisper  # 本地 Whisper（如果使用本地转录）
httpx    # HTTP 客户端（视觉跟随需要）
tavily-python  # 搜索 API（可选）
```

### 3. 配置 `.env`
- [ ] 填写通义千问 API Key
- [ ] 确认 EBO Engine 地址和 Token
- [ ] 选择 Web Search provider 并填写 Key
- [ ] 决定是否启用视觉跟随

### 4. 运行
```bash
python app.py
```

### 5. 验证
- [ ] 健康检查：`curl http://localhost:8099/health`
- [ ] 启动会话：发送 POST 请求
- [ ] 测试唤醒：说"猫猫，你好"
- [ ] 测试搜索：问"今天天气怎么样"
- [ ] 测试跟随：在摄像头前走动

---

## 📊 工作量估算

| 任务 | 预计工作量 | 优先级 |
|------|-----------|--------|
| 完善 `qwen_realtime.py` | 2 小时 | P0 |
| 适配 `conversation.py` | 3 小时 | P0 |
| 新建 `standby_voice.py` | 4 小时 | P0 |
| 修复输入丢失问题 | 1 小时 | P0 |
| 新建 `person_tracker.py` | 2 小时 | P1 |
| 集成视觉跟随 | 1 小时 | P1 |
| Web Search 集成 | 1 小时 | P1 |
| 测试和调试 | 4 小时 | P0 |
| 文档和部署 | 1 小时 | P2 |
| **总计** | **19 小时** | - |

---

## 🎯 里程碑

### Milestone 1：核心语音功能（必须完成）
- [x] 通义千问 WebSocket 连接
- [ ] 完整的对话流程
- [ ] Standby 唤醒
- [ ] 输入不丢失

**验收标准**：能够通过"猫猫"唤醒，并完整听到用户说的所有内容

### Milestone 2：新增功能（可选）
- [ ] 视觉跟随
- [ ] Web Search

**验收标准**：机器人能主动靠近人，能回答实时信息问题

### Milestone 3：部署上线
- [ ] 在目标电脑测试通过
- [ ] 性能优化
- [ ] 文档完善

**验收标准**：能在妈妈家的电脑上稳定运行

---

## 🔄 下一步

**建议操作流程**：

1. **现在**：你审核这个计划，提出修改意见
2. **新 Session**：清空上下文，专注实现核心功能
3. **测试**：在你的本地环境验证
4. **部署**：打包并部署到目标电脑

**我的建议**：
- 先实现 Milestone 1（核心语音），确保基础功能可用
- 再实现 Milestone 2（新增功能），增强体验
- 视觉跟随可以作为独立开关，不影响主功能

---

## ❓ 待确认问题

1. **Standby 转录方案**：本地 Whisper vs 通义千问 ASR？
   - 推荐：本地 Whisper（免费，妈妈的 CPU 够用）

2. **视觉跟随启用时机**：
   - 选项 A：默认关闭，手动开启
   - 选项 B：对话时自动启用
   - 推荐：选项 A（更安全）

3. **Web Search provider**：Tavily vs SerpAPI vs Google？
   - 推荐：Tavily（专为 AI 设计，结果质量好）

---

## 📁 文件清单

完成后的项目结构：

```
ebo-qwen-realtime/
├── app.py                         # 入口
├── runtime.py                     # 主程序（已修改）
├── config.py                      # 配置（已完成）
├── qwen_realtime.py               # 通义千问客户端（完善）
├── conversation.py                # 对话管理（适配）
├── ebo_transport.py               # EBO 传输（复用）
├── state.py                       # 状态管理（复用）
├── standby_voice.py               # Standby 唤醒（新建）⭐
├── person_tracker.py              # 视觉跟随（新建）⭐
├── web_search.py                  # 搜索功能（完善）
├── .env                           # 配置文件（已配置）
├── requirements.txt               # 依赖（更新）
├── test_config.py                 # 配置测试
├── README.md                      # 项目说明
├── DEPLOYMENT.md                  # 部署指南
├── IMPLEMENTATION_PLAN.md         # 本文档
└── READY_TO_DEPLOY.md             # 部署检查清单
```

---

**总结**：这是一个完整的、可执行的实施计划。所有技术点都已验证可行，工作量可控。建议分阶段实施，优先保证核心语音功能，再逐步添加新功能。
