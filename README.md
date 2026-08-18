# claude-afk

A Claude Code plugin that resumes a turn the API killed, so an unattended run keeps
going instead of sitting at

```
⏺ API Error: The response stopped arriving. The response above may be incomplete.
```

until someone comes back and types `continue`.

## Install

From inside a Claude Code session:

```
/plugin marketplace add fuadnafiz98/claude-afk
/plugin install afk@claude-afk
```

Then restart the session (monitors only start at session start) and turn it on:

```
/afk on
```

Verify with `/afk`, which reports which delivery channel is actually live. Note that
marketplaces are managed by the in-session `/plugin` command; `claude marketplace ...`
is not a CLI subcommand.

It is **off by default**. Auto-continuing is what you want overnight, not while you
are watching.

## How it works

Claude Code raises two different end-of-turn events, and the difference is the whole
design:

| Event | Fires when | Can it block the stop? |
| --- | --- | --- |
| `Stop` | Claude finishes responding normally | yes |
| `StopFailure` | the turn ends on an API error | **no** |

An API error fires **only** `StopFailure`, and `StopFailure`'s output is discarded.
So a hook cannot simply refuse to stop — it has to get a message into the session
from outside the turn. This plugin does that in three parts:

1. **Detect.** The `StopFailure` hook receives the error already classified:

   ```json
   {"session_id": "…", "error": "server_error",
    "last_assistant_message": "API Error: The response stopped arriving", …}
   ```

   It decides retryable vs fatal, applies backoff and a retry cap, and writes a
   resume request stamped with a `deliver_at` time. It never sleeps — a hook that
   blocks would just delay the session for no benefit, since its output is ignored.

2. **Deliver.** Whichever channel is available takes the request:

   - **monitor** (preferred, in-band, no dependencies) — the plugin runs a background
     monitor, and a monitor's stdout lines reach Claude as notifications. That is the
     only way to wake a session without touching its terminal.
   - **tmux** — if no monitor is running, a detached sender resolves the session's
     pane by walking the process tree from the session pid to a `pane_pid`, then
     `tmux send-keys`.
   - **notification only** — outside tmux with no monitor, nothing can type for you,
     so the plugin rings the bell and posts a desktop notification rather than
     pretending it recovered.

   Exactly one channel can act: the request is claimed with an atomic rename, so the
   monitor and the tmux sender can never both resume the same turn.

3. **Resume.** The delivered message tells Claude the turn was cut off mid-work, to
   pick up where it stopped, and to re-check anything that was in flight (a file edit
   or command may or may not have landed) rather than blindly redoing it.

## What it will and will not retry

The `error` field from `StopFailure` drives the decision.

| Retried | Never retried |
| --- | --- |
| `rate_limit`, `overloaded`, `server_error`, `unknown` | `authentication_failed`, `oauth_org_not_allowed`, `billing_error`, `invalid_request`, `model_not_found`, `max_output_tokens` |

The error type alone is not sufficient, so the message text is checked first. A spent
quota arrives as `rate_limit` but retrying it for an hour in 60-second steps is
pointless, so text like `usage limit reached`, `credit balance`, `prompt is too long`
is treated as fatal regardless of type.

An error type in neither list is reported and **not** retried. Widening the retry
list is a deliberate change with a test, not a default.

Backoff is 5s → 15s → 30s → 60s → 120s, with a 60s floor for rate limits and
overload, capped at 8 retries per session (`AFK_MAX_RETRIES`). The counter resets
after 12 idle hours.

## Commands

```
/afk                 # which channel is live, per session
/afk on | off        # arm or disarm
```

From a shell, or from Claude's own Bash tool (the plugin puts `afk` on its PATH):

```sh
afk on | off | status
afk doctor           # enabled? monitor up? tmux reachable? anything queued?
afk log 20
afk reset            # clear retry counters and queued resumes
afk test             # run the test suite
```

Start with `afk doctor`. If it reports `channel=notify only`, nothing can type for
you — run the session inside tmux, or restart it so the monitor comes up.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `AFK_ENABLED` | unset | `1`/`0` overrides the on/off flag |
| `AFK_MAX_RETRIES` | `8` | retries per session before giving up |
| `AFK_CHANNEL` | auto | force `monitor` or `terminal` |
| `AFK_HOME` | `~/.claude/afk` | state directory |

State lives outside the plugin directory, so updating or reinstalling the plugin
keeps your flag, counters, and log.

## Verified vs not

Verified on macOS, Claude Code 2.1.234:

- `Stop` does **not** fire on an API error; `StopFailure` does. Probed with a real
  failing turn, not inferred from docs.
- The `StopFailure` payload shape quoted above, including the classified `error` field.
- tmux delivery end to end: pane resolved by walking the process tree, `continue\n`
  arrived on the target program's stdin, request drained, exactly once.
- Classification, backoff, cap, per-session counters, single-claim race, and malformed
  input: 41 checks in `tests/test_afk.py`, and 11 deliberate mutations of the source
  are each caught by them.

Not verified: a live `The response stopped arriving` (it cannot be triggered on
demand — the error-type mapping is inferred for that specific message), and the
monitor wake-up path, which needs a session restart to observe. When the tmux path
is available it is the one with an end-to-end proof.

For a fully unattended run with no TUI at all, a `claude -p --resume` retry loop is
sturdier than any in-session mechanism. This plugin is for the case where you want to
leave a live session working overnight.

## License

MIT
