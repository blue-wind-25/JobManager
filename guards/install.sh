#!/bin/sh
#
# Copyright (C) Aloysius Indrayanto
#               https://github.com/blue-wind-25/JobManager
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# claude-guards installer (design/CLAUDE_GUARDS.md sec 5):
#   install.sh --user                       every repository on this machine (~/.claude)
#   install.sh --repo PATH                  one repository (PATH/.claude, committed: cloud sessions get it)
#   options: --guards a,b                   subset of secret-echo,broad-search,protected-read (default: all)
#            --version TAG                  recorded in VERSION (default: this copy's VERSION)
#   install.sh --check [--user | --repo PATH]   print the installed and this copy's versions
# Copies hooks/ to <dest>/hooks/claude-guards/, merges one PreToolUse entry per guard into <dest>/settings.json
# (other hooks kept, an earlier claude-guards entry replaced), and writes <dest>/guards.conf if absent.
# Restart the Claude Code session afterwards: hooks are read at start.

set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
MODE=""
DEST=""
GUARDS="secret-echo,broad-search,protected-read"
VERSION="$(cat "$HERE/VERSION")"
CHECK=no

usage() {
    sed -n '/^#   install.sh/,/^# Restart/p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --user) MODE=user; shift ;;
        --repo) [ $# -ge 2 ] || usage; MODE=repo; DEST="$2"; shift 2 ;;
        --guards) [ $# -ge 2 ] || usage; GUARDS="$2"; shift 2 ;;
        --version) [ $# -ge 2 ] || usage; VERSION="$2"; shift 2 ;;
        --check) CHECK=yes; shift ;;
        -h|--help) usage ;;
        *) echo "install.sh: unknown argument $1" >&2; usage ;;
    esac
done
[ -n "$MODE" ] || MODE=user

if [ "$MODE" = user ]; then
    CLAUDE_DIR="$HOME/.claude"
    CMD_BASE='"$HOME"/.claude/hooks/claude-guards/guard.sh'
else
    [ -d "$DEST" ] || { echo "install.sh: no such directory: $DEST" >&2; exit 1; }
    CLAUDE_DIR="$(cd "$DEST" && pwd)/.claude"
    CMD_BASE='"$CLAUDE_PROJECT_DIR"/.claude/hooks/claude-guards/guard.sh'
fi
TARGET="$CLAUDE_DIR/hooks/claude-guards"

if [ "$CHECK" = yes ]; then
    if [ -f "$TARGET/VERSION" ]; then
        echo "installed: $(cat "$TARGET/VERSION") ($TARGET)"
    else
        echo "installed: none ($TARGET)"
    fi
    echo "this copy: $(cat "$HERE/VERSION") ($HERE)"
    exit 0
fi

PY=$(command -v python3 || command -v python || command -v python2) || {
    echo "install.sh: needs python3 (or python2) to merge settings.json" >&2; exit 1; }

for g in $(echo "$GUARDS" | tr ',' ' '); do
    case "$g" in
        secret-echo|broad-search|protected-read) ;;
        *) echo "install.sh: unknown guard $g" >&2; exit 2 ;;
    esac
done

mkdir -p "$TARGET"
cp "$HERE/hooks/guard.sh" "$HERE/hooks/guard.py" "$HERE/hooks/guards.conf.example" "$TARGET/"
chmod +x "$TARGET/guard.sh"
echo "$VERSION" > "$TARGET/VERSION"
[ -f "$CLAUDE_DIR/guards.conf" ] || cp "$HERE/hooks/guards.conf.example" "$CLAUDE_DIR/guards.conf"

"$PY" - "$CLAUDE_DIR/settings.json" "$CMD_BASE" "$GUARDS" <<'PYEOF'
import json
import os
import sys

path, base, guards = sys.argv[1], sys.argv[2], sys.argv[3].split(",")
settings = {}
if os.path.exists(path):
    with open(path) as f:
        text = f.read()
    settings = json.loads(text) if text.strip() else {}
hooks = settings.setdefault("hooks", {})
pre = hooks.setdefault("PreToolUse", [])
# Drop every earlier claude-guards hook, keep the rest (and drop entries left empty by that).
kept = []
for entry in pre:
    inner = [h for h in entry.get("hooks", []) if "claude-guards/guard.sh" not in h.get("command", "")]
    if inner:
        entry["hooks"] = inner
        kept.append(entry)
    elif not entry.get("hooks"):
        kept.append(entry)
status = {"secret-echo": "Checking secret exposure", "broad-search": "Checking search scope",
          "protected-read": "Checking protected-file access"}


def hook(g):
    return {"type": "command", "command": "%s %s" % (base, g), "timeout": 10, "statusMessage": status[g]}


kept.append({"matcher": "Bash", "hooks": [hook(g) for g in guards]})
if "protected-read" in guards:
    kept.append({"matcher": "Read|Grep|Glob|NotebookRead", "hooks": [hook("protected-read")]})
hooks["PreToolUse"] = kept
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
os.rename(tmp, path)
PYEOF

echo "claude-guards $VERSION installed in $TARGET ($GUARDS)"
echo "settings: $CLAUDE_DIR/settings.json; settings for the guards: $CLAUDE_DIR/guards.conf"
echo "restart the Claude Code session to load the hooks"
