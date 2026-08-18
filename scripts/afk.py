#!/usr/bin/env python3
"""AFK: resume a Claude Code turn that an API error killed.

An API error ends a turn without firing Stop, so no hook can refuse the stop.
Only StopFailure fires, and its output is discarded -- so the resume has to be
delivered from outside the turn. This one file is the whole plugin:

    afk.py hook        StopFailure hook: classify, back off, queue a resume
    afk.py monitor     delivery via monitor stdout (in band, no tmux needed)
    afk.py deliver ID  delivery by typing into a tmux pane (fallback)
    afk.py doctor      which channel would fire right now
    afk.py on|off|status|log|reset|config|set
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("AFK_HOME") or Path.home() / ".claude" / "afk")
CONFIG_FILE = HOME / "config.json"

DEFAULTS = {
    "enabled": False,
    "max_retries": 8,
    "backoff": [5, 15, 30, 60, 120],
    "slow_floor": 60,
    "slow_types": ["rate_limit", "overloaded"],
    "retry_types": ["rate_limit", "overloaded", "server_error", "unknown"],
    "fatal_types": [
        "authentication_failed", "oauth_org_not_allowed", "billing_error",
        "invalid_request", "model_not_found", "max_output_tokens",
    ],
    # Checked before the type: a spent quota arrives as rate_limit but retrying
    # it for an hour in 60s steps is pointless.
    "fatal_text": [
        "usage limit reached", "credit balance", "prompt is too long",
        "exceeds the maximum", "failed to authenticate",
    ],
    "channel": "auto",
    "poll_seconds": 2,
    "heartbeat_stale": 30,
    "counter_ttl_hours": 12,
    "resume_message": (
        "The previous turn did not finish: it was cut off by a transient API error "
        "({error}: {message}). This is automatic retry {attempt} of {max_retries}, "
        "equivalent to a human typing \"continue\". Resume the task exactly where it "
        "stopped. Re-check anything that was mid-flight, since a file edit or command "
        "may or may not have landed, before redoing it. Do not restart the task, "
        "re-explain finished work, or apologise."
    ),
}

# Every key is settable as AFK_<KEY>; lists take commas, JSON is tried first.
def _coerce(default, raw):
    try:
        val = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        val = raw
    if isinstance(default, bool):
        return val not in (False, 0, "", "0", "false", "no", "off")
    if isinstance(default, list) and isinstance(val, str):
        return [p.strip() for p in val.split(",") if p.strip()]
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(val)
        except (TypeError, ValueError):
            return default
    return val


def config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text()))
    except (OSError, json.JSONDecodeError):
        pass
    for key, default in DEFAULTS.items():
        raw = os.environ.get(f"AFK_{key.upper()}")
        if raw is not None:
            cfg[key] = _coerce(default, raw)
    return cfg


def save_config(updates):
    HOME.mkdir(parents=True, exist_ok=True)
    stored = {}
    try:
        stored = json.loads(CONFIG_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        pass
    stored.update(updates)
    CONFIG_FILE.write_text(json.dumps(stored, indent=2) + "\n")
    return stored


def log(msg):
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        with (HOME / "log").open("a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _safe(name):
    return "".join(c for c in (name or "") if c.isalnum() or c in "-_") or "unknown"


def _path(session_id, suffix):
    HOME.mkdir(parents=True, exist_ok=True)
    return HOME / f"{_safe(session_id)}.{suffix}"


def attempts(session_id, cfg):
    p = _path(session_id, "attempts")
    try:
        if time.time() - p.stat().st_mtime > cfg["counter_ttl_hours"] * 3600:
            return 0
        return int(p.read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def monitor_alive(session_id, cfg, now=None):
    now = time.time() if now is None else now
    try:
        return now - _path(session_id, "monitor").stat().st_mtime < cfg["heartbeat_stale"]
    except OSError:
        return False


def read_request(session_id):
    try:
        return json.loads(_path(session_id, "resume").read_text())
    except (OSError, json.JSONDecodeError):
        return None


def claim(session_id):
    """Atomic take, so monitor and tmux can never both resume one turn."""
    src = _path(session_id, "resume")
    dst = _path(session_id, "claimed")
    try:
        src.rename(dst)
    except OSError:
        return None
    try:
        req = json.loads(dst.read_text())
    except (OSError, json.JSONDecodeError):
        req = None
    dst.unlink(missing_ok=True)
    return req


def classify(error_type, message, cfg):
    """(verdict, detail) where verdict is retry | fatal | unrecognised."""
    error_type = (error_type or "unknown").strip()
    low = (message or "").lower()
    hit = next((t for t in cfg["fatal_text"] if t in low), None)
    if hit:
        return "fatal", f"known-fatal: message says {hit!r}"
    if error_type in cfg["fatal_types"]:
        return "fatal", f"known-fatal error type {error_type}"
    if error_type in cfg["retry_types"]:
        return "retry", f"error type {error_type}"
    return "unrecognised", f"unrecognised error type {error_type!r}"


def backoff(attempt, error_type, cfg):
    steps = cfg["backoff"] or DEFAULTS["backoff"]
    delay = steps[min(max(attempt, 1) - 1, len(steps) - 1)]
    if error_type in cfg["slow_types"]:
        delay = max(delay, cfg["slow_floor"])
    return delay


def resume_text(req, cfg):
    return cfg["resume_message"].format(
        error=req.get("error", "unknown"), message=(req.get("message") or "")[:200],
        attempt=req.get("attempt"), max_retries=req.get("max_retries"),
    )


def sessions():
    try:
        out = subprocess.run(["claude", "agents", "--json"],
                             capture_output=True, text=True, timeout=20).stdout
        rows = json.loads(out)
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


# ---------------------------------------------------------------- hook --------
def cmd_hook():
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return
    cfg = config()
    sid = payload.get("session_id") or ""
    error = payload.get("error") or "unknown"
    message = payload.get("last_assistant_message") or ""

    if not cfg["enabled"]:
        log(f"session={sid} error={error} (afk off)")
        return

    verdict, detail = classify(error, message, cfg)
    if verdict != "retry":
        _path(sid, "attempts").unlink(missing_ok=True)
        log(f"session={sid} not resuming: {detail}")
        tail = "" if verdict == "fatal" else " Report it if it was in fact transient."
        print(json.dumps({"systemMessage": f"afk: not resuming, {detail}.{tail}"}))
        return

    n = attempts(sid, cfg) + 1
    if n > cfg["max_retries"]:
        _path(sid, "attempts").unlink(missing_ok=True)
        log(f"session={sid} gave up after {cfg['max_retries']} attempts")
        print(json.dumps({"systemMessage": f"afk: gave up after {cfg['max_retries']} retries."}))
        return

    _path(sid, "attempts").write_text(str(n))
    delay = backoff(n, error, cfg)
    _path(sid, "resume").write_text(json.dumps({
        "session_id": sid, "error": error, "message": message, "attempt": n,
        "max_retries": cfg["max_retries"], "delay": delay,
        "deliver_at": time.time() + delay,
    }))

    channel = cfg["channel"]
    if channel == "auto":
        channel = "monitor" if monitor_alive(sid, cfg) else "tmux"
    log(f"session={sid} retry={n}/{cfg['max_retries']} in {delay}s via {channel} ({detail})")
    if channel == "tmux":
        spawn_deliver(sid, delay)
    print(json.dumps({"systemMessage": f"afk: retry {n}/{cfg['max_retries']} in {delay}s via {channel}."}))


# ------------------------------------------------------------- monitor --------
def cmd_monitor():
    """Monitor stdout reaches Claude as a notification: the only in-band wake."""
    cfg = config()
    sid, next_try = None, 0.0
    while True:
        now = time.time()
        if not sid and now >= next_try:
            sid = os.environ.get("AFK_SESSION_ID") or resolve_session()
            next_try = now + 30
            if sid:
                log(f"monitor bound to session={sid}")
        if sid:
            cfg = config()
            if cfg["enabled"]:
                _path(sid, "monitor").touch()
                req = read_request(sid)
                if req and now >= req.get("deliver_at", 0):
                    req = claim(sid)
                    if req:
                        log(f"session={sid} delivered via monitor")
                        sys.stdout.write(resume_text(req, cfg) + "\n")
                        sys.stdout.flush()
        time.sleep(cfg["poll_seconds"])


def _ps(pid, fmt):
    try:
        return subprocess.run(["ps", "-o", fmt, "-p", str(pid)],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def resolve_session():
    """Walk up to the owning claude process, then map its pid to a session id.

    Guessing by working directory is deliberately avoided: two sessions in one
    checkout would steal each other's resumes.
    """
    pid = os.getpid()
    for _ in range(12):
        parent = _ps(pid, "ppid=")
        if not parent.isdigit() or int(parent) <= 1:
            return None
        if "claude" in _ps(parent, "comm=").lower():
            return next((s["sessionId"] for s in sessions()
                         if s.get("pid") == int(parent) and s.get("sessionId")), None)
        pid = int(parent)
    return None


# ------------------------------------------------------------- deliver --------
def spawn_deliver(session_id, delay):
    try:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "deliver",
                          session_id, str(delay)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        log(f"session={session_id} could not spawn deliverer: {exc}")


def tmux_pane(session_id):
    """The pane whose process tree contains this session's claude process."""
    pid = next((s.get("pid") for s in sessions() if s.get("sessionId") == session_id), None)
    if not pid:
        return None
    try:
        if subprocess.run(["tmux", "info"], capture_output=True, timeout=5).returncode:
            return None
        panes = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "{}\t{}".format("#{pane_pid}", "#{session_name}:#{window_index}.#{pane_index}")],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    owners = {}
    for line in panes.splitlines():
        pane_pid, _, target = line.partition("\t")
        if pane_pid.strip().isdigit():
            owners[int(pane_pid.strip())] = target.strip()
    probe = int(pid)
    for _ in range(12):
        if probe in owners:
            return owners[probe]
        parent = _ps(probe, "ppid=")
        if not parent.isdigit() or int(parent) <= 1:
            return None
        probe = int(parent)
    return None


def cmd_deliver(session_id, delay):
    time.sleep(float(delay))
    req = claim(session_id)
    if not req:
        log(f"session={session_id} already claimed, tmux sender standing down")
        return
    pane = tmux_pane(session_id)
    if pane:
        # Text and Enter separately: one send-keys can submit before a busy TUI
        # has finished accepting the text.
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", "continue"], check=False)
        time.sleep(0.3)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=False)
        log(f"session={session_id} delivered via tmux pane {pane}")
        return
    log(f"session={session_id} NO channel (no monitor, no tmux pane) -- notified only")
    subprocess.run(["osascript", "-e",
                    'display notification "A turn stalled on an API error and needs a manual continue." '
                    'with title "Claude Code AFK"'], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# -------------------------------------------------------------- control -------
def cmd_doctor():
    cfg = config()
    rows = sessions()
    tmux_up = False
    try:
        tmux_up = subprocess.run(["tmux", "info"], capture_output=True, timeout=5).returncode == 0
    except Exception:
        pass
    print(f"enabled:     {'yes' if cfg['enabled'] else 'no   (run: afk on)'}")
    print(f"state:       {HOME}")
    print(f"max retries: {cfg['max_retries']}   backoff: {cfg['backoff']}   channel: {cfg['channel']}")
    print(f"tmux:        {'running' if tmux_up else 'not running'}")
    print(f"sessions:    {len(rows)}")
    now = time.time()
    for r in rows:
        sid = r.get("sessionId") or "?"
        alive = monitor_alive(sid, cfg, now)
        chan = "monitor" if alive else ("tmux" if tmux_pane(sid) else "none - cannot resume")
        line = f"  {sid[:8]}  {r.get('status', '?'):5}  monitor={'up' if alive else 'down'}  would use: {chan}"
        req = read_request(sid)
        if req:
            line += f"  [queued retry {req['attempt']}, due in {int(req['deliver_at'] - now)}s]"
        print(line)
    if rows and not any(monitor_alive(r.get("sessionId") or "", cfg, now) for r in rows):
        print("\nNo monitor heartbeat: restart the session after installing "
              "(monitors start at session start, interactive CLI only).")


def main(argv):
    cmd = argv[0] if argv else "status"
    cfg = config()
    if cmd == "hook":
        cmd_hook()
    elif cmd == "monitor":
        cmd_monitor()
    elif cmd == "deliver":
        cmd_deliver(argv[1], argv[2] if len(argv) > 2 else 0)
    elif cmd == "doctor":
        cmd_doctor()
    elif cmd == "on":
        save_config({"enabled": True})
        print(f"AFK on. Turns killed by a transient API error retry themselves "
              f"(max {cfg['max_retries']}).")
    elif cmd == "off":
        save_config({"enabled": False})
        print("AFK off.")
    elif cmd == "status":
        print("AFK on" if cfg["enabled"] else "AFK off")
    elif cmd == "log":
        n = int(argv[1]) if len(argv) > 1 else 20
        try:
            print("".join((HOME / "log").read_text().splitlines(keepends=True)[-n:]), end="")
        except OSError:
            print("nothing logged yet")
    elif cmd == "reset":
        for suffix in ("attempts", "resume", "claimed", "monitor"):
            for p in HOME.glob(f"*.{suffix}"):
                p.unlink(missing_ok=True)
        print("counters, queued resumes and heartbeats cleared")
    elif cmd == "config":
        for key in DEFAULTS:
            mark = "" if cfg[key] == DEFAULTS[key] else "  <- changed"
            print(f"{key:18} {json.dumps(cfg[key])}{mark}")
        print(f"\nfile: {CONFIG_FILE}   override any key with AFK_<KEY>")
    elif cmd == "set" and len(argv) >= 3:
        key = argv[1]
        if key not in DEFAULTS:
            print(f"unknown key {key!r}. See: afk config", file=sys.stderr)
            return 1
        save_config({key: _coerce(DEFAULTS[key], argv[2])})
        print(f"{key} = {json.dumps(config()[key])}")
    else:
        print("usage: afk {on|off|status|doctor|log [n]|reset|config|set KEY VALUE}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
