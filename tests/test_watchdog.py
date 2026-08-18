#!/usr/bin/env python3
"""Tests for Watchdog. The hook runs as a real subprocess against a throwaway state dir."""

import errno
import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "watchdog.py"
MONITOR = SCRIPT.parent / "monitor.sh"
sys.path.insert(0, str(SCRIPT.parent))

failures = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   <- {detail}"))
    if not cond:
        failures.append(name)


def home():
    return tempfile.mkdtemp()


def run(args, h, stdin="", **env):
    e = dict(os.environ, WATCHDOG_HOME=str(h), WATCHDOG_ENABLED="1", WATCHDOG_CHANNEL="monitor")
    for k in list(e):
        if k.startswith("WATCHDOG_") and k not in ("WATCHDOG_HOME", "WATCHDOG_ENABLED", "WATCHDOG_CHANNEL"):
            del e[k]
    e.update({k: str(v) for k, v in env.items()})
    return subprocess.run([sys.executable, str(SCRIPT)] + args, input=stdin,
                          capture_output=True, text=True, env=e, timeout=30)


def hook(h, error="server_error", message="API Error: The response stopped arriving",
         sid="s1", **env):
    payload = {"session_id": sid, "hook_event_name": "StopFailure", "error": error,
               "last_assistant_message": message, "cwd": "/tmp"}
    p = run(["hook"], h, json.dumps(payload), **env)
    out = {}
    if p.stdout.strip():
        try:
            out = json.loads(p.stdout)
        except json.JSONDecodeError:
            out = {"raw": p.stdout}
    return out, p


def incident(h, sid="s1"):
    p = Path(h) / f"{sid}.incident"
    return json.loads(p.read_text()) if p.exists() else None


def msg(out):
    return str(out.get("systemMessage"))


# --- the reported failure ----------------------------------------------------
h = home()
out, p = hook(h)
inc = incident(h)
check("transient error records an incident", inc is not None, p.stderr[-300:])
check("attempt starts at 1", inc and inc["attempt"] == 1, inc)
check("hook reports the retry", "retry 1/8" in msg(out), out)
check("hook returns immediately", p.returncode == 0 and not p.stderr.strip(), p.stderr[-200:])

# --- off means off ----------------------------------------------------------
h = home()
out, _ = hook(h, WATCHDOG_ENABLED="0")
check("disabled records nothing", incident(h) is None, out)

# --- fatal error types ------------------------------------------------------
for etype in ["model_not_found", "authentication_failed", "billing_error",
              "invalid_request", "oauth_org_not_allowed", "max_output_tokens"]:
    h = home()
    out, _ = hook(h, error=etype, message="nope")
    check(f"fatal type not retried: {etype}", incident(h) is None, out)
    check(f"named as known-fatal: {etype}", "known-fatal" in msg(out), msg(out))

# --- a spent quota looks retryable but is not ------------------------------
h = home()
out, _ = hook(h, error="rate_limit",
              message="API Error: Request rejected (429) - usage limit reached, resets at 3pm")
check("spent quota beats retryable type", incident(h) is None, out)
check("spent quota reported as known-fatal", "known-fatal" in msg(out), msg(out))

# --- never-seen error type --------------------------------------------------
h = home()
out, _ = hook(h, error="teapot_error", message="short and stout")
check("unknown type not retried", incident(h) is None, out)
check("unknown type flagged unrecognised",
      "unrecognised" in msg(out) and "known-fatal" not in msg(out), msg(out))

# --- backoff, floor, cap ----------------------------------------------------
import watchdog as w  # noqa: E402
os.environ["WATCHDOG_HOME"] = home()
importlib.reload(w)
cfg = w.config()
check("backoff escalates", [w.backoff(i, "server_error", cfg) for i in range(1, 7)]
      == [5, 15, 30, 60, 120, 120])
check("rate_limit floors at 60s", w.backoff(1, "rate_limit", cfg) == 60)
check("overloaded floors at 60s", w.backoff(1, "overloaded", cfg) == 60)
check("a broken backoff list falls back",
      w.backoff(1, "server_error", dict(cfg, backoff=7)) == 5, "non-list must not crash")

h = home()
for _ in range(8):
    hook(h)
    (Path(h) / "s1.incident").unlink(missing_ok=True)
out, _ = hook(h)
check("cap stops the 9th", incident(h) is None and "gave up" in msg(out), out)
check("counter cleared after giving up", not (Path(h) / "s1.attempts").exists())

h = home()
outs = [hook(h, WATCHDOG_MAX_RETRIES=2)[0] for _ in range(3)]
check("WATCHDOG_MAX_RETRIES honoured", "gave up after 2" in msg(outs[2]), outs[2])

# --- counters are per session ----------------------------------------------
h = home()
hook(h, sid="alpha"); (Path(h) / "alpha.incident").unlink(missing_ok=True)
hook(h, sid="alpha"); hook(h, sid="beta")
check("per-session counters", incident(h, "beta")["attempt"] == 1, incident(h, "beta"))
check("other session keeps its count", incident(h, "alpha")["attempt"] == 2, incident(h, "alpha"))

# --- configuration ---------------------------------------------------------
check("bool from env", w._coerce(False, "1") is True and w._coerce(True, "off") is False)
check("int list from a single value", w._coerce(w.DEFAULTS["backoff"], "1") == [1],
      "regression: a scalar must not leave a list key holding an int")
check("int list from commas", w._coerce(w.DEFAULTS["backoff"], "10,30,60") == [10, 30, 60],
      "regression: elements must be ints, or max(delay, floor) explodes")
check("int list from json", w._coerce(w.DEFAULTS["backoff"], "[5,10]") == [5, 10])
check("garbage int list falls back",
      w._coerce(w.DEFAULTS["backoff"], "fast,slow") == w.DEFAULTS["backoff"])
check("string list from commas",
      w._coerce(w.DEFAULTS["retry_types"], "a,b") == ["a", "b"])

h = home()
Path(h, "config.json").write_text(json.dumps({"max_retries": 1, "retry_types": ["teapot_error"]}))
out, _ = hook(h, error="teapot_error", message="now allowed", sid="cfg")
check("config file widens retry_types", incident(h, "cfg") is not None, out)
out, _ = hook(h, error="server_error", sid="cfg2")
check("config file narrows retry_types too", incident(h, "cfg2") is None, out)

h = home()
r = run(["set", "max_retries", "3"], h)
check("set writes the file", '"max_retries": 3' in Path(h, "config.json").read_text(), r.stdout)
r = run(["set", "backoff", "9,9"], h)
check("set parses an int list", json.loads(Path(h, "config.json").read_text())["backoff"] == [9, 9], r.stdout)
r = run(["set", "nonsense", "1"], h)
check("unknown config key refused", r.returncode == 1 and "unknown key" in r.stderr, r.stderr)
r = run(["config"], h, WATCHDOG_MAX_RETRIES=99)
check("env beats file in config output", "99" in r.stdout and "<- changed" in r.stdout, r.stdout)

# --- on/off round trip ----------------------------------------------------
h = home()
e = dict(os.environ, WATCHDOG_HOME=h)
e.pop("WATCHDOG_ENABLED", None)
def ctl(*a):
    return subprocess.run([sys.executable, str(SCRIPT)] + list(a), capture_output=True,
                          text=True, env=e, timeout=30).stdout.strip()
check("default is off", ctl("status") == "Watchdog off", ctl("status"))
ctl("on")
check("on persists", ctl("status") == "Watchdog on", ctl("status"))
ctl("off")
check("off persists", ctl("status") == "Watchdog off", ctl("status"))

# --- session correlation: the exact bug that reached a real run ------------
def fake_tree(table):
    """table: pid -> (ppid, comm, args)"""
    def fake(pid, fmt):
        row = table.get(int(pid))
        if not row:
            return ""
        return {"ppid=": str(row[0]), "comm=": row[1], "args=": row[2]}.get(fmt, "")
    return fake

real_ps = w._ps
me = os.getpid()
w._ps = fake_tree({
    me: (200, "python3", "python3 watchdog.py hook"),
    # a shell whose command line mentions a path containing the word "claude"
    200: (300, "/opt/homebrew/bin/bash", "bash -c /Users/x/claude-watchdog/scripts/watchdog.py"),
    300: (1, "claude", "claude --resume"),
})
check("walk skips a shell that merely mentions claude in its argv", w.claude_pid() == 300,
      "regression: matching argv stopped the walk on the shell, so hook and monitor disagreed")
w._ps = fake_tree({
    me: (200, "python3", "python3 watchdog.py hook"),
    200: (1, "node", "node /usr/lib/claude/cli.js --resume"),
})
check("a node runtime hosting claude is accepted", w.claude_pid() == 200)
w._ps = fake_tree({me: (200, "python3", "x"), 200: (1, "tmux", "tmux")})
check("no claude ancestor -> None", w.claude_pid() is None)
w._ps = real_ps

# --- fifo channel ---------------------------------------------------------
h = home()
os.environ["WATCHDOG_HOME"] = h
importlib.reload(w)
fifo = w.fifo_for(4242)
check("no fifo -> no reader", not w.fifo_reader_present(4242))
os.mkfifo(str(fifo))
check("fifo with nobody reading -> no reader", not w.fifo_reader_present(4242),
      "ENXIO is the signal that no monitor is listening")
fd = os.open(str(fifo), os.O_RDWR | os.O_NONBLOCK)
check("fifo held open -> reader present", w.fifo_reader_present(4242),
      "monitor.sh holds it O_RDWR precisely so this is true on Darwin too")
check("write succeeds", w.fifo_write(4242, "hello monitor"))
check("reader sees the line", os.read(fd, 4096).decode().strip() == "hello monitor")
check("newlines are flattened to keep one message per line",
      w.fifo_write(4242, "two\nlines") and b"\n" == os.read(fd, 4096)[-1:])
os.close(fd)

# --- delivery happens exactly once ---------------------------------------
h = home()
os.environ["WATCHDOG_HOME"] = h
importlib.reload(w)
Path(h, "once.incident").write_text(json.dumps(
    {"session_id": "once", "error": "server_error", "message": "x",
     "attempt": 1, "max_retries": 8, "owner": None}))
a = run(["deliver", "once"], h, WATCHDOG_CHANNEL="monitor")
b = run(["deliver", "once"], h, WATCHDOG_CHANNEL="monitor")
delivered = (Path(h) / "log").read_text() if (Path(h) / "log").exists() else ""
check("incident consumed", not (Path(h) / "once.incident").exists())
check("second delivery is a no-op", delivered.count("once") <= 1, delivered)

# --- the resident monitor really is POSIX and really relays --------------
for shell in ("sh", "dash"):
    if not any((Path(d) / shell).exists() for d in os.environ.get("PATH", "").split(":")):
        continue
    check(f"monitor.sh parses under {shell}",
          subprocess.run([shell, "-n", str(MONITOR)], capture_output=True).returncode == 0)

# --- the resident monitor, driven with an explicit owner -----------------
def spawn_monitor(h, owner, liveness=1, shell="sh"):
    out = Path(h) / "monitor.out"
    fh = out.open("w")
    env = dict(os.environ, WATCHDOG_HOME=str(h), WATCHDOG_OWNER_PID=str(owner),
               WATCHDOG_LIVENESS_SECONDS=str(liveness))
    proc = subprocess.Popen([shell, str(MONITOR)], stdout=fh, stderr=subprocess.STDOUT, env=env)
    for _ in range(50):
        if (Path(h) / f"{owner}.resume").exists():
            break
        time.sleep(0.1)
    return proc, out


h = home()
owner = subprocess.Popen(["sh", "-c", "while :; do sleep 1; done"])
mon, out = spawn_monitor(h, owner.pid)
try:
    check("monitor creates a fifo named for its owner", (Path(h) / f"{owner.pid}.resume").is_fifo())
    os.environ["WATCHDOG_HOME"] = h
    importlib.reload(w)
    check("hook would see the monitor as listening", w.fifo_reader_present(owner.pid))
    check("write reaches the monitor", w.fifo_write(owner.pid, "RELAYED LINE"))
    time.sleep(1)
    check("monitor relays it to stdout", "RELAYED LINE" in out.read_text(), out.read_text()[:200])
    time.sleep(2.5)  # at least two liveness ticks
    check("liveness ticks are never relayed", "__watchdog_liveness__" not in out.read_text(),
          "a leaked tick would become a notification, and cost tokens on a timer")
    check("monitor still alive while its owner is", mon.poll() is None)
    owner.kill()
    owner.wait()
    deadline = time.time() + 10
    while mon.poll() is None and time.time() < deadline:
        time.sleep(0.2)
    check("monitor exits once its owner is gone", mon.poll() is not None,
          "regression: Claude Code does not reap monitors on a hard kill, so this must self-terminate")
    check("monitor removes its fifo on exit", not (Path(h) / f"{owner.pid}.resume").exists())
finally:
    if mon.poll() is None:
        mon.kill()
    if owner.poll() is None:
        owner.kill()

h = home()
owner = subprocess.Popen(["sh", "-c", "while :; do sleep 1; done"])
mon, _ = spawn_monitor(h, owner.pid, liveness=300)
try:
    mon.terminate()
    deadline = time.time() + 5
    while mon.poll() is None and time.time() < deadline:
        time.sleep(0.1)
    check("SIGTERM kills a monitor parked on the fifo", mon.poll() is not None,
          "regression: trapping TERM made it unkillable, because a shell defers a "
          "trapped signal until the blocking read returns")
finally:
    if mon.poll() is None:
        mon.kill()
    owner.kill()

# --- garbage in, no crash ------------------------------------------------
h = home()
for junk in ["not json at all", "{}", ""]:
    p = run(["hook"], h, junk)
    check(f"junk payload exits clean: {junk[:12]!r}",
          p.returncode == 0 and not p.stderr.strip(), p.stderr[-200:])

print()
if failures:
    print("FAILED:", failures)
    sys.exit(1)
print("all green")
