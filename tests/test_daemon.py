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

# With a running daemon (poll = 0.2s): admission by cpu / ram / tokens / disk, backfill and the drain guard,
# dependencies, the job life cycle (stop, restart, requeue, cancel, retries, timeout, hooks), daemon and
# wrapper crashes (adoption, on_boot), PID reuse, groups and dynamic children, secrets at run time.

import os
import time
import unittest

from jmtest import JmTestCase, jm, sim, q, PY, JM, IS_WIN, hard_kill


def spec(cmd, **keys):
    return "".join("%s = %s\n" % (k, v) for k, v in keys.items()) + "start = %s\n" % cmd


class TestAdmission(JmTestCase):
    config = ("poll=0.2s", "cpus=2", "ram=1000", "ram_reserve=200", "tokens=gpu:1")

    def test_cpu(self):
        a = self.submit(spec(sim(1.5), cpu=2))
        b = self.submit(spec(sim(0.2), cpu=1))
        self.wait_state(a, "running")
        self.wait_state(b, "waiting")
        self.assertIn("cpu 1 > free 0", self.reason(b))
        self.assertEqual(self.wait_ended(b), "done")
        self.assertGreaterEqual(self.runs(b)[0][1], self.runs(a)[0][2])

    def test_ram(self):
        a = self.submit(spec(sim(1.5), ram=600, cpu=0.5))
        b = self.submit(spec(sim(0.2), ram=300, cpu=0.5))
        self.wait_state(a, "running")
        self.wait_state(b, "waiting")
        self.assertIn("ram 300 > free 200", self.reason(b))
        self.assertEqual(self.wait_ended(b), "done")

    def test_tokens(self):
        a = self.submit(spec(sim(1.5), tokens="gpu", cpu=0.5))
        b = self.submit(spec(sim(0.2), tokens="gpu", cpu=0.5))
        self.wait_state(a, "running")
        self.wait_state(b, "waiting")
        self.assertIn("token gpu:1 > free 0", self.reason(b))
        self.assertIn("tokens gpu:0/1", self.jm_ok("free"))
        self.assertEqual(self.wait_ended(b), "done")

    def test_disk(self):
        self.stop_daemon()
        big = os.path.join(self.work, "big")
        os.makedirs(big)
        self.start_daemon({"JM_TEST_DISK_FREE": "%s=100" % big})
        a = self.submit(spec(sim(0.2), disk="200 @ %s" % big, cpu=0.5))
        b = self.submit(spec(sim(0.2), disk="50 @ %s" % big, cpu=0.5))
        self.assertEqual(self.wait_ended(b), "done")
        self.wait_state(a, "waiting")
        self.assertIn("disk 200 > free 100", self.reason(a))

    def test_backfill(self):
        # The blocker runs until the test cancels it: no race with slow process start-up (Windows).
        r = self.submit(spec(sim(60), cpu=1))
        self.wait_state(r, "running")
        h = self.submit(spec(sim(0.2), cpu=2, name="head"))
        b = self.submit(spec(sim(0.2), cpu=1, name="small"))
        self.assertEqual(self.wait_ended(b), "done")  # backfilled while the head waits
        self.assertEqual(self.state(h), "waiting")
        self.jm_ok("cancel", r)
        self.assertEqual(self.wait_ended(h), "done")
        self.assertLessEqual(self.runs(b)[0][2], self.runs(h)[0][1])

    def test_drain_yes(self):
        r = self.submit(spec(sim(1.5), cpu=1))
        self.wait_state(r, "running")
        h = self.submit(spec(sim(0.2), cpu=2, drain="yes"))
        b = self.submit(spec(sim(0.2), cpu=1))
        self.wait_state(b, "waiting")
        time.sleep(0.6)
        self.assertEqual(self.state(b), "waiting")
        self.assertIn("drain", self.reason(b))
        self.assertEqual(self.wait_ended(h), "done")
        self.assertEqual(self.wait_ended(b), "done")
        self.assertLessEqual(self.runs(h)[0][1], self.runs(b)[0][1])

    def test_drain_after(self):
        self.jm_ok("config", "drain_after=0.5s")
        r = self.submit(spec(sim(2.5), cpu=1))
        self.wait_state(r, "running")
        h = self.submit(spec(sim(0.2), cpu=2))
        time.sleep(1.0)  # the head has now waited longer than drain_after
        b = self.submit(spec(sim(0.2), cpu=1))
        self.wait_state(b, "waiting")
        time.sleep(0.5)
        self.assertEqual(self.state(b), "waiting")
        self.assertIn("drain (head waited", self.reason(h))
        self.assertEqual(self.wait_ended(b), "done")
        self.assertLessEqual(self.runs(h)[0][1], self.runs(b)[0][1])

    def test_fifo(self):
        self.jm_ok("config", "backfill=no")
        r = self.submit(spec(sim(1.5), cpu=1))
        self.wait_state(r, "running")
        h = self.submit(spec(sim(0.2), cpu=2))
        b = self.submit(spec(sim(0.2), cpu=1))
        self.wait_state(b, "waiting")
        self.assertIn("fifo", self.reason(b))
        self.assertEqual(self.wait_ended(b), "done")
        self.assertLessEqual(self.runs(h)[0][1], self.runs(b)[0][1])

    def test_never_fits_does_not_block(self):
        self.jm_ok("config", "cpus=8")
        big = self.submit(spec(sim(0.2), cpu=8), "--hold")
        self.jm_ok("config", "cpus=2")
        self.jm_ok("release", big)
        small = self.submit(spec(sim(0.2), cpu=1))
        self.assertEqual(self.wait_ended(small), "done")
        self.assertIn("never fits", self.reason(big))


class TestLifeCycle(JmTestCase):
    def test_after(self):
        a = self.submit(spec(sim(0.5, exit=1)))
        b = self.submit(spec(sim(0.1), after=a))
        c = self.submit(spec(sim(0.1), after=a, after_ok="no"))
        self.assertEqual(self.wait_ended(a), "failed")
        self.assertEqual(self.wait_ended(c), "done")
        self.wait_state(b, "waiting")
        self.until(lambda: "failed (after_ok = yes)" in self.reason(b), "b reason")
        d = self.submit(spec(sim(0.1)))
        e = self.submit(spec(sim(0.1), after=d))
        self.assertEqual(self.wait_ended(e), "done")
        self.assertGreaterEqual(self.runs(e)[0][1], self.runs(d)[0][2])

    def test_hold_release(self):
        a = self.submit(spec(sim(0.1)), "--hold")
        time.sleep(0.6)
        self.assertEqual(self.state(a), "held")
        self.jm_ok("release", a)
        self.assertEqual(self.wait_ended(a), "done")

    def test_stop_never_resumed(self):
        a = self.submit(spec(sim(20)))
        self.wait_state(a, "running")
        self.jm_ok("stop", a)
        self.assertEqual(self.wait_ended(a), "stopped")
        self.stop_daemon()
        self.start_daemon()
        time.sleep(0.8)
        self.assertEqual(self.state(a), "stopped")
        self.assertEqual(len(self.runs(a)), 1)
        self.jm_ok("requeue", a, "--fresh")
        self.wait_state(a, "running")
        self.jm_ok("cancel", a)
        self.assertEqual(self.wait_ended(a), "cancelled")
        # `queue` is rewritten at the end of the daemon's transaction, just after `state`: wait for it.
        self.until(lambda: a not in self.queue(), "cancelled job leaves the queue")

    def test_stop_grace_kill(self):
        a = self.submit(spec(sim(30, trap=True), stop_grace="0.5"))
        self.wait_state(a, "running")
        self.until(lambda: "sim:" in self.jobfile(a, "log") or os.path.exists(
            os.path.join(self.home, "jobs", a, "child")), "child started")
        time.sleep(0.5)
        t0 = time.time()
        self.jm_ok("stop", a)
        self.assertEqual(self.wait_ended(a), "stopped")
        self.assertLess(time.time() - t0, 10)
        if not IS_WIN:
            self.assertIn("killing", self.jobfile(a, "log"))

    def test_stop_command(self):
        marker = os.path.join(self.work, "stopped.txt")
        a = self.submit(spec(sim(30), stop="%s %s _sim --secs 0 --print-env JM_PID --out %s" % (
            q(PY), q(JM), q(marker)), stop_grace="2"))
        self.wait_state(a, "running")
        time.sleep(0.5)
        self.jm_ok("stop", a)
        self.assertEqual(self.wait_ended(a), "stopped")
        self.until(lambda: os.path.exists(marker), "stop command ran")
        self.assertIn("env JM_PID=", jm.read_text(marker))

    def test_restart(self):
        out = os.path.join(self.work, "how.txt")
        a = self.submit(spec(sim(20, print_env=["JM_HOW", "JM_RUN"], out=out)))
        self.wait_state(a, "running")
        self.until(lambda: os.path.exists(out), "first run printed")
        self.jm_ok("restart", a)
        self.until(lambda: len(self.runs(a)) == 1 and self.state(a) == "running", "restarted")
        self.until(lambda: "JM_HOW=restart" in jm.read_text(out), "second run printed")
        self.assertIn("JM_RUN=2", jm.read_text(out))
        self.jm_ok("cancel", a)
        self.wait_ended(a)

    def test_requeue_done(self):
        out = os.path.join(self.work, "how.txt")
        a = self.submit(spec(sim(0.1, print_env=["JM_HOW"], out=out)))
        self.assertEqual(self.wait_ended(a), "done")
        self.jm_ok("requeue", a, "--fresh")
        self.until(lambda: len(self.runs(a)) == 2 and self.state(a) == "done", "second run")
        self.jm_ok("requeue", a)
        self.until(lambda: len(self.runs(a)) == 3 and self.state(a) == "done", "third run")
        self.assertEqual(jm.read_text(out).split(), ["env", "JM_HOW=start"] * 2 + ["env", "JM_HOW=resume"])

    def test_retries(self):
        a = self.submit(spec(sim(0.1, exit=3), retries=2))
        self.assertEqual(self.wait_ended(a), "failed")
        self.until(lambda: len(self.runs(a)) == 3, "3 runs")
        self.assertEqual([r[3] for r in self.runs(a)], ["3", "3", "3"])
        self.assertEqual([r[4] for r in self.runs(a)], ["start", "resume", "resume"])
        rc, _, err = self.jm("wait", a)
        self.assertEqual(rc, 3)

    def test_retry_then_success(self):
        # Fails the first time (creates `flag`), succeeds on the retry.
        cmd = q(PY) + " -c " + q("import os,sys; e=os.path.exists('flag'); open('flag','w').close(); "
                                 "sys.exit(0 if e else 1)")
        b = self.submit(spec(cmd, retries=1))
        self.assertEqual(self.wait_ended(b), "done")
        self.assertEqual([r[3] for r in self.runs(b)], ["1", "0"])

    def test_timeout(self):
        a = self.submit(spec(sim(30), timeout="0.6s"))
        self.assertEqual(self.wait_ended(a), "failed")
        self.assertIn("timeout", self.reason(a))

    def test_done_check_pre_post_notify(self):
        mark = os.path.join(self.work, "m.txt")
        dc = "%s %s _sim --secs 0 --exit 0" % (q(PY), q(JM))
        a = self.submit(spec(sim(5, print_env=["X"], out=mark), done_check=dc))
        self.assertEqual(self.wait_ended(a), "done")
        self.assertEqual(self.reason(a), "done_check passed")
        self.assertFalse(os.path.exists(mark))
        pre = "%s %s _sim --secs 0 --exit 7" % (q(PY), q(JM))
        b = self.submit(spec(sim(5), pre=pre))
        self.assertEqual(self.wait_ended(b), "failed")
        self.assertIn("pre failed (exit 7)", self.reason(b))
        post_out = os.path.join(self.work, "post.txt")
        notes = os.path.join(self.work, "notify.txt")
        c = self.submit(spec(sim(0.1, exit=4),
                             post="%s %s _sim --secs 0 --print-env JM_EXIT --out %s" % (q(PY), q(JM), q(post_out)),
                             notify="%s %s _sim --secs 0 --print-env JM_STATE --out %s" % (q(PY), q(JM), q(notes))))
        self.assertEqual(self.wait_ended(c), "failed")
        self.until(lambda: "JM_EXIT=4" in jm.read_text(post_out), "post ran")
        self.until(lambda: "JM_STATE=failed" in jm.read_text(notes), "notify ran")
        self.assertIn("JM_STATE=running", jm.read_text(notes))

    def test_wait_and_log(self):
        a = self.submit(spec(sim(0.6, progress=True)))
        rc, out, _ = self.jm("log", a, "-f", timeout=30)
        self.assertEqual(rc, 0)
        self.assertIn("jm:progress=6/6", out)
        self.assertIn("sim: finished", out)
        self.assertEqual(self.jm("wait", a)[0], 0)
        self.assertEqual(self.jm_ok("log", a, "-n", "1").count("\n"), 1)
        b = self.submit(spec(sim(20)))
        rc, _, err = self.jm("wait", b, "--timeout", "0.5s")
        self.assertEqual(rc, 1)
        self.assertIn("timeout", err)

    def test_progress_and_status(self):
        self.jm_ok("config", "progress_every=0.2s")
        a = self.submit(spec(sim(3, progress=True, ram=30), progress="%s %s _sim --secs 0 --exit 0" % (q(PY), q(JM)),
                             name="busy"))
        self.wait_state(a, "running")
        self.until(lambda: "sim: finished" in self.jobfile(a, "progress"), "progress sampled")
        row = [r for r in self.jm_ok("list").splitlines() if r.startswith(a)][0].split("\t")
        self.assertEqual(row[1], "running")
        self.assertEqual(row[4], "sim: finished, exit 0")
        st = self.jm_ok("status", a)
        self.assertIn("state: running", st)
        self.assertIn("progress: sim: finished", st)
        if not IS_WIN:
            self.until(lambda: "live: rss" in self.jm_ok("status", a), "live usage")
        self.assertIn("daemon: running", self.jm_ok("status"))
        self.jm_ok("cancel", a)
        self.wait_ended(a)

    @unittest.skipIf(IS_WIN, "pause / cont are Unix only")
    def test_pause_cont(self):
        a = self.submit(spec(sim(1.0, resumable=os.path.join(self.work, "c"))))
        self.wait_state(a, "running")
        time.sleep(0.3)
        self.jm_ok("pause", a)
        self.assertIn("paused", self.reason(a))
        c1 = jm.read_text(os.path.join(self.work, "c"))
        time.sleep(0.6)
        self.assertEqual(jm.read_text(os.path.join(self.work, "c")), c1)
        self.jm_ok("cont", a)
        self.assertEqual(self.wait_ended(a), "done")

    def test_clean(self):
        a = self.submit(spec(sim(0.1)))
        self.wait_ended(a)
        self.assertEqual(self.jm_ok("clean"), "")
        self.assertIn("removed %s" % a, self.jm_ok("clean", "--older", "0"))
        self.assertEqual(self.jm("status", a)[0], 3)

    def test_cwd_expands_config_env(self):
        data = os.path.join(self.work, "data")
        os.makedirs(os.path.join(data, "proj"))
        self.jm_ok("config", "env=DATA=" + data)
        a = self.submit(spec(sim(0.1, print_env=["DATA"], out="here.txt"), cwd="$DATA/proj"))
        self.assertEqual(self.wait_ended(a), "done")
        self.assertIn("DATA=" + data, jm.read_text(os.path.join(data, "proj", "here.txt")))

    def test_artifacts(self):
        path = self.write("out.bin", "x" * 10)
        a = self.submit(spec(sim(0.1), env="OUT=out.bin", artifacts="$OUT"))
        self.jm_ok("artifacts", a)
        lines = self.jm_ok("artifacts", a).split("\t")
        self.assertEqual(lines[0], path)
        self.assertEqual(lines[1], "10")


class TestHooks(JmTestCase):
    """on_first_start / on_idle: run outside the state lock, once per busy period, with JM_HOOK set."""

    def hook(self, out, secs=0, exit=0):
        return "%s %s _sim --secs %s --exit %d --print-env JM_HOOK --print-env JM_HOME --out %s" % (
            q(PY), q(JM), secs, exit, q(out))

    def test_once_per_busy_period(self):
        first = os.path.join(self.work, "first.txt")
        idle = os.path.join(self.work, "idle.txt")
        self.jm_ok("config", "on_first_start=" + self.hook(first), "on_idle=" + self.hook(idle), "idle_after=1.5s")
        a = self.submit(spec(sim(0.5)))
        b = self.submit(spec(sim(0.5), after=a))  # starts right after a: within idle_after, no flip in between
        self.wait_ended(a)
        self.wait_ended(b)
        self.until(lambda: os.path.exists(idle), "on_idle ran")
        self.assertEqual(jm.read_text(first).count("JM_HOOK=on_first_start"), 1)
        self.assertEqual(jm.read_text(idle).count("JM_HOOK=on_idle"), 1)
        self.until(lambda: jm.read_text(os.path.join(self.home, "machine")).startswith("idle "), "machine idle")
        self.assertIn("machine: idle since", self.jm_ok("status"))

    def test_cli_not_blocked_by_hook(self):
        first = os.path.join(self.work, "first.txt")
        self.jm_ok("config", "on_first_start=" + self.hook(first, secs=3), "idle_after=0")
        a = self.submit(spec(sim(0.2)))
        self.until(lambda: "on_first_start" in self.reason(a), "waits for the hook")
        t0 = time.time()
        self.jm_ok("list")
        self.jm_ok("hold", a)
        self.jm_ok("release", a)
        self.assertLess(time.time() - t0, 2.5)  # the hook holds no lock
        self.assertEqual(self.wait_ended(a), "done")
        self.assertIn("JM_HOOK=on_first_start", jm.read_text(first))

    def test_failing_first_start_is_retried(self):
        first = os.path.join(self.work, "first.txt")
        self.jm_ok("config", "on_first_start=" + self.hook(first, exit=3), "idle_after=0")
        a = self.submit(spec(sim(0.2)))
        self.until(lambda: "on_first_start failed (exit 3" in self.reason(a), "failure shown")
        self.assertEqual(self.state(a), "waiting")
        self.jm_ok("config", "on_first_start=" + self.hook(first))
        self.assertEqual(self.wait_ended(a), "done")


class TestCrashes(JmTestCase):
    def test_daemon_killed_job_finishes_meanwhile(self):
        a = self.submit(spec(sim(1.0)))
        self.wait_state(a, "running")
        self.kill_daemon()
        self.until(lambda: os.path.exists(os.path.join(self.home, "jobs", a, "exit")), "job ended alone")
        self.assertEqual(self.state(a), "running")  # nobody recorded it yet
        self.start_daemon()
        self.assertEqual(self.wait_ended(a), "done")
        self.assertEqual(len(self.runs(a)), 1)

    def test_daemon_killed_job_adopted(self):
        a = self.submit(spec(sim(2.0)))
        self.wait_state(a, "running")
        self.kill_daemon()
        self.start_daemon()
        time.sleep(0.6)
        self.assertEqual(self.state(a), "running")  # adopted, not restarted
        self.assertEqual(self.wait_ended(a), "done")
        self.assertEqual(len(self.runs(a)), 1)
        self.assertNotIn("cut off", self.jobfile(a, "events"))

    def kill_wrapper(self, jid):
        f = self.jobfile(jid, "pid").split()
        self.until(lambda: os.path.exists(os.path.join(self.home, "jobs", jid, "child")), "child started")
        self.stop_daemon()  # so the cut-off is found when the daemon starts, as after a reboot
        hard_kill(f[0])
        self.until(lambda: not jm.proc_alive(f[0], f[1]), "wrapper dead")

    def test_on_boot_resume(self):
        counter = os.path.join(self.work, "count")
        a = self.submit(spec(sim(1.5, resumable=counter)))
        self.wait_state(a, "running")
        time.sleep(0.5)
        self.kill_wrapper(a)
        self.start_daemon()
        self.assertEqual(self.wait_ended(a), "done")
        runs = self.runs(a)
        self.assertEqual([r[3] for r in runs], ["cut", "0"])
        self.assertEqual(runs[1][4], "resume")
        self.assertIn("sim: resumed at", self.jobfile(a, "log"))
        self.assertIn("cut off", self.jobfile(a, "events"))

    def test_on_boot_hold_and_fail(self):
        a = self.submit(spec(sim(20), on_boot="hold"))
        b = self.submit(spec(sim(20), on_boot="fail"))
        self.wait_state(a, "running")
        self.wait_state(b, "running")
        fb = self.jobfile(b, "pid").split()
        self.until(lambda: os.path.exists(os.path.join(self.home, "jobs", b, "child")), "child b")
        self.kill_wrapper(a)
        hard_kill(fb[0])
        self.until(lambda: not jm.proc_alive(fb[0], fb[1]), "wrapper b dead")
        self.start_daemon()
        self.assertEqual(self.wait_state(a, "held"), "held")
        self.assertEqual(self.wait_state(b, "failed"), "failed")
        self.assertIn("cut off", self.reason(b))
        for jid in (a, b):  # the orphaned job processes were killed
            ch = self.jobfile(jid, "runs")
            self.assertIn("cut", ch)

    def test_pid_reuse(self):
        self.stop_daemon()
        a = self.submit(spec(sim(0.1), on_boot="fail"))
        d = os.path.join(self.home, "jobs", a)
        # A pid file pointing at a live, unrelated process (this test) with its real start time.
        me = os.getpid()
        with open(os.path.join(d, "pid"), "w") as f:
            f.write("%d %s %s\n" % (me, jm.proc_start_time(me), "0123456789abcdef"))
        with open(os.path.join(d, "state"), "w") as f:
            f.write("running\n")
        self.start_daemon()
        self.assertEqual(self.wait_ended(a), "failed")
        self.assertIn("cut off", self.reason(a))


class TestGroups(JmTestCase):
    def test_static_group(self):
        self.write("c1.jm", "name = c1\nstart = %s\n" % sim(0.4))
        self.write("c2.jm", "name = c2\nstart = %s\n" % sim(0.4))
        path = self.write("g.jm", "name = group\nchildren = c1.jm\nchildren = c2.jm\nparallel = 1\n")
        other = self.submit(spec(sim(0.1)), "--hold")
        ids = self.jm_ok("submit", path).split()
        self.assertEqual(len(ids), 3)
        p, c1, c2 = ids
        self.assertEqual(self.queue(), [other, p, c1, c2])
        self.assertEqual(self.wait_ended(p), "done")
        self.assertEqual([self.state(c1), self.state(c2)], ["done", "done"])
        self.assertGreaterEqual(self.runs(c2)[0][1], self.runs(c1)[0][2])  # parallel = 1
        tree = self.jm_ok("list", "--tree").splitlines()
        self.assertTrue([r for r in tree if r.startswith("  " + c1 + "\t")], tree)

    def test_group_child_fails(self):
        self.write("ok.jm", "start = %s\n" % sim(0.1))
        self.write("bad.jm", "start = %s\n" % sim(0.1, exit=2))
        self.write("later.jm", "start = %s\n" % sim(0.1))
        path = self.write("g.jm", "children = bad.jm\nchildren = later.jm\nparallel = 1\n")
        p, bad, later = self.jm_ok("submit", path).split()
        self.assertEqual(self.wait_ended(p), "failed")
        self.assertEqual(self.state(bad), "failed")
        self.assertEqual(self.state(later), "cancelled")
        path2 = self.write("g2.jm", "children = bad.jm\nchildren = ok.jm\nparallel = 1\ncontinue_on_fail = yes\n")
        p2, bad2, ok2 = self.jm_ok("submit", path2).split()
        self.assertEqual(self.wait_ended(p2), "failed")
        self.assertEqual(self.state(ok2), "done")
        self.assertIn(bad2, self.reason(p2))

    def test_continue_on_fail_flag(self):
        # The submit flag sets continue_on_fail: the queued sibling still runs after the first child fails.
        self.write("bad.jm", "start = %s\n" % sim(0.1, exit=2))
        self.write("ok.jm", "start = %s\n" % sim(0.1))
        path = self.write("g.jm", "children = bad.jm\nchildren = ok.jm\nparallel = 1\n")
        p, bad, ok = self.jm_ok("submit", path, "--continue-on-fail").split()
        self.assertTrue(jm.parse_spec(self.jobfile(p, "spec"))["continue_on_fail"])
        self.assertEqual(self.wait_ended(p), "failed")
        self.assertEqual(self.state(bad), "failed")
        self.assertEqual(self.state(ok), "done")

    def test_dynamic_children(self):
        jmcmd = "%JM_JM%" if IS_WIN else "$JM_JM"
        jid = "%JM_ID%" if IS_WIN else '"$JM_ID"'
        cmd = "%s submit --parent %s --name kid -- %s %s _sim --secs 0.8" % (jmcmd, jid, q(PY), q(JM))
        p = self.submit(spec(cmd))
        self.until(lambda: self.jobfile(p, "children").strip(), "child submitted")
        kid = self.jobfile(p, "children").split()[0]
        self.until(lambda: os.path.exists(os.path.join(self.home, "jobs", p, "exit")), "parent command done")
        self.assertEqual(self.wait_state(p, "running", "done"), "running")  # waits for its child
        self.assertEqual(self.wait_ended(kid), "done")
        self.assertEqual(self.wait_ended(p), "done")
        self.assertEqual(jm.Job(jm.Store(self.home), kid).parent, p)

    def test_cancel_parent_asks(self):
        self.write("c.jm", "start = %s\n" % sim(20))
        path = self.write("g.jm", "children = c.jm\n")
        p, c = self.jm_ok("submit", path).split()
        self.wait_state(c, "running")
        rc, _, err = self.jm("cancel", p)
        self.assertEqual(rc, 4)
        self.assertIn("--children", err)
        self.jm_ok("cancel", p, "--children")
        self.assertEqual(self.wait_ended(c), "cancelled")
        self.assertEqual(self.wait_ended(p), "cancelled")


class TestLogs(JmTestCase):
    """`jm log --children / --stdout / --stderr` (split_stderr) and `jm applog` (RDD_011)."""

    def test_split_stderr_and_children(self):
        self.write("c1.jm", "split_stderr = yes\nstart = %s\n" % sim(0.1, stderr="err-one"))
        self.write("c2.jm", "start = %s\n" % sim(0.1, stderr="err-two"))
        path = self.write("g.jm", "children = c1.jm\nchildren = c2.jm\nparallel = 1\n")
        p, c1, c2 = self.jm_ok("submit", path).split()
        rc, out, _ = self.jm("log", p, "--children", "-f", timeout=30)
        self.assertEqual(rc, 0)
        self.assertEqual(self.state(p), "done")  # -f returns once the group and its children have ended
        heads = ["==> %s <==" % c1, "==> %s <==" % c2]
        self.assertEqual([x for x in out.splitlines() if x.startswith("==> ")], heads)  # the group has no output
        self.assertIn("err-one", self.jobfile(c1, "stderr"))
        self.assertNotIn("err-one", self.jobfile(c1, "log"))
        self.assertIn("err-two", self.jobfile(c2, "log"))
        out = self.jm_ok("log", p, "--children")
        self.assertEqual([x for x in out.splitlines() if x.startswith("==> ")], heads)
        self.assertEqual(out.count("sim: finished"), 2)
        rc, out, err = self.jm("log", p, "--children", "--stderr")
        self.assertEqual(rc, 0)
        self.assertIn("err-one", out)
        self.assertNotIn("sim: finished", out)
        self.assertIn("%s: split_stderr = no" % c2, err)
        out = self.jm_ok("log", c1, "--stdout")
        self.assertIn("sim: finished", out)
        self.assertNotIn("err-one", out)
        rc, _, err = self.jm("log", c2, "--stderr")
        self.assertEqual(rc, 1)
        self.assertIn("split_stderr = yes", err)
        self.assertIn("stderr (last 3 lines)", self.jm_ok("status", c1))


class TestApplog(JmTestCase):
    daemon = False

    def test_applog(self):
        self.start_daemon({"MY_API_TOKEN": "s3cr3t-value"})
        show = "%s %s _sim --secs 0 --print-env JM_ID --print-env JM_LINES --print-env MY_API_TOKEN" % (q(PY), q(JM))
        self.write("c.jm", "applog = %s\nstart = %s\n" % (show, sim(0.1)))
        self.write("plain.jm", "start = %s\n" % sim(0.1))
        path = self.write("g.jm", "applog = %s\nenv_pass = MY_API_TOKEN\nchildren = c.jm\nchildren = plain.jm\n" % show)
        p, c, plain = self.jm_ok("submit", path).split()
        self.assertEqual(self.wait_ended(p), "done")
        out = self.jm_ok("applog", p)
        self.assertIn("env JM_ID=%s" % p, out)
        self.assertIn("env MY_API_TOKEN=<unset>", out)  # env_pass is for the job, never for its applog
        self.assertNotIn("==> ", out)
        out = self.jm_ok("applog", p, "--children", "-n", "2")
        self.assertEqual(out.splitlines(), ["==> %s <==" % p, "env MY_API_TOKEN=<unset>", "sim: finished, exit 0",
                                            "==> %s <==" % c, "env MY_API_TOKEN=<unset>", "sim: finished, exit 0"])
        self.assertIn("env JM_LINES=5", self.jm_ok("applog", c, "-n", "5"))
        # -f on an ended job: a command that is slow to print still gets to, before jm ends it.
        slow = self.submit(spec(sim(0.1), applog="%s %s _sim --secs 0.5" % (q(PY), q(JM))))
        self.wait_ended(slow)
        self.assertIn("sim: finished", self.jm_ok("applog", slow, "-f"))
        rc, _, err = self.jm("applog", plain)
        self.assertEqual(rc, 1)
        self.assertIn("no `applog`", err)
        bad = self.submit(spec(sim(0.1), applog="%s %s _sim --secs 0 --exit 3" % (q(PY), q(JM))))
        self.wait_ended(bad)
        rc, _, err = self.jm("applog", bad)
        self.assertEqual(rc, 1)
        self.assertIn("exited 3", err)

    def test_follow_ends_with_the_job(self):
        # An applog that never ends by itself (think `tail -f`): `-f` ends it once the job has ended.
        self.start_daemon()
        a = self.submit(spec(sim(0.5), applog="%s %s _sim --secs 120" % (q(PY), q(JM))))
        self.wait_state(a, "running")
        t0 = time.time()
        rc, _, err = self.jm("applog", a, "-f", timeout=60)
        self.assertEqual(rc, 0, err)
        self.assertLess(time.time() - t0, 30)
        self.assertEqual(self.state(a), "done")


class TestSecretsRun(JmTestCase):
    daemon = False

    def test_env_pass_and_filtering(self):
        self.start_daemon({"MY_API_TOKEN": "s3cr3t-value", "OTHER_SECRET": "hidden", "PLAIN": "visible"})
        out = os.path.join(self.work, "env.txt")
        a = self.submit(spec(sim(0.1, print_env=["MY_API_TOKEN", "OTHER_SECRET", "PLAIN"], out=out),
                             env_pass="MY_API_TOKEN"))
        self.assertEqual(self.wait_ended(a), "done")
        text = jm.read_text(out)
        self.assertIn("MY_API_TOKEN=s3cr3t-value", text)
        self.assertIn("OTHER_SECRET=<unset>", text)
        self.assertIn("PLAIN=visible", text)
        st = self.jm_ok("status", a)
        self.assertIn("env_pass MY_API_TOKEN", st)
        self.assertNotIn("s3cr3t", st)
        b = self.submit(spec(sim(0.1), env_pass="NOT_SET_ANYWHERE_TOKEN"))
        self.assertEqual(self.wait_ended(b), "failed")
        self.assertIn("env_pass NOT_SET_ANYWHERE_TOKEN", self.reason(b))

    @unittest.skipIf(IS_WIN, "file modes are not checked on Windows")
    def test_env_file(self):
        self.start_daemon()
        path = self.write("s.env", "# comment\nexport DB_PASSWORD='pw 1'\n", 0o600)
        out = os.path.join(self.work, "env.txt")
        a = self.submit(spec(sim(0.1, print_env=["DB_PASSWORD"], out=out), env_file=path))
        self.assertEqual(self.wait_ended(a), "done")
        self.assertIn("DB_PASSWORD=pw 1", jm.read_text(out))
        os.chmod(path, 0o644)
        self.jm_ok("requeue", a)
        self.until(lambda: len(self.runs(a)) == 2 and self.state(a) == "failed", "second run refused")
        self.assertIn("chmod 600", self.reason(a))

    def test_log_masking(self):
        self.start_daemon()
        tok = "github_pat_11ABCDEFG0123456789_abcdefghijklmnop"
        cmd = "%s -c %s" % (q(PY), q("print('pushing with ' + 'github_pat_' + '%s')" % tok[len("github_pat_"):]))
        a = self.submit(spec(cmd))
        self.assertEqual(self.wait_ended(a), "done")
        self.assertIn(tok, self.jobfile(a, "log"))  # the file on disk is as the job wrote it
        shown = self.jm_ok("log", a)
        self.assertIn("pushing with github_pat_***", shown)
        self.assertNotIn(tok, shown)
        self.assertNotIn(tok, self.jm_ok("status", a))


if __name__ == "__main__":
    unittest.main()
