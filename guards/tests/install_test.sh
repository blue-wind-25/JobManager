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

# Installer test: install.sh --repo into a temporary directory twice (other hooks kept, the claude-guards entry
# replaced, not duplicated), then run the installed launcher end to end on one deny and one allow case.
#   sh tests/install_test.sh

set -eu

HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY=$(command -v python3 || command -v python || command -v python2)
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/.claude"
printf '{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "other.sh"}]}]}}\n' \
    > "$T/.claude/settings.json"
sh "$HERE/install.sh" --repo "$T" > /dev/null
sh "$HERE/install.sh" --repo "$T" --version vTEST > /dev/null

"$PY" - "$T/.claude/settings.json" <<'PYEOF'
import json
import sys
s = json.load(open(sys.argv[1]))
pre = s["hooks"]["PreToolUse"]
cmds = [h["command"] for e in pre for h in e["hooks"]]
assert "other.sh" in cmds, cmds
ours = [c for c in cmds if "claude-guards/guard.sh" in c]
assert len(ours) == 4, ours  # three on Bash, protected-read again on Read|Grep|Glob
assert len(set(ours)) == 3, ours
print("settings.json merge: ok")
PYEOF

[ "$(cat "$T/.claude/hooks/claude-guards/VERSION")" = vTEST ]
[ -f "$T/.claude/guards.conf" ]

# End to end through the installed launcher (the payloads are built by Python, never typed as commands).
deny=$("$PY" -c 'import json; print(json.dumps({"tool_name": "Bash", "tool_input": {"command": "printenv " + "GH" + "_TOKEN"}}))' \
    | CLAUDE_PROJECT_DIR="$T" sh "$T/.claude/hooks/claude-guards/guard.sh" secret-echo)
case "$deny" in *'"deny"'*) echo "launcher deny: ok" ;; *) echo "launcher did not deny: $deny"; exit 1 ;; esac
allow=$("$PY" -c 'import json; print(json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}}))' \
    | CLAUDE_PROJECT_DIR="$T" sh "$T/.claude/hooks/claude-guards/guard.sh" secret-echo)
[ -z "$allow" ] && echo "launcher allow: ok"
