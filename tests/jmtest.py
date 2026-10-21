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

# Shared test harness (design sec 12.5): every test gets its own temporary JM_HOME and, when it needs one, a
# daemon with poll = 0.2s. Jobs are `jm _sim` fake jobs. When JM_TEST_ARTIFACTS names a directory, the JM_HOME of
# every failed test is copied there (CI uploads it: state files and logs are text, readable from the artifact).

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
JM = os.path.join(os.path.dirname(HERE), "jm.py")
sys.path.insert(0, os.path.dirname(HERE))
import jm  # noqa: E402

IS_WIN = os.name == "nt"
PY = sys.executable


def q(s):
    """Quote one word for a job command: double quotes work in both sh and cmd."""
    return '"%s"' % s


def sim(secs=0.3, exit=0, ram=0, progress=False, resumable=None, trap=False, print_env=(), out=None):
    parts = [q(PY), q(JM), "_sim", "--secs", str(secs), "--exit", str(exit)]
    if ram:
        parts += ["--ram", str(ram)]
    if progress:
        parts.append("--progress")
    if resumable:
        parts += ["--resumable", q(resumable)]
    if trap:
        parts.append("--trap")
    for n in print_env:
        parts += ["--print-env", n]
    if out:
        parts += ["--out", q(out)]
    return " ".join(parts)


def hard_kill(pid):
    try:
        os.kill(int(pid), signal.SIGTERM if IS_WIN else signal.SIGKILL)
    except OSError:
        pass


class JmTestCase(unittest.TestCase):
    config = ("poll=0.2s", "cpus=4", "ram=4000")
    daemon = True

    def setUp(self):
        # realpath: on macOS the temp dir is under /var, a symlink to /private/var, and os.getcwd() in a
        # child process reports the resolved path.
        self.home = os.path.realpath(tempfile.mkdtemp(prefix="jm-test-"))
        self.work = os.path.join(self.home, "work")
        os.makedirs(self.work)
        self.env = dict(os.environ)
        self.env.pop("JM_HOME", None)
        self.env.pop("JM_TEST_DISK_FREE", None)
        self.jm_ok("config", *self.config)
        self.daemon_up = False
        if self.daemon:
            self.start_daemon()

    def run(self, result=None):
        result = result if result is not None else self.defaultTestResult()
        before = len(result.failures) + len(result.errors)
        unittest.TestCase.run(self, result)
        home = getattr(self, "home", None)
        if not home:
            return result
        failed = len(result.failures) + len(result.errors) > before
        dest = os.environ.get("JM_TEST_ARTIFACTS")
        if failed and dest:
            try:
                shutil.copytree(home, os.path.join(dest, self.id().replace("/", "_")), symlinks=True)
            except OSError:
                pass
        shutil.rmtree(home, ignore_errors=True)
        return result

    def tearDown(self):
        if self.daemon_up:
            self.jm("daemon", "stop")
        # Leave no job processes behind.
        jobs = os.path.join(self.home, "jobs")
        for jid in (os.listdir(jobs) if os.path.isdir(jobs) else []):
            for name in ("child", "pid"):
                f = jm.read_text(os.path.join(jobs, jid, name)).split()
                if len(f) >= 2 and jm.proc_alive(f[0], f[1]):
                    if name == "child":
                        jm.kill_group(f[0], hard=True)
                    else:
                        hard_kill(f[0])

    # running jm ---------------------------------------------------------------------------------------------

    def jm(self, *args, **kw):
        env = dict(self.env)
        env.update(kw.get("env") or {})
        p = subprocess.Popen([PY, JM, "--home", self.home] + list(args), stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                             cwd=kw.get("cwd", self.work), universal_newlines=True)
        out, err = p.communicate(kw.get("input", ""), timeout=kw.get("timeout", 60))
        return p.returncode, out, err

    def jm_ok(self, *args, **kw):
        rc, out, err = self.jm(*args, **kw)
        self.assertEqual(rc, 0, "jm %s -> %d\nstdout: %s\nstderr: %s" % (" ".join(args), rc, out, err))
        return out

    def start_daemon(self, env=None):
        if env:
            self.env.update(env)
        self.jm_ok("daemon", "start")
        self.daemon_up = True

    def stop_daemon(self):
        self.jm_ok("daemon", "stop")
        self.daemon_up = False

    def kill_daemon(self):
        f = jm.read_text(os.path.join(self.home, "daemon.pid")).split()
        hard_kill(f[0])
        self.until(lambda: not jm.Store(self.home).daemon_running(), "daemon dies")
        self.daemon_up = False

    def submit(self, text=None, *args, **kw):
        """Submit a spec text (via stdin) or, with text None, the inline `args`; returns the first id."""
        if text is not None:
            out = self.jm_ok("submit", "-", *args, input=text, **kw)
        else:
            out = self.jm_ok("submit", *args, **kw)
        return out.split()[0]

    def write(self, name, text, mode=None):
        path = os.path.join(self.work, name)
        with open(path, "w") as f:
            f.write(text)
        if mode is not None:
            os.chmod(path, mode)
        return path

    # reading state ------------------------------------------------------------------------------------------

    def jobfile(self, jid, name):
        return jm.read_text(os.path.join(self.home, "jobs", jid, name))

    def state(self, jid):
        return self.jobfile(jid, "state").strip()

    def reason(self, jid):
        return self.jobfile(jid, "reason").strip()

    def runs(self, jid):
        return [r.split() for r in self.jobfile(jid, "runs").splitlines() if r.strip()]

    def queue(self):
        return jm.read_text(os.path.join(self.home, "queue")).split()

    def until(self, fn, what, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if fn():
                return
            time.sleep(0.05)
        self.fail("timed out waiting for: %s" % what)

    def wait_state(self, jid, *states, **kw):
        self.until(lambda: self.state(jid) in states, "%s in %s (is %s: %s)" % (
            jid, states, self.state(jid), self.reason(jid)), kw.get("timeout", 30))
        return self.state(jid)

    def wait_ended(self, jid, timeout=30):
        return self.wait_state(jid, "done", "failed", "cancelled", "stopped", timeout=timeout)
