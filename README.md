# EBO Bot × Qwen Realtime — a voice companion that actually moves

[中文版](README_zh.md)

> **This is the third generation of [EBOBotToDigitalPet](https://github.com/YeChen-coder/EBOBotToDigitalPet), rewritten as a separate, local-only project.** If you want the full story — local + cloud, Diagnostic Agent, monitoring dashboard — start with v1.

## Where this sits in the project family

| Generation | Where | What it is |
|---|---|---|
| **v1 — complete edition** | [`EBOBotToDigitalPet` / `main`](https://github.com/YeChen-coder/EBOBotToDigitalPet/tree/main) | The full story and the main entry point. Home Assistant + EBO Engine + OpenAI Realtime voice assistant. It runs on local Docker or on AWS ECS/Fargate (one at a time), with a local Diagnostic Agent, Host Bridge and dashboard for health checks, safe switching, limited recovery, two-level diagnosis and transcript viewing. It also has a development journal. |
| v1 — cloud side | [`EboBotOnAWSCloud`](https://github.com/YeChen-coder/EboBotOnAWSCloud) | The AWS half of v1, kept in its own repository on purpose so local-only users don't have to carry the cloud parts: Fargate deployment (`ca-central-1`, 1 vCPU / 2 GiB), CloudWatch conversation logs, cost and sizing notes, diagnostic tooling. |
| v2 — Frigate edition | [`EBOBotToDigitalPet` / `frigate`](https://github.com/YeChen-coder/EBOBotToDigitalPet/tree/frigate) | A rewrite around Frigate + MQTT, with audio-first family sessions, personal memory and updated local diagnostics. Local only; its AWS migration was never finished. |
| **v3 — this repository** | `EBOBotQwenRealtime` | A ground-up refactor. Home Assistant is gone and the voice backend moves from OpenAI to Qwen (Tongyi Qianwen) Omni Realtime. It is a single self-contained Docker Compose stack, and the robot finally **gets off the dock and moves**: it sees a person, drives over, and goes back to charge when nobody is around. Local only, with no cloud deployment planned. |

The [version guide](https://github.com/YeChen-coder/EBOBotToDigitalPet/blob/main/VERSION-GUIDE.md) in the original repository explains v1 and v2 in detail.

## What v3 does

A home voice assistant for an Enabot EBO robot that **doesn't need a wake word**:

- **Standby listening on the device.** Local Whisper (faster-whisper, CPU) transcribes speech segment by segment. Saying a wake word ("猫猫", "小朋友") wakes it instantly. Anything else goes to a small model (`qwen-plus`), which decides whether the speech was meant for the robot, so TV dialogue and family members talking to each other are ignored.
- **Realtime conversation** with Qwen `qwen3.8-omni-flash-realtime`, including built-in web search. Server VAD is turned off and turn-taking is detected locally, so the robot's own voice can't interrupt it.
- **Visual following (optional).** Frigate (YOLOv9-s, OpenVINO on CPU) detects people in the robot's camera feed. The robot turns toward them and approaches. After 5 minutes with nobody in view it does a search spin and returns to the dock. "Go charge" sends it home and keeps it there for a rest period.
- **Dashboard** at `http://127.0.0.1:8099/` with status, live video (WebRTC), conversations and logs.

## Components

| Service | Role | Location |
|---|---|---|
| `assistant` | Voice assistant + dashboard | Python code in the repository root |
| `ebo-engine` | Enabot cloud protocol adapter (mic, speaker, video, movement) | `engine/`, vendored from [Playcolors-co/ha-enabot](https://github.com/Playcolors-co/ha-enabot) (MIT) with local changes; see `engine/VENDORED.md` |
| `frigate` | Person detection for visual following | `frigate/config.yml` |
| `mosquitto` | Frigate event bus | `frigate/mosquitto.conf` |

Main modules: `runtime.py` (main loop, dashboard API), `conversation.py` (one conversation), `qwen_realtime.py` (Qwen Realtime client), `standby_voice.py` (standby wake), `addressee.py` (is this speech for the robot?), `person_tracker.py` (following + auto-docking), `ebo_transport.py` (audio transport to the Engine), `monitoring.py` (logs and dashboard).

## Running it

1. Install Docker Desktop.
2. Copy `.env.example` to `.env` and fill in your Qwen (DashScope) API key and Engine token.
3. Configure the Engine with your own Enabot account and the app crypto keys. This project does not ship them; see `engine/README.md`. Robot pairing data lives in `data/engine/`.
4. Export the Frigate model `frigate/models/yolov9-s-320.onnx` using the command in [`frigate/models/README.md`](frigate/models/README.md). The weights are GPL-3.0 and are not committed.
5. `docker compose up -d`. On Windows you can use `scripts/start.ps1` / `scripts/stop.ps1` instead: they start Docker Desktop if needed, bring everything up, wait for readiness and open the dashboard.

- Dashboard: http://127.0.0.1:8099/
- Robot control panel: http://127.0.0.1:8101/ (no login, bound to localhost)

The first start builds the images and downloads the Whisper model. For 24/7 use, set Docker Desktop to start at login and keep the PC from sleeping.

## Never commit

- `.env`: API keys, Engine token, feature switches
- `data/engine/`: robot account and pairing data
- `data/`: transcripts, reply recordings (kept 7 days), Whisper cache, Frigate database
- `logs/`

> ⚠️ Unofficial hobby project, not affiliated with Enabot. The Engine talks to the Enabot cloud through reverse engineering, using your own account and devices.
