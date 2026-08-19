---
description: Arm or disarm the watchdog, cap the wait on a stalled stream, or check whether it can actually resume this session
allowed-tools: Bash(watchdog:*)
---

Run `watchdog $ARGUMENTS` (default `doctor` when no argument is given) and report the
result in one or two lines.

If the report says it is not armed, say so first and give the user the exact command to
arm it — `/watchdog on` — because nothing will be resumed until they do. If it is armed
but no monitor is listening, tell them to restart the session, since monitors only start
at session start.

If the argument is `stall`, this is the other failure -- a request that was accepted
and then went silent, which Claude Code waits out for 180s. Report the cap it set or
reports, and that it applies to sessions started from now on.

Do not change any other watchdog state.
