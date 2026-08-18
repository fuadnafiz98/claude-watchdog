#!/usr/bin/env python3
"""Report which delivery channel a resume would actually use right now."""

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import afk_common as afk  # noqa: E402


def sessions():
    try:
        out = subprocess.run(
            ["claude", "agents", "--json"], capture_output=True, text=True, timeout=20
        ).stdout
        rows = json.loads(out)
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def tmux_ok():
    try:
        return subprocess.run(["tmux", "info"], capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False


print(f"enabled:       {'yes' if afk.enabled() else 'no  (run: afk on)'}")
print(f"state dir:     {afk.HOME}")
print(f"max retries:   {afk.max_retries()}")
print(f"tmux server:   {'running' if tmux_ok() else 'not running  (terminal fallback cannot type)'}")

rows = sessions()
print(f"sessions seen: {len(rows)}")
now = time.time()
for r in rows:
    sid = r.get("sessionId") or "?"
    alive = afk.monitor_alive(sid, now)
    queued = afk.read_request(sid)
    channel = "monitor" if alive else ("tmux/notify" if tmux_ok() else "notify only")
    line = f"  {sid[:8]}  {r.get('status', '?'):5}  monitor={'up' if alive else 'down'}  channel={channel}"
    if queued:
        line += f"  queued(retry {queued.get('attempt')}, due in {int(queued.get('deliver_at', 0) - now)}s)"
    print(line)

if rows and not any(afk.monitor_alive(r.get("sessionId") or "", now) for r in rows):
    print()
    print("No monitor heartbeat yet. Monitors start at session start, so restart the")
    print("session after installing, and keep in mind they only run in interactive CLI.")
