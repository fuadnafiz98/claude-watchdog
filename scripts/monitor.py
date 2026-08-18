#!/usr/bin/env python3
"""Plugin monitor: deliver a queued resume request into this session, in band.

A monitor's stdout lines reach Claude as notifications, which is the only way to
wake a session without typing into its terminal. The monitor must therefore know
which session it belongs to: monitors are given a working directory but not a
session id, so it walks its own process ancestry to the owning `claude` process
and maps that pid through `claude agents --json`.

Until that mapping succeeds the monitor writes no heartbeat, and the hook falls
back to the terminal channel. Guessing by working directory is deliberately not
attempted -- two sessions in one checkout would steal each other's resumes.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import afk_common as afk  # noqa: E402

POLL = 2.0
RESOLVE_RETRY = 30.0


def ppid_of(pid: int):
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None, ""
    if not out:
        return None, ""
    parts = out.split(None, 1)
    try:
        return int(parts[0]), (parts[1] if len(parts) > 1 else "")
    except ValueError:
        return None, ""


def owning_claude_pid():
    pid = os.getpid()
    for _ in range(12):
        parent, _ = ppid_of(pid)
        if parent is None or parent <= 1:
            return None
        _, parent_comm = ppid_of(parent)
        if "claude" in parent_comm.lower():
            return parent
        pid = parent
    return None


def session_for_pid(pid: int):
    try:
        out = subprocess.run(
            ["claude", "agents", "--json"], capture_output=True, text=True, timeout=20
        ).stdout
        sessions = json.loads(out)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        return None
    for s in sessions if isinstance(sessions, list) else []:
        if s.get("pid") == pid and s.get("sessionId"):
            return s["sessionId"]
    return None


def resolve_session():
    override = os.environ.get("AFK_SESSION_ID")
    if override:
        return override
    claude_pid = owning_claude_pid()
    if not claude_pid:
        return None
    return session_for_pid(claude_pid)


def emit(line: str):
    sys.stdout.write(line.rstrip("\n") + "\n")
    sys.stdout.flush()


def main():
    session_id = None
    next_resolve = 0.0
    while True:
        now = time.time()
        if not session_id and now >= next_resolve:
            session_id = resolve_session()
            next_resolve = now + RESOLVE_RETRY
            if session_id:
                afk.log(f"monitor bound to session={session_id}")

        if session_id and afk.enabled():
            afk.ensure_dirs()
            afk.heartbeat_path(session_id).touch()
            req = afk.read_request(session_id)
            if req and now >= req.get("deliver_at", 0):
                claimed = afk.claim_request(session_id)
                if claimed:
                    afk.log(f"session={session_id} delivered via monitor")
                    emit(afk.resume_text(claimed))

        time.sleep(POLL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
