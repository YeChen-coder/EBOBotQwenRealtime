# EBO Engine (vendored)

Copied on 2026-10-17 from the working tree of `../ebo-ai-home/ha-enabot/ebo` (ebo-ai-home commit `1689b98`, version 0.26.100), so this project builds and runs without `ebo-ai-home`.

Upstream: https://github.com/Playcolors-co/ha-enabot (base revision `e5e4f5b5932a8591369b635428fd06c5be8472d9`, see `LICENSE`). Local changes made in ebo-ai-home on top of upstream are included.

`tests/` and `docs/` were not copied.

Local change in this project: `panel.py` `goBack()` no longer sends `connected/set off` (leaving the robot page used to put the robot to sleep, which cut off the 24/7 assistant).

Local change: `ebo_bridge.py` no longer lets a later RTC joiner (the official app) replace the robot uid — it used to point our audio subscription at the app and cut the mic. The uid that actually sends audio is saved to `/data/robot_uid`; other joiners are reported as `other_viewers` in audio_health.

Build: `docker compose build ebo-engine`.

Local change (2026-11-23): `ebo_bridge.py` initializes the Agora service once per process (`_agora_service`), releases replaced RTC connections (`_drop_rtc`), and keeps logged-out RTM clients referenced (`_retire_rtm`); token renewal renews the RTM token instead of leaving the old login on a stale token. Before this the bridge segfaulted (rc=139) about once a day. See `../docs/incident-2026-11-23-bridge-segfault.md`. Worth sending upstream.
