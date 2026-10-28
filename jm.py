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
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback

VERSION = "1.6.1"
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


_BOOT_ID = []


def boot_id():
    """Identifies this boot (Linux), so a time.monotonic() value kept in a file can be compared after a daemon
    restart; None elsewhere (there durations stored in files fall back to the wall clock)."""
    if not _BOOT_ID:
        _BOOT_ID.append((read_text("/proc/sys/kernel/random/boot_id").strip() or None) if IS_LINUX else None)
    return _BOOT_ID[0]


def stamp_now(info, key):
    """Record "now" in `info` as UTC text (for people and other boots) and, on Linux, as a monotonic stamp."""
    info[key] = utc()
    b = boot_id()
    if b:
        info[key + "_mono"] = "%s %.3f" % (b, time.monotonic())
    else:
        info.pop(key + "_mono", None)


def elapsed(info, key):
    """Seconds since the moment stamp_now() recorded, or None. The monotonic clock when the stamp is from this
    boot: a wall-clock jump (NTP setting the date on a Pi that booted without RTC) must not fire a timeout or
    the drain guard. Else the wall clock."""
    f = info.get(key + "_mono", "").split()
    if len(f) == 2 and f[0] == boot_id():
        try:
            return max(0.0, time.monotonic() - float(f[1]))
        except ValueError:
            pass
    t0 = parse_utc(info.get(key, ""))
    return None if t0 is None else max(0.0, time.time() - t0)


def pop_stamp(info, key):
    had = info.pop(key, None) is not None
    info.pop(key + "_mono", None)
    return had


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


def rotate_log(path, max_mb):
    """Between runs (nothing writes it): over `max_mb`, the log becomes PATH.1 and a new one starts."""
    try:
        if max_mb and os.path.getsize(path) > max_mb * 1048576:
            os.replace(path, path + ".1")
    except OSError:
        pass


def trim_log(path, max_mb):
    """While a log is written: over `max_mb`, copy it to PATH.1 and empty it. Its writers append (O_APPEND), so
    they go on at the new end; at most the lines written in between are lost. True when trimmed. Not on Windows,
    where a writer's own file position would leave a gap of NULs: logs rotate between runs only there."""
    if not max_mb or IS_WIN:
        return False
    try:
        if os.path.getsize(path) <= max_mb * 1048576:
            return False
        shutil.copyfile(path, path + ".1")
        with open(path, "r+b") as f:
            f.seek(os.path.getsize(path + ".1"))
            rest = f.read()  # written while it was copied
            f.truncate(0)
        if rest:
            with open(path + ".1", "ab") as f:
                f.write(rest)
        return True
    except (IOError, OSError):
        return False


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
        # Unix: flock needs no write access, and a read-only fd is no writer on the mount, so a hook can remount
        # JM_HOME read-only while the daemon holds its lock (RDD_014). A lock file that cannot be created on a
        # read-only home is granted without a file: nothing can change the state there either.
        try:
            fd = os.open(self.path, (os.O_RDWR if IS_WIN else os.O_RDONLY) | os.O_CREAT, 0o600)
        except OSError:
            if IS_WIN or os.path.exists(self.path) or os.access(os.path.dirname(self.path), os.W_OK):
                raise
            return True
        deadline = None if timeout is None else time.monotonic() + timeout
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
                if not blocking or (deadline is not None and time.monotonic() >= deadline):
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
        # Between fork and exec a Unix child still shows its parent's command line (the daemon writes a wrapper's
        # pid file right after the fork), so a miss is final only once it has held for a second. A Windows
        # process starts with its own command line.
        deadline = time.monotonic() + (0 if IS_WIN else 1.0)
        while True:
            cmd = proc_cmdline(pid)
            if cmd is None:
                return False
            if token in cmd or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
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
    ("done_check", "cmd", None), ("progress", "cmd", None), ("applog", "cmd", None), ("report", "cmd", None), ("split_stderr", "bool", False),
    ("pre", "cmd", None), ("post", "cmd", None), ("notify", "cmd", None),
    ("cpu", "num", 1.0), ("ram", "num", 0.0), ("disk", "disk", None), ("tokens", "tokens", None),
    ("after", "list", None), ("after_ok", "bool", True), ("priority", "int", 0), ("retries", "int", 0),
    ("timeout", "dur", None), ("on_boot", "enum:resume|hold|fail", "resume"), ("drain", "bool", False),
    ("children", "list", None), ("parallel", "int", 0), ("continue_on_fail", "bool", False),
    ("artifacts", "list", None), ("nice", "int", 5), ("log_max", "num", None), ("keep", "dur", None),
    ("estimate", "dur", None), ("tags", "list", None),
    ("class", "str", None), ("progress_re", "str", None), ("progress_total", "num", None),  # RDD_019
]
CONFIG_KEYS = [
    ("cpus", "num", None), ("ram", "num", None), ("ram_reserve", "num", 0.0), ("tokens", "tokens", None),
    ("backfill", "bool", True), ("drain_after", "dur", 1800.0), ("poll", "dur", 5.0),
    ("progress_every", "dur", 60.0), ("keep", "dur", 7 * 86400.0), ("env", "env", None),
    ("secret_names", "list", None), ("not_secret", "list", None), ("secret_prefixes", "list", None),
    ("on_first_start", "cmd", None), ("on_idle", "cmd", None), ("hook_timeout", "dur", 120.0),
    ("idle_after", "dur", 60.0), ("log_max", "num", 100.0), ("home_rw", "cmd", None),
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
# home_rw        = data-rw.sh    # any command: makes this state dir writable when it is read-only (on_idle)
# log_max        = 100         # MB per job log and for daemon.log (the older part goes to *.1); 0 = no limit
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

    def writable(self):
        return not os.path.isdir(self.home) or os.access(self.home, os.W_OK)

    def need_write(self):
        """Before any write: a read-only home (an on_idle hook remounted it) is made writable by the config's
        `home_rw` command first (RDD_014). Commands that only read work on a read-only home as it is."""
        if self.writable():
            return
        cfg = self.config()
        if not cfg["home_rw"]:
            raise JmError("%s is read-only; set home_rw in its config to a command that makes it writable"
                          % self.home)
        env = base_env(cfg)
        env.update({"JM_HOME": self.home, "JM_HOOK": "home_rw"})
        try:
            p = subprocess.Popen(cfg["home_rw"], shell=True, env=env, cwd=self.home, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                text, _ = p.communicate(timeout=cfg["hook_timeout"])
                rc = p.returncode
            except subprocess.TimeoutExpired:
                p.kill()
                text, _ = p.communicate()
                rc = 124
        except OSError as e:
            text, rc = str(e).encode("utf-8"), 127
        text = (text or b"").decode("utf-8", "replace").strip()
        if rc != 0 or not self.writable():
            raise JmError("home_rw (%s) exited %d and %s is still read-only%s" % (
                cfg["home_rw"], rc, self.home, (": " + text) if text else ""))
        self.log("home_rw: %s exited 0%s" % (cfg["home_rw"], ("\n" + text) if text else ""))

    def init(self):
        if not self.writable():
            return  # a read-only home: readers use what is there; writers call need_write() first
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
        try:
            append_line(self.p("daemon.log"), "%s %s" % (utc(), msg))
        except OSError:
            pass  # a read-only home (after on_idle): the line is lost, the daemon goes on


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
                pop_stamp(j.info, "waiting_since")
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
    # Its output through a pipe, not daemon.log: an on_idle hook may remount JM_HOME read-only, which fails
    # (EBUSY) while any file on it is open for writing (RDD_014).
    try:
        p = subprocess.Popen(cmd, shell=True, env=env, cwd=store.home, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            text, _ = p.communicate(timeout=cfg["hook_timeout"])
            rc = p.returncode
        except subprocess.TimeoutExpired:
            p.kill()
            text, _ = p.communicate()
            rc = 124
    except OSError as e:
        store.log("%s failed: %s" % (key, e))
        return 127
    text = (text or b"").decode("utf-8", "replace").rstrip()
    if text:
        store.log("%s output:\n%s" % (key, text))
    store.log("%s exited %d" % (key, rc))
    return rc


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
    if spec["progress_re"]:
        try:
            if not re.compile(spec["progress_re"]).groups:
                raise JmError("%s: progress_re needs a group: (done) or (done)...(total)" % what)
        except re.error as e:
            raise JmError("%s: progress_re: %s" % (what, e))
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
            record_history(ctx.store, j)
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
    j.info.update({"run": str(run), "how": how})
    stamp_now(j.info, "run_start")
    pop_stamp(j.info, "waiting_since")
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
                if t and (elapsed(j.info, "run_start") or 0.0) > t and not os.path.exists(j.path("stop_req")):
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
            if pop_stamp(j.info, "waiting_since"):
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
            stamp_now(j.info, "waiting_since")
            j.save_info()
        if head is None:
            head = j
            waited = elapsed(j.info, "waiting_since") or 0.0
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
        daemon.idle_since = daemon.idle_since or time.monotonic()
        if time.monotonic() - daemon.idle_since >= cfg["idle_after"]:
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
    if key == "on_idle":
        daemon.set_machine(store, False)  # first: the hook may make JM_HOME read-only (RDD_014)
        run_global_hook(store, cfg, key)
        return False
    rc = run_global_hook(store, cfg, key)
    if rc == 0:
        daemon.hook_error = None
        daemon.set_machine(store, True)
        return True
    daemon.hook_error = "on_first_start failed (exit %d, see daemon.log); retried every poll" % rc
    return False


def sample_progress(store, cfg, daemon):
    """Run each running job's `progress` command every progress_every; keep the last 20 lines."""
    now = time.monotonic()
    for jid in store.read_queue():
        j = Job(store, jid)
        if not j.exists() or j.state != RUNNING or not j.spec["progress"] or not j.cmd_active():
            continue
        run, last = daemon.progress_at.get(jid, (None, 0.0))
        if run == j.info.get("run") and now - last < cfg["progress_every"]:
            continue
        daemon.progress_at[jid] = (j.info.get("run"), now)
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


class _Idle(Exception):
    """A daemon pass skipped: the home is read-only."""


def release_home_writers(store):
    """A detached daemon's stdout / stderr are daemon.log, so start-up errors land there. Once it runs it logs
    through store.log (open, append, close) only, so it lets go of those writers: a remount read-only of JM_HOME's
    file system (an on_idle hook) fails with EBUSY while any file on it is open for writing (RDD_014)."""
    if IS_WIN:
        return
    dev = os.stat(store.home).st_dev
    null = None
    for fd in (1, 2):
        try:
            st = os.fstat(fd)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and st.st_dev == dev:
            if null is None:
                null = os.open(os.devnull, os.O_WRONLY)
            os.dup2(null, fd)
    if null is not None:
        os.close(null)


def daemon_run(store, quiet=False):
    store.need_write()
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
    release_home_writers(store)
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
                if not store.writable():
                    # Read-only home (on_idle remounted it): no pass, no log line, until a jm command makes it
                    # writable again (home_rw). Nothing can have been queued meanwhile (RDD_014).
                    raise _Idle()
                with store.lock():
                    daemon_pass(store, daemon)
                if daemon.want_hook:
                    again = run_wanted_hook(store, cfg, daemon)
                sample_progress(store, cfg, daemon)
                if trim_log(store.p("daemon.log"), cfg["log_max"]):
                    store.log("daemon.log over log_max %s MB; the part before is in daemon.log.1" % fmt_num(
                        cfg["log_max"]))
            except _Idle:
                pass
            except Exception:
                store.log("pass failed:\n" + traceback.format_exc())
            reap_spawned()
            if again:
                continue
            if os.path.exists(store.p("daemon.stop")):
                break
            last = read_text(store.p("wake"))
            deadline = time.monotonic() + max(0.05, cfg["poll"])
            while time.monotonic() < deadline and not stop:
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
    try:
        cap = spec["log_max"] if spec["log_max"] is not None else store.config()["log_max"]
    except JmError:
        cap = 0  # a bad config fails the start below, with its message in the log
    for logp in paths:
        rotate_log(logp, cap)
    log = open(paths[0], "ab")
    err = open(paths[1], "ab") if len(paths) > 1 else None  # split_stderr: every command's stderr goes here

    def say(msg):
        # The banner goes into both files, so the runs in `stderr` can be told apart too.
        for f in (log, err) if err else (log,):
            f.write(("==== jm %s run %s (%s): %s ====\n" % (jid, run, how, msg)).encode("utf-8"))
            f.flush()

    def trim():
        for path, f in zip(paths, (log, err)):
            if trim_log(path, cap):
                f.write(("==== jm %s run %s (%s): over log_max %s MB at %s; the part before is in %s.1 ====\n" % (
                    jid, run, how, fmt_num(cap), utc(), os.path.basename(path))).encode("utf-8"))
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
        trim_at = 0.0
        while True:
            rc = child.poll()
            if rc is not None:
                break
            if time.monotonic() >= trim_at:
                trim_at = time.monotonic() + 1.0
                trim()
            if not stopping and os.path.exists(j.path("stop_req")):
                stopping = True
                hard_at = time.monotonic() + (spec["stop_grace"] if spec["stop_grace"] is not None else 60)
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
            if stopping and not killed and time.monotonic() >= hard_at:
                killed = True
                say("still running %s after stop_grace; killing" % utc())
                kill_group(child.pid, hard=True)
            time.sleep(0.1)
        if stopping and not IS_WIN:
            # The shell may be gone while something it started ignores TERM: the whole group goes.
            while group_exists(child.pid):
                if time.monotonic() >= hard_at:
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
    store.need_write()
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


LIST_FIELDS = ("id", "state", "name", "reason", "progress")  # the default columns, unchanged since 1.0
ALL_FIELDS = LIST_FIELDS + ("eta",)  # RDD_019: only when asked for (--fields, --eta)


def list_row(j, depth=0, ends=None):
    reason = j.reason
    if j.state == RUNNING and j.cmd_active():
        t = elapsed(j.info, "run_start")
        if t is not None:
            reason = "%s, %s" % (reason, fmt_duration(t))
    return ["  " * depth + j.id, j.state, j.name(), reason, job_progress(j), eta_text(j, ends or {})]


def run_order(ctx, queued):
    """The queued jobs in the order they are expected to start (RDD_017): running ones first, then queue order
    with each job moved after the jobs its `after` names. Resources and estimates are left out: on one machine a
    chain of `after` jobs is the order that matters. A job whose `after` can never be met goes last."""
    started = [j for j in queued if j.state in (RUNNING, STOPPING)]
    rest = [j for j in queued if j.state not in (RUNNING, STOPPING)]
    in_q = set(j.id for j in queued)
    placed = set(j.id for j in started)

    def ready(j):
        for dep in j.spec["after"]:
            if dep in placed:
                continue
            d = ctx.job(dep)
            if d is None or dep in in_q:
                return False
            if j.spec["after_ok"] and d.state != DONE:
                return False
        return True
    order = list(started)
    while rest:
        nxt = next((j for j in rest if ready(j)), None)
        if nxt is None:
            break
        order.append(nxt)
        placed.add(nxt.id)
        rest.remove(nxt)
    return order + rest


# Estimates and ETA (design sec 12.2, RDD_019): shown only, nothing in the scheduler acts on them. ---------------

PROGRESS_TOKEN_RE = re.compile(r"jm:progress=(\d+(?:\.\d+)?)(?:/(\d+(?:\.\d+)?)|(%))")


def progress_value(spec, text):
    """(done, total) read from one progress line, total None when unknown; None when the line says nothing."""
    m = PROGRESS_TOKEN_RE.search(text)
    if m:
        return (float(m.group(1)), 100.0) if m.group(3) else (float(m.group(1)), float(m.group(2)))
    if not spec["progress_re"]:
        return None
    try:
        m = re.search(spec["progress_re"], text)
    except re.error:
        return None
    if not m:
        return None
    try:
        done = float(m.group(1))
        total = float(m.group(2)) if m.re.groups >= 2 and m.group(2) else spec["progress_total"]
    except (TypeError, ValueError):
        return None
    return done, total


def progress_rate(j):
    """(done, total, seconds per unit or None, when `done` was first seen) from the stored progress samples
    (`jobs/<id>/progress`, `<utc> <line>`, the last 20), or None. The rate spans the first appearance of the
    oldest and of the newest value, so a value that stays the same between samples does not count as slow."""
    first_seen = []
    for line in read_text(j.path("progress")).splitlines():
        t, _, text = line.partition(" ")
        t = parse_utc(t)
        v = progress_value(j.spec, text) if t is not None else None
        if v is None:
            continue
        if not first_seen or first_seen[-1][1] != v[0]:
            first_seen.append((t, v[0], v[1]))
    if not first_seen:
        return None
    t1, d1, total = first_seen[-1]
    t0, d0, _ = first_seen[0]
    rate = (t1 - t0) / (d1 - d0) if d1 > d0 and t1 > t0 else None
    return d1, total, rate, t1


def run_seconds(j):
    """Seconds the job's ended runs took (`jobs/<id>/runs`: n start end exit how); a run cut off by a reboot
    counts up to when the daemon noticed."""
    total = 0.0
    for r in read_text(j.path("runs")).splitlines():
        f = r.split()
        a, b = (parse_utc(f[1]), parse_utc(f[2])) if len(f) >= 3 else (None, None)
        if a is not None and b is not None:
            total += max(0.0, b - a)
    return total


def record_history(store, j):
    """A job with a `class` that ended with exit 0: `<class> <seconds> <id>` into STATE/history (survives clean)."""
    if j.spec["class"]:
        try:
            append_line(store.p("history"), "%s %d %s" % (j.spec["class"], int(run_seconds(j)), j.id))
        except OSError:
            pass  # an estimate is a nicety; never fail a job's end over it


def class_estimate(ctx, cls):
    """(median seconds of the last 5 successful jobs of `cls`, how many) or None: STATE/history first, then done
    jobs still in the state dir that carry the class (set by hand on older jobs) and are not in the history."""
    secs, seen = [], set()
    for line in read_text(ctx.store.p("history")).splitlines():
        f = line.split()
        if len(f) >= 3 and f[0] == cls:
            secs.append(float(f[1]))
            seen.add(f[2])
    for jid in ctx.store.job_ids():
        x = ctx.job(jid) if jid not in seen else None
        if x is not None and x.state == DONE and x.reason == "exit 0" and x.spec["class"] == cls:
            s = run_seconds(x)
            if s > 0:
                secs.append(s)
    secs = sorted(secs[-5:])
    if not secs:
        return None
    n = len(secs)
    return (secs[n // 2] if n % 2 else (secs[n // 2 - 1] + secs[n // 2]) / 2.0), n


def job_estimate(ctx, j):
    """(seconds for the whole job, where it came from) or (None, None): the spec's `estimate`, else its class."""
    if j.spec["estimate"]:
        return j.spec["estimate"], "estimate"
    if j.spec["class"]:
        c = class_estimate(ctx, j.spec["class"])
        if c:
            return c[0], "class %s, %d run%s" % (j.spec["class"], c[1], "" if c[1] == 1 else "s")
    return None, None


def remaining_seconds(ctx, j, now):
    """Expected seconds left for a running job, or None: progress with a total and a rate first, then this
    run's elapsed time over the fraction done (a first run only), then the estimate minus the time already run."""
    p = progress_rate(j)
    run_el = elapsed(j.info, "run_start") or 0.0
    if p is not None and p[1]:
        done, total, rate, seen = p
        if rate is not None:
            return max(60.0, (total - done) * rate - max(0.0, now - seen))
        if j.info.get("how") == "start" and 0 < done < total:
            return max(60.0, run_el * (total - done) / done)
    est, _ = job_estimate(ctx, j)
    if est is not None:
        return max(60.0, est - run_seconds(j) - run_el)
    return None


def forecast(ctx, jobs, now):
    """{id: expected end (epoch seconds) or None} for the queued `jobs`, in run order. A job starts when every
    `after` job has ended; resources and backfill are ignored (on one machine the chains decide). held and stopped
    jobs, and everything after them or after a job without an estimate, are None."""
    ends = {}
    for j in run_order(ctx, jobs):
        if j.state in (RUNNING, STOPPING):
            r = remaining_seconds(ctx, j, now)
            ends[j.id] = None if r is None else now + r
            continue
        if j.state not in PENDING:
            ends[j.id] = None
            continue
        start = now
        for dep in j.spec["after"]:
            d = ctx.job(dep)
            if d is not None and (d.state == DONE or (d.state in TERMINAL and not j.spec["after_ok"])):
                continue
            e = ends.get(dep) if d is not None and d.state not in TERMINAL else None
            if e is None:
                start = None
                break
            start = max(start, e)
        est, _ = job_estimate(ctx, j)
        ends[j.id] = None if start is None or est is None else start + est
    return ends


def fmt_when(t):
    return "~" + time.strftime("%m-%d %H:%M", time.localtime(t))


def eta_text(j, ends):
    if j.state in TERMINAL:
        return ""
    if j.state in (HELD, STOPPED):
        return "-"
    e = ends.get(j.id)
    return "?" if e is None else fmt_when(e)


def print_rows(rows, fields, align):
    idx = [ALL_FIELDS.index(f) for f in fields]
    rows = [[r[i] for i in idx] for r in rows]
    if not align:
        for r in rows:
            out("\t".join(r))
        return
    # Pad every column but the last to its widest cell, capped so one long name or reason cannot push the rest off
    # a phone's screen (a longer cell is printed whole, unpadded).
    widths = [min(40, max([len(r[c]) for r in rows] or [0])) for c in range(len(fields) - 1)]
    for r in rows:
        out("  ".join([v.ljust(w) for v, w in zip(r, widths)] + r[len(widths):]).rstrip())


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
    finished = [j for _, j in finished]
    if a.order:
        rows = finished + run_order(ctx, queued)
    else:
        rows = queued + finished
    if a.tag:
        rows = [j for j in rows if a.tag in j.spec["tags"]]
    fields = [f.strip() for f in (a.fields or ",".join(LIST_FIELDS)).split(",") if f.strip()]
    for f in fields:
        if f not in ALL_FIELDS:
            raise JmError("list --fields: no field %s (%s)" % (f, ", ".join(ALL_FIELDS)), EXIT_USAGE)
    if a.eta and "eta" not in fields:
        fields.append("eta")
    ends = forecast(ctx, queued, time.time()) if "eta" in fields else {}
    lines = []
    if a.tree:
        ids = set(j.id for j in rows)
        order = dict((j.id, i) for i, j in enumerate(rows))

        def emit(j, depth):
            lines.append(list_row(j, depth, ends))
            kids = [ctx.job(c) for c in j.children()]
            for k in sorted([k for k in kids if k is not None and k.id in ids], key=lambda k: order[k.id]):
                emit(k, depth + 1)
        for j in rows:
            if not (j.parent and j.parent in ids):
                emit(j, 0)
    else:
        lines = [list_row(j, 0, ends) for j in rows]
    align = a.align if a.align is not None else sys.stdout.isatty()
    print_rows(lines, fields, align)
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
    waits = [elapsed(j.info, "waiting_since") for j in pending]
    waits = [w for w in waits if w is not None]
    if waits:
        parts.append("head_wait %s" % fmt_duration(max(waits)))
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
    needed = dependents(ctx, j.id)
    if needed:
        out("needed by: %s" % " ".join("%s:%s" % (d.id, d.state) for d in needed))
    out("cwd: %s" % job_cwd(store, j))
    out("start: %s" % mask_secrets(s["start"] or "-", ctx.cfg))
    for key in ("resume", "done_check", "pre", "post", "stop"):
        if s[key]:
            out("%s: %s" % (key, mask_secrets(s[key], ctx.cfg)))
    if s["applog"]:
        out("applog: %s" % mask_secrets(s["applog"], ctx.cfg))
    if s["report"]:
        out("report: %s" % mask_secrets(s["report"], ctx.cfg))
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
    if j.state not in TERMINAL:
        # RDD_019: what the estimate rests on, then the expected end along the `after` chains.
        bits = []
        p = progress_rate(j)
        if p is not None:
            bits.append("%s%s" % (fmt_num(p[0]), "/%s" % fmt_num(p[1]) if p[1] else ""))
            if p[2] is not None:
                bits.append("%s per step" % fmt_duration(p[2]))
        est, src = job_estimate(ctx, j)
        if est is not None:
            bits.append("estimate %s (%s)" % (fmt_duration(est), src))
        queued = [x for x in (ctx.job(i) for i in ctx.queue) if x is not None]
        ends = forecast(ctx, queued, time.time())
        out("eta: %s%s" % (eta_text(j, ends), (" (%s)" % ", ".join(bits)) if bits else ""))
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


def applog_start(store, cfg, j, a, cmd=None):
    """Start the job's `applog` (or `cmd`: its `report`) command, with applog_env and stdout piped."""
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
    return subprocess.Popen(cmd or j.spec["applog"], **kw)


def cmd_report(store, a):
    """`jm report [ID] [-n N]` (RDD_015): run the spec's `report` command - a status summary printed by the job's
    own tools, e.g. a training run's per-language table - for one job, or for every job in the queue that has one,
    in queue order, a `==> ID <==` section each. Same environment and masking as `applog`; -n is JM_LINES."""
    store.init()
    ctx = Ctx(store)
    if a.id:
        jobs = [ctx.need(a.id)]
        if not jobs[0].spec["report"]:
            raise JmError("%s: no `report` command in the spec (report = a command that prints the job's own status)"
                          % a.id)
    else:
        jobs = [k for k in (ctx.job(jid) for jid in store.read_queue()) if k is not None and k.spec["report"]]
        if not jobs:
            out("no job in the queue has a `report` command")
            return EXIT_OK
    show = log_show(a, ctx.cfg)
    sec = Sections(not a.id, show)
    a.follow = False
    bad = []
    for k in jobs:
        try:
            p = applog_start(store, ctx.cfg, k, a, k.spec["report"])
        except (JmError, OSError) as e:
            warn("report of %s: %s" % (k.id, e))
            bad.append(k.id)
            continue
        try:
            sec.write(k.id, "".join(applog_lines(p)))
            rc = p.wait()
        finally:
            if p.poll() is None:
                kill_group(p.pid, hard=True)
        if rc:
            warn("report of %s exited %d" % (k.id, rc))
            bad.append(k.id)
    return EXIT_ERR if bad else EXIT_OK


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
                    heard[k.id] = time.monotonic() + 1.0  # quiet counts from its start, plus a second to start up
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
                        heard[jid] = time.monotonic()
                        sec.write(jid, line)
                except queue.Empty:
                    pass
                now = time.monotonic()
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
    deadline = None if a.timeout is None else time.monotonic() + parse_duration(a.timeout)
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
        if deadline is not None and time.monotonic() >= deadline:
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


def dependents(ctx, jid):
    """The jobs whose `after` names jid, in job id order (RDD_018)."""
    return [x for x in (ctx.job(i) for i in ctx.store.job_ids()) if x is not None and jid in x.spec["after"]]


def chain_of(ctx, jid):
    """Every job that waits for jid through `after`, directly or not, each once, depth first (RDD_018)."""
    seen, order = set([jid]), []

    def walk(i):
        for d in dependents(ctx, i):
            if d.id not in seen:
                seen.add(d.id)
                order.append(d)
                walk(d.id)
    walk(jid)
    return order


def cmd_hold(store, a):
    def go(ctx):
        j = ctx.need(a.id)
        if j.state in PENDING:
            ctx.set_state(j, HELD, "held by hand")
        elif j.state == STOPPED:
            # RDD_018: parked either way; release then resumes it, as `jm requeue` of the stopped job would.
            j.info["how"] = "resume"
            j.info["retries_used"] = "0"
            j.save_info()
            ctx.set_state(j, HELD, "held by hand (was stopped)")
        elif j.state == RUNNING:
            j.info["hold_after"] = "yes"
            j.save_info()
            j.event("hold asked while running: held if it goes back to the queue")
            out("%s is running; it will be held if it goes back to the queue (retry)" % j.id)
        elif j.state != HELD and not a.chain:
            raise JmError("%s is %s; cannot hold" % (j.id, j.state), EXIT_STATE)
        if a.chain:
            # The job itself may be in any state (a running one: park what comes after this run). Only jobs that
            # could start are parked; running, stopped, held and finished ones are left as they are.
            for d in chain_of(ctx, j.id):
                if d.state in PENDING:
                    ctx.set_state(d, HELD, "held with %s (--chain)" % j.id)
                    out("held %s" % d.id)
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
        elif not a.chain:
            raise JmError("%s is %s, not held (stopped jobs: jm requeue or jm hold)" % (j.id, j.state), EXIT_STATE)
        if a.chain:
            for d in chain_of(ctx, j.id):
                if d.state == HELD:
                    ctx.set_state(d, QUEUED, "released with %s (--chain)" % j.id)
                    out("released %s" % d.id)
    mutate(store, go)
    return EXIT_OK


def cmd_skip(store, a):
    def go(ctx):
        j = ctx.need(a.id)
        if j.state in (RUNNING, STOPPING):
            raise JmError("%s is %s; stop it first (then jm skip)" % (j.id, j.state), EXIT_STATE)
        if j.state == DONE:
            raise JmError("%s is already done" % j.id, EXIT_STATE)
        open_kids = [c for c in ctx.descendants(j.id) if ctx.job(c) and ctx.job(c).state not in TERMINAL]
        if open_kids:
            raise JmError("%s has %d unfinished children (%s); skip or cancel them first" % (
                j.id, len(open_kids), " ".join(open_kids)), EXIT_STATE)
        # RDD_018: done without a run, so `after_ok = yes` dependents may start; the reason tells it apart.
        ctx.set_state(j, DONE, "skipped by hand (was %s)" % j.state)
    mutate(store, go)
    return EXIT_OK


def cmd_deps(store, a):
    store.init()
    ctx = Ctx(store)
    root = ctx.need(a.id)
    lines, seen = [], set([root.id])

    def walk(jid, depth):
        for d in dependents(ctx, jid):
            if d.id in seen:
                continue
            seen.add(d.id)
            lines.append(list_row(d, depth))
            walk(d.id, depth + 1)
    walk(root.id, 0)
    align = a.align if a.align is not None else sys.stdout.isatty()
    print_rows(lines, ("id", "state", "reason"), align)
    return EXIT_OK


def after_warnings(ctx, jid, spec):
    """RDD_018: an `after` that names no job, or that leads back to the job itself, waits forever; say so."""
    msgs = []
    for dep in spec["after"]:
        if dep != jid and ctx.job(dep) is None:
            msgs.append("after: no such job %s (%s waits until one is submitted)" % (dep, jid))

    def path_back(start):
        # Breadth first, so the warning names the shortest way back.
        todo, seen = [(start, [start])], set()
        while todo:
            cur, path = todo.pop(0)
            if cur == jid:
                return path
            if cur in seen:
                continue
            seen.add(cur)
            x = ctx.job(cur)
            for nxt in (x.spec["after"] if x is not None else []):
                todo.append((nxt, path + [nxt]))
        return None
    for dep in spec["after"]:
        p = path_back(dep)
        if p is not None:
            msgs.append("after: cycle %s (never starts)" % " -> ".join([jid] + p))
            break
    for m in msgs:
        warn(m)


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
    spec = parse_spec(text, "%s/spec" % j.id)
    check_spec(spec, ctx.cfg, j.id)
    write_atomic(j.path("spec"), text)
    j._spec = None
    j.event("spec changed: " + " ".join(k for k, _, _ in assigns))
    if [k for k, _, _ in assigns if k == "after"]:
        after_warnings(ctx, j.id, spec)


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
            jj._spec = None
            jj.event("spec edited")
            after_warnings(ctx, j.id, spec)
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
    store.need_write()
    store.init()
    try:
        rotate_log(store.p("daemon.log"), store.config()["log_max"])
    except JmError:
        pass
    log = open(store.p("daemon.log"), "ab")
    try:
        p = spawn_detached(jm_argv(store) + ["daemon", "run"], stdout=log, cwd=store.home)
    finally:
        log.close()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
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
    store.need_write()
    write_atomic(store.p("daemon.stop"), utc() + "\n")
    store.wake()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
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


# remote hosts: jm ssh / scp / keygen / remote (RDD_016) -------------------------------------------------------
# jm calls the system OpenSSH client (ssh, scp, ssh-keygen): Python's standard library has no SSH, and the client
# ships with Linux, macOS and Windows 10 1809+ / 11. Nothing here touches the state dir or the daemon.

HOST_KEYS = ("User", "Host", "Port", "SignInKey", "Jm", "JmHome")  # the rest pass to ssh / scp as `-o Key=Value`
REMOTE_COMMANDS = ("ssh", "ssx", "scp", "keygen", "remote")


def hosts_path():
    return os.environ.get("JM_HOSTS") or os.path.join(os.path.expanduser("~"), ".jmhosts")


def load_hosts(path=None):
    """The hosts file as an ordered list of (name, {key: value}); [] when it does not exist."""
    import configparser
    path = path or hosts_path()
    if not os.path.exists(path):
        return []
    if not IS_WIN and os.stat(path).st_mode & 0o022:
        # It names a private key and decides where commands run: nobody else may change it.
        raise JmError("%s: writable by group or others (chmod go-w)" % path)
    cp = configparser.ConfigParser(interpolation=None, default_section="\0none", inline_comment_prefixes=("#", ";"))
    cp.optionxform = str  # keep the keys' case: they are ssh option names
    try:
        with codecs.open(path, "r", "utf-8") as f:
            cp.read_file(f)
    except (configparser.Error, UnicodeDecodeError) as e:
        raise JmError("%s: %s" % (path, str(e).splitlines()[0]))
    hosts = []
    for name in cp.sections():
        keys = dict((k, v.strip()) for k, v in cp.items(name))
        if not keys.get("Host"):
            raise JmError("%s: [%s] has no Host" % (path, name))
        hosts.append((name, keys))
    return hosts


def pick_host(name=None, path=None):
    hosts = load_hosts(path)
    if not hosts:
        raise JmError("no hosts: write %s ([name] with Host = ..., see docs/REMOTE.md)" % (path or hosts_path()),
                      EXIT_USAGE)
    if name is None:
        return hosts[0]
    for h in hosts:
        if h[0] == name:
            return h
    raise JmError("no host [%s] in %s (have: %s)" % (name, path or hosts_path(), ", ".join(h[0] for h in hosts)),
                  EXIT_USAGE)


def ssh_options(keys, scp=False):
    """The client options for one host: -i, the port, and every other key as -o Key=Value."""
    opts = []
    if keys.get("SignInKey"):
        opts += ["-i", os.path.expanduser(keys["SignInKey"])]
    if keys.get("Port"):
        opts += ["-P" if scp else "-p", keys["Port"]]
    for k in sorted(keys):
        if k in HOST_KEYS:
            continue
        v = keys[k]
        if v == "/dev/null":
            v = os.devnull  # NUL on Windows
        opts += ["-o", "%s=%s" % (k, v)]
    return opts


def ssh_target(keys):
    return ("%s@%s" % (keys["User"], keys["Host"])) if keys.get("User") else keys["Host"]


def split_host_args(args, flags=()):
    """Leading --host NAME / --host=NAME and the given flags, then an optional `--`; returns (host, set, rest)."""
    host, seen, i = None, set(), 0
    while i < len(args):
        a = args[i]
        if a == "--host" and i + 1 < len(args):
            host, i = args[i + 1], i + 2
        elif a.startswith("--host="):
            host, i = a[len("--host="):], i + 1
        elif a in flags:
            seen.add(a)
            i += 1
        elif a == "--":
            i += 1
            break
        else:
            break
    return host, seen, args[i:]


def ssh_argv(keys, command, tty=False):
    argv = ["ssh"] + ssh_options(keys) + (["-t"] if tty else []) + [ssh_target(keys)]
    return argv + list(command)


def scp_argv(keys, paths):
    """`:path` is a path on the host (a bare `:` = the login directory); anything else is local."""
    target = ssh_target(keys)
    conv = [(target + p) if p.startswith(":") else p for p in paths]
    return ["scp", "-r"] + ssh_options(keys, scp=True) + conv


def remote_jm_argv(keys, jm_args):
    """The words of `jm ...` on the host, quoted once for its POSIX login shell."""
    words = [keys.get("Jm") or "jm"]
    if keys.get("JmHome"):
        words += ["--home", keys["JmHome"]]
    return [" ".join(shlex.quote(w) for w in words + list(jm_args))]


def run_client(argv, stdin=None):
    exe = shutil.which(argv[0])
    if exe is None:
        raise JmError("%s not found on PATH (install the OpenSSH client)" % argv[0])
    try:
        return subprocess.call([exe] + argv[1:], stdin=stdin)
    except KeyboardInterrupt:
        return EXIT_ERR


def keygen_argv(path, comment, passphrase):
    argv = ["ssh-keygen", "-t", "ed25519", "-f", path, "-C", comment]
    return argv + (["-N", ""] if passphrase is False else [])


def install_key_command(pub):
    """Append the public key to ~/.ssh/authorized_keys on a POSIX host, once."""
    k = shlex.quote(pub.strip())
    return ("umask 077 && mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
            "(grep -qxF %s ~/.ssh/authorized_keys || echo %s >> ~/.ssh/authorized_keys)" % (k, k))


def cmd_ssh(args):
    name, seen, rest = split_host_args(args, ("-t",))
    host, keys = pick_host(name)
    return run_client(ssh_argv(keys, rest, tty="-t" in seen))


def cmd_scp(args):
    name, _, rest = split_host_args(args)
    if len(rest) < 2:
        raise JmError("usage: jm scp [--host NAME] SRC... DST   (`:path` = on the host)", EXIT_USAGE)
    if not any(p.startswith(":") for p in rest):
        raise JmError("scp: no `:path` among the paths, nothing is on the host", EXIT_USAGE)
    host, keys = pick_host(name)
    return run_client(scp_argv(keys, rest))


def cmd_remote(args):
    name, _, rest = split_host_args(args)
    if not rest:
        raise JmError("usage: jm remote [--host NAME] COMMAND ...   (any jm command, run on the host)", EXIT_USAGE)
    host, keys = pick_host(name)
    if rest[0] == "submit" and len(rest) >= 2 and rest[1] != "-" and os.path.isfile(rest[1]):
        # A local spec goes on stdin (`jm submit -`), so nothing has to be copied first (RDD_012).
        with open(rest[1], "rb") as f:
            return run_client(ssh_argv(keys, remote_jm_argv(keys, ["submit", "-"] + rest[2:])), stdin=f)
    # A terminal gets -t (log -f, prompts, colours); a script or pipe does not (no \r\n, stderr kept apart).
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    return run_client(ssh_argv(keys, remote_jm_argv(keys, rest), tty=tty))


def cmd_keygen(args):
    p = Parser(prog="jm keygen", description="a new ed25519 sign-in key for a host (ssh-keygen)")
    p.add_argument("--host", help="default: the first host in the hosts file")
    p.add_argument("--file", help="default: the host's SignInKey, else ~/.ssh/jm_<host>")
    p.add_argument("--comment", help="default: jm-<host>")
    p.add_argument("--no-passphrase", action="store_true", help="an unencrypted key (unattended use)")
    p.add_argument("--install", action="store_true",
                   help="append the public key to the host's ~/.ssh/authorized_keys over ssh (asks for its password)")
    a = p.parse_args(args)
    hosts = load_hosts()
    if a.host is None and not hosts and a.file is None:
        raise JmError("keygen: no hosts file and no --host / --file", EXIT_USAGE)
    keys = {}
    name = a.host
    if hosts and (a.install or a.host is None or any(h[0] == a.host for h in hosts)):
        name, keys = pick_host(a.host)
    elif a.install:
        raise JmError("keygen --install: no host [%s] in %s" % (a.host, hosts_path()), EXIT_USAGE)
    path = os.path.expanduser(a.file or keys.get("SignInKey") or os.path.join("~", ".ssh", "jm_%s" % name))
    if os.path.exists(path) and not a.install:
        raise JmError("keygen: %s exists; remove it first, pass another --file, or --install it" % path)
    if not os.path.exists(path):
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d, 0o700)
        rc = run_client(keygen_argv(path, a.comment or "jm-%s" % name, False if a.no_passphrase else None))
        if rc != 0:
            return rc
    if keys and os.path.abspath(os.path.expanduser(keys.get("SignInKey") or "")) != os.path.abspath(path):
        out("jm: add `SignInKey = %s` to [%s] in %s" % (path, name, hosts_path()))
    if not a.install:
        out("jm: to allow it on the host: jm keygen --install --file %s%s   (or append %s.pub to the host's "
            "~/.ssh/authorized_keys)" % (path, "" if a.host is None else " --host " + name, path))
        return EXIT_OK
    with open(path + ".pub", "r") as f:
        pub = f.read()
    # The new key is not authorised yet: sign in with the host's other SignInKey if it has one, else ssh falls back
    # to the host's password, asked on this terminal (jm never sees it).
    k = dict(keys)
    if os.path.abspath(os.path.expanduser(k.get("SignInKey") or "")) == os.path.abspath(path):
        k.pop("SignInKey")
    return run_client(ssh_argv(k, [install_key_command(pub)]))


def remote_main(cmd, args):
    if cmd in ("ssh", "ssx"):
        return cmd_ssh(args)
    if cmd == "scp":
        return cmd_scp(args)
    if cmd == "remote":
        return cmd_remote(args)
    return cmd_keygen(args)


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
    li.add_argument("--order", action="store_true",
                    help="finished jobs, then the queue in the order it is expected to run (after chains followed)")
    li.add_argument("--fields", metavar="F,F",
                    help="columns to print: " + ",".join(ALL_FIELDS) + " (default: all but eta)")
    li.add_argument("--eta", action="store_true", help="add the eta column: expected end along the `after` chains")
    al_ = li.add_mutually_exclusive_group()
    al_.add_argument("--align", dest="align", action="store_true", default=None,
                     help="pad the columns with spaces (the default on a terminal)")
    al_.add_argument("--tabs", dest="align", action="store_false", help="tab-separated (the default when piped)")

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

    rp = sub.add_parser("report", help="run the job's `report` command (a status summary); no ID: every queued job's")
    rp.add_argument("id", nargs="?")
    rp.add_argument("-n", "--lines", type=int, help="passed to the command as JM_LINES")
    rp.add_argument("--raw", action="store_true", help="unmasked (terminal only)")

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

    for name in ("hold", "release"):
        x = sub.add_parser(name)
        x.add_argument("id")
        x.add_argument("--chain", action="store_true", help="also every job that waits for it through `after`")
    for name in ("restart", "pause", "cont"):
        x = sub.add_parser(name)
        x.add_argument("id")
    x = sub.add_parser("skip", help="mark a job that is not running done without running it")
    x.add_argument("id")
    x = sub.add_parser("deps", help="the jobs that wait for ID through `after`, as a tree")
    x.add_argument("id")
    g = x.add_mutually_exclusive_group()
    g.add_argument("--align", dest="align", action="store_true", default=None)
    g.add_argument("--tabs", dest="align", action="store_false")
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

    # RDD_016: listed for --help only; main() hands these words to remote_main() before argparse sees them.
    for name, h in (("ssh", "[--host NAME] [-t] [--] COMMAND...: ssh to a host from the hosts file (alias: ssx)"),
                    ("scp", "[--host NAME] SRC... DST: copy, `:path` = on the host (always -r)"),
                    ("keygen", "[--host NAME] [--file F] [--no-passphrase] [--install]: an ed25519 sign-in key"),
                    ("remote", "[--host NAME] COMMAND...: any jm command on the host (a local spec goes on stdin)")):
        sub.add_parser(name, help=h, add_help=False)

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
    "report": cmd_report,
    "wait": cmd_wait, "move": cmd_move, "swap": cmd_swap, "hold": cmd_hold, "release": cmd_release,
    "stop": cmd_stop, "cancel": cmd_cancel, "restart": cmd_restart, "requeue": cmd_requeue, "set": cmd_set,
    "edit": cmd_edit, "skip": cmd_skip, "deps": cmd_deps, "config": cmd_config, "clean": cmd_clean, "artifacts": cmd_artifacts,
    "daemon": cmd_daemon, "pause": lambda s, a: cmd_pause(s, a), "cont": lambda s, a: cmd_pause(s, a, True),
}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        # RDD_016: the ssh client commands take their own words (a remote command, scp paths) and need no state dir.
        if argv and argv[0] in REMOTE_COMMANDS:
            return remote_main(argv[0], argv[1:])
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
