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

The project is **self-contained**: the robot adapter (EBO Engine), person detection (Frigate), MQTT and the assistant all live here. It does not depend on `ebo-ai-home`.

## Components

| Service | Role | Location |
|---|---|---|
| `assistant` | Voice assistant + dashboard | Python code in the repository root |
| `ebo-engine` | Enabot cloud protocol adapter (mic, speaker, video, movement) | `engine/`, vendored from [Playcolors-co/ha-enabot](https://github.com/Playcolors-co/ha-enabot) (MIT) with local changes; see `engine/VENDORED.md` |
| `frigate` | Person detection for visual following | `frigate/config.yml` |
| `mosquitto` | Frigate event bus | `frigate/mosquitto.conf` |

Main modules: `runtime.py` (main loop, dashboard API), `conversation.py` (one conversation), `qwen_realtime.py` (Qwen Realtime client), `standby_voice.py` (standby wake), `addressee.py` (is this speech for the robot?), `person_tracker.py` (following + auto-docking), `ebo_transport.py` (audio transport to the Engine), `monitoring.py` (logs and dashboard).

## Start / stop

- The **Start EBO Assistant** / **Stop EBO Assistant** shortcuts on the desktop (that is, `scripts/start.ps1` and `scripts/stop.ps1`)
- Or from the command line: `docker compose up -d` / `docker compose stop`

The start script starts Docker Desktop if needed → stops the old `ebo-ai-home` container, which would otherwise fight over the robot → brings everything up → waits for readiness → opens the dashboard.

- Dashboard (status, live video, conversations, logs): http://127.0.0.1:8099/
- Robot control panel: http://127.0.0.1:8101/ (no login, bound to localhost)

## Deploying from scratch

1. Install Docker Desktop.
2. Copy `.env.example` to `.env` and fill in your Qwen (DashScope) API key and Engine token.
3. Configure the Engine with your own Enabot account and the app crypto keys. This project does not ship them; see `engine/README.md`. Robot pairing data lives in `data/engine/`.
4. Export the Frigate model `frigate/models/yolov9-s-320.onnx` using the command in [`frigate/models/README.md`](frigate/models/README.md). The weights are GPL-3.0 and are not committed.
5. `docker compose up -d`, or double-click the start script.

To move to another PC, just copy the whole project folder (including `.env`, `data/engine/` and the model file), install Docker Desktop and double-click the start script. The first start builds the images and downloads the Whisper model. For 24/7 use, set Docker Desktop to start at login and keep the PC from sleeping.

## Config and data (never commit or share)

- `.env`: Qwen key, Engine token and feature switches; see `.env.example`
- `data/engine/`: robot account and pairing data (`options.json` etc.)
- `data/`: transcripts, reply recordings (kept 7 days), Whisper model cache, Frigate database
- `logs/assistant.log`: assistant log (rotated automatically)

> ⚠️ Unofficial hobby project, not affiliated with Enabot. The Engine talks to the Enabot cloud through reverse engineering, using your own account and devices.

---

## Development log

Okay, rant time.

This version adds automatic person-following, and of course that one feature brought a whole new round of problems with it.

I said this before: the camera is a fisheye lens, so it really does need tuning and adapting. The first config I set up was tuned, but it wasn't great. The last round of optimization already fixed how it judges whether a person is near or far, though, so that's not where the problem is.

The core problem is that when it drives forward to chase someone or turns, there isn't enough power to overcome the friction between the wheels and the floor. So chasing people doesn't work very well.

It can be fixed, but I have to wait for my tokens to reset.

There are more problems. First, some context: this project currently runs on my computer, and the EBO bot is half a planet away. My computer has no trouble calling the AI and local latency is fine, but every round trip goes all the way around the Earth, so the baseline latency is already high, at least 2 to 2.5 seconds.

Earlier I added a filter layer because the Qwen model has no built-in noise reduction. Without noise reduction there's no way to filter out the TV in the room, so that part has to be handled by me. I explained the approach before: an extra layer where a small model first decides whether the current sentence is actually the user talking to the robot.

But stack up filtering layer after filtering layer on top of the network latency, and now the user says something and waits 5 seconds for an answer. That easily leads to this: the user says the first sentence, pauses a little, starts the second one, and only then does the robot return and read out its answer to the first. There's always a huge gap between it and the user. I tested it myself, and 5 seconds really is too long.

This one is easy to solve, though: once it's deployed on the computer next to the robot (my family's computer), all of this goes away. So I'm setting it aside. It's not the core problem yet.

The really serious problem is in this screenshot, from yesterday's log: it started repeating itself again. To be fair, since I gave it two layers of transcription the number of repeats has already been cut in half, but even halved it still repeated itself several times. That's the real problem.

<img width="551" height="628" alt="image" src="https://github.com/user-attachments/assets/47248552-5a2a-4ff2-b39d-7874fc31a76f" />

Below is Claude Code's analysis, but honestly I didn't really get it.

I genuinely don't understand why TV audio or short videos on a phone would produce this "same phrase three times" thing. I couldn't figure out what the scenario actually is, so naturally I didn't get where the problem was coming from either. Anyway, let's just run its fix first.

The root cause of all of this is really noise reduction. When I used OpenAI's Realtime model, it was very good at telling apart TV audio, things falling on the floor and other noise, so this problem simply didn't exist with OpenAI Realtime. But Qwen doesn't support that, so it still needs some adapting to the real physical environment.

claude code:

Why it kept repeating "我在听" ("I'm listening"):

False wake: at 02:53 the TV was on, and Whisper produced a classic hallucination, "做一件事做一件事做一件事" (one phrase repeated three times). The AI judge took it as someone talking to the robot and woke it.
My timeout rule did the rest: after each turn the program waits for Qwen’s transcript. Under the old rule, if it wasn’t back within 6 seconds it replied anyway. Qwen’s transcription of TV noise is often slow (over 6 seconds) and comes back empty, so the model received empty turns again and again and could only answer "我在听".
It couldn’t stop:
Late transcripts were applied to the next turn as that turn’s text.
Each of those replies counted as activity, so the conversation never timed out. It went on for nearly half an hour.

Fixes:

No more blind reply on timeout: if Qwen’s transcript isn’t back within 3 seconds, this turn’s audio is transcribed with local Whisper instead (about 0.5 seconds). Empty means silence; real speech is still answered.
Transcripts are matched by turn ID: late ones are discarded, never applied to another turn.
Repeated-phrase hallucinations are filtered: text like "做一件事做一件事做一件事" no longer wakes it. Ordinary repetition like "你好你好" is unaffected.

---

Anyway, something broke again. See this screenshot. It's a bit blurry because the room was very dark at the time.

The white thing in the bottom-right corner is the charging dock. But the dock is facing away, so the little robot can't pick up its signal at all, and it just spun perfectly in place, several times. I tried many times and it never managed to get back onto the dock by itself.

A hardware design problem like this is honestly not something I think I can fix on the software side. I'm pretty lost right now.

<img width="1705" height="858" alt="image" src="https://github.com/user-attachments/assets/a2f65124-3300-409e-8bae-0537e4e25715" />

No fix for now; I handled it manually. Actually no, this problem is pretty big.

I logged into the official app and manually sent the command to go back:

1. I could clearly see the dock in the video (night vision was definitely on, and the dock was definitely in the bottom-right corner).
   
2. I also manually adjusted its position to make sure it wasn't facing completely away from the dock.
   
3. It still skipped past the dock and kept driving forward. Something is definitely very wrong here.

Also, the feature I set up was: when the user tells it to go back, it triggers the return-to-dock command by itself. But look at the chat log below: that command was never sent.

What I'm seeing on the camera, and what the user is telling me, both say it only replied and didn't actually move at all. Looking into this now.

<img width="462" height="232" alt="image" src="https://github.com/user-attachments/assets/45115599-2862-4ea7-b0b4-75a21f13819f" />

Cause: the user said "嗯，回去吧" ("mm, go back") and "回去吧" ("go back"), without the word "充电" ("charge"). My program only considers returning to the dock when it sees words like "充电" ("charge"), "回窝" ("back to your nest") or "回去休息" ("go back and rest"), so neither sentence triggered it. But the conversation model understood the meaning, and the prompt says "if someone tells you to go back, you'll go back automatically", so it agreed, "好嘞，我这就回去充电啦" ("Okay, heading back to charge now"), while no command was actually sent. - One more note: this layer currently uses a local Whisper model, and I picked base because it's faster. On my computer, base takes about 0.3 seconds per inference; go one size up and it gets slower. I think there's room to optimize here, so I switched it to small. But since it won't end up running on my computer but on my family's, and the two machines have different specs, whether the CPU usage will get in the way of their everyday use can only be found out by putting it on their computer once things are mostly done and running some real tests there. So I'm not digging into this one for now.

Then I noticed another problem: speech recognition sometimes hears "充电" (chōngdiàn, "charge") as "通电" (tōngdiàn, "power on"). So the keyword isn't matched, and nothing is triggered. The fix is to make the trigger words easier to hit: "通电", "通电吧", "回去" ("go back"), "回家" ("go home") and so on. Just add all of them.

Since we're here, let me walk through the current filtering logic.

Split into local and cloud:

**Local (runs on this computer, free)**

| Purpose | What exactly |
|---|---|
| Detect "someone finished a sentence" | WebRTC VAD (`webrtcvad`, aggressiveness level 2). Not an AI model, a voice-activity detection algorithm |
| Transcribe speech while in standby | **Whisper small** (faster-whisper build, model `Systran/faster-whisper-small`, CPU, int8 quantization), fixed to Chinese |
| Filter out non-speech before transcribing | **Silero VAD** (small model bundled with faster-whisper) |
| Traditional → Simplified Chinese | OpenCC (text conversion tool, not a model) |
| Match wake words by pronunciation | pypinyin to pinyin + pronunciation-similarity rules (tools, not models) |
| Detect people in the video (normal following) | **YOLOv9-s**, 320×320 input, running in **Frigate** on CPU via OpenVINO |
| Double-check during search spins | The same **YOLOv9-s 320** model, running on CPU via onnxruntime |
| Robot microphone noise reduction | Built-in noise reduction of the Agora SDK, enabled in the Engine |

**Cloud (Alibaba Cloud Model Studio / Qwen, pay per use)**

| Purpose | Model |
|---|---|
| Realtime voice conversation (listens and speaks directly) | **`qwen3.8-omni-flash-realtime`**, voice: **Long'an Lingxin `longanlingxin`**, web search on |
| Transcribe the user's speech during conversation (to decide whether to answer, and for the log) | **`qwen3-asr-flash-realtime`** |
| Decide "is this for the robot?", "is this asking it to go charge?", "is this asking it to shut up?" | **`qwen-plus`** (text model, answers yes/no only) |

- Whisper small: about **2.6 seconds** per sentence on this computer; base used to be about 0.75 seconds.
- YOLOv9-s: about **10–18 ms** per detection in Frigate; about 70–100 ms per check during search spins (full frame plus left and right halves, 3 detections in total).

Below is the full pipeline. It's too long, so I'm pasting it straight in. Claude Code explains it pretty clearly:

Okay, here it is step by step, with the model or tool used at each step.

## 1. Voice

**A. Standby (no conversation yet)**
1. **Hear sound, decide the sentence is finished** (pause of about 0.9 s): WebRTC VAD, a local voice-detection algorithm, not an AI model.
2. **Transcribe**: local **Whisper small** (`Systran/faster-whisper-small`, CPU). Before transcribing, **Silero VAD** (bundled with faster-whisper) filters out non-speech; afterwards **OpenCC** converts Traditional to Simplified Chinese.
3. **Drop useless text** (empty, "嗯嗯" ("mm-hmm"), the same word repeated several times, stray English fragments): local rules.
4. **In quiet mode?** (someone said "shut up" within the last 15 minutes): only wake words are accepted, everything else is ignored, no cloud model is called.
5. **Is it a "shut up" type phrase?** Check keywords locally first; if there's a hit, ask cloud **`qwen-plus`** to confirm. Confirmed → enter quiet mode.
6. **Is it a "go charge" type phrase?** Check keywords locally by pronunciation ("空店" kōngdiàn and "通店" tōngdiàn count too); if there's a hit, ask **`qwen-plus`** to confirm. Confirmed → return to the dock, **no following for 18 minutes**.
7. **Is there a wake word, "猫猫 / 小朋友" ("kitty / little one")?** Convert to pinyin with **pypinyin** and compare by pronunciation ("毛毛" and "小盆友" count too), all done locally. Yes → wake immediately.
8. **No wake word** → ask **`qwen-plus`** "is this for the robot?", also looking at what it heard in the last minute, to block out the TV. Yes → wake.

**B. During a conversation**
1. **Decide the sentence is finished**: WebRTC VAD (local).
2. **Send the audio to the conversation model**: cloud **`qwen3.8-omni-flash-realtime`**.
3. **Get the text of this sentence**: transcribed by cloud **`qwen3-asr-flash-realtime`**; if it's not back in 3 seconds, local **Whisper small** listens again.
4. **No content** → don't answer.
5. **"Shut up" type** (confirmed by **`qwen-plus`**) → don't answer, end the conversation, enter quiet mode.
6. **Not talking to it** (judged by **`qwen-plus`**, e.g. TV audio) → don't answer.
7. **"Go charge" type** (confirmed by **`qwen-plus`**) → return to the dock, no following for 18 minutes, and answer as usual. If it says "I'm heading back now" by itself, a local rule makes it actually go back too.
8. **Normal answer**: **`qwen3.8-omni-flash-realtime`** answers directly in voice, using the **Long'an Lingxin (`longanlingxin`)** voice, with web search when needed.
9. **No real interaction for 4.5 minutes** → end the conversation, back to standby.

Also, before the robot's microphone audio enters any of the above, it goes through **Agora SDK noise reduction**, which happens in the Engine.

## 2. Vision (visual following)

**A. Watching for people**
1. **Get the video**: the Engine turns the robot camera feed into a video stream, **640×360 at 5 fps**.
2. **Detect people**: **Frigate** runs **YOLOv9-s (320×320 input, OpenVINO, CPU)** on the regions of the frame that changed.
3. **Threshold for "person"**: single-frame score ≥ 0.5 to start tracking; median score over several frames ≥ **0.6**; box area ≥ **2500 pixels** (tiny ones, like silhouettes outside the window, don't count). Only then is our program notified.
4. **Our program filters again**: drop boxes that are too small, drop "ghost boxes" that wrap around a real person; keep following the current person, or if there isn't one, pick the largest person in the frame.

**B. How it follows (local rules, no model)**
1. **On the dock** → drive straight out first (speed 70, 2.5 s); never spin in place on the dock.
2. **Person not centered** (off by more than 6 degrees) → **turn by angle**, all in one go (up to 60 degrees).
3. **Centered, person still far away** (box height under half the frame) → drive forward: 2 s if very far, 1.2 s if a bit far, speed 70.
4. **Too close** (box height ≥ 80%) → stop, **never reverse**.
5. **A person it has already centered on who hasn't moved** (e.g. sleeping, watching TV) → stop readjusting.
6. After every move, wait for the video to settle and a fresh detection before deciding the next step.
7. **Don't move when**: the assistant is paused, someone is talking, or the 18-minute cooldown after "go charge" is running.

**C. When nobody is found**
1. **No person seen for 5 minutes**:
   - Not far from the dock, or position uncertain (e.g. the program just restarted) → **go straight back to the dock**.
   - Far away → **search spin**.
2. **Search spin**: turn 30 degrees at a time, 24 times (two full turns). At each stop, check with its own **YOLOv9-s 320 (onnxruntime, CPU)**: threshold 0.5, one check each on the full frame and the left and right halves; check again 1 second later, and it only counts if both checks see someone.
3. Person found → stop spinning, turn over and follow. Two full turns and still nobody → back to the dock. If it still isn't charging 5 minutes after returning, try again.

**D. Periodic background tasks**
- **Every 10 seconds**, read the robot's status (charging or not) to tell whether it's on the dock.
- If **desktop mode** is on, turn it off automatically (the robot is always on the floor).
- **Every 6 hours**, set night vision to "auto".

---

Next, a problem that was purely a coincidence. It isn't a real problem.

The setup is: if the user tells it to go charge, it goes back to the dock and cools down for 10 minutes, because in that situation the user usually doesn't want to be bothered. But just now I watched the user using it, and I'm sure it hadn't been 10 minutes yet when the little robot, back on the dock, suddenly shot right back out.

That turned out to be a mix-up, though: after looking into it, it was only because I'm still fixing bugs constantly, and right at that moment Claude Code sent it a redeploy command. The service restarted, and it forgot all about the 10-minute cooldown.

I don't think this matters. In daily use it won't be redeployed or restarted often, so in everyday scenarios it's not a problem and I don't need to worry about it.

While I'm on this: I actually think 10 minutes may be a bit short, especially for long-term use at home. So for now I've changed it to 18 minutes.

If it still feels short later, I'll raise it to 25 minutes; if it's too long, I'll bring it down to around 13–14 minutes.

One more thing: since I'm still in a phase of frequent restarts and performance debugging, and I don't want to bother my family too much, I added a manual cooldown button to the dashboard.

That way I can take control on my side and set a cooldown period (the status refreshes every 10 seconds, so sometimes there's a delay. That's not a problem, it's expected. Just wait a little and the status will refresh). It saves the case where it restarts, sees someone, and all the talking comes pouring out again. Basically, a little care for the developer's (that is, my) cardiovascular health.

My family hasn't said anything about it; it's just me getting worked up watching it. I also added a matching "end cooldown, back to work" function while I was at it.

Current dashboard:

Chinese version:

<img width="1763" height="1289" alt="Screenshot_19-10-2026_132026_127 0 0 1" src="https://github.com/user-attachments/assets/b1033504-d763-4b9e-bd6f-133397ed3974" />

English version:

<img width="1868" height="1425" alt="Screenshot_19-10-2026_132222_127 0 0 1" src="https://github.com/user-attachments/assets/2b360bcb-82b1-422b-804e-7512d90985cd" />

---
