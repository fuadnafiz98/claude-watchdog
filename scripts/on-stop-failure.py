#!/usr/bin/env python3
"""StopFailure hook: queue a resume request for the turn the API just killed.

StopFailure's output is ignored by Claude Code, so this hook cannot continue the
turn itself. It records what happened and hands delivery to whichever channel is
available -- the plugin monitor when one is running, otherwise a detached tmux
sender. It never blocks: the backoff is a deliver_at timestamp, not a sleep.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import afk_common as afk  # noqa: E402


def deliver_out_of_band(session_id: str, delay: int):
    """Fall back to a detached sender that types into the session's terminal."""
    script = Path(__file__).resolve().parent / "deliver.sh"
    try:
        subprocess.Popen(
            [str(script), session_id, str(delay)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        afk.log(f"session={session_id} could not start deliver.sh: {exc}")


def main():
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return

    session_id = payload.get("session_id") or ""
    error_type = payload.get("error") or "unknown"
    message = payload.get("last_assistant_message") or ""

    if not afk.enabled():
        afk.log(f"session={session_id} error={error_type} (afk off, ignoring)")
        return

    verdict, detail = afk.classify(error_type, message)
    if verdict != "retry":
        afk.clear_attempts(session_id)
        afk.log(f"session={session_id} not resuming: {verdict} -- {detail}")
        tail = "" if verdict == "fatal" else " Report it if it was in fact transient."
        print(json.dumps({"systemMessage": f"afk: not resuming, {detail}.{tail}"}))
        return

    attempt = afk.read_attempts(session_id) + 1
    limit = afk.max_retries()
    if attempt > limit:
        afk.clear_attempts(session_id)
        afk.log(f"session={session_id} gave up after {limit} attempts ({detail})")
        print(json.dumps({"systemMessage": f"afk: gave up after {limit} retries ({detail})."}))
        return

    afk.write_attempts(session_id, attempt)
    delay = afk.backoff_for(attempt, error_type)
    request = {
        "session_id": session_id,
        "error_type": error_type,
        "message": message,
        "attempt": attempt,
        "max_retries": limit,
        "delay": delay,
        "deliver_at": time.time() + delay,
        "cwd": payload.get("cwd") or os.getcwd(),
    }
    afk.write_request(session_id, request)

    channel = "monitor" if afk.monitor_alive(session_id) else "terminal"
    forced = os.environ.get("AFK_CHANNEL")
    if forced in ("monitor", "terminal"):
        channel = forced

    afk.log(
        f"session={session_id} retry={attempt}/{limit} in {delay}s "
        f"via {channel} ({detail})"
    )
    if channel == "terminal":
        deliver_out_of_band(session_id, delay)

    print(json.dumps({"systemMessage": f"afk: retry {attempt}/{limit} in {delay}s via {channel}."}))


if __name__ == "__main__":
    main()
