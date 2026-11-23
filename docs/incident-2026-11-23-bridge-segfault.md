# Engine 的 bridge 每天崩一次：复盘（2026-11-23）<br>Incident review: the Engine bridge crashed about once a day (2026-11-23)

Engine 里有一个 bridge 进程，负责连声网（Agora），把机器人的麦克风、扬声器和控制指令接到程序里。从 10-17 起，它一共段错误崩溃了 **37 次**（退出码 rc=139），差不多每天一次。每次大约 15–20 秒机器人听不到；如果崩溃时正好在播回复，这句也会丢掉。

The Engine runs a bridge process that connects to Agora and carries the robot's microphone, speaker and control commands into the program. From 10-17 on it segfaulted **37 times** (exit code rc=139), about once a day. Each crash left the robot deaf for about 15–20 seconds, and a reply that was playing at that moment was lost.

| | |
|---|---|
| 表现 Symptom | 日志里出现 `bridge (…) exited (rc=139), restarting in 15s…`，15 秒后自己恢复 <br> `bridge (…) exited (rc=139), restarting in 15s…` in the log; it recovers by itself 15 s later |
| 影响 Impact | 每次约 15–20 秒聋；11-19 08:18 妈妈刚到家，一句回复没播出来 <br> ~15–20 s deaf each time; on 11-19 08:18 a reply to mom, just home, never played |
| 原因 Cause | bridge 重建声网会话的代码：重复初始化声网服务；换下来的 RTM 客户端被 Python 回收后，原生层还在回调它 <br> The code that rebuilds the Agora session: it initialized the Agora service again in the same process, and replaced RTM clients were garbage-collected while the native layer still called them back |
| 修复 Fix | 声网服务每个进程只初始化一次；换下来的连接都正确释放；旧 RTM 客户端一直留着引用；崩溃期间丢掉的回复会补播 <br> Initialize the Agora service once per process; release replaced connections properly; keep old RTM clients referenced; replay a reply lost during a crash |
| 上线 Deployed | 2026-11-23 12:58（多伦多 / Toronto），家里凌晨、机器人待机时 <br> while it was the middle of the night at home and the robot was in standby |

---

## 怎么发现的 / How it was found

每天下午的定时健康检查（ebo-daily-health-review）从 11-14 起几乎每次都报 "Engine 的 bridge 段错误崩溃"，一开始只当偶发、能自愈的问题。11-21 的报告数了一下，从 11-09 起已经崩了 12 次，而且 README 和 docs 里都没记，这才单独开了一次调查。

The daily health check (ebo-daily-health-review) reported "Engine bridge segfault" in almost every run from 11-14 on. At first it was treated as occasional and self-healing. The 11-21 report counted 12 crashes since 11-09, none of them recorded in the README or docs, and that's what started a dedicated investigation.

教训：**能自愈的故障也要计数**。每次只丢十几秒，单看哪一天都不起眼，可一个月就是 37 次，还会丢话。

Lesson: **count self-healing faults too.** Each one costs only a dozen seconds and looks harmless on any single day, but it added up to 37 in a month, and it lost replies.

---

## 怎么查的 / How it was investigated

1. **把每次崩溃前后的日志排在一起看。** Engine 日志在 `data/engine/logs/ebo-engine.log*`（只有时分秒，日期要倒推），助手日志在 `logs/assistant.log*`。对上每次崩溃那一刻：是不是在对话、在推流播放、在续期凭证、断网了、在发视频帧。
   **Lined up the logs around every crash.** Engine log: `data/engine/logs/ebo-engine.log*` (time of day only; dates have to be worked out). Assistant log: `logs/assistant.log*`. For each crash: was a conversation going, was audio streaming, was a credential renewal running, was the network down, were video frames flowing.
2. **找共同点。** 对话、推流、断网都不是每次都有。唯一每次都有的是：**这个进程之前做过至少一次"重新加入声网频道"（rejoin）**。从没 rejoin 过的进程一次都没崩过。
   **Looked for what every crash had in common.** Conversations, streaming and network drops were each missing from some crashes. The one thing every crash had: **the process had done at least one rejoin of the Agora channel earlier.** Processes that never rejoined never crashed.
3. **看崩溃现场（core dump）。** WSL 会自动把崩溃现场存到 `C:\Users\yeche\AppData\Local\Temp\wsl-crashes\`（每个 360–470 MB，只保留最近几个）。日志里那行 `Failed to open ELF file 'python'` 就是这个抓取程序打出来的。主机和容器里都没有 gdb，就用纯 Python 写了个 ELF core 解析脚本，读寄存器、内存映射和线程栈，再对照从容器里拷出来的 `.so` 符号表，看出崩在哪个函数里。
   **Read the crash dumps.** WSL saves them automatically to `C:\Users\yeche\AppData\Local\Temp\wsl-crashes\` (360–470 MB each; only the latest few are kept). The log line `Failed to open ELF file 'python'` comes from that capture tool. With no gdb on the host or in the container, a small pure-Python ELF core parser read the registers, memory maps and thread stacks, then matched addresses against the symbol tables of the `.so` files copied out of the container.

---

## 原因 / Cause

37 次崩溃分两类，都出在"重建声网会话"的代码里。

The 37 crashes fall into two kinds, both in the code that rebuilds the Agora session.

### A 类：重连当场崩（14 次）/ Kind A: crash during the rejoin itself (14)

- 日志顺序固定：`[watchdog] … scheduling rejoin` → `[RTM] login and subscribe ok` → `[audio-apm] requested: …` → `Segmentation fault`，之后等不到 `Agora service initialized`。
  The log always reads `[watchdog] … scheduling rejoin` → `[RTM] login and subscribe ok` → `[audio-apm] requested: …` → `Segmentation fault`, and `Agora service initialized` never follows.
- 3 个 core 都崩在主线程的 `libagora_rtc_sdk.so agora_service_initialize`。
  All 3 cores crashed on the main thread in `libagora_rtc_sdk.so agora_service_initialize`.
- 原因：每 20 小时续期一次凭证时，代码会**再建一个新的声网服务**，旧的不释放；之后 rejoin 又先释放服务、再在同一个进程里重新初始化。声网的原生库不支持这样反复初始化。
  Cause: every 20 hours the credential renewal **created a second Agora service** without releasing the old one; a later rejoin then released the service and initialized it again in the same process. The native SDK doesn't survive being initialized repeatedly like that.

| rejoin 之前同一进程里 / before the rejoin, same process | 成功 OK | 崩溃 crash |
|---|---|---|
| 做过续期 / had a renewal | 4 | 14 |
| 没做过续期 / no renewal | 34 | 0 |

### B 类：重连之后过一阵才崩（23 次）/ Kind B: crash minutes to hours after a rejoin (23)

- 崩之前日志很平静，往往只是在发云台指令，或者刚放完一句回复。
  The log is quiet right before; often a movement command was just sent or a reply just finished.
- 4 个 core 都崩在 RTM 的回调线程：`RtmEventHandlerBridge::onMessageEvent` 或 `::onPublishResult` → libffi 闭包 → 一个已经不属于任何代码的地址。
  All 4 cores crashed on the RTM callback thread: `RtmEventHandlerBridge::onMessageEvent` or `::onPublishResult` → libffi closure → an address that no longer belongs to any code.
- 原因：rejoin 时旧的 RTM 客户端只做了 `logout()` 就把引用丢掉。SDK 的回调表只挂在这个 Python 对象上，而且有循环引用，要等 Python 的垃圾回收随机触发才会被释放。原生层的旧客户端却还活着，之后再回调，就跳进了已经释放的内存。什么时候崩，全看垃圾回收什么时候跑。
  Cause: on a rejoin the old RTM client was only `logout()`-ed and then dropped. The SDK keeps its callback table only on that Python object, inside a reference cycle, so it's freed whenever Python's cycle collector happens to run. The native client is still alive, calls back later, and jumps into freed memory. When it crashes depends on when the collector runs.

### 排除掉的 / Ruled out

- **断网**：11-22 那次只是让 watchdog 触发了 rejoin，真正崩在 initialize。
  **Network drop**: on 11-22 it only triggered the rejoin; the crash was still in initialize.
- **"Invalid buffer size"**：是 bridge 死了、ffmpeg 管道断了之后才报的，是结果不是原因。
  **"Invalid buffer size"**: printed by ffmpeg after the bridge died and its pipe broke; a consequence, not the cause.
- **播放推流、视频**：崩溃线程都不在这两条路径上。
  **Audio streaming, video**: neither crash thread was on those paths.
- **和 11-08 的关系**：[11-08 机器人哑了 13 个半小时](incident-2026-11-08-robot-silent.md)用的是同一段续期代码。当时只修了"推声音的线程还挂在旧连接上"，没处理旧连接、旧服务不释放。
  **Relation to 11-08**: [the 13.5-hour mute on 11-08](incident-2026-11-08-robot-silent.md) came from the same renewal code. That fix only stopped the audio thread from staying on the old connection; it didn't release the old connection or service.

---

## 修复 / Fix

都在 `engine/ebo_bridge.py`：

All in `engine/ebo_bridge.py`:

1. **`_agora_service()`：声网服务每个进程只建一次、只初始化一次。** rejoin、续期、待机断开都只换 RTC 连接，不再释放或重建服务。
   **`_agora_service()`: the Agora service is created and initialized once per process.** Rejoin, renewal and standby replace only the RTC connection, never the service.
2. **`_drop_rtc()`：换下来的 RTC 连接先 `disconnect()` 再 `release()`。** 原来续期时旧连接只断开不释放，Python 对象一丢，它身上的观察者也可能被回收。
   **`_drop_rtc()`: a replaced RTC connection is `disconnect()`-ed and then `release()`-d.** Renewal used to only disconnect it; once the Python object was dropped, its observers could be collected too.
3. **`_retire_rtm()`：换下来的 RTM 客户端登出后放进 `self._retired_rtm`，一直留着引用，回调表就不会被回收。** 旧客户端迟到的消息由一个"代号"挡掉，不再进 `_on_rtm`。每次 rejoin 只多占一点内存，bridge 一重启就清空。
   **`_retire_rtm()`: a replaced RTM client is logged out and kept in `self._retired_rtm`, so its callback table is never collected.** Late messages from it are dropped by a generation check and no longer reach `_on_rtm`. Each rejoin costs a little memory, cleared whenever the bridge restarts.
4. **续期时 RTM 用 `renew_token()` 换新凭证**，失败才重新登录。原来续期后 RTM 一直用旧凭证。
   **On renewal, RTM gets the new token through `renew_token()`**, logging in again only if that's refused. It used to keep the old token.
5. **`docker-compose.yml` 给 engine 加了 `PYTHONFAULTHANDLER=1`**：以后万一再崩，日志里能直接看到每个线程的 Python 栈。
   **`docker-compose.yml` sets `PYTHONFAULTHANDLER=1` for the engine**: if it ever crashes again, the log shows every thread's Python stack.

**缓解：补播丢掉的那句**（`ebo_transport.py` 的 `Speaker._replay_if_bridge_down`）。每次用 WAV 链接发出 talk 后 2 秒，查一下 bridge 在不在线（MQTT 遗嘱消息：进程一死就变 offline）。不在线就等它回来，50 秒内回来、期间又没有更新的回复，就再发一次。日志里会出现 `Replayed a … reply lost while the robot bridge restarted`。

**Mitigation: replay the lost reply** (`Speaker._replay_if_bridge_down` in `ebo_transport.py`). Two seconds after a talk is sent by WAV URL, it checks whether the bridge is online (MQTT last will: it goes offline the moment the process dies). If it's offline, it waits; if the bridge is back within 50 s and no newer reply has started, the reply is sent again. The log shows `Replayed a … reply lost while the robot bridge restarted`.

测试 / Tests：`tests/test_bridge_reconnect.py`（在 engine 镜像里跑 / run in the engine image）和 `tests/test_talk_replay.py`。在旧代码上 `test_bridge_reconnect.py` 会失败。

`tests/test_bridge_reconnect.py` (run in the engine image) and `tests/test_talk_replay.py`. `test_bridge_reconnect.py` fails on the old code.

```powershell
docker run --rm -v "${PWD}/tests:/t" --entrypoint python ebo-qwen/ebo-engine:0.26.100 /t/test_bridge_reconnect.py
```

---

## 怎么确认修好了 / How to confirm it's fixed

- 修之前大约每天崩一次（37 天 37 次）。**上线后连续 7 天没有 `rc=139`，就算修好了。**
  Before the fix: about one crash a day (37 in 37 days). **Seven days in a row with no `rc=139` after the deploy counts as fixed.**
- 每次 rejoin 之后不应该再出现 `[audio-apm] requested`，`Agora service initialized` 只在 bridge 启动时出现一次。
  After a rejoin, `[audio-apm] requested` should no longer appear; `Agora service initialized` appears once, when the bridge starts.
- 续期之后应该看到 `[RTM] token renewed`。
  After a renewal, `[RTM] token renewed` should appear.

```powershell
Select-String -Path data\engine\logs\ebo-engine.log* -Pattern "rc=139|token renewed|scheduling rejoin|Agora service initialized"
```

**还没验证的 / Not verified yet**：上线时没有人为触发续期和 rejoin，要等真实的续期（每 20 小时）和 rejoin 发生后看日志。如果 B 类还在，说明还有别的对象在被回收，下一步看 `PYTHONFAULTHANDLER` 打出来的栈。

No renewal or rejoin was forced at deploy time; this needs a real renewal (every 20 hours) and rejoin to happen, then a look at the log. If kind B still shows up, some other object is still being collected; the next step is the stack `PYTHONFAULTHANDLER` prints.

---

## 经验 / Lessons

1. **能自愈的故障也要计数、要记下来。** 健康检查连续一周都报了它，但每次都只写"已自愈"，没人把次数加起来。
   **Count and record self-healing faults.** The health check reported this for a week, each time noting "recovered", and nobody added up the count.
2. **原生 SDK 的对象生命周期要自己管。** Python 里"把引用丢掉"不等于原生层也放手了；回调表被回收而原生对象还活着，就是释放后使用。原生对象要么显式 `release()`，要么留着引用。
   **Own the lifetime of native SDK objects.** Dropping a reference in Python doesn't mean the native side has let go; a collected callback table with a live native object is a use-after-free. Either `release()` it explicitly or keep it referenced.
3. **全局的东西只初始化一次。** 声网服务是进程级的，跟着每次重连反复建、反复释放，迟早出事。
   **Initialize process-wide things once.** The Agora service is process-level; creating and releasing it on every reconnect was bound to break.
4. **崩溃现场一直都在，只是没人看。** WSL 一直在自动保存 core，日志里每次都有提示。以后遇到段错误，先去 `wsl-crashes` 目录看。
   **The crash dumps were there all along.** WSL had been saving cores automatically, and the log mentioned it every time. For the next segfault, look in the `wsl-crashes` folder first.
5. **同一段代码出过两次事，要整段检查。** 11-08 修续期时只补了眼前那个漏洞，同一段代码里不释放旧连接、旧服务的问题一直留着。
   **When the same code fails twice, review all of it.** The 11-08 renewal fix patched only the visible hole; the same code kept leaking old connections and services.

---

## 以后再崩怎么查 / If it crashes again

1. `Select-String -Path data\engine\logs\ebo-engine.log* -Pattern "rc=139"` 数次数，看每次崩溃前 30 行（`-Context 30,0`）。
   Count with `Select-String … -Pattern "rc=139"`, and read the 30 lines before each (`-Context 30,0`).
2. 看 `Fatal Python error` / `Current thread` 开头的栈（`PYTHONFAULTHANDLER` 打的）。
   Read the stack starting at `Fatal Python error` / `Current thread` (printed by `PYTHONFAULTHANDLER`).
3. 去 `C:\Users\yeche\AppData\Local\Temp\wsl-crashes\` 找对应时间的 core。要完整的符号化回溯，就用同一个 engine 镜像另起一个临时容器、装 gdb 来分析，不要动正在跑的 `ebo-qwen-engine`。
   Find the core for that time in `C:\Users\yeche\AppData\Local\Temp\wsl-crashes\`. For a full symbolized backtrace, start a throwaway container from the same engine image and install gdb there; don't touch the running `ebo-qwen-engine`.
