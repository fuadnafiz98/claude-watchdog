"""Shared state for the AFK plugin.

Everything lives under ~/.claude/afk so the plugin can be reinstalled or updated
without losing the enabled flag or an in-flight resume request.
"""

import json
import os
import time
from pathlib import Path

HOME = Path(os.environ.get("AFK_HOME") or Path.home() / ".claude" / "afk")
QUEUE = HOME / "queue"
STATE = HOME / "state"
LOG = HOME / "log"
FLAG = HOME / "enabled"

DEFAULT_MAX_RETRIES = 8
BACKOFF = [5, 15, 30, 60, 120]
RATE_LIMIT_FLOOR = 60
HEARTBEAT_STALE = 30

# StopFailure hands us a classified error type. These are worth replaying as-is.
RETRYABLE = {"rate_limit", "overloaded", "server_error", "unknown"}

# These fail identically on every retry until a human intervenes.
FATAL = {
    "authentication_failed",
    "oauth_org_not_allowed",
    "billing_error",
    "invalid_request",
    "model_not_found",
    "max_output_tokens",
}

# The error type alone is not always enough: a rate_limit that is really a spent
# usage quota should not be retried for an hour in 60s steps.
FATAL_TEXT = (
    "usage limit reached",
    "credit balance",
    "resets at",
    "prompt is too long",
    "exceeds the maximum",
    "failed to authenticate",
)

SLOW_TYPES = {"rate_limit", "overloaded"}


def ensure_dirs():
    QUEUE.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(parents=True, exist_ok=True)


def enabled() -> bool:
    env = os.environ.get("AFK_ENABLED")
    if env is not None:
        return env not in ("", "0", "false", "no", "off")
    return FLAG.exists()


def max_retries() -> int:
    try:
        return max(1, int(os.environ.get("AFK_MAX_RETRIES", DEFAULT_MAX_RETRIES)))
    except ValueError:
        return DEFAULT_MAX_RETRIES


def log(msg: str):
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def safe(name: str) -> str:
    return "".join(c for c in (name or "") if c.isalnum() or c in "-_") or "unknown"


def attempts_path(session_id: str) -> Path:
    return STATE / f"{safe(session_id)}.attempts"


def read_attempts(session_id: str) -> int:
    p = attempts_path(session_id)
    try:
        if time.time() - p.stat().st_mtime > 12 * 3600:
            return 0
        return int(p.read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def write_attempts(session_id: str, n: int):
    ensure_dirs()
    attempts_path(session_id).write_text(str(n))


def clear_attempts(session_id: str):
    attempts_path(session_id).unlink(missing_ok=True)


def heartbeat_path(session_id: str) -> Path:
    return STATE / f"{safe(session_id)}.monitor"


def monitor_alive(session_id: str, now: float | None = None) -> bool:
    """True when this session's monitor touched its heartbeat recently."""
    now = time.time() if now is None else now
    try:
        return now - heartbeat_path(session_id).stat().st_mtime < HEARTBEAT_STALE
    except OSError:
        return False


def queue_path(session_id: str) -> Path:
    return QUEUE / f"{safe(session_id)}.json"


def backoff_for(attempt: int, error_type: str) -> int:
    delay = BACKOFF[min(max(attempt, 1) - 1, len(BACKOFF) - 1)]
    if error_type in SLOW_TYPES:
        delay = max(delay, RATE_LIMIT_FLOOR)
    return delay


def classify(error_type: str, message: str):
    """Return (verdict, detail) where verdict is retry | fatal | unrecognised."""
    error_type = (error_type or "unknown").strip()
    low = (message or "").lower()

    hit = next((t for t in FATAL_TEXT if t in low), None)
    if hit:
        return "fatal", f"known-fatal: message says {hit!r}"
    if error_type in FATAL:
        return "fatal", f"known-fatal error type {error_type}"
    if error_type in RETRYABLE:
        return "retry", f"error type {error_type}"
    return "unrecognised", f"unrecognised error type {error_type!r}"


def write_request(session_id: str, payload: dict):
    ensure_dirs()
    tmp = queue_path(session_id).with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(queue_path(session_id))


def read_request(session_id: str):
    try:
        return json.loads(queue_path(session_id).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def claim_request(session_id: str):
    """Take the request so only one delivery channel can act on it."""
    p = queue_path(session_id)
    claimed = p.with_suffix(".claimed")
    try:
        p.rename(claimed)
    except OSError:
        return None
    try:
        data = json.loads(claimed.read_text())
    except (OSError, json.JSONDecodeError):
        data = None
    claimed.unlink(missing_ok=True)
    return data


def resume_text(req: dict) -> str:
    return (
        "The previous turn did not finish: it was cut off by a transient API error "
        f"({req.get('error_type', 'unknown')}: {req.get('message', '')[:200]}). "
        f"This is automatic retry {req.get('attempt')} of {req.get('max_retries')}, "
        "equivalent to a human typing \"continue\". Resume the task exactly where it "
        "stopped. Re-check anything that was mid-flight, since a file edit or command "
        "may or may not have landed, before redoing it. Do not restart the task, "
        "re-explain finished work, or apologise."
    )
