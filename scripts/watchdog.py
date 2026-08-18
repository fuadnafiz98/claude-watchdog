#!/usr/bin/env python3
"""Watchdog: resume a Claude Code turn that an API error killed.

Runs only when something has gone wrong, so it can afford to be a Python script.
The half that stays resident for the whole session is monitor.sh, which is POSIX
sh blocked in open(2) on a fifo -- see that file.

    watchdog.py hook            StopFailure hook: classify, then schedule a resume
    watchdog.py deliver SID     write the resume into the session (fifo, else tmux)
    watchdog.py doctor          platform preflight and per-session channel
    watchdog.py on|off|status|log|reset|config|set
"""

import errno
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("WATCHDOG_HOME") or Path.home() / ".claude" / "watchdog")
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
    # Checked before the type: a spent quota arrives as rate_limit, but retrying
    # it for an hour in 60s steps is pointless.
    "fatal_text": [
        "usage limit reached", "credit balance", "prompt is too long",
        "exceeds the maximum", "failed to authenticate",
    ],
    "channel": "auto",
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


def _coerce(default, raw):
    """Env vars arrive as strings; lists also accept a comma-separated form."""
    try:
        val = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        val = raw
    if isinstance(default, bool):
        return val not in (False, 0, "", "0", "false", "no", "off")
    if isinstance(default, list):
        if isinstance(val, str):
            val = [p.strip() for p in val.split(",") if p.strip()]
        elif not isinstance(val, list):
            val = [val]
        # A numeric list stays numeric, or comparisons against it explode later.
        if default and all(isinstance(d, int) for d in default):
            out = []
            for item in val:
                try:
                    out.append(int(item))
                except (TypeError, ValueError):
                    return default
            return out or default
        return [str(item) for item in val] or default
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
        raw = os.environ.get(f"WATCHDOG_{key.upper()}")
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


def log(msg):
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        with (HOME / "log").open("a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _path(name, suffix):
    HOME.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in (name or "") if c.isalnum() or c in "-_") or "unknown"
    return HOME / f"{safe}.{suffix}"


# ------------------------------------------------------------- correlation ----
def _ps(pid, fmt):
    try:
        return subprocess.run(["ps", "-o", fmt, "-p", str(pid)],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def claude_pid():
    """The owning claude process, found by walking up from here with ps.

    monitor.sh walks the same way from its own pid, so both sides agree on the
    fifo path without either shelling out to the claude CLI.

    The command *name* is compared exactly. Searching the full argv for "claude"
    instead looks tempting and is wrong: any ancestor shell whose command line
    merely mentions a path containing the word -- including this repository --
    matches, and the walk stops on the shell instead of the session. The argv is
    consulted only for a runtime that hosts claude under its own name.
    """
    pid = os.getpid()
    for _ in range(12):
        parent = _ps(pid, "ppid=")
        if not parent.isdigit() or int(parent) <= 1:
            return None
        parent = int(parent)
        name = Path(_ps(parent, "comm=").strip() or "?").name.lower()
        if name == "claude":
            return parent
        if name in ("node", "bun", "deno") and "claude" in _ps(parent, "args=").lower():
            return parent
        pid = parent
    return None


def fifo_for(owner_pid):
    return HOME / f"{owner_pid}.resume"


def fifo_reader_present(owner_pid):
    """True when monitor.sh is blocked on the fifo. ENXIO means nobody is reading."""
    path = fifo_for(owner_pid)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        return exc.errno not in (errno.ENXIO, errno.ENOENT)
    os.close(fd)
    return True


def fifo_write(owner_pid, text):
    try:
        fd = os.open(str(fifo_for(owner_pid)), os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        os.write(fd, (text.replace("\n", " ") + "\n").encode())
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


# ------------------------------------------------------------------ policy ----
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
    steps = cfg["backoff"] if isinstance(cfg["backoff"], list) and cfg["backoff"] else DEFAULTS["backoff"]
    delay = steps[min(max(attempt, 1) - 1, len(steps) - 1)]
    if error_type in cfg["slow_types"]:
        delay = max(delay, cfg["slow_floor"])
    return delay


def attempts(session_id, cfg):
    p = _path(session_id, "attempts")
    try:
        if time.time() - p.stat().st_mtime > cfg["counter_ttl_hours"] * 3600:
            return 0
        return int(p.read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def resume_text(incident, cfg):
    return cfg["resume_message"].format(
        error=incident.get("error", "unknown"),
        message=(incident.get("message") or "")[:200],
        attempt=incident.get("attempt"), max_retries=incident.get("max_retries"))


# -------------------------------------------------------------------- hook ----
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
        # The one moment the user provably cares, so it is worth one line: the turn
        # just died and the thing that would have resumed it is installed but idle.
        log(f"session={sid} error={error} (not armed)")
        print(json.dumps({"systemMessage":
                          "watchdog: installed but not armed, so this turn was not resumed. "
                          "Run /watchdog on (or `watchdog on`) to arm it."}))
        return

    verdict, detail = classify(error, message, cfg)
    if verdict != "retry":
        _path(sid, "attempts").unlink(missing_ok=True)
        log(f"session={sid} not resuming: {detail}")
        tail = "" if verdict == "fatal" else " Report it if it was in fact transient."
        print(json.dumps({"systemMessage": f"watchdog: not resuming, {detail}.{tail}"}))
        return

    n = attempts(sid, cfg) + 1
    if n > cfg["max_retries"]:
        _path(sid, "attempts").unlink(missing_ok=True)
        log(f"session={sid} gave up after {cfg['max_retries']} attempts")
        print(json.dumps({"systemMessage": f"watchdog: gave up after {cfg['max_retries']} retries."}))
        return

    owner = claude_pid()
    _path(sid, "attempts").write_text(str(n))
    _path(sid, "incident").write_text(json.dumps({
        "session_id": sid, "error": error, "message": message,
        "attempt": n, "max_retries": cfg["max_retries"], "owner": owner,
    }))

    delay = backoff(n, error, cfg)
    channel = cfg["channel"]
    if channel == "auto":
        channel = "monitor" if owner and fifo_reader_present(owner) else "tmux"
    log(f"session={sid} retry={n}/{cfg['max_retries']} in {delay}s via {channel} ({detail})")

    # The wait happens in a detached sh, not here: a hook whose output is ignored
    # gains nothing by blocking, and sleeping in sh costs a fraction of a Python
    # interpreter for the duration.
    try:
        subprocess.Popen(
            ["sh", "-c", f'sleep {int(delay)}; exec "$0" "$1" deliver "$2"',
             sys.executable, str(Path(__file__).resolve()), sid],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        log(f"session={sid} could not schedule delivery: {exc}")

    print(json.dumps({"systemMessage": f"watchdog: retry {n}/{cfg['max_retries']} in {delay}s via {channel}."}))


# ----------------------------------------------------------------- deliver ----
def tmux_pane(owner_pid):
    """The pane whose process tree contains the session, or None."""
    if not owner_pid:
        return None
    try:
        if subprocess.run(["tmux", "info"], capture_output=True, timeout=5).returncode:
            return None
        listing = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_pid}\t#{session_name}:#{window_index}.#{pane_index}"],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    owners = {}
    for line in listing.splitlines():
        pane_pid, _, target = line.partition("\t")
        if pane_pid.strip().isdigit():
            owners[int(pane_pid.strip())] = target.strip()
    probe = int(owner_pid)
    for _ in range(12):
        if probe in owners:
            return owners[probe]
        parent = _ps(probe, "ppid=")
        if not parent.isdigit() or int(parent) <= 1:
            return None
        probe = int(parent)
    return None


def notify(text):
    """Best effort, and silent when the platform offers nothing."""
    for argv in (["osascript", "-e", f'display notification "{text}" with title "Claude Code Watchdog"'],
                 ["notify-send", "Claude Code Watchdog", text]):
        try:
            if subprocess.run(argv, capture_output=True, timeout=10).returncode == 0:
                return
        except Exception:
            continue
    sys.stderr.write("\a")


def cmd_deliver(session_id):
    cfg = config()
    src = _path(session_id, "incident")
    taken = _path(session_id, "delivering")
    try:
        src.rename(taken)                      # exactly once, even if two land
        incident = json.loads(taken.read_text())
    except (OSError, json.JSONDecodeError):
        return
    taken.unlink(missing_ok=True)

    owner = incident.get("owner")
    text = resume_text(incident, cfg)
    want = cfg["channel"]

    if want in ("auto", "monitor") and owner and fifo_write(owner, text):
        log(f"session={session_id} delivered via monitor")
        return
    if want in ("auto", "tmux"):
        pane = tmux_pane(owner)
        if pane:
            # Text and Enter separately: a single send-keys can submit before a
            # busy TUI has taken the text.
            subprocess.run(["tmux", "send-keys", "-t", pane, "-l", "continue"], check=False)
            time.sleep(0.3)
            subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=False)
            log(f"session={session_id} delivered via tmux pane {pane}")
            return
    log(f"session={session_id} no delivery channel available -- notified only")
    notify("A turn stalled on an API error and needs a manual continue.")


# ------------------------------------------------------------------ doctor ----
def _which(name):
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = Path(d) / name
        if p.exists() and os.access(p, os.X_OK):
            return str(p)
    return None


def cmd_doctor():
    cfg = config()
    armed = cfg["enabled"]
    print(f"armed:       {'yes' if armed else 'NO -- nothing will be resumed'}")
    if not armed:
        print("             arm it with:  /watchdog on      (or: watchdog on)")
    print(f"state:       {HOME}")
    print(f"max retries: {cfg['max_retries']}   backoff: {cfg['backoff']}   channel: {cfg['channel']}")

    print("\nrequirements")
    for label, ok, note in [
        ("sh", bool(_which("sh")), "runs the resident monitor"),
        ("ps", bool(_which("ps")), "finds the owning session"),
        ("mkfifo", bool(_which("mkfifo")), "the in-band channel"),
        ("python3", True, "this script"),
        ("tmux", bool(_which("tmux")), "optional fallback only"),
        ("desktop notify", bool(_which("osascript") or _which("notify-send")), "optional, last resort"),
    ]:
        mark = "ok " if ok else ("--  " if "optional" in note else "MISSING")
        print(f"  {mark} {label:15}{note}")

    live = sorted(HOME.glob("*.resume"))
    print(f"\nmonitors listening: {len(live)}")
    for f in live:
        pid = f.stem
        reading = fifo_reader_present(pid) if pid.isdigit() else False
        alive = bool(_ps(pid, "comm=")) if pid.isdigit() else False
        if not alive:
            state = "session gone -- run: watchdog reap"
        elif reading:
            state = "listening"
        else:
            state = "session alive, monitor not reading"
        print(f"  pid {pid:<8} {state}")
    pending = sorted(HOME.glob("*.incident"))
    if pending:
        print(f"\npending resumes: {', '.join(p.stem[:8] for p in pending)}")
    if not live:
        print("\nNo monitor is listening: monitors start at session start, and only in an")
        print("interactive session, so restart this session to pick one up.")
        if not armed:
            print("Arm it first with /watchdog on, or nothing will be resumed even then.")


# ----------------------------------------------------------------- control ----
def main(argv):
    cmd = argv[0] if argv else "status"
    cfg = config()
    if cmd == "hook":
        cmd_hook()
    elif cmd == "deliver" and len(argv) > 1:
        cmd_deliver(argv[1])
    elif cmd == "doctor":
        cmd_doctor()
    elif cmd == "on":
        save_config({"enabled": True})
        print(f"Watchdog on. Turns killed by a transient API error retry themselves "
              f"(max {cfg['max_retries']}).")
    elif cmd == "off":
        save_config({"enabled": False})
        print("Watchdog off.")
    elif cmd == "status":
        print("Watchdog on" if cfg["enabled"] else "Watchdog off")
    elif cmd == "log":
        n = int(argv[1]) if len(argv) > 1 else 20
        try:
            print("".join((HOME / "log").read_text().splitlines(keepends=True)[-n:]), end="")
        except OSError:
            print("nothing logged yet")
    elif cmd == "reset":
        for pattern in ("*.attempts", "*.incident", "*.delivering"):
            for p in HOME.glob(pattern):
                p.unlink(missing_ok=True)
        print("counters and pending resumes cleared")
    elif cmd == "reap":
        # Monitors self-terminate on a liveness check, so this only matters for
        # ones left by a version that could not, or by a kill during a check.
        killed, cleaned = [], []
        for fifo in HOME.glob("*.resume"):
            if fifo.stem.isdigit() and not _ps(fifo.stem, "comm="):
                fifo.unlink(missing_ok=True)
                cleaned.append(fifo.stem)
        try:
            listing = subprocess.run(["ps", "-ax", "-o", "pid=,ppid=,args="],
                                     capture_output=True, text=True, timeout=10).stdout
        except Exception:
            listing = ""
        for line in listing.splitlines():
            parts = line.split(None, 2)
            if len(parts) < 3 or "monitor.sh" not in parts[2]:
                continue
            pid, ppid = parts[0], parts[1]
            if ppid != "1":                      # still owned by a live session
                continue
            try:
                os.kill(int(pid), 15)
                killed.append(int(pid))
            except (OSError, ValueError):
                pass
        # A shell parked in `read` with a trapped TERM never processes it, which is
        # how these survived in the first place. Escalate to what it cannot defer.
        if killed:
            time.sleep(1)
            for pid in killed:
                try:
                    os.kill(pid, 0)
                    os.kill(pid, 9)
                except OSError:
                    pass
        killed = [str(pid) for pid in killed]
        print(f"killed {len(killed)} orphaned monitor(s)" + (f": {', '.join(killed)}" if killed else ""))
        if cleaned:
            print(f"removed {len(cleaned)} stale fifo(s): {', '.join(cleaned)}")
    elif cmd == "config":
        for key in DEFAULTS:
            mark = "" if cfg[key] == DEFAULTS[key] else "   <- changed"
            print(f"{key:18} {json.dumps(cfg[key])}{mark}")
        print(f"\nfile: {CONFIG_FILE}   override any key with WATCHDOG_<KEY>")
    elif cmd == "set" and len(argv) >= 3:
        if argv[1] not in DEFAULTS:
            print(f"unknown key {argv[1]!r}. See: watchdog config", file=sys.stderr)
            return 1
        save_config({argv[1]: _coerce(DEFAULTS[argv[1]], argv[2])})
        print(f"{argv[1]} = {json.dumps(config()[argv[1]])}")
    else:
        print("usage: watchdog {on|off|status|doctor|log [n]|reset|reap|config|set KEY VALUE}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
