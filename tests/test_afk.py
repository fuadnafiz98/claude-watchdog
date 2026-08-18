#!/usr/bin/env python3
"""Tests for the AFK plugin. Runs the real hook as a subprocess against a temp state dir."""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

failures = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   <- {detail}"))
    if not cond:
        failures.append(name)


def run_hook(payload, home, **env):
    e = dict(os.environ, AFK_HOME=str(home), AFK_ENABLED="1", AFK_CHANNEL="monitor")
    e.update({k: str(v) for k, v in env.items()})
    p = subprocess.run(
        [sys.executable, str(SCRIPTS / "on-stop-failure.py")],
        input=json.dumps(payload), capture_output=True, text=True, env=e, timeout=30,
    )
    out = {}
    if p.stdout.strip():
        try:
            out = json.loads(p.stdout)
        except json.JSONDecodeError:
            out = {"raw": p.stdout}
    return out, p


def failure(error="server_error", message="API Error: The response stopped arriving", sid="s1"):
    return {
        "session_id": sid, "hook_event_name": "StopFailure", "error": error,
        "last_assistant_message": message, "cwd": "/tmp",
        "transcript_path": "/tmp/none.jsonl",
    }


def queued(home, sid):
    p = Path(home) / "queue" / f"{sid}.json"
    return json.loads(p.read_text()) if p.exists() else None


def fresh():
    return tempfile.mkdtemp()


# --- the reported error: a stream that dies mid-turn -------------------------
home = fresh()
out, p = run_hook(failure(), home)
req = queued(home, "s1")
check("transient error queues a resume", req is not None, p.stderr[-300:])
check("attempt starts at 1", req and req["attempt"] == 1, req)
check("first backoff is 5s", req and req["delay"] == 5, req)
check("deliver_at is in the future", req and 3 < req["deliver_at"] - time.time() <= 5, req)
check("hook reports the retry", "retry 1/8" in str(out.get("systemMessage")), out)

# --- disabled is genuinely disabled ------------------------------------------
home = fresh()
e = dict(os.environ, AFK_HOME=home, AFK_ENABLED="0")
p = subprocess.run([sys.executable, str(SCRIPTS / "on-stop-failure.py")],
                   input=json.dumps(failure()), capture_output=True, text=True, env=e, timeout=30)
check("disabled queues nothing", queued(home, "s1") is None, p.stdout)

# --- fatal error types must never retry -------------------------------------
for etype in ["model_not_found", "authentication_failed", "billing_error",
              "invalid_request", "oauth_org_not_allowed", "max_output_tokens"]:
    home = fresh()
    out, _ = run_hook(failure(error=etype, message="nope"), home)
    msg = str(out.get("systemMessage"))
    check(f"fatal type not retried: {etype}", queued(home, "s1") is None, out)
    check(f"fatal type named as known-fatal: {etype}", "known-fatal" in msg, msg)

# --- a spent quota looks like a rate limit but is fatal ----------------------
home = fresh()
out, _ = run_hook(failure(error="rate_limit",
                          message="API Error: Request rejected (429) - usage limit reached, resets at 3pm"), home)
check("spent quota beats retryable type",
      queued(home, "s1") is None and "not resuming" in str(out.get("systemMessage")), out)

# --- an error type we have never seen is surfaced, not retried ---------------
home = fresh()
out, _ = run_hook(failure(error="teapot_error", message="short and stout"), home)
check("unknown type not retried", queued(home, "s1") is None, out)
check("unknown type flagged as unrecognised, not known-fatal",
      "unrecognised" in str(out.get("systemMessage")) and "known-fatal" not in str(out.get("systemMessage")),
      out)

# --- rate limits wait, even on the first attempt ----------------------------
for etype in ["rate_limit", "overloaded"]:
    home = fresh()
    run_hook(failure(error=etype, message="slow down"), home)
    req = queued(home, "s1")
    check(f"{etype} waits >=60s", req and req["delay"] >= 60, req)

# --- backoff escalates, then the cap stops it -------------------------------
home = fresh()
delays, verdicts = [], []
for _ in range(9):
    out, _ = run_hook(failure(), home)
    req = queued(home, "s1")
    delays.append(req["delay"] if req else None)
    verdicts.append(bool(req))
    if req:
        (Path(home) / "queue" / "s1.json").unlink()
check("backoff escalates", delays[:6] == [5, 15, 30, 60, 120, 120], delays)
check("queues while under the cap", all(verdicts[:8]), verdicts)
check("cap stops the 9th", not verdicts[8] and "gave up" in str(out.get("systemMessage")), out)
check("counter cleared after giving up",
      not (Path(home) / "state" / "s1.attempts").exists())

# --- the cap is configurable -------------------------------------------------
home = fresh()
outs = [run_hook(failure(), home, AFK_MAX_RETRIES=2)[0] for _ in range(3)]
check("AFK_MAX_RETRIES honoured", "gave up after 2" in str(outs[2].get("systemMessage")), outs[2])

# --- a clean retry of a different session keeps its own counter --------------
home = fresh()
run_hook(failure(sid="alpha"), home)
run_hook(failure(sid="alpha"), home)
run_hook(failure(sid="beta"), home)
check("counters are per session", queued(home, "beta")["attempt"] == 1, queued(home, "beta"))
check("other session kept its count", queued(home, "alpha")["attempt"] == 2, queued(home, "alpha"))

# --- only one channel may deliver a given request ---------------------------
import afk_common as afk  # noqa: E402
home = fresh()
os.environ["AFK_HOME"] = home
import importlib  # noqa: E402
importlib.reload(afk)
afk.write_request("race", {"session_id": "race", "attempt": 1, "max_retries": 8,
                           "deliver_at": 0, "error_type": "server_error", "message": "x"})
first = afk.claim_request("race")
second = afk.claim_request("race")
check("first claim wins", first is not None, first)
check("second claim gets nothing", second is None, second)

# --- claim.py, used by the terminal sender, agrees --------------------------
afk.write_request("race2", {"session_id": "race2", "attempt": 1, "max_retries": 8,
                            "deliver_at": 0, "error_type": "server_error", "message": "x"})
env = dict(os.environ, AFK_HOME=home)
a = subprocess.run([sys.executable, str(SCRIPTS / "claim.py"), "race2"],
                   capture_output=True, text=True, env=env, timeout=20).stdout.strip()
b = subprocess.run([sys.executable, str(SCRIPTS / "claim.py"), "race2"],
                   capture_output=True, text=True, env=env, timeout=20).stdout.strip()
check("claim.py is single-shot", a and not b, (a[:40], b[:40]))

# --- heartbeat decides the channel -----------------------------------------
check("no heartbeat -> monitor considered down", not afk.monitor_alive("ghost"))
afk.ensure_dirs()
afk.heartbeat_path("live").touch()
check("fresh heartbeat -> monitor up", afk.monitor_alive("live"))
old = afk.heartbeat_path("stale")
old.touch()
os.utime(old, (time.time() - 600, time.time() - 600))
check("stale heartbeat -> monitor down", not afk.monitor_alive("stale"))

# --- the resume text has to actually instruct a resume ---------------------
text = afk.resume_text({"attempt": 2, "max_retries": 8, "error_type": "server_error",
                        "message": "The response stopped arriving"})
check("resume text says resume where it stopped", "exactly where it stopped" in text, text)
check("resume text warns about mid-flight work", "mid-flight" in text, text)
check("resume text names the attempt", "retry 2 of 8" in text, text)

# --- garbage in, no crash --------------------------------------------------
home = fresh()
e = dict(os.environ, AFK_HOME=home, AFK_ENABLED="1")
p = subprocess.run([sys.executable, str(SCRIPTS / "on-stop-failure.py")],
                   input="not json at all", capture_output=True, text=True, env=e, timeout=30)
check("malformed payload exits clean", p.returncode == 0 and not p.stderr.strip(), p.stderr[-200:])
p = subprocess.run([sys.executable, str(SCRIPTS / "on-stop-failure.py")],
                   input="{}", capture_output=True, text=True, env=e, timeout=30)
check("empty payload exits clean", p.returncode == 0, p.stderr[-200:])

print()
if failures:
    print("FAILED:", failures)
    sys.exit(1)
print(f"all green")
