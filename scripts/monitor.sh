#!/bin/sh
# Resident half of the watchdog: relays a resume into this session.
#
# A monitor's stdout reaches Claude as a notification, which is the only in-band
# way to wake a waiting session. This process therefore has to stay alive for the
# whole session, so it does as close to nothing as possible: it blocks in open(2)
# on a fifo and costs no CPU and ~2 MB until something is written to it. No poll,
# no timer, no heartbeat.
#
# The fifo is named after the owning claude process, found by walking up from
# here with ps. The hook is spawned by the same process and walks the same way,
# so both sides agree on the path without either calling the claude CLI.

set -u
DIR="${WATCHDOG_HOME:-$HOME/.claude/watchdog}"

# The command name is compared exactly, and the argv is consulted only for a
# runtime that hosts claude. Grepping the whole argv for "claude" would match any
# ancestor shell whose command line mentions such a path, and stop on the shell.
claude_pid() {
  pid=$$
  i=0
  while [ "$i" -lt 12 ]; do
    parent=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
    case "$parent" in ''|0|1) return 1 ;; esac
    comm=$(ps -o comm= -p "$parent" 2>/dev/null)
    case "${comm##*/}" in
      claude)
        echo "$parent"; return 0 ;;
      node|bun|deno)
        case "$(ps -o args= -p "$parent" 2>/dev/null)" in
          *claude*) echo "$parent"; return 0 ;;
        esac ;;
    esac
    pid=$parent
    i=$((i + 1))
  done
  return 1
}

owner=$(claude_pid) || exit 0
mkdir -p "$DIR" || exit 0
FIFO="$DIR/$owner.resume"

trap 'rm -f "$FIFO"' EXIT HUP INT TERM
rm -f "$FIFO"
mkfifo "$FIFO" 2>/dev/null || exit 0

# Hold the fifo open read-write, then block in `read`.
#
# The <> is not a nicety. A read-only open blocks until a writer arrives, and on
# Darwin a reader parked in open(2) does not satisfy another process opening
# O_WRONLY|O_NONBLOCK -- it still gets ENXIO, so the hook concludes no monitor is
# listening and falls back to tmux. Holding a write end too makes the reader
# visible from the moment the monitor starts, on both Darwin and Linux, and also
# means `read` never sees EOF, so there is no reopen churn.
#
# `read` is a builtin, so the steady state forks nothing and runs no timer: the
# process sleeps in the kernel until a resume is written.
exec 3<> "$FIFO" || exit 0
while :; do
  while IFS= read -r line <&3; do
    [ -n "$line" ] && printf '%s\n' "$line"
  done
  sleep 1
done
