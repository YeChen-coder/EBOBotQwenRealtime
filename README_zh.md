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

好的，开始吐槽。

当前版本加上了自动跟人的功能，结果围绕这个功能又迎来了新一轮的问题。

其实之前就说过，因为它是一个鱼眼镜头，确实需要对它进行调参和适配。最开始配置的那套虽然调了，但其实不太行。不过上一轮优化已经对判断人是远是近做了调整，所以问题其实不在这儿。

核心问题在于：它现在往前追人或者转向的动力太小了，无法克服地板对轮子的摩擦力，所以在追人这块效果不太好。

能改是能改的，但是得等 token 重置。

此外它还有问题。首先说明一下，因为现在这个项目运行在我的电脑上，跟 Ebo bot 之间隔着半个地球，所以虽然我这边电脑请求 AI 没有问题、本地时延可以，但一来一回跑了整整一个地球，基础时延本来就比较高，起码在 2 到 2.5 秒级别。

之前给它加了一层过滤，是因为 Qwen 这个模型本身不带 noise reduction。不做降噪就无法过滤掉环境里电视的声音，这部分只能由我来处理。具体做法之前也讲过：加了一层 layer，先用一个小模型判断当前这句话到底是不是用户在跟机器人讲。

但这么一层层 filtering layer 加下来，叠加上原本的网络时延，现在用户说一句话要 5 秒它才会回答。这就很容易出现一种情况：用户说完第一句话稍微顿了一下，接着说第二句，这时机器人那边才返回第一句的回答并念出来，跟用户之间始终隔着一个非常大的 gap。我自己测下来，5 秒确实太长了。

不过这个问题好解：只要把它部署在机器人旁边那台电脑（我家人那台电脑）上，这些问题就都不复存在了。所以这个先放一边，还不算核心问题。

真正严重的问题看这张图，这是昨天的记录：它又开始重复自己的话了。我先补充一下，因为给它设置了两层 transcribe，重复次数其实已经减半了，但就算减半，它还是重复了自己好几句，这个才是真正的问题。

<img width="551" height="628" alt="image" src="https://github.com/user-attachments/assets/47248552-5a2a-4ff2-b39d-7874fc31a76f" />

下面是 Claude Code 分析出来的，但讲道理我其实没太懂这件事情。

我确实不是很理解，为什么电视或者手机短视频的声音，会有这种"做一件事重复三遍"的情况。我确实没想明白这个场景到底是怎么回事，自然也就没搞懂它这边问题究竟是出在哪儿。不过先照着用它的 fix 跑一下吧。

其实这一切的原因，都是因为 noise reduction 的问题。之前用 OpenAI 的 Realtime 模型时，它能很好地把电视声、东西掉落声这类杂音区分出来，所以用 OpenAI Realtime 的时候完全没有这个问题。但现在这个 Qwen 它不是不支持嘛，所以还是得对它做一些针对现实物理环境的适配

claude code:

Why it kept repeating "我在听":

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

反正就是又出问题了，看这张图吧。这张图确实有点糊，因为当时环境太黑了。

右下角那个白色的东西是充电桩。但充电桩现在处于背过身的状态，导致它发的信号小机器这边根本收不到，直接让小机器人在原地完美转了好几圈。尝试了很多次，它都没办法自己回到充电桩上。

这种硬件设计的问题，我真不觉得是我在软件这边能解决的，我现在很懵

<img width="1705" height="858" alt="image" src="https://github.com/user-attachments/assets/a2f65124-3300-409e-8bae-0537e4e25715" />

现在暂时没有解决方法，是我手动处理的 - 不对，这个问题还挺大的。

我手动登上了原版的 App，手动发送了让它回去的命令：

1. 我在画面中能清晰看到充电桩（夜视模式确实是打开的，画面右下角确实有充电桩）。
   
2. 我也手动给它调了位置，确保它不是跟充电桩完全背离。
   
3. 但它还是跳过充电桩，继续往前走。这个现在肯定是有大问题。

另外，我本来设定的功能是：当用户跟它说让它回去时，它能够自己触发回充电桩的指令。但看下图的聊天记录，这个指令并没有发出。

我现在在摄像头观测到的情况，以及用户那边的反馈，都是说它只回复了，但实际根本没有动。现在正在排查这个问题

<img width="462" height="232" alt="image" src="https://github.com/user-attachments/assets/45115599-2862-4ea7-b0b4-75a21f13819f" />

原因： 用户说的是“嗯，回去吧”和“回去吧”，没有“充电”两个字。我的程序只有看到“充电”“回窝”“回去休息”这类词，才会判断要不要回充电座，所以这两句根本没触发回充。但对话模型听懂了意思，提示词里又写着“有人让你回去，你会自动回去”，于是它答应了“好嘞，我这就回去充电啦”，实际上没有任何命令发出去。-补充一下：现在在这层上用的是 Whisper 的本地模型，而且选的是 base，因为这样快一点。反正目前在我电脑上，base 这个级别的推理速度大概是 0.3 秒；如果往上提一点，时间就会延长。我觉得这里能优化，改成small了，可是因为它最终不会抛在我的电脑上，而是在家人的电脑上，两台电脑配置有差别，具体的 CPU 占用会不会影响用户平时的日常使用，也只能等差不多搞好之后，放到家人那边的电脑上实地跑一下看看，做一些实测。所以这个问题我现在不打算深究.

然后就是还注意到一个问题：它那个语音识别的时候，会把“充电”给听成“通电”。那所以等于没有触发关键词，那就没有触发了。 解决方案就是再把触发词搞得更加容易一点，比如各种“通电”、“通电吧”、“回去”、“回家”，把这些词全给加上吧。

既然谈到这里了，那就顺手把当前 filtering 的逻辑先理一遍吧。

按“本地”和“云端”分开列：

**本地（在这台电脑上跑，不花钱）**

| 用途 | 具体是什么 |
|---|---|
| 判断“有人说完一句话” | WebRTC VAD（`webrtcvad`，灵敏度档位 2）。不是 AI 模型，是一套声音检测算法 |
| 待机时把语音转成文字 | **Whisper small**（faster-whisper 版本，模型 `Systran/faster-whisper-small`，CPU，int8 量化），固定按中文识别 |
| 转文字前先滤掉非人声 | **Silero VAD**（faster-whisper 自带的小模型） |
| 繁体转简体 | OpenCC（文字转换工具，不是模型） |
| 唤醒词按读音比对 | pypinyin 转拼音 + 读音相似度规则（工具，不是模型） |
| 识别画面里的人（平时跟随） | **YOLOv9-s**，输入 320×320，在 **Frigate** 里用 OpenVINO 跑在 CPU 上 |
| 转圈找人时再看一眼 | 同一个 **YOLOv9-s 320** 模型，用 onnxruntime 在 CPU 上跑 |
| 机器人麦克风降噪 | Agora（声网）SDK 自带的降噪，在 Engine 里开启 |

**云端（阿里云百炼 / 通义千问，按量收费）**

| 用途 | 具体模型 |
|---|---|
| 实时语音对话（直接听、直接说） | **`qwen3.8-omni-flash-realtime`**，声音：**龙安灵心 `longanlingxin`**，开了联网搜索 |
| 对话中把用户的话转成文字（用来判断要不要回答、写记录） | **`qwen3-asr-flash-realtime`** |
| 判断“是不是在跟机器人说话”“是不是让它回去充电”“是不是让它闭嘴” | **`qwen-plus`**（文本模型，只回答 yes/no） |

- Whisper small：这台电脑上每句话约 **2.6 秒**；之前的 base 约 0.75 秒。
- YOLOv9-s：Frigate 里每次识别约 **10～18 毫秒**；转圈找人时每次约 70～100 毫秒（整张画面加左右两半，一共识别 3 次）。

下面是全流程，太长了，直接粘过来， claude code 讲的比较明白：

好，按流程写，每一步标上用的是什么模型或工具。

## 一、语音

**A. 待机时（还没开始聊天）**
1. **听到声音，判断一句话说完了**（停顿约 0.9 秒）：WebRTC VAD，本地的声音检测算法，不是 AI 模型。
2. **转成文字**：本地 **Whisper small**（`Systran/faster-whisper-small`，CPU）。转之前先用 **Silero VAD**（faster-whisper 自带）滤掉不是人声的部分；转完用 **OpenCC** 把繁体转成简体。
3. **过滤没用的**（空的、“嗯嗯”、同一个词重复好几遍、零碎英文）：本地规则。
4. **安静模式中？**（15 分钟内有人说过“闭嘴”）：只认唤醒词，其他话不理，不调用任何云端模型。
5. **是不是“闭嘴”类的话？** 先本地查关键词，有的话再请云端 **`qwen-plus`** 确认。确认是 → 进入安静模式。
6. **是不是“回去充电”类的话？** 本地按读音查关键词（“空店”“通店”也算），有的话请 **`qwen-plus`** 确认。确认是 → 回充电座，**18 分钟不追人**。
7. **有没有唤醒词“猫猫 / 小朋友”？** 用 **pypinyin** 转拼音后按读音比对（“毛毛”“小盆友”也算），本地完成。有 → 马上醒。
8. **没有唤醒词** → 请 **`qwen-plus`** 判断“是不是在跟机器人说话”，同时参考最近一分钟听到的话，用来挡掉电视。是 → 醒。

**B. 对话中**
1. **判断一句话说完了**：WebRTC VAD（本地）。
2. **把声音发给对话模型**：云端 **`qwen3.8-omni-flash-realtime`**。
3. **拿到这句话的文字**：云端 **`qwen3-asr-flash-realtime`** 转写；3 秒没回来，就用本地 **Whisper small** 再听一遍。
4. **没内容** → 不回答。
5. **“闭嘴”类**（**`qwen-plus`** 确认）→ 不回答，结束对话，进入安静模式。
6. **不是在跟它说话**（**`qwen-plus`** 判断，比如电视声）→ 不回答。
7. **“回去充电”类**（**`qwen-plus`** 确认）→ 回充电座，18 分钟不追人，同时照常回答。如果它自己说了“我这就回去”，本地规则也会让它真的回去。
8. **正常回答**：**`qwen3.8-omni-flash-realtime`** 直接用声音回答，声音是**龙安灵心（`longanlingxin`）**，需要时会联网搜索。
9. **4 分半钟没有真正的交流** → 结束对话，回到待机。

另外，机器人麦克风的声音在进入以上流程之前，先经过 **Agora（声网）SDK 降噪**，这一步在 Engine 里完成。

## 二、图像（视觉跟随）

**A. 平时看人**
1. **取画面**：Engine 把机器人摄像头的画面转成视频流，**640×360，每秒 5 帧**。
2. **识别人**：**Frigate** 在画面有变化的区域运行 **YOLOv9-s（输入 320×320，OpenVINO，CPU）**。
3. **算作“人”的门槛**：单帧分数 ≥ 0.5 开始跟踪；连续几帧的分数中位数 ≥ **0.6**；框的面积 ≥ **2500 像素**（太小的，比如窗外的剪影，不算）。满足后才通知我们的程序。
4. **我们的程序再筛一遍**：丢掉太小的框，丢掉包住真人的“幽灵框”；继续跟当前的人，没有当前的人就选画面里最大的那个。

**B. 怎么跟（本地规则，不用模型）**
1. **在充电座上** → 先笔直开出去（速度 70，2.5 秒），不在座上原地转。
2. **人不在画面中间**（偏差超过 6 度）→ **按角度转向**，一次转过去（最多 60 度）。
3. **对准了、人还很远**（框高度不到画面一半）→ 往前开：很远开 2 秒，稍远开 1.2 秒，速度 70。
4. **太近**（框高度 ≥ 80%）→ 停住，**绝不倒车**。
5. **对准过、一直没动的人**（比如睡觉、看电视的人）→ 不再反复调整。
6. 每动一下，都要等画面稳定、拿到新的识别结果，再决定下一步。
7. **以下情况不动**：助手被暂停、有人正在说话、“回去充电”后的 18 分钟冷却中。

**C. 找不到人时**
1. **5 分钟没看到人**：
   - 离充电座不远，或者位置不确定（比如程序刚重启）→ **直接回充电座**。
   - 走远了 → **转圈找人**。
2. **转圈找人**：每次转 30 度，共 24 次（两圈）。每停一个方向，用 **YOLOv9-s 320（onnxruntime，CPU）**自己看一眼：门槛 0.5，整张画面加左右两半各看一次；隔 1 秒再看一次，两次都看到才算。
3. 找到人 → 停止转圈，转过去跟。转满两圈还没人 → 回充电座。回充 5 分钟后还没开始充电，就再试一次。

**D. 后台定期做的事**
- **每 10 秒**读一次机器人状态（是否在充电），用来判断它在不在充电座上。
- 发现**桌面模式**开着就自动关掉（机器人一直在地上）。
- **每 6 小时**把夜视设成“自动”一次。

---

然后再提一个单纯是“巧了”的问题，它不是一个真正的问题。

本来设定的是，如果用户让他回去充电，他回到充电桩上是要冷却 10 分钟的，因为一般在这种场景下用户不想被他打扰。但我刚才观察到用户在用的时候，我很确定还没到 10 分钟，小机器人回到充电桩之后，哗的一下又出来了。

不过这其实是个乌龙：排查之后发现只是因为现在还在频繁修 bug，恰好在那个时间 Claude Code 给他发了一个重新部署的命令，导致服务重启了，他就忘了之前设定的冷却 10 分钟这回事。

我觉得这个无所谓，日常使用时本来也不会频繁部署、频繁重启，在日常场景中不是问题，不用怎么管。

顺手提到这个问题，我其实觉得 10 分钟可能有点短，尤其是在家里这种长期场景下确实偏短，所以暂时先改成 18 分钟了。

如果之后还是觉得短，那就提到 25 分钟；如果嫌长，再往下降到 13、14 分钟左右.

补充一下：因为现在仍然处于一个频繁重启、性能 debug 的阶段，为了不太影响家人，我又在 dashboard 上加了个手动冷却的按钮。

这样我这边能主动控制它，并且设置一段冷却时间（这个是每 10 秒刷新一次状态，所以有的时候会有延迟。这个不是问题，是 expected 的，你多等会儿它状态刷新就好了）。省得它一重启、看到人，话哗地一下又全出来了，就是，关爱一下开发者（也就是我）的心脑血管健康。

我家人那边倒没说什么，只是我看着它火有点大。以及顺手加了一个对应的“停止冷却，开始干活”的功能。

当前dashborad:

中文版本：

<img width="1763" height="1289" alt="Screenshot_19-10-2026_132026_127 0 0 1" src="https://github.com/user-attachments/assets/b1033504-d763-4b9e-bd6f-133397ed3974" />

英文版本：

<img width="1868" height="1425" alt="Screenshot_19-10-2026_132222_127 0 0 1" src="https://github.com/user-attachments/assets/2b360bcb-82b1-422b-804e-7512d90985cd" />

---
