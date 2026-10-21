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

# claude-guards: PreToolUse hook checker (design/CLAUDE_GUARDS.md). One checker, three guards:
#
#   guard.py secret-echo      a secret env var's value must never reach the output (sec 3)
#   guard.py broad-search     no find / grep -r / rg / ... rooted at /, /home, /usr, ~
#   guard.py protected-read   the `protected_files` may be written, never read
#
# Reads the hook JSON on stdin and prints a deny decision, or nothing (= allow). Python 2.7 and 3, standard
# library only. Fails OPEN: unparseable input or any exception allows - a guard must never break the session.
# Settings: `.claude/guards.conf` of the project layered over `~/.claude/guards.conf` (CLAUDE_GUARDS_CONF, if set,
# names one more file read last). Limits: a name assembled at run time, or a variable read inside a script FILE
# (the hook sees only the command line), gets through; the hook stops commands, not output.

import json
import os
import re
import sys

try:
    STRING_TYPES = (str, basestring)  # noqa: F821 (Python 2)
except NameError:
    STRING_TYPES = (str,)

# ------------------------------------------------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------------------------------------------------

LIST_KEYS = ("secret_names", "not_secret", "secret_prefixes", "protected_files", "search_roots")
DEFAULT_ROOTS = ["/", "/home", "/usr", "~"]


def read_conf(path, conf):
    try:
        f = open(path)
    except (IOError, OSError):
        return
    with f:
        for line in f:
            line = re.sub(r"(^|\s)#.*$", "", line).strip()
            if "=" not in line:
                continue
            k, v = [x.strip() for x in line.split("=", 1)]
            if not v:
                continue
            if k in LIST_KEYS:
                conf.setdefault(k, []).append(v)
            else:
                conf[k] = v


def load_conf(project_dir):
    conf = {}
    read_conf(os.path.join(os.path.expanduser("~"), ".claude", "guards.conf"), conf)
    if project_dir:
        read_conf(os.path.join(project_dir, ".claude", "guards.conf"), conf)
    extra = os.environ.get("CLAUDE_GUARDS_CONF")
    if extra:
        read_conf(extra, conf)
    for k in LIST_KEYS:
        conf.setdefault(k, [])
    return conf


def deny(msg):
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                  "permissionDecisionReason": msg}}
    sys.stdout.write(json.dumps(out) + "\n")
    sys.exit(0)


# ------------------------------------------------------------------------------------------------------------
# Shared: segments of a command and their command words
# ------------------------------------------------------------------------------------------------------------

SEP = r"(?:^|[\s;&|(`'\"])"
WORD = r"[A-Za-z_]\w*"
WRAPPERS = re.compile(
    r"^(?:[!{'\"\s]+"
    r"|" + WORD + r"=(?:\"[^\"]*\"|'[^']*'|\S*)\s+"
    r"|(?:\S*/)?(?:sudo|command|builtin|exec|nohup|time|eval|stdbuf|unbuffer|xargs)(?:\s+-\S+)*(?:\s+|$)"
    r"|(?:\S*/)?nice(?:\s+-n\s*\S+|\s+-\d+)?\s+"
    r"|(?:\S*/)?timeout(?:\s+-\S+)*\s+\S+\s+"
    r"|(?:\S*/)?(?:ba|da|z|k)?sh(?:\s+-[A-Za-z]+)*\s+-c\s+)")
SEGMENT_SPLIT = re.compile(r"[;&|()\n`{}]")


def strip_wrappers(seg):
    while True:
        m = WRAPPERS.match(seg)
        if not m or not m.group(0):
            return seg.strip().strip("'\"")
        seg = seg[m.end():]


def segments(cmd):
    for seg in SEGMENT_SPLIT.split(cmd):
        seg = strip_wrappers(seg)
        if seg:
            yield seg


# ------------------------------------------------------------------------------------------------------------
# secret-echo (design sec 3)
# ------------------------------------------------------------------------------------------------------------

SECRET_NAME = re.compile(r"(?:^|_)(?:TOKEN|SECRET|PASSPHRASE|PASSWORD|PASSWD|API_?KEY|PRIVATE_KEY|CREDENTIALS?)"
                         r"(?:_|$)", re.I)
VALUE_PATTERNS = [
    r"(?:ghp_|gho_|ghs_|ghu_|github_pat_|glpat-|sk-ant-|xox[abpr]-)[A-Za-z0-9_\-]+",
    r"sk-[A-Za-z0-9_\-]{20,}",
    r"(?:AKIA|ASIA)[A-Z0-9]{16}",
    r"AIza[A-Za-z0-9_\-]{35}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
]
NAME_TOKEN = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z_][A-Za-z0-9_]*")
VAR_CONTEXT = re.compile(r"(?:\$\{?!?|\benviron\W{0,4}|getenv\W{0,4}|\bENV\W{0,4}|process\.env\W{0,4}"
                         r"|\bprintenv\s+(?:-\S+\s+)*|\b(?:declare|typeset)\s+-\w+\s+)$")

LEAK_MSG = ("This command names the secret variable %s where its value could be printed or written out. A secret "
            "may be named only to set it (NAME=value, export NAME=value), unset it (unset NAME, env -u NAME), read "
            "it from a prompt (read -s NAME) or hand the NAME to the tool that reads it (env:NAME, "
            "-storepass:env NAME). To search the repository for the name use the Grep tool; paraphrase it in "
            "commit messages; to authenticate gh use its keyring login (gh auth login). If you have a real reason "
            "to inspect it, ask the owner first.")
VALUE_MSG = ("This command holds what looks like a secret token or key on the command line. Put it in an env "
             "file or a credential helper, not on the command line.")
DUMP_MSG = ("This command can print the whole environment (bare env/printenv/set, export/declare -p, ps e, "
            "jq env, history), which would include every exported secret - even piped into grep/sort/cat. "
            "Name the variable you need instead (printenv PATH, echo \"$JAVA_HOME\").")
ACCESS_MSG = ("This command reads the environment without naming a variable (os.environ, ENVIRON, getenv(), "
              "%ENV, process.env, /proc/*/environ, ${!var}), which can expose every exported secret. Read one "
              "named variable instead (os.environ[\"PATH\"], ENVIRON[\"HOME\"]).")

PRE_ASSIGN = re.compile(SEP + r"$")  # plus post starting with "="
PRE_EXPORT = re.compile(SEP + r"(?:export|readonly)\s+(?:" + WORD + r"(?:=\S*)?\s+)*$")
PRE_UNSET = re.compile(SEP + r"unset\s+(?:-[vf]\s+)?(?:" + WORD + r"\s+)*$")
PRE_ENV_U = re.compile(r"(?:\s-u\s*|\s--unset=)$")
PRE_READ = re.compile(SEP + r"read(?:\s+-[A-Za-z]+(?:\s+(?:\"[^\"]*\"|'[^']*'|\S+))?)*\s+(?:" + WORD + r"\s+)*$")
PRE_TOOL = re.compile(r"(?:\benv:|pass:env\s+)$")
POST_END = re.compile(r"(?:$|[\s;&|)'\"])")

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


def secret_names_in(cmd, conf):
    """(start, end, name) of every word that names a secret: an exact `secret_names` entry anywhere, or a word
    matching the pattern that is written like a variable name (upper case) or used as one ($x, os.environ[x])."""
    listed = set(conf["secret_names"])
    allowed = set(conf["not_secret"])
    for m in NAME_TOKEN.finditer(cmd):
        name = m.group(0)
        if name in allowed:
            continue
        if name in listed:
            yield m.start(), m.end(), name
        elif SECRET_NAME.search(name) and (name.isupper() or VAR_CONTEXT.search(cmd[:m.start()])):
            yield m.start(), m.end(), name


def names_safe(cmd, conf):
    for start, end, name in secret_names_in(cmd, conf):
        pre, post = cmd[:start], cmd[end:]
        if post.startswith("=") and PRE_ASSIGN.search(pre):
            continue
        if POST_END.match(post) and (PRE_EXPORT.search(pre) or PRE_UNSET.search(pre) or PRE_ENV_U.search(pre)
                                     or PRE_READ.search(pre) or PRE_TOOL.search(pre)):
            continue
        return name
    return None


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


def check_secret(data, conf):
    cmd = (data.get("tool_input") or {}).get("command") or ""
    if not cmd:
        return
    vals = VALUE_PATTERNS + [re.escape(p) + r"[A-Za-z0-9_\-+/=.]*" for p in conf["secret_prefixes"]]
    if re.search(r"(?<![A-Za-z0-9])(?:" + "|".join(vals) + ")", cmd):
        deny(VALUE_MSG)
    name = names_safe(cmd, conf)
    if name:
        deny(LEAK_MSG % name)
    for seg in SEGMENT_SPLIT.split(cmd):
        if dumps(seg):
            deny(DUMP_MSG)
    if UNKEYED.search(KEYED.sub("", cmd)):
        deny(ACCESS_MSG)


# ------------------------------------------------------------------------------------------------------------
# broad-search
# ------------------------------------------------------------------------------------------------------------

SEARCH_MSG = ("This search is rooted at %s, which walks a whole file system or home tree: slow, and it can "
              "print other projects' or the system's files. Search the project (the Grep / Glob tools, or a "
              "path inside the project) instead.")
GREP_RECURSIVE = re.compile(r"^-(?:[A-Za-z]*[rR][A-Za-z]*|-recursive|-dereference-recursive|d\s*recurse)$")


def norm_root(p, home):
    p = p.strip("'\"")
    if p in ("~", "$HOME", "${HOME}") or p.startswith(("~/", "$HOME/", "${HOME}/")):
        p = home + p[p.index("/"):] if "/" in p else home
    p = p.rstrip("/") or "/"
    return p


def check_search(data, conf):
    cmd = (data.get("tool_input") or {}).get("command") or ""
    if not cmd:
        return
    cmd = re.sub(r"\$\{(\w+)\}", r"$\1", cmd)  # ${HOME} -> $HOME, so the segment split keeps it whole
    home = os.path.expanduser("~").rstrip("/") or "/"
    roots = set(norm_root(r, home) for r in (conf["search_roots"] or DEFAULT_ROOTS))
    cwd_root = None
    for seg in segments(cmd):
        words = seg.split()
        name = words[0].rsplit("/", 1)[-1]
        args = words[1:]
        if name == "cd":
            target = norm_root(args[0], home) if args else home
            cwd_root = target if target in roots else None
            continue
        if name in ("find", "rg", "ag", "fd", "fdfind", "ack"):
            searching = True
        elif name in ("grep", "egrep", "fgrep"):
            searching = any(GREP_RECURSIVE.match(a) for a in args) or "-d recurse" in seg
        else:
            searching = False
        if not searching:
            continue
        paths = [a for a in args if not a.startswith("-")]
        for a in paths:
            if norm_root(a, home) in roots:
                deny(SEARCH_MSG % a.strip("'\""))
        if cwd_root and (not paths or [a for a in paths if a.strip("'\"") in (".", "./")]
                         or name in ("rg", "ag", "fd", "fdfind", "ack", "grep", "egrep", "fgrep")):
            deny(SEARCH_MSG % (cwd_root + " (via cd)"))


# ------------------------------------------------------------------------------------------------------------
# protected-read
# ------------------------------------------------------------------------------------------------------------

PROTECTED_MSG = ("%s is a protected file: it may be written (>, >>, tee, the Write tool) but never read in this "
                 "session. If you need its content, ask the owner.")
WRITE_PRE = re.compile(r"(?:>>?|\btee(?:\s+-a)?)\s*['\"]?(?:\S*/)?$")
WRITE_CMDS = ("rm", "touch", "chmod", "chown")


def check_protected(data, conf):
    names = conf["protected_files"]
    if not names:
        return
    tool = data.get("tool_name") or ""
    ti = data.get("tool_input") or {}
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return
    if tool and tool != "Bash":
        for key in ("file_path", "path", "pattern", "notebook_path", "glob"):
            v = ti.get(key)
            if isinstance(v, STRING_TYPES):
                for n in names:
                    if os.path.basename(v.rstrip("/")) == n or v.endswith("/" + n) or v == n:
                        deny(PROTECTED_MSG % n)
        return
    cmd = ti.get("command") or ""
    for n in names:
        rx = re.compile(r"(?:^|(?<=[\s'\"/=<>]))" + re.escape(n) + r"(?=$|[\s'\";&|)])")
        for m in rx.finditer(cmd):
            pre = cmd[:m.start()]
            if WRITE_PRE.search(pre):
                continue
            seg = SEGMENT_SPLIT.split(pre)[-1] if pre else ""
            words = strip_wrappers(seg).split()
            if words and words[0].rsplit("/", 1)[-1] in WRITE_CMDS:
                continue
            if len(words) > 1 and words[0] == "git" and words[1] in ("add", "rm"):
                continue
            deny(PROTECTED_MSG % n)


# ------------------------------------------------------------------------------------------------------------

GUARDS = {"secret-echo": check_secret, "broad-search": check_search, "protected-read": check_protected}


def main():
    guard = sys.argv[1] if len(sys.argv) > 1 else ""
    if guard not in GUARDS:
        return
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        return
    if not isinstance(data, dict):
        return
    project = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or ""
    conf = load_conf(project)
    if conf.get("skip_if_user_level") == "yes":
        here = os.path.dirname(os.path.abspath(__file__))
        user_copy = os.path.join(os.path.expanduser("~"), ".claude", "hooks", "claude-guards")
        if os.path.abspath(user_copy) != here and os.path.isfile(os.path.join(user_copy, "guard.py")):
            return
    GUARDS[guard](data, conf)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        pass
