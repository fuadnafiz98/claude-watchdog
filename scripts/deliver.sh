#!/bin/bash
# Fallback delivery: type the resume into the session's own terminal.
#
# Used when no plugin monitor is running for the session. tmux is the only
# reliable way to push text onto another process's stdin, so a session outside
# tmux gets a notification instead of a resume -- see README.
set -uo pipefail

SESSION_ID="${1:-}"
DELAY="${2:-0}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AFK_DIR="${AFK_HOME:-$HOME/.claude/afk}"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$AFK_DIR/log" 2>/dev/null || true; }

[ -n "$SESSION_ID" ] || exit 0
sleep "$DELAY"

# The monitor may have taken the request while we were sleeping.
REQ=$(python3 "$HERE/claim.py" "$SESSION_ID") || exit 0
[ -n "$REQ" ] || { log "session=$SESSION_ID request already claimed, terminal sender standing down"; exit 0; }

# Resolve the pane whose process tree contains the claude session.
CLAUDE_PID=$(claude agents --json 2>/dev/null | python3 -c '
import json,sys
want = sys.argv[1]
try: rows = json.load(sys.stdin)
except Exception: rows = []
for r in rows if isinstance(rows, list) else []:
    if r.get("sessionId") == want and r.get("pid"):
        print(r["pid"]); break
' "$SESSION_ID")

pane=""
if [ -n "$CLAUDE_PID" ] && command -v tmux >/dev/null 2>&1 && tmux info >/dev/null 2>&1; then
  while read -r pid target; do
    [ -n "$pid" ] || continue
    probe="$CLAUDE_PID"
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
      [ -n "$probe" ] && [ "$probe" != "1" ] || break
      if [ "$probe" = "$pid" ]; then pane="$target"; break; fi
      probe=$(ps -o ppid= -p "$probe" 2>/dev/null | tr -d ' ')
    done
    [ -n "$pane" ] && break
  done < <(tmux list-panes -a -F '#{pane_pid} #{session_name}:#{window_index}.#{pane_index}' 2>/dev/null)
fi

if [ -n "$pane" ]; then
  # Send the text and the newline separately: one send-keys with Enter can submit
  # before a busy TUI has finished accepting the text.
  tmux send-keys -t "$pane" -l "continue"
  sleep 0.3
  tmux send-keys -t "$pane" Enter
  log "session=$SESSION_ID delivered via tmux pane $pane"
  exit 0
fi

printf '\a' > /dev/tty 2>/dev/null || true
if command -v osascript >/dev/null 2>&1; then
  osascript -e 'display notification "A turn stalled on an API error and needs a manual continue." with title "Claude Code AFK"' >/dev/null 2>&1 || true
fi
log "session=$SESSION_ID NO delivery channel (no monitor, no tmux pane) -- notified only"
