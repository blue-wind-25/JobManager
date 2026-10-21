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

# Regression cases for hooks/guard.py. Each line of cases_<guard>.tsv is "D<TAB>command" (must be denied) or
# "A<TAB>command" (must pass); "@Tool<TAB>path" in place of the command is a Read / Grep / Glob / Write / Edit
# call. Three malformed inputs per guard must pass (fail open). Every case is fed to the checker as hook JSON and
# never executed - run this from a file, never with the cases on a command line (the live hook would refuse them).
#   python3 tests/run.py [python]   (default: this interpreter; also run it with python2)

import json
import os
import subprocess
import sys

here = os.path.dirname(os.path.abspath(__file__))
checker = os.path.join(os.path.dirname(here), "hooks", "guard.py")
py = sys.argv[1] if len(sys.argv) > 1 else sys.executable
env = dict(os.environ)
env["HOME"] = "/home/tester"
env["CLAUDE_GUARDS_CONF"] = os.path.join(here, "test.conf")
env.pop("CLAUDE_PROJECT_DIR", None)
env.pop("USERPROFILE", None)

FILES = (("secret-echo", "cases_secret.tsv"), ("broad-search", "cases_search.tsv"),
         ("protected-read", "cases_protected.tsv"))
PATH_KEY = {"Read": "file_path", "Write": "file_path", "Edit": "file_path", "Grep": "path", "Glob": "pattern"}


def verdict(guard, raw):
    p = subprocess.Popen([py, checker, guard], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
    out, _ = p.communicate(raw)
    return "D" if b'"deny"' in out else ("A" if not out.strip() else "?")


def payload(case):
    if case.startswith("@"):
        tool, path = case[1:].split("\t", 1)
        ti = {PATH_KEY[tool]: path}
        if tool == "Grep":
            ti["pattern"] = "x"
        return {"tool_name": tool, "tool_input": ti}
    return {"tool_name": "Bash", "tool_input": {"command": case}}


bad = n = 0
for guard, name in FILES:
    for line in open(os.path.join(here, name)):
        line = line.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        want, case = line.split("\t", 1)
        n += 1
        got = verdict(guard, json.dumps(payload(case)).encode("utf-8"))
        if got != want:
            bad += 1
            print("MISMATCH %s want=%s got=%s: %s" % (guard, want, got, case))
    for raw in (b"", b"not json", b'{"tool_input": null}', b"[1, 2]"):
        n += 1
        if verdict(guard, raw) != "A":
            bad += 1
            print("MISMATCH %s fail-open: %r" % (guard, raw))
    n += 1
    if verdict("no-such-guard", b'{"tool_input": {"command": "env"}}') != "A":
        bad += 1
        print("MISMATCH unknown guard name must allow")
print("%s: %d cases, %d mismatches" % (py, n, bad))
sys.exit(1 if bad else 0)
