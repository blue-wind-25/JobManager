# claude-guards - reusable safety hooks for Claude Code sessions

Design notes for a small, separate repository of Claude Code `PreToolUse` hooks, installed once per user (every repo
on a workstation) and copied into a repository (cloud sessions, which only see the repository). Started from the
three guards of HowToTrainYourPetAI (`.claude/hooks/`), which today are tied to that project's names and files.
The secret guard is reproduced in full in the appendix as the starting implementation.

Status: design, not built. Nothing here changes any repository or `~/.claude` yet.

---

## 1. Why

- A session's commands and their output stay in the conversation (and its logs). A value printed once is exposed.
- Today only HowToTrainYourPetAI is guarded, and only for its two passphrase variables. Example of the gap: the
  owner's shell exports `GITHUB_TOKEN` (a fine-grained PAT); in every other repo, and even in HowToTrainYourPetAI by
  name, nothing stops `echo $GITHUB_TOKEN` or `printenv GITHUB_TOKEN`.
- Cloud sessions run in ephemeral containers that never see the owner's `~/.claude`, so user-level hooks alone do
  not protect them.

## 2. Guards in scope

| Guard | Today (HowToTrainYourPetAI) | In claude-guards |
|---|---|---|
| **secret-echo** | `block-secret-echo.sh` + `secret_echo_check.py`, two fixed names | generic: names by pattern, values by prefix, per-repo extras (sec 3) |
| **broad-search** | `block-broad-search.sh`: no `find`/`grep -r`/`rg` rooted at `/`, `/home`, `/usr`, `~` | same, roots configurable |
| **protected-file read** | `block-handoff-read.sh`: never read `_handoff.txt` (writes allowed) | generic: `protected_files` list per repo, each read-blocked, write-allowed |

Each guard is one checker (Python, 2.7 and 3, standard library only) behind a tiny `sh` launcher, fails **open** (no
Python, bad input, any exception = allow; a guard must never break a session), and reads its settings from
`.claude/guards.conf` (repo) layered over `~/.claude/guards.conf` (user).

## 3. secret-echo, generalised

The current checker's three rules stay; only *what counts as a secret* widens.

**Secret names** (any of):
- the pattern `(^|_)(TOKEN|SECRET|PASSPHRASE|PASSWORD|PASSWD|API_?KEY|PRIVATE_KEY|CREDENTIALS?)(_|$)`, case-insensitive,
  matched against every `$NAME`, `${NAME}`, `printenv NAME`, `os.environ["NAME"]`, etc. in the command;
- `secret_names = NAME` lines in guards.conf (e.g. HowToTrainYourPetAI keeps `PETAI_RELEASE_SIGNING_PASSPHRASE`
  and `PETAI_DONGLE_SECRET_PASSPHRASE` here - already covered by the pattern, listed for clarity);
- `not_secret = NAME` lines to allow a harmless match (e.g. `MAX_TOKENS`, `TOKENIZERS_PARALLELISM`).

**Secret values** in the command text itself (a token pasted into a command): known prefixes `ghp_`, `gho_`, `ghs_`,
`ghu_`, `github_pat_`, `glpat-`, `sk-ant-`, `sk-` followed by 20+ word chars, `xox[abpr]-`, `AKIA`/`ASIA` + 16
upper-case chars, `AIza` + 35 chars, `-----BEGIN [A-Z ]*PRIVATE KEY-----`; plus `secret_prefixes = PREFIX` lines.
A match is refused ("put it in an env file or a credential helper, not on the command line").

**Rules (unchanged from the appendix code):**
1. A secret NAME may appear only where it cannot print a value: `NAME=...`, `export NAME=...`, `export NAME`,
   `unset NAME`, `env -u NAME ...`, `read [-s] NAME`, `env:NAME`, `-storepass:env NAME`. Anything else is refused.
2. No whole-environment dump anywhere in a pipeline or substitution: bare `env` / `printenv` / `set`,
   `export -p`, `declare -p`, `ps e`, `jq env`, `history`. (`env` with options but no command is a dump too: this is
   what refused a command whose text merely ended in "env -u both".)
3. No unkeyed environment read from an interpreter or `/proc` (`os.environ`, `ENVIRON`, `getenv()`, `%ENV`,
   `process.env`, `/proc/*/environ`, `${!var}`), unless keyed by a literal name that is not a secret name.

**Messages** name the rule and the safe alternative, generically (the current ones name the two PETAI variables):
"set it, unset it, or hand the NAME to the tool that reads it (`env:NAME`); to search for the name use the Grep
tool; to authenticate gh use its keyring login".

**Known limits (kept in the README):** a name assembled at run time, or a variable read inside a script *file*
(the hook sees only the command line), gets through. The hook stops commands, not output: the real defence is not
exporting long-lived tokens in shell profiles (prefer `gh auth login` keyring storage, credential helpers, 0600 env
files passed to the one command that needs them).

## 4. Repository layout

```
hooks/guard.sh                 launcher: picks python3 / python / python2, runs guard.py with the guard name
hooks/guard.py                 one checker, three guards (secret-echo, broad-search, protected-read)
hooks/guards.conf.example      every setting, commented
tests/cases_secret.tsv         D/A<TAB>command cases (the appendix list, extended for patterns and prefixes)
tests/cases_search.tsv
tests/cases_protected.tsv
tests/run.py                   feeds cases as hook JSON (never executes them); python2 and python3
install.sh                     --user | --repo PATH [--guards a,b] [--version TAG]
.github/workflows/ci.yml       tests on Linux / macOS / Windows (git-bash) with python3, plus python2.7 in a container
README.md  LICENSE  CHANGELOG.md
```

## 5. Install

- `install.sh --user`: copies `hooks/` to `~/.claude/hooks/claude-guards/`, merges a `PreToolUse` `Bash` entry per
  guard into `~/.claude/settings.json` (JSON merge with Python: existing hooks kept, an existing claude-guards entry
  replaced), writes `~/.claude/guards.conf` if absent. Applies to every repository on that machine.
- `install.sh --repo PATH`: the same into `PATH/.claude/hooks/claude-guards/` and `PATH/.claude/settings.json`
  (committed with the repo, so cloud sessions get it), and `PATH/.claude/guards.conf` for repo extras
  (`secret_names`, `protected_files`, search roots). A `VERSION` file records the tag.
- Both installed: the user-level and repo-level hooks both run; each refusal is the same, so the double check is
  harmless (or set `skip_if_user_level = yes` in the repo's conf to let the repo copy step aside on the PC).
- Upgrade = run `install.sh` again with a newer tag; `--check` reports the installed and latest versions.
- Settings changes go through the documented `settings.json` hook schema only; restart the Claude Code session
  after installing (hooks are read at start).

## 6. Adoption

- **HowToTrainYourPetAI:** replace `.claude/hooks/block-secret-echo.sh`, `secret_echo_check.py`,
  `test_secret_echo.py`, `secret_echo_cases.txt`, `block-broad-search.sh`, `block-handoff-read.sh` with
  `install.sh --repo .`; `guards.conf`: `secret_names` (the two PETAI passphrases), `protected_files = _handoff.txt`;
  keep CLAUDE.md's prose rules (they explain *why*; the hooks enforce). An RDD records the switch. Run the old and
  new case lists through the new checker first: every old D must stay D, every old A stay A.
- **jm** (JM_DESIGN.md sec 16): `install.sh --repo .` from its first commit.
- **Every other repo:** covered on the PC by `--user`; add `--repo` when a repo is worked on from the cloud.

## 7. Tests

Every case is fed as hook JSON to the checker and never executed (the live hook would refuse typing the D cases on
a command line, so tests run from files). Three malformed inputs must pass (fail open). The secret cases add:
`echo $GITHUB_TOKEN` D, `printenv GH_TOKEN` D, `curl -H "Authorization: token ghp_abc..."` D (value prefix),
`echo $MAX_TOKENS` A with `not_secret = MAX_TOKENS`, `env -u GITHUB_TOKEN gh pr list` A, `git commit -m "rotate
the API token"` A (prose, no NAME match).

---

## Appendix A - the current secret guard (HowToTrainYourPetAI, 2026-10-21), verbatim

The starting implementation. Its copyright headers belong to HowToTrainYourPetAI; the new repository carries its
own. Note the hard-coded `NAME` regex and the PETAI-specific messages, which section 3 replaces.

### A.1 `.claude/settings.json` (hook part)

```json
"hooks": {
  "PreToolUse": [
    {
      "matcher": "Bash",
      "hooks": [
        { "type": "command", "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/block-broad-search.sh",
          "timeout": 10, "statusMessage": "Checking search scope" },
        { "type": "command", "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/block-handoff-read.sh",
          "timeout": 10, "statusMessage": "Checking handoff-file access" },
        { "type": "command", "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/block-secret-echo.sh",
          "timeout": 10, "statusMessage": "Checking secret-passphrase exposure" }
      ]
    }
  ]
}
```

The same file also carries `permissions.deny` rules (`Bash(find / *)`, `Read(./_handoff.txt)`, ...) as a second
layer for the other two guards.

### A.2 `.claude/hooks/block-secret-echo.sh` - the launcher

```sh
#!/bin/sh
#
# Copyright (C) https://github.com/Collaboration-Ambengan/HowToTrainYourPetAI
# All rights reserved.

# PreToolUse (Bash) guard: the release-signing and dongle-secret passphrase env vars must never be
# echoed, printed, dumped, or written to a file via a Bash tool call (owner direction, 2026-09-21).
#
# Neither tools/dist/release_keystore.sh nor tools/dongle/dongle_secret.sh ever needs shell EXPANSION
# of these vars - both hand the bare NAME to openssl/keytool's own `env:NAME` / `-storepass:env NAME`
# mechanism, which reads the environment itself, in-process. So the checker
# (secret_echo_check.py, next to this file) is an allow-list, not a block-list:
#   - a NAME may appear only where it cannot print a value: NAME=... / export NAME=... (the normal
#     way to run the real scripts), export NAME, unset NAME, env -u NAME, read -s NAME, env:NAME,
#     -storepass:env NAME. Any other mention ($NAME, printenv NAME, os.environ["NAME"],
#     ENVIRON["NAME"], perl $ENV{NAME}, a grep, a commit message) is refused;
#   - no whole-environment dump anywhere in a pipeline or substitution (env | grep X, printenv | sort,
#     $(set), export -p, declare -px, ps e, jq env, history) - piping a dump does not narrow it;
#   - no unkeyed environment read (os.environ, ENVIRON, getenv(), %ENV, process.env,
#     /proc/*/environ, ${!var}); a read keyed by a literal other name (os.environ["PATH"]) passes.
#
# Fails OPEN: no Python, unparseable input, or any error in the checker allows the command - a guard
# must never break the session. Python 2.7 or 3 (CentOS 7 has /usr/bin/python 2.7, RDD_106). Cannot
# catch every path (a name assembled at run time, or a variable read inside a script FILE this hook
# never inspects) - same honest limit block-handoff-read.sh states for its own file.

PY=$(command -v python3 || command -v python || command -v python2) || exit 0
[ -n "$PY" ] || exit 0
exec "$PY" "$(dirname "$0")/secret_echo_check.py"
```

### A.3 `.claude/hooks/secret_echo_check.py` - the checker

```python
#
# Copyright (C) https://github.com/Collaboration-Ambengan/HowToTrainYourPetAI
# All rights reserved.

# Checker behind block-secret-echo.sh (PreToolUse, Bash). Reads the hook JSON on stdin and prints a
# deny decision, or nothing (= allow). Python 2.7 and 3 (CentOS 7 has only /usr/bin/python 2.7,
# RDD_106). Any parse error or exception allows: a guard must never break the session.
#
# Three rules, each judged on the command text only:
#   1. Every occurrence of a secret's NAME must sit in a context that cannot print its value:
#      an assignment (NAME=... / export NAME=...), a bare `export NAME`, `unset NAME`, `env -u NAME`,
#      `read [-s] NAME`, or the tools' own `env:NAME` / `-storepass:env NAME`. Anything else - `$NAME`,
#      `printenv NAME`, os.environ["NAME"], ENVIRON["NAME"], a grep, a commit message - is refused.
#   2. No whole-environment dump, wherever it sits in a pipeline or substitution: bare env/printenv/set,
#      export/declare/typeset/readonly with only options, `ps e`, jq's env / $ENV, history.
#   3. No unkeyed environment access from an interpreter or /proc: os.environ / ENVIRON / getenv() /
#      %ENV / process.env / ENV. / /proc/*/environ, unless keyed by a literal name (os.environ["PATH"]).
# Limits: a name built from pieces at run time, or a script FILE (never inspected), still gets through.

import json
import re
import sys

NAME = re.compile(r"PETAI_(?:RELEASE_SIGNING|DONGLE_SECRET)_PASSPHRASE")

LEAK_MSG = ("PETAI_RELEASE_SIGNING_PASSPHRASE and PETAI_DONGLE_SECRET_PASSPHRASE must never be echoed, "
            "printed, or written to output or a file (owner direction, 2026-09-21). A command may name them "
            "only to set them (NAME=value, export NAME=value), unset them, read them from a prompt (read -s NAME) "
            "or hand them to openssl/keytool (env:NAME, -storepass:env NAME). tools/dist/release_keystore.sh and "
            "tools/dongle/dongle_secret.sh read them themselves - run the script by name. To search the repo for "
            "the name use the Grep tool, and paraphrase it in commit messages. If you have a real reason to "
            "inspect one, ask the owner first.")
DUMP_MSG = ("This command can print the whole environment (bare env/printenv/set, export/declare -p, ps e, "
            "jq env, history), which would include PETAI_RELEASE_SIGNING_PASSPHRASE or "
            "PETAI_DONGLE_SECRET_PASSPHRASE if the owner has them exported - even piped into grep/sort/cat. "
            "Name the variable you need instead (printenv PATH, echo \"$JAVA_HOME\").")
ACCESS_MSG = ("This command reads the environment without naming a variable (os.environ, ENVIRON, getenv(), "
              "%ENV, process.env, /proc/*/environ, ${!var}), which can expose PETAI_RELEASE_SIGNING_PASSPHRASE or "
              "PETAI_DONGLE_SECRET_PASSPHRASE. Read one named variable instead (os.environ[\"PATH\"], "
              "ENVIRON[\"HOME\"]).")

# Rule 1: what may come right before a NAME (pre) for the occurrence to be harmless.
SEP = r"(?:^|[\s;&|(`'\"])"
WORD = r"[A-Za-z_]\w*"
PRE_ASSIGN = re.compile(SEP + r"$")  # plus post starting with "="
PRE_EXPORT = re.compile(SEP + r"(?:export|readonly)\s+(?:" + WORD + r"(?:=\S*)?\s+)*$")
PRE_UNSET = re.compile(SEP + r"unset\s+(?:-[vf]\s+)?(?:" + WORD + r"\s+)*$")
PRE_ENV_U = re.compile(r"(?:\s-u\s*|\s--unset=)$")
PRE_READ = re.compile(SEP + r"read(?:\s+-[A-Za-z]+(?:\s+(?:\"[^\"]*\"|'[^']*'|\S+))?)*\s+(?:" + WORD + r"\s+)*$")
PRE_TOOL = re.compile(r"(?:\benv:|pass:env\s+)$")
POST_END = re.compile(r"(?:$|[\s;&|)'\"])")

# Rule 3: keyed (named) environment reads, removed before looking for unkeyed ones.
LIT = r"\s*['\"]" + WORD + r"['\"]\s*"
KEYED = re.compile(
    r"os\.environ\s*\[" + LIT + r"\]"
    r"|os\.environ\.get\(" + LIT +
    r"|os\.getenv\(" + LIT +
    r"|ENVIRON\s*\[" + LIT + r"\]"
    r"|getenv\(" + LIT +
    r"|\$ENV\s*\{\s*['\"]?" + WORD + r"['\"]?\s*\}"
    r"|process\.env\." + WORD +
    r"|process\.env\s*\[" + LIT + r"\]"
    r"|\bENV\s*\[" + LIT + r"\]"
    r"|\bENV\.fetch\(" + LIT)
UNKEYED = re.compile(r"environ|ENVIRON|getenv|%ENV\b|\$ENV\b|\bENV\s*[.\[{]|process\.env"
                     r"|\$\{!" + WORD + r"[*@]?\}|_history\b")

# Rule 2: wrappers stripped off a segment's front before its command word is judged.
WRAPPERS = re.compile(
    r"^(?:[!{'\"\s]+"
    r"|" + WORD + r"=(?:\"[^\"]*\"|'[^']*'|\S*)\s+"
    r"|(?:\S*/)?(?:sudo|command|builtin|exec|nohup|time|eval|stdbuf|unbuffer|xargs)(?:\s+-\S+)*(?:\s+|$)"
    r"|(?:\S*/)?nice(?:\s+-n\s*\S+|\s+-\d+)?\s+"
    r"|(?:\S*/)?timeout(?:\s+-\S+)*\s+\S+\s+"
    r"|(?:\S*/)?(?:ba|da|z|k)?sh(?:\s+-[A-Za-z]+)*\s+-c\s+)")
SEGMENT_SPLIT = re.compile(r"[;&|()\n`{}]")


def deny(msg):
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                  "permissionDecisionReason": msg}}
    sys.stdout.write(json.dumps(out) + "\n")
    sys.exit(0)


def names_safe(cmd):
    for m in NAME.finditer(cmd):
        pre, post = cmd[:m.start()], cmd[m.end():]
        if post.startswith("=") and PRE_ASSIGN.search(pre):
            continue
        if POST_END.match(post) and (PRE_EXPORT.search(pre) or PRE_UNSET.search(pre) or PRE_ENV_U.search(pre)
                                     or PRE_READ.search(pre) or PRE_TOOL.search(pre)):
            continue
        return False
    return True


def strip_wrappers(seg):
    while True:
        m = WRAPPERS.match(seg)
        if not m or not m.group(0):
            return seg.strip().strip("'\"")
        seg = seg[m.end():]


def env_command(args):
    """What `env args` runs, or None when it only prints the environment."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-u", "--unset", "-C", "--chdir"):
            i += 2
        elif (re.match(r"^-[0iv]+$", a) or a in ("-", "--", "--null", "--ignore-environment", "--debug", "-S",
                                                    "--split-string")
              or re.match(r"^--(?:unset|chdir|split-string)=", a) or re.match(r"^" + WORD + r"=", a)):
            i += 1
        else:
            return " ".join(args[i:])
    return None


def dumps(seg, depth=0):
    seg = strip_wrappers(seg)
    words = seg.split()
    if not words or depth > 8:
        return False
    cmd = words[0].strip("'\"").rsplit("/", 1)[-1]
    args = [w.strip("'\"") for w in words[1:]]
    if cmd == "env":
        rest = env_command(args)
        return rest is None or dumps(rest, depth + 1)
    if cmd == "printenv":
        return not [a for a in args if not a.startswith("-")]
    if cmd == "set":
        return not args
    if cmd in ("export", "declare", "typeset", "readonly", "local"):
        return all(a.startswith(("-", "+")) for a in args)
    if cmd == "history":
        return True
    if cmd == "ps":
        skip = False
        for a in args:
            if skip:
                skip = False
            elif a.startswith("-"):
                skip = a[-1] in "CpuUgGoOtqsk" or a in ("--pid", "--ppid", "--sort", "--format", "--user",
                                                         "--group", "--tty", "--cols", "--columns", "--width")
            elif re.match(r"^[A-Za-z]*e[A-Za-z]*$", a):
                return True
        return False
    if cmd == "jq":
        return re.search(r"\benv\b|\$ENV\b", seg) is not None
    return False


def main():
    try:
        raw = sys.stdin.read()
        cmd = (json.loads(raw).get("tool_input") or {}).get("command") or ""
    except Exception:
        return
    if not cmd:
        return
    if not names_safe(cmd):
        deny(LEAK_MSG)
    for seg in SEGMENT_SPLIT.split(cmd):
        if dumps(seg):
            deny(DUMP_MSG)
    if UNKEYED.search(KEYED.sub("", cmd)):
        deny(ACCESS_MSG)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        pass
```

### A.4 `.claude/hooks/test_secret_echo.py` - the test runner

```python
#
# Copyright (C) https://github.com/Collaboration-Ambengan/HowToTrainYourPetAI
# All rights reserved.

# Regression cases for secret_echo_check.py (block-secret-echo.sh). Each line of secret_echo_cases.txt is
# "D<TAB>command" (must be denied) or "A<TAB>command" (must pass); three malformed inputs must pass too
# (fail open). Feeds each case to the checker as hook JSON - nothing in the cases is ever executed.
#   python3 .claude/hooks/test_secret_echo.py [python]   (default: this interpreter; also run with python2)
# Run it from a file like this, never with the cases on a Bash command line: the live hook would refuse them.

import json
import os
import subprocess
import sys

here = os.path.dirname(os.path.abspath(__file__))
checker = os.path.join(here, "secret_echo_check.py")
py = sys.argv[1] if len(sys.argv) > 1 else sys.executable


def verdict(raw):
    p = subprocess.Popen([py, checker], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    out, _ = p.communicate(raw)
    return "D" if b'"deny"' in out else ("A" if not out.strip() else "?")


bad = n = 0
for line in open(os.path.join(here, "secret_echo_cases.txt")):
    line = line.rstrip("\n")
    if not line or line.startswith("#"):
        continue
    want, cmd = line.split("\t", 1)
    n += 1
    got = verdict(json.dumps({"tool_input": {"command": cmd}}).encode("utf-8"))
    if got != want:
        bad += 1
        print("MISMATCH want=%s got=%s: %s" % (want, got, cmd))
for raw in (b"", b"not json", b'{"tool_input": null}'):
    n += 1
    if verdict(raw) != "A":
        bad += 1
        print("MISMATCH fail-open: %r" % raw)
print("%s: %d cases, %d mismatches" % (py, n, bad))
sys.exit(1 if bad else 0)
```

### A.5 `.claude/hooks/secret_echo_cases.txt` - the cases (D = must be denied, A = must pass; TAB-separated)

```text
D	echo $PETAI_DONGLE_SECRET_PASSPHRASE
D	echo "${PETAI_RELEASE_SIGNING_PASSPHRASE}"
D	printenv PETAI_DONGLE_SECRET_PASSPHRASE
D	python3 -c 'import os; print(os.environ["PETAI_DONGLE_SECRET_PASSPHRASE"])'
D	awk 'BEGIN{print ENVIRON["PETAI_RELEASE_SIGNING_PASSPHRASE"]}'
D	perl -e 'print $ENV{PETAI_DONGLE_SECRET_PASSPHRASE}'
D	x=PETAI_DONGLE_SECRET_PASSPHRASE; echo ${!x}
D	grep PETAI_DONGLE_SECRET_PASSPHRASE ~/.bashrc
D	declare -p PETAI_DONGLE_SECRET_PASSPHRASE
D	env | grep PASS
D	env | cat
D	printenv | sort
D	cd /tmp && env
D	echo "$(set)" | wc -c
D	export -p
D	declare -px
D	/usr/bin/env -0
D	FOO=1 env
D	env -u PATH
D	sh -c 'env'
D	ps eww 1234
D	ps auxe
D	jq -n env
D	jq -n '$ENV'
D	cat /proc/self/environ
D	tr '\0' '\n' < /proc/$PPID/environ
D	python3 -c 'import os; print(os.environ)'
D	python3 -c 'import os; print(dict(os.environ))'
D	awk 'BEGIN{for (k in ENVIRON) print k, ENVIRON[k]}'
D	perl -e 'print "$_=$ENV{$_}\n" for keys %ENV'
D	node -p process.env
D	java Foo; System.getenv()
D	for v in ${!PETAI@}; do :; done
D	history | tail
D	cat ~/.bash_history
D	set
A	export PETAI_DONGLE_SECRET_PASSPHRASE=hunter2
A	PETAI_RELEASE_SIGNING_PASSPHRASE='x y' sh tools/dist/release_keystore.sh
A	PETAI_DONGLE_SECRET_PASSPHRASE=abc PETAI_RELEASE_SIGNING_PASSPHRASE=def make release
A	unset PETAI_DONGLE_SECRET_PASSPHRASE
A	unset -v PETAI_DONGLE_SECRET_PASSPHRASE PETAI_RELEASE_SIGNING_PASSPHRASE
A	env -u PETAI_DONGLE_SECRET_PASSPHRASE make test
A	read -rsp "Passphrase: " PETAI_DONGLE_SECRET_PASSPHRASE && export PETAI_DONGLE_SECRET_PASSPHRASE
A	openssl enc -aes-256-cbc -pass env:PETAI_DONGLE_SECRET_PASSPHRASE -in a -out b
A	keytool -list -keystore k.jks -storepass:env PETAI_RELEASE_SIGNING_PASSPHRASE
A	printenv PATH
D	env | grep JAVA
A	env JAVA_HOME=/opt/j make bench
A	env -u GITHUB_TOKEN gh pr list
A	echo "$JAVA_HOME"
A	python3 -c 'import os; print(os.environ["HOME"])'
A	python3 -c 'import os; print(os.environ.get("PATH", ""))'
A	awk 'BEGIN{print ENVIRON["HOME"]}'
A	ps -ef | grep pettrain
A	ps -p 1234 -o pid,etime,args
A	ps -C pettrain -o pid=
A	ps aux | head
A	set -e; make vision-test
A	set -o pipefail
A	export FOO=1 BAR=2
A	declare -f myfunc
A	cd /home/aloysius/Projects/HowToTrainYourPetAI && git status --short
A	jq -r '.tool_input.command' f.json
A	sh -c 'make bench && java -jar build/bench.jar eval real-photos'
A	git log --oneline -5
A	ssh pi@cm5 'sh tools/cm5/cm5_distill_gen5_run.sh status'
A	for f in a b; do echo "$f"; done
A	awk '{print $1}' file | sort | uniq -c
A	export PATH="$JAVA_HOME/bin:$PATH"
```
