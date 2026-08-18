#!/usr/bin/env python3
"""Tests for AFK. The hook runs as a real subprocess against a throwaway state dir."""

import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

AFK = Path(__file__).resolve().parent.parent / "scripts" / "afk.py"
failures = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   <- {detail}"))
    if not cond:
        failures.append(name)


def home():
    return tempfile.mkdtemp()


def run(args, h, stdin="", **env):
    e = dict(os.environ, AFK_HOME=str(h), AFK_ENABLED="1", AFK_CHANNEL="monitor")
    e.pop("AFK_MAX_RETRIES", None)
    e.update({k: str(v) for k, v in env.items()})
    p = subprocess.run([sys.executable, str(AFK)] + args, input=stdin,
                       capture_output=True, text=True, env=e, timeout=30)
    return p


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


def queued(h, sid="s1"):
    p = Path(h) / f"{sid}.resume"
    return json.loads(p.read_text()) if p.exists() else None


def msg(out):
    return str(out.get("systemMessage"))


# --- the reported failure: a stream that dies mid-turn ----------------------
h = home()
out, p = hook(h)
req = queued(h)
check("transient error queues a resume", req is not None, p.stderr[-300:])
check("attempt starts at 1", req and req["attempt"] == 1, req)
check("first backoff is 5s", req and req["delay"] == 5, req)
check("delivery is scheduled, not immediate", req and 3 < req["deliver_at"] - time.time() <= 5, req)
check("hook reports the retry", "retry 1/8" in msg(out), out)

# --- off means off ----------------------------------------------------------
h = home()
out, _ = hook(h, AFK_ENABLED="0")
check("disabled queues nothing", queued(h) is None, out)

# --- fatal error types ------------------------------------------------------
for etype in ["model_not_found", "authentication_failed", "billing_error",
              "invalid_request", "oauth_org_not_allowed", "max_output_tokens"]:
    h = home()
    out, _ = hook(h, error=etype, message="nope")
    check(f"fatal type not retried: {etype}", queued(h) is None, out)
    check(f"named as known-fatal: {etype}", "known-fatal" in msg(out), msg(out))

# --- a spent quota looks retryable but is not ------------------------------
h = home()
out, _ = hook(h, error="rate_limit",
              message="API Error: Request rejected (429) - usage limit reached, resets at 3pm")
check("spent quota beats retryable type", queued(h) is None, out)
check("spent quota reported as known-fatal", "known-fatal" in msg(out), msg(out))

# --- never-seen error type is surfaced, not retried ------------------------
h = home()
out, _ = hook(h, error="teapot_error", message="short and stout")
check("unknown type not retried", queued(h) is None, out)
check("unknown type flagged unrecognised", "unrecognised" in msg(out) and "known-fatal" not in msg(out), msg(out))

# --- rate limits wait even on attempt 1 ------------------------------------
for etype in ["rate_limit", "overloaded"]:
    h = home()
    hook(h, error=etype, message="slow down")
    check(f"{etype} waits >=60s", queued(h)["delay"] >= 60, queued(h))

# --- backoff escalates, cap stops it ---------------------------------------
h = home()
delays, seen = [], []
for _ in range(9):
    out, _ = hook(h)
    req = queued(h)
    delays.append(req["delay"] if req else None)
    seen.append(bool(req))
    if req:
        (Path(h) / "s1.resume").unlink()
check("backoff escalates", delays[:6] == [5, 15, 30, 60, 120, 120], delays)
check("queues while under the cap", all(seen[:8]), seen)
check("cap stops the 9th", not seen[8] and "gave up" in msg(out), out)
check("counter cleared after giving up", not (Path(h) / "s1.attempts").exists())

# --- counters are per session ---------------------------------------------
h = home()
hook(h, sid="alpha"); hook(h, sid="alpha"); hook(h, sid="beta")
check("per-session counters", queued(h, "beta")["attempt"] == 1, queued(h, "beta"))
check("other session keeps its count", queued(h, "alpha")["attempt"] == 2, queued(h, "alpha"))

# --- configuration: env, file, and afk set --------------------------------
h = home()
outs = [hook(h, AFK_MAX_RETRIES=2)[0] for _ in range(3)]
check("AFK_MAX_RETRIES honoured", "gave up after 2" in msg(outs[2]), outs[2])

h = home()
Path(h, "config.json").write_text(json.dumps({"max_retries": 1, "backoff": [7]}))
e = dict(os.environ, AFK_HOME=h, AFK_ENABLED="1", AFK_CHANNEL="monitor")
e.pop("AFK_MAX_RETRIES", None)
p1 = subprocess.run([sys.executable, str(AFK), "hook"], input=json.dumps(
    {"session_id": "cfg", "error": "server_error", "last_assistant_message": "x"}),
    capture_output=True, text=True, env=e, timeout=30)
check("config file sets backoff", queued(h, "cfg")["delay"] == 7, queued(h, "cfg"))
p2 = subprocess.run([sys.executable, str(AFK), "hook"], input=json.dumps(
    {"session_id": "cfg", "error": "server_error", "last_assistant_message": "x"}),
    capture_output=True, text=True, env=e, timeout=30)
check("config file sets max_retries", "gave up after 1" in p2.stdout, p2.stdout)

h = home()
r = run(["set", "max_retries", "3"], h)
check("afk set writes the file", '"max_retries": 3' in Path(h, "config.json").read_text(), r.stdout)
r = run(["set", "retry_types", "server_error,teapot_error"], h)
check("afk set parses a list", json.loads(Path(h, "config.json").read_text())["retry_types"]
      == ["server_error", "teapot_error"], r.stdout)
out, _ = hook(h, error="teapot_error", message="now allowed")
check("widened retry_types takes effect", queued(h) is not None, out)
r = run(["set", "nonsense", "1"], h)
check("unknown config key refused", r.returncode == 1 and "unknown key" in r.stderr, r.stderr)

h = home()
r = run(["config"], h, AFK_MAX_RETRIES=99)
check("config shows effective value", "99" in r.stdout and "<- changed" in r.stdout, r.stdout)

# --- on/off/status round trip --------------------------------------------
h = home()
e = dict(os.environ, AFK_HOME=h); e.pop("AFK_ENABLED", None)
def ctl(*a):
    return subprocess.run([sys.executable, str(AFK)] + list(a), capture_output=True,
                          text=True, env=e, timeout=30).stdout.strip()
check("default is off", ctl("status") == "AFK off", ctl("status"))
ctl("on")
check("on persists", ctl("status") == "AFK on", ctl("status"))
ctl("off")
check("off persists", ctl("status") == "AFK off", ctl("status"))

# --- direct-import helpers ----------------------------------------------
h = home()
os.environ["AFK_HOME"] = h
sys.path.insert(0, str(AFK.parent))
import afk as mod  # noqa: E402
importlib.reload(mod)
cfg = mod.config()

mod._path("race", "resume").write_text(json.dumps(
    {"session_id": "race", "attempt": 1, "max_retries": 8, "deliver_at": 0,
     "error": "server_error", "message": "x"}))
first, second = mod.claim("race"), mod.claim("race")
check("first claim wins", first is not None, first)
check("second claim gets nothing", second is None, second)

check("no heartbeat -> monitor down", not mod.monitor_alive("ghost", cfg))
mod._path("live", "monitor").touch()
check("fresh heartbeat -> monitor up", mod.monitor_alive("live", cfg))
stale = mod._path("stale", "monitor"); stale.touch()
os.utime(stale, (time.time() - 600,) * 2)
check("stale heartbeat -> monitor down", not mod.monitor_alive("stale", cfg))

text = mod.resume_text({"attempt": 2, "max_retries": 8, "error": "server_error",
                        "message": "The response stopped arriving"}, cfg)
check("resume text says where to resume", "exactly where it stopped" in text, text)
check("resume text warns about mid-flight work", "mid-flight" in text, text)
check("resume text names the attempt", "retry 2 of 8" in text, text)
custom = dict(cfg, resume_message="go on, {attempt}/{max_retries}, {error}")
check("resume text is configurable",
      mod.resume_text({"attempt": 1, "max_retries": 4, "error": "server_error"}, custom)
      == "go on, 1/4, server_error")

# --- garbage in, no crash ----------------------------------------------
h = home()
for junk in ["not json at all", "{}", ""]:
    p = run(["hook"], h, junk)
    check(f"junk payload exits clean: {junk[:12]!r}", p.returncode == 0 and not p.stderr.strip(),
          p.stderr[-200:])

print()
if failures:
    print("FAILED:", failures)
    sys.exit(1)
print("all green")
