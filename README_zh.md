# EBO 机器人 × 通义千问 Realtime：终于会动的语音小伙伴

[English](README.md)

> **这是 [EBOBotToDigitalPet](https://github.com/YeChen-coder/EBOBotToDigitalPet) 的第三代重构，单独开了一个仓库，只在本地跑。** 想看完整的故事（本地 + 云端、Diagnostic Agent、监控看板），请从第一版看起。

## 这个项目的来龙去脉

| 版本 | 位置 | 是什么 |
|---|---|---|
| **v1：完整版** | [`EBOBotToDigitalPet` / `main`](https://github.com/YeChen-coder/EBOBotToDigitalPet/tree/main) | 项目故事的主入口。Home Assistant + EBO Engine + OpenAI Realtime 语音助手；可以在本地 Docker 或 AWS ECS/Fargate 上运行（二选一）。本机有 Diagnostic Agent、Host Bridge 和看板，负责健康检查、安全切换、有限自动恢复、两级诊断和转写记录查看。README 里还有一整篇开发/吐槽/踩坑日志。 |
| v1：云端部分 | [`EboBotOnAWSCloud`](https://github.com/YeChen-coder/EboBotOnAWSCloud) | v1 的 AWS 那一半。故意单独放一个仓库：不是所有人都用云端，不想跟本地版绑得太紧。内容包括 Fargate 部署（`ca-central-1`，1 vCPU / 2 GiB）、CloudWatch 对话日志、成本与降配评估、诊断工具。 |
| v2：Frigate 版 | [`EBOBotToDigitalPet` / `frigate`](https://github.com/YeChen-coder/EBOBotToDigitalPet/tree/frigate) | 围绕 Frigate + MQTT 重写，有语音优先的家庭会话、个人记忆，本地诊断也更新了。只做了本地版，AWS 迁移没做完。 |
| **v3：本仓库** | `EBOBotQwenRealtime` | 推倒重来：去掉 Home Assistant，语音从 OpenAI 换成通义千问 Omni Realtime，整个项目是一个自包含的 Docker Compose。最大的变化是机器人**终于会下充电座、自己动了**：看到人就过去，没人了就回去充电。只在本地跑，不打算做云端。 |

v1 和 v2 的详细区别见原仓库的 [版本导航](https://github.com/YeChen-coder/EBOBotToDigitalPet/blob/main/VERSION-GUIDE.md)。

## v3 能做什么

EBO 机器人的家庭语音助手，**不用唤醒词**，自然接话：

- **待机时在本机听写**：本地 Whisper（faster-whisper，CPU）分段转写。说"猫猫""小朋友"马上回应；其他话交给小模型（`qwen-plus`）判断是不是在跟它说，电视对白和家人之间的话都不理。
- **实时对话**：通义千问 `qwen3.8-omni-flash-realtime`，自带联网搜索。关掉了服务端 VAD，改成在本地判断一句话有没有说完，机器人自己的声音不会打断自己。
- **视觉跟随（可选）**：Frigate（YOLOv9-s，OpenVINO 跑在 CPU 上）从机器人画面里认人，看到人就转过去、靠近。连续 5 分钟没看到人，就原地找一圈，然后回充；跟它说"回去充电"，它会回充电座，并在一段时间内不追人。
- **看板**：http://127.0.0.1:8099/ ，有状态、实时画面（WebRTC）、对话和日志。

本项目是**自包含**的：机器人适配（EBO Engine）、人体检测（Frigate）、MQTT 和助手都在这里，不依赖 `ebo-ai-home`。

## 组成

| 服务 | 作用 | 位置 |
|---|---|---|
| `assistant` | 语音助手 + 看板 | 项目根目录的 Python 代码 |
| `ebo-engine` | 机器人云端协议适配（麦克风、喇叭、视频、移动） | `engine/`，源码来自 [Playcolors-co/ha-enabot](https://github.com/Playcolors-co/ha-enabot)（MIT），有本地修改，见 `engine/VENDORED.md` |
| `frigate` | 人体检测，供视觉跟随 | `frigate/config.yml` |
| `mosquitto` | Frigate 事件总线 | `frigate/mosquitto.conf` |

主要代码：`runtime.py`（主程序、看板接口）、`conversation.py`（一次对话）、`qwen_realtime.py`（通义千问客户端）、`standby_voice.py`（待机唤醒）、`addressee.py`（判断是不是在跟机器人说话）、`person_tracker.py`（视觉跟随与自动回充）、`ebo_transport.py`（与 Engine 的音频传输）、`monitoring.py`（日志与看板）。

## 启动 / 停止

- 桌面上的 **启动EBO助手** / **停止EBO助手**（即 `scripts/start.ps1`、`scripts/stop.ps1`）
- 或命令行：`docker compose up -d` / `docker compose stop`

启动脚本会：必要时启动 Docker Desktop → 停掉会抢机器人的旧 `ebo-ai-home` 容器 → 启动全部组件 → 等就绪 → 打开看板。

- 看板（状态、实时画面、对话、日志）：http://127.0.0.1:8099/
- 机器人控制面板：http://127.0.0.1:8101/ （没有登录保护，只开放给本机）

## 从零部署

1. 装好 Docker Desktop。
2. 把 `.env.example` 复制成 `.env`，填上通义千问（DashScope）API Key 和 Engine Token。
3. 用你自己的 Enabot 账号和 App 加密 key 配置 Engine（本项目不附带这些 key，见 `engine/README.md`）。机器人配对信息在 `data/engine/`。
4. 按 [`frigate/models/README.md`](frigate/models/README.md) 里的命令导出 Frigate 模型 `frigate/models/yolov9-s-320.onnx`（权重是 GPL-3.0，没有放进仓库）。
5. `docker compose up -d`，或者双击启动脚本。

换电脑时，直接复制整个项目文件夹（含 `.env`、`data/engine/` 和模型文件），装好 Docker Desktop 再双击启动脚本就行。第一次启动会构建镜像、下载 Whisper 模型。要长期运行的话：Docker Desktop 设为开机启动，电脑不要睡眠。

## 配置与数据（都不要提交/外传）

- `.env`：通义千问 Key、Engine Token 和各项开关，参考 `.env.example`
- `data/engine/`：机器人账号与配对信息（`options.json` 等）
- `data/`：对话记录、回复录音（保留 7 天）、Whisper 模型缓存、Frigate 数据库
- `logs/assistant.log`：助手日志（自动轮转）

> ⚠️ 非官方的个人项目，与 Enabot 无关。Engine 通过逆向工程与 Enabot 云端通信，使用的是你自己的账号和设备。

---

## 开发日志

（从这里往下续写。）
