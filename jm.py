#!/usr/bin/env python3
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

"""jm - a single-machine job manager (queue, scheduler, monitor) for long-running jobs.

One file, Python 3.6+ standard library only; every state and config file is plain text. The CLI and the
daemon share a state dir (default ~/.jm, or JM_HOME / --home) under one lock; there is no socket. Each job
runs under a detached wrapper (`jm _run`), so jobs keep running while the daemon is stopped or restarted.
See design/JM_DESIGN.md in the repository for the full design.
"""

import argparse
import calendar
import codecs
import collections
import os
import queue
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback

VERSION = "1.0.0.dev0"
IS_WIN = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")
JM_PATH = os.path.abspath(__file__)

EXIT_OK, EXIT_ERR, EXIT_USAGE, EXIT_NOJOB, EXIT_STATE = 0, 1, 2, 3, 4

# Job states (design sec 7). TERMINAL states leave the queue; the others stay in it.
QUEUED, WAITING, RUNNING, STOPPING = "queued", "waiting", "running", "stopping"
DONE, FAILED, HELD, STOPPED, CANCELLED = "done", "failed", "held", "stopped", "cancelled"
STATES = (QUEUED, WAITING, RUNNING, STOPPING, DONE, FAILED, HELD, STOPPED, CANCELLED)
TERMINAL = (DONE, FAILED, CANCELLED)
PENDING = (QUEUED, WAITING)
ENDED = TERMINAL + (STOPPED,)  # what `jm wait` waits for

if IS_WIN:
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_NO_WINDOW = 0x08000000
    BELOW_NORMAL_PRIORITY_CLASS = 0x00004000


class JmError(Exception):
    def __init__(self, msg, code=EXIT_ERR):
        Exception.__init__(self, msg)
        self.code = code


# ---------------------------------------------------------------------------------------------------------------
# Small helpers: time, durations, files
# ---------------------------------------------------------------------------------------------------------------

def utc(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if t is None else t))


def parse_utc(s):
    try:
        return float(calendar.timegm(time.strptime(s.strip(), "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, AttributeError):
        return None


DUR_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
DUR_RE = re.compile(r"^(?:\s*\d+(?:\.\d+)?\s*[smhdw]?\s*)+$")


def parse_duration(s):
    """'90', '5s', '0.2s', '30m', '12h', '7d', '1h30m' -> seconds (float)."""
    s = (s or "").strip().lower()
    if not s or not DUR_RE.match(s):
        raise ValueError("not a duration: %r (use e.g. 90, 5s, 30m, 12h, 7d, 1h30m)" % s)
    return float(sum(float(n) * DUR_UNITS[u] for n, u in re.findall(r"(\d+(?:\.\d+)?)\s*([smhdw]?)", s)))


def fmt_duration(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return "%ds" % sec
    if sec < 3600:
        return "%dm%02ds" % (sec // 60, sec % 60)
    if sec < 86400:
        return "%dh%02dm" % (sec // 3600, sec % 3600 // 60)
    return "%dd%02dh" % (sec // 86400, sec % 86400 // 3600)


def fmt_num(x):
    return ("%d" % x) if float(x).is_integer() else ("%g" % x)


def read_text(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except (IOError, OSError):
        return default


def write_atomic(path, text):
    """Write `text` to `path` via a temp file, fsync and rename: readers see the old or the new file only."""
    tmp = "%s.tmp%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    for i in range(100):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # Windows: the target is open in another process for a moment.
            if not IS_WIN or i == 99:
                raise
            time.sleep(0.02)


def append_line(path, line):
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(line.rstrip("\n") + "\n")


def remove_file(path):
    try:
        os.remove(path)
    except OSError:
        pass


def read_kv_file(path):
    """`key = value` lines (no comments, no lists): the small machine-written files (info, stop_req)."""
    out = {}
    for line in read_text(path).splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def write_kv_file(path, d):
    write_atomic(path, "".join("%s = %s\n" % (k, d[k]) for k in sorted(d) if d[k] is not None and d[k] != ""))


def shell_join(argv):
    if IS_WIN:
        return subprocess.list2cmdline(argv)
    return " ".join(shlex.quote(a) for a in argv)


def default_home():
    return os.environ.get("JM_HOME") or os.path.join(os.path.expanduser("~"), ".jm")


# ---------------------------------------------------------------------------------------------------------------
# Locking: flock on Unix, msvcrt.locking on Windows
# ---------------------------------------------------------------------------------------------------------------

if IS_WIN:
    import msvcrt
else:
    import fcntl


class FileLock(object):
    def __init__(self, path):
        self.path = path
        self.fd = None

    def acquire(self, blocking=True, timeout=None):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = None if timeout is None else time.time() + timeout
        while True:
            try:
                if IS_WIN:
                    os.lseek(fd, 0, 0)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | (0 if (blocking and timeout is None) else fcntl.LOCK_NB))
                self.fd = fd
                return True
            except (IOError, OSError):
                if not blocking or (deadline is not None and time.time() >= deadline):
                    os.close(fd)
                    return False
                time.sleep(0.05)

    def release(self):
        if self.fd is None:
            return
        try:
            if IS_WIN:
                os.lseek(self.fd, 0, 0)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
        except (IOError, OSError):
            pass
        os.close(self.fd)
        self.fd = None

    def __enter__(self):
        if not self.acquire(timeout=120):
            raise JmError("could not take the state lock %s within 120 s" % self.path)
        return self

    def __exit__(self, *exc):
        self.release()


# ---------------------------------------------------------------------------------------------------------------
# Process identity and information (design sec 3, 10)
# ---------------------------------------------------------------------------------------------------------------

_CMDLINE_OK = {}  # (pid, start) -> bool: a (pid, start time) pair names one process, so the answer never changes


def _run_out(argv, timeout=20):
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             universal_newlines=True)
        out, _ = p.communicate(timeout=timeout)
        return out if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        try:
            p.kill()
        except Exception:
            pass
        return None


def _win_handle_times(pid):
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        code = ctypes.c_ulong()
        if k32.GetExitCodeProcess(h, ctypes.byref(code)) and code.value != 259:  # 259 = STILL_ACTIVE
            return None
        ct, et, kt, ut = (ctypes.c_ulonglong() for _ in range(4))
        if not k32.GetProcessTimes(h, ctypes.byref(ct), ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut)):
            return None
        return str(ct.value)
    finally:
        k32.CloseHandle(h)


def proc_start_time(pid):
    """An opaque string that changes when `pid` is reused, or None when no such (live) process exists."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if IS_WIN:
        return _win_handle_times(pid)
    if os.path.isdir("/proc/%d" % pid) or IS_LINUX:
        stat = read_text("/proc/%d/stat" % pid, None)
        if stat is None:
            return None
        fields = stat[stat.rfind(")") + 2:].split()
        if len(fields) < 20 or fields[0] == "Z":
            return None
        return fields[19]  # field 22: start time in clock ticks since boot
    out = _run_out(["ps", "-o", "lstart=", "-p", str(pid)])
    if not out or not out.strip():
        return None
    stat = _run_out(["ps", "-o", "stat=", "-p", str(pid)]) or ""
    if stat.strip().startswith("Z"):
        return None
    return "-".join(out.split())


def proc_cmdline(pid):
    if IS_WIN:
        out = _run_out(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        "(Get-CimInstance Win32_Process -Filter 'ProcessId=%d').CommandLine" % int(pid)], 60)
        return out.strip() if out else None
    if os.path.isdir("/proc/%d" % int(pid)):
        raw = read_text("/proc/%d/cmdline" % int(pid), None)
        return None if raw is None else raw.replace("\0", " ")
    out = _run_out(["ps", "-ww", "-o", "command=", "-p", str(pid)])
    return out.strip() if out else None


def proc_alive(pid, start, token=None):
    """Alive = the pid exists, its start time matches, and (when given) its command line holds the token."""
    st = proc_start_time(pid)
    if st is None or st != start:
        return False
    if not token:
        return True
    key = (str(pid), start, token)
    if key not in _CMDLINE_OK:
        cmd = proc_cmdline(pid)
        if cmd is None:
            return False
        _CMDLINE_OK[key] = token in cmd
    return _CMDLINE_OK[key]


def kill_group(pid, hard=False):
    """Signal a job's process group (Unix) or process tree (Windows). Returns False when it is gone."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if IS_WIN:
        argv = ["taskkill", "/T", "/PID", str(pid)] + (["/F"] if hard else [])
        try:
            return subprocess.call(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
        except OSError:
            return False
    try:
        os.killpg(pid, signal.SIGKILL if hard else signal.SIGTERM)
        return True
    except OSError:
        return False


def group_exists(pgid):
    try:
        os.killpg(int(pgid), 0)
        return True
    except OSError:
        return False


def group_usage(pid):
    """(rss MB, cpu %) of a job's process group, or None. cpu % is the lifetime average of each process."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if IS_WIN:
        out = _run_out(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"])
        m = re.search(r'"([\d,.\s]+) K"', out or "")
        return (int(re.sub(r"\D", "", m.group(1))) / 1024.0, None) if m else None
    if os.path.isdir("/proc/%d" % pid) or IS_LINUX:
        try:
            ticks = os.sysconf("SC_CLK_TCK")
            page = os.sysconf("SC_PAGE_SIZE")
            uptime = float(read_text("/proc/uptime", "0").split()[0])
        except (ValueError, OSError, IndexError):
            return None
        rss = cpu = 0.0
        found = False
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            stat = read_text("/proc/%s/stat" % d, None)
            if not stat:
                continue
            f = stat[stat.rfind(")") + 2:].split()
            if len(f) < 22 or int(f[2]) != pid:  # f[2] = pgrp
                continue
            found = True
            rss += int(f[21]) * page / 1048576.0
            elapsed = uptime - int(f[19]) / float(ticks)
            if elapsed > 0:
                cpu += (int(f[11]) + int(f[12])) / float(ticks) / elapsed * 100.0
        return (rss, cpu) if found else None
    out = _run_out(["ps", "-A", "-o", "pgid=,rss=,%cpu="])
    if not out:
        return None
    rss = cpu = 0.0
    found = False
    for line in out.splitlines():
        f = line.split()
        if len(f) == 3 and f[0] == str(pid):
            found = True
            rss += float(f[1]) / 1024.0
            cpu += float(f[2].replace(",", "."))
    return (rss, cpu) if found else None


def total_ram_mb():
    if IS_WIN:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(st)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return st.ullTotalPhys // 1048576
        return 1024
    m = re.search(r"MemTotal:\s+(\d+)", read_text("/proc/meminfo"))
    if m:
        return int(m.group(1)) // 1024
    out = _run_out(["sysctl", "-n", "hw.memsize"])
    if out and out.strip().isdigit():
        return int(out.strip()) // 1048576
    return 1024


def disk_free_mb(path):
    """Free MB on `path`. JM_TEST_DISK_FREE='PATH=MB;PATH=MB' overrides it for tests (longest prefix wins)."""
    fake = os.environ.get("JM_TEST_DISK_FREE")
    if fake:
        best = None
        for part in fake.split(";"):
            if "=" in part:
                p, mb = part.rsplit("=", 1)
                if os.path.abspath(path).startswith(os.path.abspath(p)) and (best is None or len(p) > len(best[0])):
                    best = (p, float(mb))
        if best:
            return best[1]
    try:
        return shutil.disk_usage(path).free / 1048576.0
    except OSError:
        return 0.0


def spawn_detached(argv, env=None, cwd=None, stdout=None, shell=False):
    kw = dict(stdin=subprocess.DEVNULL, stdout=stdout or subprocess.DEVNULL,
              stderr=subprocess.STDOUT if stdout else subprocess.DEVNULL, env=env, cwd=cwd, shell=shell)
    if IS_WIN:
        kw["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
        kw["close_fds"] = True
    return subprocess.Popen(argv, **kw)


SPAWNED = []  # detached children of this process (wrappers, notify hooks), reaped by the daemon loop


def reap_spawned():
    SPAWNED[:] = [p for p in SPAWNED if p.poll() is None]


# ---------------------------------------------------------------------------------------------------------------
# Secrets (design sec 16; the same rule set as the shell guard, design/CLAUDE_GUARDS.md sec 3)
# ---------------------------------------------------------------------------------------------------------------

SECRET_NAME_RE = re.compile(r"(?:^|_)(?:TOKEN|SECRET|PASSPHRASE|PASSWORD|PASSWD|API_?KEY|PRIVATE_KEY|CREDENTIALS?)"
                            r"(?:_|$)", re.I)
SECRET_VALUE_PATTERNS = [
    r"(?:ghp_|gho_|ghs_|ghu_|github_pat_|glpat-|sk-ant-|xox[abpr]-)[A-Za-z0-9_\-]+",
    r"sk-[A-Za-z0-9_\-]{20,}",
    r"(?:AKIA|ASIA)[A-Z0-9]{16}",
    r"AIza[A-Za-z0-9_\-]{35}",
]
PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:.*?-----END [A-Z ]*PRIVATE KEY-----|.*\Z)", re.S)
SAFE_WAYS = ("pass it with `env_pass = NAME` (taken from the daemon's environment at start) or "
             "`env_file = PATH` (KEY=VALUE lines, mode 0600)")


def is_secret_name(name, cfg=None):
    if cfg is not None:
        if name in cfg["not_secret"]:
            return False
        if name in cfg["secret_names"]:
            return True
    return bool(SECRET_NAME_RE.search(name))


def secret_value_re(cfg=None):
    pats = list(SECRET_VALUE_PATTERNS)
    if cfg is not None:
        pats += [re.escape(p) + r"[A-Za-z0-9_\-+/=.]*" for p in cfg["secret_prefixes"]]
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(pats) + ")")


def has_secret_value(text, cfg=None):
    return bool(secret_value_re(cfg).search(text) or PRIVATE_KEY_RE.search(text))


def mask_secrets(text, cfg=None):
    rx = secret_value_re(cfg)

    def repl(m):
        s = m.group(0)
        for p in ("github_pat_", "ghp_", "gho_", "ghs_", "ghu_", "glpat-", "sk-ant-", "AKIA", "ASIA", "AIza"):
            if s.startswith(p):
                return p + "***"
        if re.match(r"xox[abpr]-", s):
            return s[:5] + "***"
        if s.startswith("sk-"):
            return "sk-***"
        for p in (cfg["secret_prefixes"] if cfg else []):
            if s.startswith(p):
                return p + "***"
        return "***"
    text = PRIVATE_KEY_RE.sub("-----BEGIN PRIVATE KEY-----***", text)
    return rx.sub(repl, text)


# ---------------------------------------------------------------------------------------------------------------
# Plain-text `key = value` files: specs and config (design sec 5, 6)
# ---------------------------------------------------------------------------------------------------------------

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TOKEN_RE = re.compile(r"^([A-Za-z0-9_.\-]+)(?::(\d+))?$")

# key -> (kind, default). Kinds: str, cmd (a shell command), list, env (list of KEY=VALUE), tokens, num, int,
# bool, dur (duration or empty), disk ("MB [@ path]"), enum:a|b|c.
SPEC_KEYS = [
    ("id", "str", None), ("name", "str", None), ("cwd", "str", None), ("note", "str", None),
    ("env", "env", None), ("env_pass", "list", None), ("env_file", "list", None),
    ("start", "cmd", None), ("resume", "cmd", None), ("stop", "cmd", None), ("stop_grace", "dur", 60.0),
    ("done_check", "cmd", None), ("progress", "cmd", None), ("applog", "cmd", None), ("split_stderr", "bool", False),
    ("pre", "cmd", None), ("post", "cmd", None), ("notify", "cmd", None),
    ("cpu", "num", 1.0), ("ram", "num", 0.0), ("disk", "disk", None), ("tokens", "tokens", None),
    ("after", "list", None), ("after_ok", "bool", True), ("priority", "int", 0), ("retries", "int", 0),
    ("timeout", "dur", None), ("on_boot", "enum:resume|hold|fail", "resume"), ("drain", "bool", False),
    ("children", "list", None), ("parallel", "int", 0), ("continue_on_fail", "bool", False),
    ("artifacts", "list", None), ("nice", "int", 5), ("log_max", "num", 0.0), ("keep", "dur", None),
    ("estimate", "dur", None), ("tags", "list", None),
]
CONFIG_KEYS = [
    ("cpus", "num", None), ("ram", "num", None), ("ram_reserve", "num", 0.0), ("tokens", "tokens", None),
    ("backfill", "bool", True), ("drain_after", "dur", 1800.0), ("poll", "dur", 5.0),
    ("progress_every", "dur", 60.0), ("keep", "dur", 7 * 86400.0), ("env", "env", None),
    ("secret_names", "list", None), ("not_secret", "list", None), ("secret_prefixes", "list", None),
    ("on_first_start", "cmd", None), ("on_idle", "cmd", None), ("hook_timeout", "dur", 120.0),
    ("idle_after", "dur", 60.0),
]
LIST_KINDS = ("list", "env", "tokens")
CMD_KEYS_SPEC = [k for k, kind, _ in SPEC_KEYS if kind == "cmd"]


def strip_comment(line):
    """`#` starts a comment at the line start or after white space; `\\#` is a literal `#`."""
    out = []
    i = 0
    while i < len(line):
        c = line[i]
        if c == "\\" and line[i + 1:i + 2] == "#":
            out.append("#")
            i += 2
            continue
        if c == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(c)
        i += 1
    return "".join(out)


def kv_entries(text):
    """Logical `key = value` entries: [(first physical line index, last index, key, value)]."""
    lines = text.splitlines()
    entries = []
    i = 0
    while i < len(lines):
        first = i
        buf = ""
        while True:
            part = strip_comment(lines[i].rstrip("\r")).rstrip()
            if part.endswith("\\") and i + 1 < len(lines):
                buf += part[:-1]
                i += 1
                continue
            buf += part.rstrip("\\")
            break
        if buf.strip():
            if "=" not in buf:
                raise JmError("line %d: expected `key = value`, got %r" % (first + 1, buf.strip()))
            k, v = buf.split("=", 1)
            entries.append((first, i, k.strip(), v.strip()))
        i += 1
    return entries


def convert(kind, value, key):
    if kind in ("str", "cmd", "list"):
        return value
    if kind == "env":
        if "=" not in value or not ENV_KEY_RE.match(value.split("=", 1)[0].strip()):
            raise ValueError("expected NAME=VALUE")
        k, v = value.split("=", 1)
        return (k.strip(), v.strip())
    if kind == "tokens":
        m = TOKEN_RE.match(value)
        if not m:
            raise ValueError("expected name or name:count")
        n = int(m.group(2) or 1)
        if n < 1:
            raise ValueError("count must be >= 1")
        return (m.group(1), n)
    if kind == "num":
        x = float(value)
        if x < 0:
            raise ValueError("must be >= 0")
        return x
    if kind == "int":
        return int(value)
    if kind == "bool":
        v = value.lower()
        if v in ("yes", "true", "on", "1"):
            return True
        if v in ("no", "false", "off", "0"):
            return False
        raise ValueError("expected yes or no")
    if kind == "dur":
        return parse_duration(value)
    if kind == "disk":
        m = re.match(r"^(\d+(?:\.\d+)?)\s*(?:@\s*(.+))?$", value)
        if not m:
            raise ValueError("expected `MB` or `MB @ path`")
        return (float(m.group(1)), (m.group(2) or "").strip() or None)
    if kind.startswith("enum:"):
        allowed = kind[5:].split("|")
        if value not in allowed:
            raise ValueError("expected one of " + ", ".join(allowed))
        return value
    raise AssertionError(kind)


def parse_kv(text, table, what):
    kinds = dict((k, (kind, d)) for k, kind, d in table)
    out = {}
    seen = {}
    for first, _last, key, value in kv_entries(text):
        if key not in kinds:
            raise JmError("%s line %d: unknown key %r" % (what, first + 1, key))
        kind = kinds[key][0]
        if value == "":
            continue  # `key =` means: the default
        try:
            v = convert(kind, value, key)
        except ValueError as e:
            raise JmError("%s line %d: %s: %s" % (what, first + 1, key, e))
        if kind in LIST_KINDS:
            out.setdefault(key, []).append(v)
        else:
            if key in seen:
                raise JmError("%s line %d: %s given twice (first on line %d)" % (what, first + 1, key, seen[key]))
            seen[key] = first + 1
            out[key] = v
    for key, kind, default in table:
        if key not in out:
            out[key] = [] if kind in LIST_KINDS else default
    return out


def escape_value(v):
    return re.sub(r"(^|(?<=\s))#", r"\\#", v)


def edit_kv_text(text, key, values, append=False):
    """Return `text` with `key`'s lines replaced by one `key = value` line per value (none: key removed).
    Comments and the other lines are kept; the new lines go where the key first appeared, else at the end."""
    lines = text.splitlines()
    spans = [(a, b) for a, b, k, _v in kv_entries(text) if k == key]
    new = ["%s = %s" % (key, escape_value(v)) for v in values]
    if append and spans:
        at = spans[-1][1] + 1
        return "\n".join(lines[:at] + new + lines[at:]) + "\n"
    at = spans[0][0] if spans else len(lines)
    drop = set()
    for a, b in spans:
        drop.update(range(a, b + 1))
    out = []
    for i, line in enumerate(lines):
        if i == at:
            out.extend(new)
        if i not in drop:
            out.append(line)
    if at >= len(lines):
        out.extend(new)
    return "\n".join(out) + "\n"


def parse_spec(text, what="spec"):
    return parse_kv(text, SPEC_KEYS, what)


DEFAULT_CONFIG = """# jm machine config (design/JM_DESIGN.md sec 6). `key = value`; a repeated key adds to a list.
# cpus         = 4           # default: all cores
# ram          = 4000        # MB; default: total RAM
# ram_reserve  = 500         # MB never handed out
# tokens       = gpu:1       # named exclusive resources (name:count)
# backfill     = yes
# drain_after  = 30m
# poll         = 5s
# env          = DATA=/mnt/data   # passed to every job
# on_first_start = data-rw.sh    # any command (example name): the machine goes from idle to busy
# on_idle        = data-ro.sh    # any command (example name): nothing has run for idle_after
# idle_after     = 60s
# hook_timeout   = 120s
"""


def parse_config(text):
    cfg = parse_kv(text, CONFIG_KEYS, "config")
    if cfg["cpus"] is None:
        cfg["cpus"] = float(os.cpu_count() or 1)
    if cfg["ram"] is None:
        cfg["ram"] = float(total_ram_mb())
    cfg["token_counts"] = dict(cfg["tokens"])
    return cfg


# ---------------------------------------------------------------------------------------------------------------
# The state dir (design sec 4)
# ---------------------------------------------------------------------------------------------------------------

class Job(object):
    def __init__(self, store, jid):
        self.store = store
        self.id = jid
        self.dir = os.path.join(store.home, "jobs", jid)
        self.state = read_text(self.path("state")).strip() or QUEUED
        self.reason = read_text(self.path("reason")).strip()
        self.info = read_kv_file(self.path("info"))
        self._spec = None

    def path(self, name):
        return os.path.join(self.dir, name)

    def exists(self):
        return os.path.isfile(self.path("spec"))

    @property
    def spec_text(self):
        return read_text(self.path("spec"))

    @property
    def spec(self):
        if self._spec is None:
            try:
                self._spec = parse_spec(self.spec_text, "%s/spec" % self.id)
            except JmError:
                self._spec = parse_spec("")
                self._spec["_bad"] = True
        return self._spec

    def save_info(self):
        write_kv_file(self.path("info"), self.info)

    def event(self, text):
        append_line(self.path("events"), "%s %s" % (utc(), text))

    def pidinfo(self):
        f = read_text(self.path("pid")).split()
        return (f[0], f[1], f[2]) if len(f) >= 3 else None

    def exitinfo(self):
        f = read_text(self.path("exit"), None)
        if f is None or not f.strip():
            return None
        parts = f.strip().split(None, 1)
        try:
            return int(parts[0]), (parts[1] if len(parts) > 1 else "")
        except ValueError:
            return 1, f.strip()

    def children(self):
        return [c for c in read_text(self.path("children")).split() if c]

    @property
    def parent(self):
        return self.info.get("parent") or None

    def cmd_active(self):
        """Its own command is in its run phase (pid file present, no exit yet)."""
        return os.path.exists(self.path("pid")) and not os.path.exists(self.path("exit"))

    def name(self):
        return self.spec["name"] or ""


class Store(object):
    def __init__(self, home):
        self.home = os.path.abspath(home)
        self.jobs_dir = os.path.join(self.home, "jobs")

    def p(self, name):
        return os.path.join(self.home, name)

    def init(self):
        if not os.path.isdir(self.jobs_dir):
            os.makedirs(self.jobs_dir, 0o700)
        if not os.path.exists(self.p("config")):
            write_atomic(self.p("config"), DEFAULT_CONFIG)
        for name in ("queue", "next_id", "wake"):
            if not os.path.exists(self.p(name)):
                write_atomic(self.p(name), "" if name != "next_id" else "1\n")

    def lock(self):
        return FileLock(self.p("lock"))

    def config(self):
        return parse_config(read_text(self.p("config")))

    def read_queue(self):
        return [x for x in read_text(self.p("queue")).split() if x]

    def write_queue(self, q):
        write_atomic(self.p("queue"), "".join(x + "\n" for x in q))

    def job_ids(self):
        try:
            return sorted(d for d in os.listdir(self.jobs_dir)
                          if os.path.isfile(os.path.join(self.jobs_dir, d, "spec")))
        except OSError:
            return []

    def has_job(self, jid):
        return bool(ID_RE.match(jid)) and os.path.isfile(os.path.join(self.jobs_dir, jid, "spec"))

    def wake(self):
        try:
            write_atomic(self.p("wake"), "%s %s\n" % (utc(), secrets.token_hex(4)))
        except OSError:
            pass

    def daemon_running(self):
        lk = FileLock(self.p("daemon.lock"))
        if lk.acquire(blocking=False):
            lk.release()
            return False
        return True

    def log(self, msg):
        append_line(self.p("daemon.log"), "%s %s" % (utc(), msg))


class Ctx(object):
    """One locked transaction over the state dir: a job cache, the queue, and the state transitions."""

    def __init__(self, store, cfg=None):
        self.store = store
        self.cfg = cfg if cfg is not None else store.config()
        self.queue = store.read_queue()
        self.queue_dirty = False
        self.cache = {}
        self.notes = []  # notify hooks to spawn after the transaction

    def job(self, jid):
        if jid not in self.cache:
            j = Job(self.store, jid)
            self.cache[jid] = j if j.exists() else None
        return self.cache[jid]

    def need(self, jid):
        j = self.job(jid) if ID_RE.match(jid or "") else None
        if j is None:
            raise JmError("no such job: %s" % jid, EXIT_NOJOB)
        return j

    # queue edits
    def q_remove(self, jid):
        if jid in self.queue:
            self.queue = [x for x in self.queue if x != jid]
            self.queue_dirty = True

    def q_insert(self, jid, pos):
        self.q_remove(jid)
        pos = max(0, min(pos, len(self.queue)))
        self.queue.insert(pos, jid)
        self.queue_dirty = True

    def q_after_family(self, parent_id):
        """The queue index just after the parent and its descendants already queued."""
        fam = set([parent_id]) | set(self.descendants(parent_id))
        idx = [i for i, x in enumerate(self.queue) if x in fam]
        return (idx[-1] + 1) if idx else len(self.queue)

    def descendants(self, jid):
        out = []
        j = self.job(jid)
        for c in (j.children() if j else []):
            if self.job(c) is not None:
                out.append(c)
                out.extend(self.descendants(c))
        return out

    def commit(self):
        if self.queue_dirty:
            self.store.write_queue(self.queue)
            self.queue_dirty = False

    # transitions
    def set_state(self, j, state, reason="", event=None):
        old = j.state
        if state != old or reason != j.reason:
            write_atomic(j.path("reason"), reason + "\n")
            j.reason = reason
        if state != old:
            write_atomic(j.path("state"), state + "\n")
            j.state = state
            j.event(event or ("%s: %s" % (state, reason) if reason else state))
            if state in TERMINAL:
                j.info["ended"] = utc()
                j.info.pop("waiting_since", None)
                j.save_info()
                self.q_remove(j.id)
            elif j.id not in self.queue:
                self.queue.append(j.id)
                self.queue_dirty = True
            if j.spec["notify"]:
                self.notes.append(j)
            if state in TERMINAL:
                self.on_terminal(j)

    def requeue(self, j, how, pos=None, reason="requeued"):
        """Back to the queue: queued, at `pos` (None: keep its place, or the end when not queued)."""
        j.info["how"] = how
        j.info.pop("ended", None)
        j.info.pop("child_failed", None)
        j.save_info()
        if pos is not None:
            self.q_insert(j.id, pos)
        self.set_state(j, QUEUED, reason)

    def on_terminal(self, j):
        """A job reached done / failed / cancelled: update its parent and its unfinished children."""
        if j.state in (FAILED, CANCELLED):
            for c in self.descendants(j.id):
                cj = self.job(c)
                if cj.state in PENDING + (HELD, STOPPED):
                    self.set_state(cj, CANCELLED, "parent %s %s" % (j.id, j.state))
        p = self.job(j.parent) if j.parent else None
        if p is None or p.state not in (RUNNING, STOPPING):
            return
        if j.state == FAILED and not p.spec["continue_on_fail"]:
            p.info["child_failed"] = j.id
            p.save_info()
            for c in p.children():
                cj = self.job(c)
                if cj is not None and cj.state in PENDING + (HELD,):
                    self.set_state(cj, CANCELLED, "sibling %s failed" % j.id)
        self.check_parent(p)

    def check_parent(self, p):
        """A running parent whose own command phase is over ends when all its children have ended."""
        if p.state != RUNNING or os.path.exists(p.path("pid")):
            return
        kids = [self.job(c) for c in p.children()]
        kids = [k for k in kids if k is not None]
        if p.info.get("child_failed"):
            if not [k for k in kids if k.state in (RUNNING, STOPPING)]:
                self.set_state(p, FAILED, "child %s failed" % p.info["child_failed"])
            else:
                self.set_reason(p, "child %s failed; waiting for running children" % p.info["child_failed"])
            return
        open_kids = [k for k in kids if k.state not in TERMINAL]
        if open_kids:
            self.set_reason(p, "waiting for %d of %d children" % (len(open_kids), len(kids)))
            return
        bad = [k.id for k in kids if k.state == FAILED]
        if bad:
            self.set_state(p, FAILED, "children failed: " + " ".join(bad))
        else:
            self.set_state(p, DONE, "all %d children done" % len(kids) if kids else "done")

    def set_reason(self, j, reason):
        if reason != j.reason:
            write_atomic(j.path("reason"), reason + "\n")
            j.reason = reason

    def spawn_notes(self):
        for j in self.notes:
            run_notify(self.store, self.cfg, j)
        self.notes = []


# ---------------------------------------------------------------------------------------------------------------
# Job environment and commands
# ---------------------------------------------------------------------------------------------------------------

def expand_vars(value, env):
    """$NAME and ${NAME} from `env`; unknown names expand to the empty string; `$$` is a literal `$`."""
    def repl(m):
        if m.group(0) == "$$":
            return "$"
        return env.get(m.group(1) or m.group(2), "")
    return re.sub(r"\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", repl, value)


def check_env_file(path):
    """Refuse an env_file that does not exist or that others can read (Unix)."""
    if not os.path.isfile(path):
        raise JmError("env_file %s: no such file" % path)
    if IS_WIN:
        return
    st = os.stat(path)
    if st.st_uid != os.getuid():
        raise JmError("env_file %s: not owned by this user" % path)
    if st.st_mode & 0o077:
        raise JmError("env_file %s: mode %o lets group/others read it; chmod 600 it" % (path, st.st_mode & 0o777))


def read_env_file(path):
    out = {}
    for n, line in enumerate(read_text(path).splitlines()):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise JmError("env_file %s line %d: expected KEY=VALUE" % (path, n + 1))
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def base_env(cfg):
    """The daemon's environment minus every secret name: secrets reach a job only through env_pass/env_file."""
    env = dict((k, v) for k, v in os.environ.items() if not is_secret_name(k, cfg) and not k.startswith("JM_"))
    for k, v in cfg["env"]:
        env[k] = expand_vars(v, env)
    return env


def job_env(store, cfg, j, run=None, how=None, extra=None):
    spec = j.spec
    env = base_env(cfg)
    for k, v in spec["env"]:
        env[k] = expand_vars(v, env)
    for name in spec["env_pass"]:
        if name not in os.environ:
            raise JmError("env_pass %s is not set in the daemon's environment" % name)
        env[name] = os.environ[name]
    for path in spec["env_file"]:
        check_env_file(path)
        env.update(read_env_file(path))
    env.update({"JM_ID": j.id, "JM_NAME": spec["name"] or "", "JM_HOME": store.home,
                "JM_RUN": str(run if run is not None else j.info.get("run", "0")),
                "JM_HOW": how or j.info.get("how", "start"), "JM_PARENT": j.parent or "",
                "JM_JM": shell_join([sys.executable, JM_PATH])})
    env.update(extra or {})
    return env


def job_cwd(store, j):
    """The job's directory; $NAME in it expands from the config's and the spec's env lines (cwd = $DATA/x)."""
    cwd = j.spec["cwd"]
    if not cwd:
        return store.home
    if "$" in cwd:
        try:
            env = base_env(store.config())
        except JmError:
            return cwd
        for k, v in j.spec["env"]:
            env[k] = expand_vars(v, env)
        cwd = expand_vars(cwd, env)
    return cwd


def run_notify(store, cfg, j):
    try:
        ex = j.exitinfo()
        env = job_env(store, cfg, j, extra={"JM_STATE": j.state, "JM_EXIT": str(ex[0]) if ex else ""})
        cwd = job_cwd(store, j)
        log = open(j.path("log"), "ab")
        try:
            SPAWNED.append(spawn_detached(j.spec["notify"], env=env, cwd=cwd if os.path.isdir(cwd) else None,
                                          stdout=log, shell=True))
        finally:
            log.close()
    except Exception as e:
        try:
            j.event("notify failed: %s" % e)
        except OSError:
            pass


def run_global_hook(store, cfg, key):
    """Run on_first_start / on_idle (outside the state lock). Returns its exit code, 124 on timeout."""
    cmd = cfg[key]
    env = base_env(cfg)
    env.update({"JM_HOME": store.home, "JM_HOOK": key})
    store.log("%s: %s" % (key, cmd))
    log = open(store.p("daemon.log"), "ab")
    try:
        p = subprocess.Popen(cmd, shell=True, env=env, cwd=store.home, stdin=subprocess.DEVNULL, stdout=log,
                             stderr=subprocess.STDOUT)
        try:
            rc = p.wait(timeout=cfg["hook_timeout"])
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            rc = 124
        store.log("%s exited %d" % (key, rc))
        return rc
    except OSError as e:
        store.log("%s failed: %s" % (key, e))
        return 127
    finally:
        log.close()


# ---------------------------------------------------------------------------------------------------------------
# Validation and submission
# ---------------------------------------------------------------------------------------------------------------

def check_spec(spec, cfg, what="spec"):
    for k, v in spec["env"]:
        if is_secret_name(k, cfg):
            raise JmError("%s: env %s looks like a secret; never put secrets in a spec: %s" % (what, k, SAFE_WAYS))
        if has_secret_value(v, cfg):
            raise JmError("%s: env %s holds a secret-looking value; %s" % (what, k, SAFE_WAYS))
    for key in CMD_KEYS_SPEC + ["note", "name"]:
        if spec[key] and has_secret_value(spec[key], cfg):
            raise JmError("%s: %s holds a secret-looking value; reference $NAME and %s" % (what, key, SAFE_WAYS))
    for name in spec["env_pass"]:
        if not ENV_KEY_RE.match(name):
            raise JmError("%s: env_pass %r is not a variable name" % (what, name))
    for path in spec["env_file"]:
        if not os.path.isabs(path):
            raise JmError("%s: env_file %s must be an absolute path" % (what, path))
        check_env_file(path)
    if not spec["start"] and not spec["children"]:
        raise JmError("%s: needs `start` (or `children` for a group)" % what)
    if spec["id"] is not None and not ID_RE.match(spec["id"]):
        raise JmError("%s: id %r: use letters, digits, `_`, `.`, `-` (max 64)" % (what, spec["id"]))
    for name, n in spec["tokens"]:
        have = cfg["token_counts"].get(name)
        if have is None:
            raise JmError("%s: token %r is not in the config (tokens = %s:N)" % (what, name, name))
        if n > have:
            raise JmError("%s: token %s:%d but the machine has %d" % (what, name, n, have))
    if spec["cpu"] > cfg["cpus"]:
        raise JmError("%s: cpu %s > cpus %s of this machine" % (what, fmt_num(spec["cpu"]), fmt_num(cfg["cpus"])))
    if spec["ram"] > cfg["ram"] - cfg["ram_reserve"]:
        raise JmError("%s: ram %s > %s MB this machine can hand out" %
                      (what, fmt_num(spec["ram"]), fmt_num(cfg["ram"] - cfg["ram_reserve"])))


def load_tree(path, text, cfg, what):
    """Parse a spec and, recursively, its `children = FILE` specs: [(text, spec, [child trees])]."""
    try:
        spec = parse_spec(text, what)
    except JmError as e:
        raise JmError(str(e))
    check_spec(spec, cfg, what)
    base = os.path.dirname(os.path.abspath(path)) if path else os.getcwd()
    kids = []
    for c in spec["children"]:
        cp = c if os.path.isabs(c) else os.path.join(base, c)
        if not os.path.isfile(cp):
            raise JmError("%s: children file %s not found" % (what, cp))
        kids.append(load_tree(cp, read_text(cp), cfg, c))
    return (text, spec, kids)


def next_auto_id(store, taken):
    n = int((read_text(store.p("next_id")).strip() or "1"))
    while True:
        jid = "j%04d" % n
        n += 1
        if jid not in taken and not os.path.exists(os.path.join(store.jobs_dir, jid)):
            write_atomic(store.p("next_id"), "%d\n" % n)
            return jid


def submit_tree(ctx, tree, parent, pos, hold, cwd_default):
    """Create the job dirs for a spec tree; returns the ids, parent first."""
    store = ctx.store
    text, spec, kids = tree
    jid = spec["id"] or next_auto_id(store, ctx.cache)
    if ctx.job(jid) is not None or os.path.exists(os.path.join(store.jobs_dir, jid)):
        raise JmError("job %s already exists (requeue it, or `jm clean` it first)" % jid)
    if not spec["cwd"]:
        text = text.rstrip("\n") + ("\n" if text.strip() else "") + "cwd = %s   # added by jm submit\n" % \
            escape_value(cwd_default)
    d = os.path.join(store.jobs_dir, jid)
    os.makedirs(d)
    write_atomic(os.path.join(d, "spec"), text)
    info = {"submitted": utc(), "how": "start", "run": "0", "retries_used": "0"}
    if parent:
        info["parent"] = parent
    write_kv_file(os.path.join(d, "info"), info)
    write_atomic(os.path.join(d, "state"), (HELD if hold else QUEUED) + "\n")
    write_atomic(os.path.join(d, "reason"), ("held at submit" if hold else "") + "\n")
    append_line(os.path.join(d, "events"), "%s submitted%s" % (utc(), " (held)" if hold else ""))
    ctx.cache.pop(jid, None)
    ctx.q_insert(jid, pos)
    if parent:
        append_line(os.path.join(store.jobs_dir, parent, "children"), jid)
        ctx.cache.pop(parent, None)
    ids = [jid]
    for k in kids:
        ids += submit_tree(ctx, k, jid, ctx.q_after_family(jid), False, spec["cwd"] or cwd_default)
    return ids


# ---------------------------------------------------------------------------------------------------------------
# The daemon: reconcile running jobs, then admit (design sec 6, 7)
# ---------------------------------------------------------------------------------------------------------------

def append_run(j, rc, how_end):
    info = j.info
    append_line(j.path("runs"), "%s %s %s %s %s" % (info.get("run", "0"), info.get("run_start", "-"), utc(),
                                                    rc, info.get("how", "start")))


def fail_or_retry(ctx, j, reason):
    spec = j.spec
    used = int(j.info.get("retries_used", "0") or 0)
    if used < spec["retries"]:
        j.info["retries_used"] = str(used + 1)
        j.save_info()
        why = "%s; retry %d of %d" % (reason, used + 1, spec["retries"])
        if j.info.get("hold_after"):
            j.info.pop("hold_after", None)
            j.info["how"] = "resume"
            j.save_info()
            ctx.set_state(j, HELD, why + " (held: hold was asked while running)")
        else:
            ctx.requeue(j, "resume", None, why)
    else:
        ctx.set_state(j, FAILED, reason)


def apply_stop_request(ctx, j, req, rc):
    after = req.get("after", "stopped")
    reason = req.get("reason", "")
    if after == "stopped":
        ctx.set_state(j, STOPPED, reason or "stopped by hand")
    elif after == "cancel":
        ctx.set_state(j, CANCELLED, reason or "cancelled by hand")
    elif after == "restart":
        j.info["retries_used"] = "0"
        ctx.requeue(j, "restart", 0, reason or "restart")
    elif after == "requeue":
        pos = {"top": 0, "end": len(ctx.queue)}.get(req.get("pos", ""))
        j.info["retries_used"] = "0"
        ctx.requeue(j, "start" if req.get("fresh") == "yes" else "resume", pos, reason or "requeued by hand")
    elif after == "hold":
        j.info["how"] = "resume"
        j.save_info()
        ctx.set_state(j, HELD, reason or "held")
    else:  # fail (timeout)
        fail_or_retry(ctx, j, reason or "failed")


def finish_run(ctx, j, rc, note):
    """The wrapper wrote `exit`: record the run and move the job on."""
    req = read_kv_file(j.path("stop_req")) if os.path.exists(j.path("stop_req")) else None
    append_run(j, rc, note)
    for f in ("pid", "child", "stop_req"):
        remove_file(j.path(f))
    j.info.pop("paused", None)
    j.save_info()
    if req is not None:
        apply_stop_request(ctx, j, req, rc)
    elif note.startswith("done_check"):
        ctx.set_state(j, DONE, "done_check passed")
    elif rc == 0 and not note:
        j.info["retries_used"] = "0"
        j.save_info()
        if j.children():
            ctx.check_parent(j)
        else:
            ctx.set_state(j, DONE, "exit 0")
    else:
        if note.startswith("pre"):
            why = "pre failed (exit %d)" % rc
        elif note.startswith("start-failed"):
            why = "start failed: " + note[len("start-failed"):].strip()
        else:
            why = "exit %d%s" % (rc, (" (" + note + ")") if note else "")
        if j.info.get("child_failed") and j.children():
            ctx.set_state(j, FAILED, why)
        else:
            fail_or_retry(ctx, j, why)
    if j.info.pop("hold_after", None):
        j.save_info()


def cut_off(ctx, j):
    """The wrapper died without writing `exit` (reboot, crash, kill -9): on_boot decides (design sec 7)."""
    child = read_text(j.path("child")).split()
    if len(child) >= 2 and proc_alive(child[0], child[1]):
        kill_group(child[0], hard=True)
    req = read_kv_file(j.path("stop_req")) if os.path.exists(j.path("stop_req")) else None
    append_run(j, "cut", "")
    for f in ("pid", "child", "stop_req"):
        remove_file(j.path(f))
    j.info.pop("paused", None)
    j.save_info()
    if req is not None:
        apply_stop_request(ctx, j, req, None)
        return
    mode = j.spec["on_boot"]
    if mode == "hold":
        j.info["how"] = "resume"
        j.save_info()
        ctx.set_state(j, HELD, "run cut off (reboot or crash); held (on_boot = hold)")
    elif mode == "fail":
        ctx.set_state(j, FAILED, "run cut off (reboot or crash); on_boot = fail")
    else:
        ctx.requeue(j, "resume", 0, "run cut off (reboot or crash); resuming")


def deps_state(ctx, j):
    """None when every `after` job is finished as required; else the reason it is not."""
    for dep in j.spec["after"]:
        d = ctx.job(dep)
        if d is None:
            return "after: %s does not exist" % dep
        if j.spec["after_ok"]:
            if d.state == DONE:
                continue
            if d.state in (FAILED, CANCELLED):
                return "after: %s %s (after_ok = yes)" % (dep, d.state)
        elif d.state in TERMINAL:
            continue
        return "after: %s %s" % (dep, d.state)
    return None


class Usage(object):
    def __init__(self, ctx):
        self.cpu = 0.0
        self.ram = 0.0
        self.tokens = {}
        self.disk_claimed = {}
        self.running = 0
        self.kids_running = {}
        for jid in ctx.queue:
            j = ctx.job(jid)
            if j is not None and j.state in (RUNNING, STOPPING) and os.path.exists(j.path("pid")):
                self.add(j)

    def add(self, j):
        s = j.spec
        self.cpu += s["cpu"]
        self.ram += s["ram"]
        for name, n in s["tokens"]:
            self.tokens[name] = self.tokens.get(name, 0) + n
        self.running += 1
        if j.parent:
            self.kids_running[j.parent] = self.kids_running.get(j.parent, 0) + 1


def never_fits(cfg, s):
    if s["cpu"] > cfg["cpus"] + 1e-9:
        return "never fits: cpu %s > cpus %s" % (fmt_num(s["cpu"]), fmt_num(cfg["cpus"]))
    avail = cfg["ram"] - cfg["ram_reserve"]
    if s["ram"] > avail + 1e-9:
        return "never fits: ram %s > %s" % (fmt_num(s["ram"]), fmt_num(avail))
    for name, n in s["tokens"]:
        have = cfg["token_counts"].get(name)
        if have is None:
            return "never fits: token %s not configured" % name
        if n > have:
            return "never fits: token %s:%d > %d" % (name, n, have)
    return None


def fits(ctx, use, j, store):
    cfg, s = ctx.cfg, j.spec
    free_cpu = cfg["cpus"] - use.cpu
    if s["cpu"] > free_cpu + 1e-9:
        return "cpu %s > free %s" % (fmt_num(s["cpu"]), fmt_num(max(0, free_cpu)))
    free_ram = cfg["ram"] - cfg["ram_reserve"] - use.ram
    if s["ram"] > free_ram + 1e-9:
        return "ram %s > free %s" % (fmt_num(s["ram"]), fmt_num(max(0, free_ram)))
    for name, n in s["tokens"]:
        free = cfg["token_counts"].get(name, 0) - use.tokens.get(name, 0)
        if n > free:
            return "token %s:%d > free %d" % (name, n, max(0, free))
    if s["disk"]:
        mb, path = s["disk"]
        path = path or job_cwd(store, j)
        free = disk_free_mb(path) - use.disk_claimed.get(path, 0.0)
        if mb > free:
            return "disk %s > free %s @ %s" % (fmt_num(mb), fmt_num(int(free)), path)
    return None


def start_job(ctx, j, use, daemon):
    store = ctx.store
    if not daemon.busy:
        daemon.set_machine(store, True)
    run = int(j.info.get("run", "0") or 0) + 1
    how = j.info.get("how") or "start"
    token = secrets.token_hex(8)
    j.info.update({"run": str(run), "run_start": utc(), "how": how})
    j.info.pop("waiting_since", None)
    j.save_info()
    for f in ("exit", "stop_req", "child"):
        remove_file(j.path(f))
    try:
        p = spawn_detached([sys.executable, JM_PATH, "--home", store.home, "_run", j.id, token])
    except OSError as e:
        ctx.set_state(j, FAILED, "could not start the wrapper: %s" % e)
        return
    SPAWNED.append(p)
    st = proc_start_time(p.pid) or "?"
    write_atomic(j.path("pid"), "%d %s %s\n" % (p.pid, st, token))
    use.add(j)
    if j.spec["disk"]:
        mb, path = j.spec["disk"]
        path = path or job_cwd(store, j)
        use.disk_claimed[path] = use.disk_claimed.get(path, 0.0) + mb
    ctx.set_state(j, RUNNING, "run %d (%s)" % (run, how), "started run %d (%s), wrapper pid %d" % (run, how, p.pid))


class DaemonState(object):
    """What the daemon remembers between passes. `STATE/machine` ("busy <since>" / "idle <since>") keeps the
    on_first_start / on_idle state across daemon restarts, and lets scripts outside jm check it."""

    def __init__(self, store):
        f = read_text(store.p("machine")).split()
        self.busy = (f[0] == "busy") if f else None
        self.idle_since = None
        self.want_hook = None   # a global hook to run after this pass, outside the lock
        self.hook_error = None  # why on_first_start failed last time (jobs wait and it is retried)
        self.progress_at = {}

    def set_machine(self, store, busy):
        self.busy = busy
        self.idle_since = None
        write_atomic(store.p("machine"), "%s %s\n" % ("busy" if busy else "idle", utc()))


def daemon_pass(store, daemon):
    ctx = Ctx(store)
    cfg = ctx.cfg
    # Repair the queue: drop ids without a job, add unfinished jobs that are missing (hand edits).
    seen = set()
    q = []
    for jid in ctx.queue:
        j = ctx.job(jid) if ID_RE.match(jid) else None
        if j is not None and jid not in seen and j.state not in TERMINAL:
            q.append(jid)
            seen.add(jid)
    for jid in store.job_ids():
        if jid not in seen:
            j = ctx.job(jid)
            if j is not None and j.state not in TERMINAL:
                q.append(jid)
    if q != ctx.queue:
        ctx.queue = q
        ctx.queue_dirty = True

    # 1. Running jobs: finished, cut off, timed out, or adopted as they are.
    now = time.time()
    for jid in list(ctx.queue):
        j = ctx.job(jid)
        if j is None or j.state not in (RUNNING, STOPPING):
            continue
        if not os.path.exists(j.path("pid")):
            ctx.check_parent(j)
            continue
        ex = j.exitinfo()
        if ex is None:
            pi = j.pidinfo()
            if pi is not None and proc_alive(*pi):
                t = j.spec["timeout"]
                started = parse_utc(j.info.get("run_start", "")) or now
                if t and now - started > t and not os.path.exists(j.path("stop_req")):
                    write_kv_file(j.path("stop_req"), {"after": "fail", "reason": "timeout after %s" %
                                                       fmt_duration(t)})
                    ctx.set_state(j, STOPPING, "timeout after %s; stopping" % fmt_duration(t))
                continue
            ex = j.exitinfo()  # it may have ended between the two reads
        if ex is not None:
            finish_run(ctx, j, ex[0], ex[1])
        else:
            cut_off(ctx, j)

    # 2. Admission: walk the queue top to bottom (design sec 6).
    use = Usage(ctx)
    if daemon.busy is None:
        daemon.set_machine(store, use.running > 0)
    head = None
    blocked = None
    for jid in list(ctx.queue):
        j = ctx.job(jid)
        if j is None or j.state not in PENDING:
            continue
        s = j.spec
        if s.get("_bad"):
            ctx.set_state(j, WAITING, "spec does not parse; fix it with jm edit")
            continue
        why = deps_state(ctx, j)
        if why is None and j.parent:
            p = ctx.job(j.parent)
            if p is None:
                why = "parent %s does not exist" % j.parent
            elif p.state != RUNNING:
                why = "parent %s %s" % (j.parent, p.state)
            elif p.spec["parallel"] and use.kids_running.get(p.id, 0) >= p.spec["parallel"]:
                why = "group %s: parallel = %d reached" % (p.id, p.spec["parallel"])
        if why is None:
            why = never_fits(cfg, s)
        if why is not None:
            if j.info.pop("waiting_since", None):
                j.save_info()
            ctx.set_state(j, WAITING, why)
            continue
        if not s["start"]:
            # A static group's parent with no command of its own: it runs as soon as it is eligible.
            ctx.set_state(j, RUNNING, "group: %d children" % len(j.children()), "started (group)")
            ctx.check_parent(j)
            continue
        why = "%s: head %s waits" % (blocked, head.id) if blocked else fits(ctx, use, j, store)
        if why is None and not daemon.busy and cfg["on_first_start"]:
            # The machine is idle: run on_first_start (after this pass, outside the lock) before any job starts.
            daemon.want_hook = "on_first_start"
            ctx.set_state(j, WAITING, daemon.hook_error or "on_first_start hook runs first")
            break
        if why is None:
            start_job(ctx, j, use, daemon)
            continue
        if not j.info.get("waiting_since"):
            j.info["waiting_since"] = utc()
            j.save_info()
        if head is None:
            head = j
            waited = now - (parse_utc(j.info["waiting_since"]) or now)
            if not cfg["backfill"]:
                blocked = "fifo (backfill = no)"
            elif s["drain"]:
                blocked = "drain (head has drain = yes)"
            elif waited > cfg["drain_after"]:
                blocked = "drain (head waited %s > drain_after)" % fmt_duration(waited)
            if blocked:
                why = "%s; %s" % (why, blocked)
        ctx.set_state(j, WAITING, why)
    ctx.commit()
    if use.running == 0 and daemon.busy and not daemon.want_hook:
        # Idle for idle_after before on_idle, so a chain of jobs does not flip the machine back and forth.
        daemon.idle_since = daemon.idle_since or now
        if now - daemon.idle_since >= cfg["idle_after"]:
            if cfg["on_idle"]:
                daemon.want_hook = "on_idle"
            else:
                daemon.set_machine(store, False)
    elif use.running:
        daemon.idle_since = None
    ctx.spawn_notes()
    return ctx


def run_wanted_hook(store, cfg, daemon):
    """Run the hook the last pass asked for. True when the next pass should follow at once."""
    key, daemon.want_hook = daemon.want_hook, None
    rc = run_global_hook(store, cfg, key)
    if key == "on_idle":
        daemon.set_machine(store, False)
        return False
    if rc == 0:
        daemon.hook_error = None
        daemon.set_machine(store, True)
        return True
    daemon.hook_error = "on_first_start failed (exit %d, see daemon.log); retried every poll" % rc
    return False


def sample_progress(store, cfg, daemon):
    """Run each running job's `progress` command every progress_every; keep the last 20 lines."""
    now = time.time()
    for jid in store.read_queue():
        j = Job(store, jid)
        if not j.exists() or j.state != RUNNING or not j.spec["progress"] or not j.cmd_active():
            continue
        last = daemon.progress_at.get(jid, 0)
        if last > (parse_utc(j.info.get("run_start", "")) or 0) and now - last < cfg["progress_every"]:
            continue
        daemon.progress_at[jid] = now
        try:
            env = job_env(store, cfg, j)
            p = subprocess.Popen(j.spec["progress"], shell=True, env=env, cwd=job_cwd(store, j),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 universal_newlines=True)
            out, _ = p.communicate(timeout=15)
        except Exception:
            try:
                p.kill()
                p.wait()
            except Exception:
                pass
            continue
        lines = [x for x in (out or "").splitlines() if x.strip()]
        if lines:
            old = read_text(j.path("progress")).splitlines()[-19:]
            write_atomic(j.path("progress"), "\n".join(old + ["%s %s" % (utc(), lines[-1].strip())]) + "\n")


def daemon_run(store, quiet=False):
    store.init()
    dlock = FileLock(store.p("daemon.lock"))
    if not dlock.acquire(blocking=False):
        raise JmError("a daemon is already running for %s" % store.home)
    stop = []
    if not IS_WIN:
        signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        signal.signal(signal.SIGINT, lambda *a: stop.append(1))
    except (ValueError, OSError):
        pass
    me = os.getpid()
    write_atomic(store.p("daemon.pid"), "%d %s %s\n" % (me, proc_start_time(me) or "?", VERSION))
    remove_file(store.p("daemon.stop"))
    store.log("daemon started, pid %d, jm %s, python %s" % (me, VERSION, sys.version.split()[0]))
    daemon = DaemonState(store)
    cfg = None
    try:
        while not stop:
            try:
                cfg = store.config()
            except JmError as e:
                store.log("config error (keeping the last good one): %s" % e)
                if cfg is None:
                    cfg = parse_config("")
            again = False
            try:
                with store.lock():
                    daemon_pass(store, daemon)
                if daemon.want_hook:
                    again = run_wanted_hook(store, cfg, daemon)
                sample_progress(store, cfg, daemon)
            except Exception:
                store.log("pass failed:\n" + traceback.format_exc())
            reap_spawned()
            if again:
                continue
            if os.path.exists(store.p("daemon.stop")):
                break
            last = read_text(store.p("wake"))
            deadline = time.time() + max(0.05, cfg["poll"])
            while time.time() < deadline and not stop:
                time.sleep(0.05)
                if read_text(store.p("wake")) != last or os.path.exists(store.p("daemon.stop")):
                    break
    finally:
        remove_file(store.p("daemon.stop"))
        remove_file(store.p("daemon.pid"))
        store.log("daemon stopped (running jobs keep running)")
        dlock.release()


# ---------------------------------------------------------------------------------------------------------------
# The per-job wrapper: `jm _run ID TOKEN` (design sec 3)
# ---------------------------------------------------------------------------------------------------------------

def popen_shell(cmd, env, cwd, log, nice=0, err=None):
    kw = dict(shell=True, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log, stderr=err or subprocess.STDOUT)
    if IS_WIN:
        kw["creationflags"] = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW | \
            (BELOW_NORMAL_PRIORITY_CLASS if nice > 0 else 0)
    else:
        kw["start_new_session"] = True
        if nice:
            kw["preexec_fn"] = lambda: os.nice(nice)
    return subprocess.Popen(cmd, **kw)


def wrapper_run(store, jid, token):
    """Never leave without an `exit` file: a wrapper that dies silently would read as a cut-off run."""
    try:
        wrapper_body(store, jid, token)
    except Exception:
        j = Job(store, jid)
        if not os.path.exists(j.path("exit")):
            err = traceback.format_exc()
            try:
                append_line(j.path("log"), "jm: wrapper error:\n" + err)
            finally:
                write_atomic(j.path("exit"), "1 start-failed wrapper error: %s\n" % err.strip().splitlines()[-1])
                store.wake()


def wrapper_body(store, jid, token):
    if not IS_WIN:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    j = Job(store, jid)
    spec = j.spec
    run = j.info.get("run", "1")
    how = j.info.get("how", "start")
    paths = [j.path("log")] + ([j.path("stderr")] if spec["split_stderr"] else [])
    for logp in paths:
        if spec["log_max"] and os.path.exists(logp) and os.path.getsize(logp) > spec["log_max"] * 1048576:
            os.replace(logp, logp + ".1")
    log = open(paths[0], "ab")
    err = open(paths[1], "ab") if len(paths) > 1 else None  # split_stderr: every command's stderr goes here

    def say(msg):
        # The banner goes into both files, so the runs in `stderr` can be told apart too.
        for f in (log, err) if err else (log,):
            f.write(("==== jm %s run %s (%s): %s ====\n" % (jid, run, how, msg)).encode("utf-8"))
            f.flush()

    def finish(rc, note=""):
        say("ended %s, exit %d%s" % (utc(), rc, (" (%s)" % note) if note else ""))
        write_atomic(j.path("exit"), ("%d %s" % (rc, note)).strip() + "\n")
        store.wake()

    say("started %s" % utc())
    try:
        cfg = store.config()
        env = job_env(store, cfg, j, run, how)
    except Exception as e:
        log.write(("jm: cannot start: %s\n" % e).encode("utf-8"))
        return finish(1, "start-failed %s" % e)
    cwd = job_cwd(store, j)
    if not os.path.isdir(cwd):
        return finish(1, "start-failed cwd %s does not exist" % cwd)
    nice = spec["nice"] or 0

    def watch(cmd, group_nice=0):
        """Run one command and wait for it, honouring a stop request (stop_req). Returns (rc, stopped)."""
        child = popen_shell(cmd, env, cwd, log, group_nice, err)
        write_atomic(j.path("child"), "%d %s\n" % (child.pid, proc_start_time(child.pid) or "?"))
        stopping = False
        hard_at = None
        killed = False
        while True:
            rc = child.poll()
            if rc is not None:
                break
            if not stopping and os.path.exists(j.path("stop_req")):
                stopping = True
                hard_at = time.time() + (spec["stop_grace"] if spec["stop_grace"] is not None else 60)
                say("stop requested %s" % utc())
                if spec["stop"]:
                    try:
                        e2 = dict(env)
                        e2["JM_PID"] = str(child.pid)
                        subprocess.Popen(spec["stop"], shell=True, env=e2, cwd=cwd, stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=err or subprocess.STDOUT)
                    except OSError as e:
                        say("stop command failed: %s" % e)
                        kill_group(child.pid)
                elif not kill_group(child.pid) and IS_WIN:
                    kill_group(child.pid, hard=True)  # taskkill without /F refused: no gentle way on Windows
            if stopping and not killed and time.time() >= hard_at:
                killed = True
                say("still running %s after stop_grace; killing" % utc())
                kill_group(child.pid, hard=True)
            time.sleep(0.1)
        if stopping and not IS_WIN:
            # The shell may be gone while something it started ignores TERM: the whole group goes.
            while group_exists(child.pid):
                if time.time() >= hard_at:
                    say("group still running %s after stop_grace; killing" % utc())
                    kill_group(child.pid, hard=True)
                    break
                time.sleep(0.1)
        sig = -rc if rc < 0 else 0
        return (128 + sig if sig else rc), stopping, sig

    def stopped_finish(rc):
        return finish(rc, "stopped")

    if spec["done_check"]:
        rc, stopped, _ = watch(spec["done_check"])
        if stopped:
            return stopped_finish(rc)
        if rc == 0:
            say("done_check passed")
            return finish(0, "done_check")
    if spec["pre"]:
        rc, stopped, _ = watch(spec["pre"])
        if stopped:
            return stopped_finish(rc)
        if rc != 0:
            return finish(rc, "pre")
    cmd = spec["resume"] if how in ("resume", "restart") and spec["resume"] else spec["start"]
    rc, stopped, sig = watch(cmd, nice)
    if spec["post"]:
        env["JM_EXIT"] = str(rc)
        try:
            p = popen_shell(spec["post"], env, cwd, log, err=err)
            p.wait()
        except OSError as e:
            say("post failed: %s" % e)
    return finish(rc, "stopped" if stopped else ("signal %d" % sig if sig else ""))


# ---------------------------------------------------------------------------------------------------------------
# `jm _sim`: a fake job for the tests (design sec 12.5)
# ---------------------------------------------------------------------------------------------------------------

def sim_main(a):
    if a.trap and not IS_WIN:
        signal.signal(signal.SIGTERM, lambda *x: sys.stdout.write("sim: ignoring TERM\n") or sys.stdout.flush())
    if a.stderr:
        sys.stderr.write(a.stderr + "\n")
        sys.stderr.flush()
    for name in a.print_env:
        line = "env %s=%s" % (name, os.environ.get(name, "<unset>"))
        if a.out:
            append_line(a.out, line)
        else:
            print(line)
    hog = bytearray(int(a.ram * 1048576)) if a.ram else None
    step = 0.1
    total = max(1, int(round(a.secs / step)))
    done = 0
    if a.resumable and os.path.exists(a.resumable):
        done = int(read_text(a.resumable).strip() or "0")
        print("sim: resumed at %d/%d" % (done, total))
    sys.stdout.flush()
    while done < total:
        time.sleep(step)
        done += 1
        if a.resumable:
            write_atomic(a.resumable, "%d\n" % done)
        if a.progress and (done % 5 == 0 or done == total):
            print("jm:progress=%d/%d" % (done, total))
            sys.stdout.flush()
    del hog
    print("sim: finished, exit %d" % a.exit)
    return a.exit


# ---------------------------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------------------------

def out(line=""):
    sys.stdout.write(line + "\n")


def warn(msg):
    sys.stderr.write("jm: %s\n" % msg)


def confirm(question):
    if not sys.stdin.isatty():
        return False
    sys.stderr.write(question + " [y/N] ")
    sys.stderr.flush()
    return sys.stdin.readline().strip().lower() in ("y", "yes")


def mutate(store, fn):
    """Run fn(ctx) inside one locked transaction, then wake the daemon."""
    store.init()
    with store.lock():
        ctx = Ctx(store)
        result = fn(ctx)
        ctx.commit()
        ctx.spawn_notes()
    store.wake()
    return result


def note_daemon(store):
    if not store.daemon_running():
        warn("note: the daemon is not running, nothing will start (jm daemon start)")


def parse_assignments(items):
    out_ = []
    for it in items:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)(\+?=)(.*)$", it, re.S)
        if not m:
            raise JmError("expected key=value or key+=value, got %r" % it, EXIT_USAGE)
        out_.append((m.group(1), m.group(2) == "+=", m.group(3).strip()))
    return out_


def apply_assignments(text, assigns, table):
    lists = set(k for k, kind, _ in table if kind in LIST_KINDS)
    known = set(k for k, _, _ in table)
    for key, append, value in assigns:
        if key not in known:
            raise JmError("unknown key %r" % key)
        if append and key not in lists:
            raise JmError("%s is not a list key; use %s=value" % (key, key))
        text = edit_kv_text(text, key, [value] if value != "" else [], append=append)
    return text


def cmd_submit(store, a):
    store.init()
    cfg = store.config()
    if a.command:
        if a.file:
            raise JmError("give a spec FILE or `-- COMMAND`, not both", EXIT_USAGE)
        text = "start = %s\n" % escape_value(shell_join(a.command))
        path = None
    elif a.file == "-":
        text, path = sys.stdin.read(), None
    elif a.file:
        if not os.path.isfile(a.file):
            raise JmError("no such spec file: %s" % a.file)
        text, path = read_text(a.file), a.file
    else:
        raise JmError("give a spec FILE, `-` (stdin) or `-- COMMAND`", EXIT_USAGE)
    assigns = []
    for key in ("name", "cpu", "ram", "disk", "timeout", "retries", "id", "cwd"):
        v = getattr(a, key, None)
        if v is not None:
            assigns.append((key, False, str(v)))
    if a.continue_on_fail:
        assigns.append(("continue_on_fail", False, "yes"))
    for v in a.tokens or []:
        assigns.append(("tokens", True, v))
    for v in a.dep or []:
        assigns.append(("after", True, v))
    for v in a.env or []:
        assigns.append(("env", True, v))
    assigns += parse_assignments(a.set or [])
    text = apply_assignments(text, assigns, SPEC_KEYS)
    tree = load_tree(path, text, cfg, path or "spec")

    def go(ctx):
        names = set()

        def collect(t):
            if t[1]["id"]:
                if t[1]["id"] in names:
                    raise JmError("id %s used twice in this submission" % t[1]["id"])
                names.add(t[1]["id"])
            for k in t[2]:
                collect(k)
        collect(tree)

        def check_deps(t):
            for dep in t[1]["after"]:
                if dep not in names and ctx.job(dep) is None:
                    raise JmError("after: no such job %s" % dep, EXIT_NOJOB)
            for k in t[2]:
                check_deps(k)
        check_deps(tree)
        parent = None
        if a.parent:
            p = ctx.need(a.parent)
            if p.state in TERMINAL:
                raise JmError("parent %s is %s" % (p.id, p.state), EXIT_STATE)
            parent = p.id
        if a.top:
            pos = 0
        elif a.after_job:
            ctx.need(a.after_job)
            if a.after_job not in ctx.queue:
                raise JmError("%s is not in the queue" % a.after_job, EXIT_STATE)
            pos = ctx.queue.index(a.after_job) + 1
        elif parent and not a.end:
            pos = ctx.q_after_family(parent)
        else:
            pos = len(ctx.queue)
        return submit_tree(ctx, tree, parent, pos, a.hold, os.getcwd())
    ids = mutate(store, go)
    for i in ids:
        out(i)
    note_daemon(store)
    return EXIT_OK


def job_progress(j):
    lines = read_text(j.path("progress")).splitlines()
    return lines[-1].split(" ", 1)[1] if lines and " " in lines[-1] else ""


def list_row(j, depth=0):
    reason = j.reason
    if j.state == RUNNING and j.cmd_active():
        t0 = parse_utc(j.info.get("run_start", ""))
        if t0:
            reason = "%s, %s" % (reason, fmt_duration(time.time() - t0))
    return "\t".join(["  " * depth + j.id, j.state, j.name(), reason, job_progress(j)])


def cmd_list(store, a):
    store.init()
    ctx = Ctx(store)
    cfg = ctx.cfg
    queued = [ctx.job(x) for x in ctx.queue]
    queued = [j for j in queued if j is not None]
    finished = []
    now = time.time()
    in_q = set(ctx.queue)
    for jid in store.job_ids():
        if jid in in_q:
            continue
        j = ctx.job(jid)
        ended = parse_utc(j.info.get("ended", "")) or 0
        keep = j.spec["keep"] if j.spec["keep"] is not None else cfg["keep"]
        if a.all or now - ended <= keep:
            finished.append((ended, j))
    finished.sort(key=lambda x: x[0])
    rows = queued + [j for _, j in finished]
    if a.tag:
        rows = [j for j in rows if a.tag in j.spec["tags"]]
    if a.tree:
        ids = set(j.id for j in rows)
        order = dict((j.id, i) for i, j in enumerate(rows))

        def emit(j, depth):
            out(list_row(j, depth))
            kids = [ctx.job(c) for c in j.children()]
            for k in sorted([k for k in kids if k is not None and k.id in ids], key=lambda k: order[k.id]):
                emit(k, depth + 1)
        for j in rows:
            if not (j.parent and j.parent in ids):
                emit(j, 0)
    else:
        for j in rows:
            out(list_row(j))
    return EXIT_OK


def free_line(store):
    ctx = Ctx(store)
    cfg = ctx.cfg
    use = Usage(ctx)
    parts = ["cpus %s/%s" % (fmt_num(round(cfg["cpus"] - use.cpu, 2)), fmt_num(cfg["cpus"])),
             "ram %s/%s" % (fmt_num(cfg["ram"] - cfg["ram_reserve"] - use.ram),
                            fmt_num(cfg["ram"] - cfg["ram_reserve"]))]
    if cfg["token_counts"]:
        parts.append("tokens " + " ".join("%s:%d/%d" % (n, c - use.tokens.get(n, 0), c)
                                          for n, c in sorted(cfg["token_counts"].items())))
    pending = [ctx.job(x) for x in ctx.queue]
    pending = [j for j in pending if j is not None and j.state in PENDING]
    parts.append("queue %d" % len(pending))
    waits = [parse_utc(j.info.get("waiting_since", "")) for j in pending]
    waits = [w for w in waits if w]
    if waits:
        parts.append("head_wait %s" % fmt_duration(time.time() - min(waits)))
    return " ".join(parts)


def cmd_free(store, a):
    store.init()
    out(free_line(store))
    return EXIT_OK


def daemon_status_line(store):
    if not store.daemon_running():
        return False, "daemon: not running"
    f = read_text(store.p("daemon.pid")).split()
    return True, "daemon: running, pid %s, jm %s" % (f[0] if f else "?", f[2] if len(f) > 2 else "?")


def cmd_status(store, a):
    store.init()
    if not a.id:
        out(daemon_status_line(store)[1])
        out("home: %s" % store.home)
        machine = read_text(store.p("machine")).split()
        if machine:
            out("machine: %s since %s" % (machine[0], machine[1] if len(machine) > 1 else "?"))
        out("free: %s" % free_line(store))
        counts = {}
        for jid in store.job_ids():
            st = Job(store, jid).state
            counts[st] = counts.get(st, 0) + 1
        out("jobs: " + (", ".join("%s %d" % (s, counts[s]) for s in STATES if s in counts) or "none"))
        return EXIT_OK
    ctx = Ctx(store)
    j = ctx.need(a.id)
    s = j.spec
    out("id: %s" % j.id)
    if s["name"]:
        out("name: %s" % s["name"])
    out("state: %s%s" % (j.state, (" (%s)" % j.reason) if j.reason else ""))
    if j.parent:
        out("parent: %s" % j.parent)
    if j.children():
        out("children: %s" % " ".join("%s:%s" % (c, ctx.job(c).state if ctx.job(c) else "?")
                                      for c in j.children()))
    need = ["cpu %s" % fmt_num(s["cpu"]), "ram %s" % fmt_num(s["ram"])]
    if s["disk"]:
        need.append("disk %s @ %s" % (fmt_num(s["disk"][0]), s["disk"][1] or job_cwd(store, j)))
    if s["tokens"]:
        need.append("tokens " + ",".join("%s:%d" % t for t in s["tokens"]))
    out("needs: " + " ".join(need))
    if s["after"]:
        out("after: %s (after_ok = %s)" % (" ".join(s["after"]), "yes" if s["after_ok"] else "no"))
    out("cwd: %s" % job_cwd(store, j))
    out("start: %s" % mask_secrets(s["start"] or "-", ctx.cfg))
    if s["resume"]:
        out("resume: %s" % mask_secrets(s["resume"], ctx.cfg))
    if s["applog"]:
        out("applog: %s" % mask_secrets(s["applog"], ctx.cfg))
    env_bits = []
    if s["env"]:
        env_bits.append("env " + " ".join(k for k, _ in s["env"]))
    if s["env_pass"]:
        env_bits.append("env_pass " + " ".join(s["env_pass"]))
    if s["env_file"]:
        env_bits.append("env_file " + " ".join(s["env_file"]))
    if env_bits:
        out("environment: " + "; ".join(env_bits))
    extra = []
    for key in ("retries", "timeout", "on_boot", "priority", "estimate"):
        v = s[key]
        if v not in (None, 0, "resume"):
            extra.append("%s %s" % (key, fmt_duration(v) if key in ("timeout", "estimate") else v))
    if j.info.get("retries_used", "0") not in ("0", ""):
        extra.append("retries_used %s" % j.info["retries_used"])
    if extra:
        out("options: " + ", ".join(extra))
    if s["note"]:
        out("note: %s" % s["note"])
    runs = read_text(j.path("runs")).splitlines()
    if j.cmd_active():
        runs.append("%s %s - running %s" % (j.info.get("run"), j.info.get("run_start"), j.info.get("how")))
    if runs:
        out("runs (n start end exit how):")
        for r in runs[-5:]:
            out("  " + r)
    if j.cmd_active():
        child = read_text(j.path("child")).split()
        u = group_usage(child[0]) if child else None
        if u:
            out("live: rss %d MB%s (declared ram %s, cpu %s)" % (
                u[0], "" if u[1] is None else ", cpu %d%%" % u[1], fmt_num(s["ram"]), fmt_num(s["cpu"])))
    prog = job_progress(j)
    if prog:
        out("progress: %s" % prog)
    if IS_WIN and s["env_file"]:
        out("warning: env_file permissions are not checked on Windows")
    for name in ("log", "stderr") if s["split_stderr"] else ("log",):
        tail = read_text(j.path(name)).splitlines()[-a.lines:] if a.lines else []
        if tail:
            out("%s (last %d lines):" % (name, len(tail)))
            for line in tail:
                out("  " + mask_secrets(line, ctx.cfg))
    return EXIT_OK


# logs: `jm log` (what jm captured) and `jm applog` (what the job's own `applog` command prints) ---------------

def log_jobs(ctx, j, with_children):
    """The job, then with --children its descendants, depth first."""
    return [j] + ([ctx.job(c) for c in ctx.descendants(j.id)] if with_children else [])


def log_show(a, cfg):
    if a.raw and not sys.stdout.isatty():
        raise JmError("--raw shows unmasked secrets and needs a terminal; refused for captured output",
                      EXIT_STATE)
    return (lambda s: s) if a.raw else (lambda s: mask_secrets(s, cfg))


class Sections(object):
    """Writes several jobs' output with a `==> ID <==` line whenever the job changes (as `tail` does for several
    files); with one job and no --children, the output is written as is."""

    def __init__(self, multi, show):
        self.multi = multi
        self.show = show
        self.last = None

    def write(self, jid, text):
        if not text:
            return
        if self.multi:
            if jid != self.last:
                sys.stdout.write("==> %s <==\n" % jid)
            if not text.endswith("\n"):
                text += "\n"  # the next section must start on its own line
        self.last = jid
        sys.stdout.write(self.show(text.replace("\r\n", "\n")))
        sys.stdout.flush()


def log_files(ctx, j, a, warned):
    """(id, path) of the files `jm log` shows. Without split_stderr a job has no separate stderr: both streams
    are in its `log`, so --stderr skips it and --stdout says so (once per job; a lone job --stderr is an error)."""
    files = []
    for k in log_jobs(ctx, j, a.children):
        split = k.spec["split_stderr"]
        if (a.stdout or (a.stderr and a.children)) and not split and k.id not in warned:
            warned.add(k.id)
            warn("%s: split_stderr = no: stdout and stderr are both in its log%s" % (
                k.id, "; skipped" if a.stderr else ""))
        if a.stderr and not split:
            continue
        files.append((k.id, k.path("stderr" if a.stderr else "log")))
    return files


def new_decoder():
    return codecs.getincrementaldecoder("utf-8")("replace")


def cmd_log(store, a):
    store.init()
    ctx = Ctx(store)
    j = ctx.need(a.id)
    show = log_show(a, ctx.cfg)
    warned = set()
    files = log_files(ctx, j, a, warned)
    if a.stderr and not files:
        raise JmError("%s: no separate stderr (spec: split_stderr = yes); `jm log %s` shows both streams" % (
            j.id, j.id))
    sec = Sections(a.children, show)
    # Positions are byte offsets in the file (a text-mode read would count \r\n as one on Windows).
    pos = {}
    decoders = {}
    partial = {}
    for jid, path in files:
        try:
            with open(path, "rb") as f:
                data = f.read()
        except (IOError, OSError):
            data = b""
        pos[path] = len(data)
        decoders[path] = new_decoder()
        text = decoders[path].decode(data)
        partial[path] = ""
        if a.follow and not text.endswith("\n"):
            # Hold back an unfinished last line: it is written once complete, in one piece.
            text, partial[path] = text.rsplit("\n", 1) if "\n" in text else ("", text)
            text = text + "\n" if text else ""
        lines = text.splitlines(True)
        if a.lines is not None:
            lines = lines[-a.lines:] if a.lines > 0 else []
        sec.write(jid, "".join(lines))
    if not a.follow:
        return EXIT_OK
    idle = 0
    while True:
        if a.children:
            files = log_files(Ctx(store), j, a, warned)  # a running group may have new children
        got = False
        for jid, path in files:
            try:
                size = os.path.getsize(path)
                if path not in pos or size < pos[path]:  # a new child, or rotated (log_max) at a new run
                    pos[path] = 0
                    decoders[path] = new_decoder()
                    partial[path] = ""
                if size == pos[path]:
                    continue
                with open(path, "rb") as f:
                    f.seek(pos[path])
                    chunk = f.read()
            except (IOError, OSError):
                continue
            pos[path] += len(chunk)
            got = True
            buf = partial[path] + decoders[path].decode(chunk)
            if "\n" in buf:
                done, buf = buf.rsplit("\n", 1)
                sec.write(jid, done + "\n")
            partial[path] = buf
        if got:
            idle = 0
        else:
            idle += 1
            if idle >= 5 and all(Job(store, jid).state in ENDED for jid, _ in files):
                for jid, path in files:
                    sec.write(jid, partial.get(path, ""))
                return EXIT_OK
        time.sleep(0.2)


def applog_env(store, cfg, j, a):
    """The job's `env` lines and JM_* variables, but no env_pass / env_file: an applog's output is printed, often
    into a captured session, so it gets no secret it could print (design sec 16)."""
    env = base_env(cfg)
    for k, v in j.spec["env"]:
        env[k] = expand_vars(v, env)
    env.update({"JM_ID": j.id, "JM_NAME": j.name(), "JM_HOME": store.home, "JM_RUN": j.info.get("run", "0"),
                "JM_HOW": j.info.get("how", "start"), "JM_PARENT": j.parent or "", "JM_STATE": j.state,
                "JM_JM": shell_join([sys.executable, JM_PATH]),
                "JM_LINES": "" if a.lines is None else str(max(a.lines, 0)), "JM_FOLLOW": "1" if a.follow else ""})
    return env


def applog_start(store, cfg, j, a):
    cwd = job_cwd(store, j)
    if not os.path.isdir(cwd):
        raise JmError("cwd %s does not exist" % cwd)
    # Its own process group, so a `tail -f` started by the shell goes too when jm ends it (Ctrl-C, closed pipe).
    kw = dict(shell=True, env=applog_env(store, cfg, j, a), cwd=cwd, stdin=subprocess.DEVNULL,
              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if IS_WIN:
        kw["creationflags"] = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(j.spec["applog"], **kw)


def applog_lines(p):
    dec = new_decoder()
    for line in iter(p.stdout.readline, b""):
        yield dec.decode(line)


def cmd_applog(store, a):
    store.init()
    ctx = Ctx(store)
    j = ctx.need(a.id)
    show = log_show(a, ctx.cfg)
    jobs = [k for k in log_jobs(ctx, j, a.children) if k.spec["applog"]]
    if not jobs:
        raise JmError("%s%s: no `applog` command in the spec (applog = a command that prints the job's own log)" % (
            j.id, " and its children" if a.children else ""))
    sec = Sections(a.children, show)
    procs = []
    bad = []
    ended = set()  # follow: commands jm ended because their job had ended

    def start(k):
        try:
            p = applog_start(store, ctx.cfg, k, a)
        except (JmError, OSError) as e:
            warn("applog of %s: %s" % (k.id, e))
            bad.append(k.id)
            return None
        procs.append((k, p))
        return p
    try:
        if a.follow:
            # All commands run at once; lines are written as they come, a `==> ID <==` line marking each switch.
            # As with `jm log -f`, a command is ended once its job has ended and it printed nothing for a second,
            # so `tail -f` in an applog needs no way to tell when to stop.
            lines = queue.Queue()
            heard = {}

            def pump(jid, p):
                for line in applog_lines(p):
                    lines.put((jid, line))
                lines.put((jid, None))
            for k in jobs:
                p = start(k)
                if p is not None:
                    heard[k.id] = time.time() + 1.0  # quiet counts from its start, plus a second to start up
                    t = threading.Thread(target=pump, args=(k.id, p))
                    t.daemon = True
                    t.start()
            running = len(procs)
            checked = 0
            while running:
                try:
                    jid, line = lines.get(timeout=0.2)  # a timeout keeps Ctrl-C working on Windows
                    if line is None:
                        running -= 1
                    else:
                        heard[jid] = time.time()
                        sec.write(jid, line)
                except queue.Empty:
                    pass
                now = time.time()
                if now - checked < 0.5:
                    continue
                checked = now
                for k, p in procs:
                    if k.id not in ended and p.poll() is None and now - heard.get(k.id, 0) >= 1.0 and \
                            Job(store, k.id).state in ENDED:
                        ended.add(k.id)
                        kill_group(p.pid, hard=True)
        else:
            for k in jobs:
                p = start(k)
                if p is None:
                    continue
                if a.lines is None:
                    for line in applog_lines(p):
                        sec.write(k.id, line)
                else:
                    sec.write(k.id, "".join(collections.deque(applog_lines(p), maxlen=max(a.lines, 0))))
                p.wait()
        for k, p in procs:
            rc = p.wait()
            if rc and k.id not in ended:
                warn("applog of %s exited %d" % (k.id, rc))
                bad.append(k.id)
    finally:
        for k, p in procs:
            if p.poll() is None:
                kill_group(p.pid, hard=True)
    return EXIT_ERR if bad else EXIT_OK


def cmd_wait(store, a):
    store.init()
    jid = Ctx(store).need(a.id).id
    deadline = None if a.timeout is None else time.time() + parse_duration(a.timeout)
    while True:
        j = Job(store, jid)
        if j.state in ENDED:
            if j.state == DONE:
                return EXIT_OK
            ex = j.exitinfo()
            runs = read_text(j.path("runs")).split()
            rc = ex[0] if ex else None
            if rc is None and len(runs) >= 4 and runs[-2].isdigit():
                rc = int(runs[-2])
            warn("%s %s%s" % (jid, j.state, (": " + j.reason) if j.reason else ""))
            return rc if rc and 0 < rc < 256 else 1
        if deadline is not None and time.time() >= deadline:
            warn("timeout: %s is still %s" % (jid, j.state))
            return EXIT_ERR
        time.sleep(0.2)


def cmd_move(store, a):
    def go(ctx):
        if a.by_priority:
            prio = {}
            for x in ctx.queue:
                prio[x] = ctx.job(x).spec["priority"] if ctx.job(x) else 0
            ctx.queue = sorted(ctx.queue, key=lambda x: -prio[x])
            ctx.queue_dirty = True
            return
        if not a.id:
            raise JmError("move: give ID (or --by-priority)", EXIT_USAGE)
        j = ctx.need(a.id)
        if j.id not in ctx.queue:
            raise JmError("%s is not in the queue (%s)" % (j.id, j.state), EXIT_STATE)
        i = ctx.queue.index(j.id)
        if a.top:
            pos = 0
        elif a.bottom:
            pos = len(ctx.queue)
        elif a.up is not None:
            pos = max(0, i - a.up)
        elif a.down is not None:
            pos = i + a.down
        elif a.before or a.after:
            other = ctx.need(a.before or a.after)
            if other.id not in ctx.queue or other.id == j.id:
                raise JmError("%s is not another queued job" % other.id, EXIT_STATE)
            ctx.q_remove(j.id)
            pos = ctx.queue.index(other.id) + (0 if a.before else 1)
        else:
            raise JmError("move: give --top, --bottom, --up, --down, --before or --after", EXIT_USAGE)
        ctx.q_insert(j.id, pos)
        j.event("moved to position %d" % (ctx.queue.index(j.id) + 1))
    mutate(store, go)
    return EXIT_OK


def cmd_swap(store, a):
    def go(ctx):
        j1, j2 = ctx.need(a.id1), ctx.need(a.id2)
        for j in (j1, j2):
            if j.id not in ctx.queue:
                raise JmError("%s is not in the queue" % j.id, EXIT_STATE)
        i1, i2 = ctx.queue.index(j1.id), ctx.queue.index(j2.id)
        ctx.queue[i1], ctx.queue[i2] = j2.id, j1.id
        ctx.queue_dirty = True
        j1.event("swapped with %s" % j2.id)
        j2.event("swapped with %s" % j1.id)
    mutate(store, go)
    return EXIT_OK


def cmd_hold(store, a):
    def go(ctx):
        j = ctx.need(a.id)
        if j.state in PENDING:
            ctx.set_state(j, HELD, "held by hand")
        elif j.state == RUNNING:
            j.info["hold_after"] = "yes"
            j.save_info()
            j.event("hold asked while running: held if it goes back to the queue")
            out("%s is running; it will be held if it goes back to the queue (retry)" % j.id)
        elif j.state != HELD:
            raise JmError("%s is %s; cannot hold" % (j.id, j.state), EXIT_STATE)
    mutate(store, go)
    return EXIT_OK


def cmd_release(store, a):
    def go(ctx):
        j = ctx.need(a.id)
        if j.state == HELD:
            ctx.set_state(j, QUEUED, "released")
        elif j.state == RUNNING and j.info.get("hold_after"):
            j.info.pop("hold_after", None)
            j.save_info()
            j.event("released (hold after run cancelled)")
        else:
            raise JmError("%s is %s, not held (stopped jobs: jm requeue)" % (j.id, j.state), EXIT_STATE)
    mutate(store, go)
    return EXIT_OK


def request_stop(ctx, j, after, reason, extra=None):
    """Ask a running job's wrapper to stop; the daemon applies `after` once it has ended."""
    req = {"after": after, "reason": reason}
    req.update(extra or {})
    write_kv_file(j.path("stop_req"), req)
    if os.path.exists(j.path("pid")):
        ctx.set_state(j, STOPPING, "%s: stopping" % reason, "stop requested (then %s)" % after)
    else:
        remove_file(j.path("stop_req"))
        apply_stop_request(ctx, j, req, None)


def family_guard(ctx, j, with_children, verb):
    kids = [ctx.job(c) for c in ctx.descendants(j.id)]
    open_kids = [k for k in kids if k is not None and k.state not in TERMINAL]
    if not open_kids or with_children:
        return open_kids if with_children else []
    if confirm("%s has %d unfinished children (%s); %s them too?" % (
            j.id, len(open_kids), " ".join(k.id for k in open_kids), verb)):
        return open_kids
    raise JmError("%s has %d unfinished children; add --children to %s them too" % (
        j.id, len(open_kids), verb), EXIT_STATE)


def stop_one(ctx, j, after="stopped", reason="stopped by hand"):
    if j.state in (RUNNING, STOPPING):
        if j.state == STOPPING and os.path.exists(j.path("stop_req")):
            req = read_kv_file(j.path("stop_req"))
            req.update({"after": after, "reason": reason})
            write_kv_file(j.path("stop_req"), req)
            return
        request_stop(ctx, j, after, reason)
    elif j.state in PENDING + (HELD, STOPPED):
        if after == "cancel":
            ctx.set_state(j, CANCELLED, reason)
        elif j.state != STOPPED:
            ctx.set_state(j, STOPPED, reason)


def cmd_stop(store, a):
    def go(ctx):
        j = ctx.need(a.id)
        if j.state in TERMINAL:
            raise JmError("%s is already %s" % (j.id, j.state), EXIT_STATE)
        kids = family_guard(ctx, j, a.children, "stop")
        for k in reversed(kids):
            stop_one(ctx, k)
        stop_one(ctx, j)
    mutate(store, go)
    return EXIT_OK


def cmd_cancel(store, a):
    def go(ctx):
        j = ctx.need(a.id)
        if j.state in TERMINAL:
            raise JmError("%s is already %s" % (j.id, j.state), EXIT_STATE)
        kids = family_guard(ctx, j, a.children, "cancel")
        for k in reversed(kids):
            stop_one(ctx, k, "cancel", "cancelled with parent %s" % j.id)
        stop_one(ctx, j, "cancel", "cancelled by hand")
    mutate(store, go)
    return EXIT_OK


def cmd_restart(store, a):
    def go(ctx):
        j = ctx.need(a.id)
        if j.state != RUNNING or not os.path.exists(j.path("pid")):
            raise JmError("%s is %s; restart is for a running job (else: jm requeue)" % (j.id, j.state),
                          EXIT_STATE)
        request_stop(ctx, j, "restart", "restart by hand")
    mutate(store, go)
    return EXIT_OK


def cmd_requeue(store, a):
    def go(ctx):
        j = ctx.need(a.id)
        if a.spec_edit:
            if j.state in (RUNNING, STOPPING):
                raise JmError("%s is %s; --spec-edit needs it ended (jm stop first)" % (j.id, j.state),
                              EXIT_STATE)
            set_spec(ctx, j, a.spec_edit)
        how = "start" if a.fresh else "resume"
        targets = [j]
        if a.children:
            targets += [ctx.job(c) for c in ctx.descendants(j.id)]
        for t in targets:
            if t is None or (t is not j and t.state == DONE):
                continue
            t.info["retries_used"] = "0"
            t.save_info()
            if t.state in (RUNNING, STOPPING) and os.path.exists(t.path("pid")):
                request_stop(ctx, t, "requeue", "requeued by hand",
                             {"pos": "top" if a.top else ("end" if a.end else ""), "fresh": "yes" if a.fresh else ""})
                continue
            if a.top:
                p = 0
            elif a.end:
                p = len(ctx.queue)
            elif t.id in ctx.queue:
                p = None
            elif t is not j and t.parent:
                p = ctx.q_after_family(t.parent)
            else:
                p = len(ctx.queue)
            ctx.requeue(t, how, p, "requeued by hand")
    mutate(store, go)
    return EXIT_OK


def set_spec(ctx, j, items):
    assigns = parse_assignments(items)
    if [k for k, _, _ in assigns if k == "id"]:
        raise JmError("the id cannot be changed", EXIT_STATE)
    text = apply_assignments(j.spec_text, assigns, SPEC_KEYS)
    check_spec(parse_spec(text, "%s/spec" % j.id), ctx.cfg, j.id)
    write_atomic(j.path("spec"), text)
    j._spec = None
    j.event("spec changed: " + " ".join(k for k, _, _ in assigns))


def cmd_set(store, a):
    def go(ctx):
        j = ctx.need(a.id)
        if j.state in (RUNNING, STOPPING):
            raise JmError("%s is %s; its spec can change once it has ended" % (j.id, j.state), EXIT_STATE)
        set_spec(ctx, j, a.assign)
    mutate(store, go)
    return EXIT_OK


def cmd_edit(store, a):
    store.init()
    j = Ctx(store).need(a.id)
    if j.state in (RUNNING, STOPPING):
        raise JmError("%s is %s; its spec can change once it has ended" % (j.id, j.state), EXIT_STATE)
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("notepad" if IS_WIN else "vi")
    fd, tmp = tempfile.mkstemp(prefix="jm-%s-" % j.id, suffix=".jm")
    os.close(fd)
    try:
        write_atomic(tmp, j.spec_text)
        rc = subprocess.call("%s %s" % (editor, shell_join([tmp])), shell=True)
        if rc != 0:
            raise JmError("editor exited %d; spec unchanged" % rc)
        text = read_text(tmp)

        def go(ctx):
            jj = ctx.need(j.id)
            if jj.state in (RUNNING, STOPPING):
                raise JmError("%s started meanwhile; spec unchanged" % j.id, EXIT_STATE)
            spec = parse_spec(text, "%s/spec" % j.id)
            if spec["id"] not in (None, j.id):
                raise JmError("the id cannot be changed", EXIT_STATE)
            check_spec(spec, ctx.cfg, j.id)
            write_atomic(jj.path("spec"), text)
            jj.event("spec edited")
        mutate(store, go)
    finally:
        remove_file(tmp)
    return EXIT_OK


def cmd_pause(store, a, cont=False):
    if IS_WIN:
        raise JmError("pause / cont are Unix only", EXIT_ERR)

    def go(ctx):
        j = ctx.need(a.id)
        child = read_text(j.path("child")).split()
        if j.state != RUNNING or not j.cmd_active() or len(child) < 2 or not proc_alive(child[0], child[1]):
            raise JmError("%s is not running a command" % j.id, EXIT_STATE)
        os.killpg(int(child[0]), signal.SIGCONT if cont else signal.SIGSTOP)
        if cont:
            j.info.pop("paused", None)
        else:
            j.info["paused"] = utc()
        j.save_info()
        j.event("continued" if cont else "paused (SIGSTOP)")
        ctx.set_reason(j, "run %s (%s)%s" % (j.info.get("run"), j.info.get("how"), "" if cont else ", paused"))
    mutate(store, go)
    return EXIT_OK


def cmd_config(store, a):
    store.init()
    if not a.assign:
        cfg = store.config()
        for key, kind, _ in CONFIG_KEYS:
            v = cfg[key]
            if kind in LIST_KINDS:
                for x in v:
                    out("%s = %s" % (key, "%s=%s" % x if kind == "env" else ("%s:%d" % x if kind == "tokens"
                                                                             else x)))
            elif v is None:
                out("%s =" % key)
            elif kind == "dur":
                out("%s = %s" % (key, fmt_num(v) + "s"))
            elif kind == "bool":
                out("%s = %s" % (key, "yes" if v else "no"))
            else:
                out("%s = %s" % (key, fmt_num(v) if isinstance(v, float) else v))
        return EXIT_OK

    def go(ctx):
        text = apply_assignments(read_text(store.p("config")), parse_assignments(a.assign), CONFIG_KEYS)
        cfg = parse_config(text)
        for k, v in cfg["env"]:
            if is_secret_name(k, cfg) or has_secret_value(v, cfg):
                raise JmError("config env %s looks like a secret; %s" % (k, SAFE_WAYS))
        write_atomic(store.p("config"), text)
    mutate(store, go)
    return EXIT_OK


def cmd_clean(store, a):
    store.init()

    def go(ctx):
        older = parse_duration(a.older) if a.older else ctx.cfg["keep"]
        now = time.time()
        gone = []
        for jid in store.job_ids():
            j = ctx.job(jid)
            if j is None or j.state not in TERMINAL:
                continue
            if [c for c in ctx.descendants(jid) if ctx.job(c).state not in TERMINAL]:
                continue
            ended = parse_utc(j.info.get("ended", "")) or 0
            if now - ended >= older:
                shutil.rmtree(j.dir, ignore_errors=True)
                gone.append(jid)
        return gone
    for jid in mutate(store, go):
        out("removed %s" % jid)
    return EXIT_OK


def cmd_artifacts(store, a):
    store.init()
    j = Ctx(store).need(a.id)
    cfg = store.config()
    env = base_env(cfg)
    for k, v in j.spec["env"]:
        env[k] = expand_vars(v, env)
    env.update({"JM_ID": j.id, "JM_HOME": store.home})
    for pattern in j.spec["artifacts"]:
        path = expand_vars(pattern, env)
        if not os.path.isabs(path):
            path = os.path.join(job_cwd(store, j), path)
        if os.path.exists(path):
            out("%s\t%s\t%s" % (path, "dir" if os.path.isdir(path) else "%d" % os.path.getsize(path),
                                utc(os.path.getmtime(path))))
        else:
            out("%s\tmissing\t-" % path)
    return EXIT_OK


# daemon management ---------------------------------------------------------------------------------------------

def jm_argv(store):
    return [sys.executable, JM_PATH, "--home", store.home]


def daemon_start(store, quiet=False):
    store.init()
    if store.daemon_running():
        if not quiet:
            out(daemon_status_line(store)[1])
        return EXIT_OK
    log = open(store.p("daemon.log"), "ab")
    try:
        p = spawn_detached(jm_argv(store) + ["daemon", "run"], stdout=log, cwd=store.home)
    finally:
        log.close()
    deadline = time.time() + 15
    while time.time() < deadline:
        if store.daemon_running() and os.path.exists(store.p("daemon.pid")):
            if not quiet:
                out(daemon_status_line(store)[1])
            return EXIT_OK
        if p.poll() is not None:
            break
        time.sleep(0.05)
    raise JmError("the daemon did not start; see %s" % store.p("daemon.log"))


def daemon_stop(store, quiet=False):
    store.init()
    if not store.daemon_running():
        if not quiet:
            out("daemon: not running")
        return EXIT_OK
    write_atomic(store.p("daemon.stop"), utc() + "\n")
    store.wake()
    deadline = time.time() + 30
    while time.time() < deadline:
        if not store.daemon_running():
            if not quiet:
                out("daemon: stopped (running jobs keep running)")
            return EXIT_OK
        time.sleep(0.05)
    f = read_text(store.p("daemon.pid")).split()
    if f and not IS_WIN and proc_alive(f[0], f[1]):
        os.kill(int(f[0]), signal.SIGTERM)
        time.sleep(1)
    if store.daemon_running():
        raise JmError("the daemon did not stop")
    return EXIT_OK


SYSTEMD_UNIT = """[Unit]
Description=jm job manager daemon (%(home)s)

[Service]
Type=simple
ExecStart=%(cmd)s daemon run
Restart=on-failure
RestartSec=10
# Only the daemon is stopped with the service: running jobs (in their own sessions) keep running.
KillMode=process

[Install]
WantedBy=default.target
"""

LAUNCHD_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>%(label)s</string>
  <key>ProgramArguments</key>
  <array>
%(args)s
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>AbandonProcessGroup</key><true/>
  <key>StandardErrorPath</key><string>%(log)s</string>
</dict>
</plist>
"""

WINDOWS_TASK = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>jm job manager daemon (%(home)s)</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>%(python)s</Command>
      <Arguments>%(args)s</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def xml_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def daemon_install(store, kind, out_path=None):
    store.init()
    argv = jm_argv(store)
    if kind == "systemd":
        path = out_path or os.path.join(os.path.expanduser("~"), ".config", "systemd", "user", "jm.service")
        text = SYSTEMD_UNIT % {"home": store.home, "cmd": " ".join(shlex.quote(x) for x in argv)}
        how = ["systemctl --user daemon-reload", "systemctl --user enable --now jm.service",
               "loginctl enable-linger $USER    # keep it running without a login session"]
    elif kind == "launchd":
        label = "io.github.blue-wind-25.jm"
        path = out_path or os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", label + ".plist")
        args = "\n".join("    <string>%s</string>" % xml_escape(x) for x in argv + ["daemon", "run"])
        text = LAUNCHD_PLIST % {"label": label, "args": args, "log": xml_escape(store.p("daemon.log"))}
        how = ["launchctl load -w %s" % shlex.quote(path)]
    elif kind == "windows":
        path = out_path or store.p("jm-task.xml")
        text = WINDOWS_TASK % {"home": xml_escape(store.home), "python": xml_escape(argv[0]),
                               "args": xml_escape(subprocess.list2cmdline(argv[1:] + ["daemon", "run"]))}
        how = ['schtasks /Create /TN jm /XML "%s"' % path, "schtasks /Run /TN jm"]
    else:
        raise JmError("daemon install: systemd, launchd or windows", EXIT_USAGE)
    d = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(d):
        os.makedirs(d)
    if kind == "windows":
        with open(path, "w", encoding="utf-16") as f:
            f.write(text)
    else:
        write_atomic(path, text)
    out("wrote %s" % path)
    out("enable it with:")
    for line in how:
        out("  " + line)
    return EXIT_OK


def cmd_daemon(store, a):
    if a.action == "start":
        return daemon_start(store, a.quiet)
    if a.action == "stop":
        return daemon_stop(store, a.quiet)
    if a.action == "restart":
        daemon_stop(store, True)
        return daemon_start(store, a.quiet)
    if a.action == "status":
        ok, line = daemon_status_line(store)
        out(line)
        return EXIT_OK if ok else EXIT_ERR
    if a.action == "run":
        daemon_run(store, a.quiet)
        return EXIT_OK
    if a.action == "install":
        return daemon_install(store, a.kind, a.out)
    if a.action == "bashrc":
        out("# jm: start the daemon at login if it is not running")
        out("%s daemon start --quiet >/dev/null 2>&1" % " ".join(shlex.quote(x) for x in jm_argv(store)))
        return EXIT_OK
    raise JmError("daemon: start, stop, restart, status, run, install or bashrc", EXIT_USAGE)


# argument parsing ----------------------------------------------------------------------------------------------

class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        raise JmError(message, EXIT_USAGE)


def build_parser():
    p = Parser(prog="jm", description="jm - a single-machine job manager")
    p.add_argument("--home", help="state dir (default: $JM_HOME or ~/.jm)")
    p.add_argument("--version", action="version", version="jm " + VERSION)
    sub = p.add_subparsers(dest="cmd", parser_class=Parser)

    d = sub.add_parser("daemon", help="start | stop | restart | status | run | install KIND | bashrc")
    d.add_argument("action", choices=["start", "stop", "restart", "status", "run", "install", "bashrc"])
    d.add_argument("kind", nargs="?", choices=["systemd", "launchd", "windows"])
    d.add_argument("--out", help="install: write the service file here instead of the default place")
    d.add_argument("-q", "--quiet", action="store_true")

    s = sub.add_parser("submit", help="submit FILE | - | [options] -- COMMAND...")
    s.add_argument("file", nargs="?")
    s.add_argument("--id")
    s.add_argument("--name")
    s.add_argument("--cpu")
    s.add_argument("--ram")
    s.add_argument("--disk")
    s.add_argument("--timeout")
    s.add_argument("--retries")
    s.add_argument("--cwd")
    s.add_argument("--tokens", action="append")
    s.add_argument("--env", action="append")
    s.add_argument("--dep", action="append", help="an `after` dependency (repeatable)")
    s.add_argument("--set", action="append", metavar="KEY=VALUE", help="any spec key (repeatable)")
    s.add_argument("--parent")
    s.add_argument("--hold", action="store_true")
    s.add_argument("--continue-on-fail", action="store_true",
                   help="a group: a failing child does not stop its siblings (spec: continue_on_fail = yes)")
    pos = s.add_mutually_exclusive_group()
    pos.add_argument("--top", action="store_true")
    pos.add_argument("--after", dest="after_job", metavar="ID", help="queue it right after job ID")
    pos.add_argument("--end", action="store_true")

    li = sub.add_parser("list", help="the queue and recently finished jobs")
    li.add_argument("--all", action="store_true")
    li.add_argument("--tree", action="store_true")
    li.add_argument("--tag")

    st = sub.add_parser("status", help="one job, or the daemon and the machine")
    st.add_argument("id", nargs="?")
    st.add_argument("-n", "--lines", type=int, default=10)

    sub.add_parser("free", help="free capacity and queue size")

    lg = sub.add_parser("log", help="a job's captured stdout + stderr (jm's banners included)")
    al = sub.add_parser("applog", help="run the job's `applog` command: the log the job keeps itself")
    for x in (lg, al):
        x.add_argument("id")
        x.add_argument("-f", "--follow", action="store_true")
        x.add_argument("-n", "--lines", type=int)
        x.add_argument("--raw", action="store_true", help="unmasked (terminal only)")
        x.add_argument("--children", action="store_true", help="also its children, one `==> ID <==` section each")
    g = lg.add_mutually_exclusive_group()
    g.add_argument("--stdout", action="store_true", help="stdout only (without split_stderr the log holds both)")
    g.add_argument("--stderr", action="store_true", help="stderr only (needs split_stderr = yes in the spec)")

    w = sub.add_parser("wait", help="block until the job ends; exit code = the job's")
    w.add_argument("id")
    w.add_argument("--timeout")

    mv = sub.add_parser("move", help="reorder the queue")
    mv.add_argument("id", nargs="?")
    g = mv.add_mutually_exclusive_group()
    g.add_argument("--top", action="store_true")
    g.add_argument("--bottom", action="store_true")
    g.add_argument("--up", type=int, nargs="?", const=1)
    g.add_argument("--down", type=int, nargs="?", const=1)
    g.add_argument("--before", metavar="ID2")
    g.add_argument("--after", metavar="ID2")
    g.add_argument("--by-priority", action="store_true", help="stable-sort the queue by priority")

    sw = sub.add_parser("swap", help="swap two queued jobs")
    sw.add_argument("id1")
    sw.add_argument("id2")

    for name in ("hold", "release", "restart", "pause", "cont"):
        x = sub.add_parser(name)
        x.add_argument("id")
    for name in ("stop", "cancel"):
        x = sub.add_parser(name)
        x.add_argument("id")
        x.add_argument("--children", action="store_true")

    rq = sub.add_parser("requeue", help="back to the queue (stops it first if running)")
    rq.add_argument("id")
    g = rq.add_mutually_exclusive_group()
    g.add_argument("--top", action="store_true")
    g.add_argument("--end", action="store_true")
    rq.add_argument("--fresh", action="store_true", help="run `start`, not `resume`")
    rq.add_argument("--children", action="store_true", help="also requeue its unfinished children")
    rq.add_argument("--spec-edit", action="append", metavar="KEY=VALUE")

    ed = sub.add_parser("edit", help="open the spec in $EDITOR")
    ed.add_argument("id")
    se = sub.add_parser("set", help="change spec keys: key=value, key+=value (lists), key= (default)")
    se.add_argument("id")
    se.add_argument("assign", nargs="+")
    cf = sub.add_parser("config", help="show / change the machine config")
    cf.add_argument("assign", nargs="*")
    cl = sub.add_parser("clean", help="drop finished jobs' dirs")
    cl.add_argument("--older", help="default: config keep (7d)")
    ar = sub.add_parser("artifacts", help="list a job's artifacts")
    ar.add_argument("id")

    r = sub.add_parser("_run")
    r.add_argument("id")
    r.add_argument("token")
    sim = sub.add_parser("_sim")
    sim.add_argument("--secs", type=float, default=1.0)
    sim.add_argument("--ram", type=float, default=0.0)
    sim.add_argument("--exit", type=int, default=0)
    sim.add_argument("--progress", action="store_true")
    sim.add_argument("--resumable")
    sim.add_argument("--trap", action="store_true", help="ignore SIGTERM")
    sim.add_argument("--print-env", action="append", default=[], metavar="NAME")
    sim.add_argument("--out", help="append the --print-env lines to this file instead of stdout")
    sim.add_argument("--stderr", metavar="TEXT", help="print this line to stderr first")
    return p


COMMANDS = {
    "submit": cmd_submit, "list": cmd_list, "status": cmd_status, "free": cmd_free, "log": cmd_log,
    "applog": cmd_applog,
    "wait": cmd_wait, "move": cmd_move, "swap": cmd_swap, "hold": cmd_hold, "release": cmd_release,
    "stop": cmd_stop, "cancel": cmd_cancel, "restart": cmd_restart, "requeue": cmd_requeue, "set": cmd_set,
    "edit": cmd_edit, "config": cmd_config, "clean": cmd_clean, "artifacts": cmd_artifacts,
    "daemon": cmd_daemon, "pause": lambda s, a: cmd_pause(s, a), "cont": lambda s, a: cmd_pause(s, a, True),
}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        # `submit ... -- COMMAND`: argparse cannot mix REMAINDER with options, so split at the first `--`.
        command = None
        if "submit" in argv and "--" in argv and argv.index("--") > argv.index("submit"):
            k = argv.index("--")
            argv, command = argv[:k], argv[k + 1:]
        a = build_parser().parse_args(argv)
        if a.cmd is None:
            build_parser().print_help()
            return EXIT_USAGE
        if a.cmd == "submit":
            if command is not None and not command:
                raise JmError("nothing after `--`", EXIT_USAGE)
            a.command = command
        if a.cmd == "_sim":
            return sim_main(a)
        store = Store(a.home or default_home())
        if a.cmd == "_run":
            wrapper_run(store, a.id, a.token)
            return EXIT_OK
        if a.cmd == "daemon" and a.action == "install" and not a.kind:
            raise JmError("daemon install: systemd, launchd or windows", EXIT_USAGE)
        return COMMANDS[a.cmd](store, a)
    except JmError as e:
        warn(str(e))
        return e.code
    except KeyboardInterrupt:
        return EXIT_ERR
    except BrokenPipeError:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
