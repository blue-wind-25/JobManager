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

# Unit tests of the pure parts of jm.py: durations, the spec / config parser and editor, secret rules,
# process identity.

import os
import shutil
import subprocess
import tempfile
import time
import unittest

from jmtest import jm, PY


class TestDurations(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(jm.parse_duration("90"), 90)
        self.assertEqual(jm.parse_duration("0.2s"), 0.2)
        self.assertEqual(jm.parse_duration("30m"), 1800)
        self.assertEqual(jm.parse_duration("12h"), 43200)
        self.assertEqual(jm.parse_duration("7d"), 7 * 86400)
        self.assertEqual(jm.parse_duration("1h30m"), 5400)
        for bad in ("", "x", "5y", "-3"):
            self.assertRaises(ValueError, jm.parse_duration, bad)

    def test_format(self):
        self.assertEqual(jm.fmt_duration(5), "5s")
        self.assertEqual(jm.fmt_duration(65), "1m05s")
        self.assertEqual(jm.fmt_duration(3720), "1h02m")
        self.assertEqual(jm.fmt_duration(90000), "1d01h")

    def test_utc_round_trip(self):
        t = 1790000000
        self.assertEqual(jm.parse_utc(jm.utc(t)), t)

    def test_elapsed(self):
        # A stamp from this boot counts on the monotonic clock: the wall-clock time beside it (here years off,
        # as on a Pi that booted without RTC before NTP set the date) does not matter.
        info = {}
        jm.stamp_now(info, "t")
        self.assertLess(jm.elapsed(info, "t"), 5)
        if jm.boot_id():
            info = {"t": "2000-01-01T00:00:00Z", "t_mono": "%s %.3f" % (jm.boot_id(), time.monotonic() - 5)}
            self.assertAlmostEqual(jm.elapsed(info, "t"), 5, delta=1)
            info["t_mono"] = "another-boot %.3f" % (time.monotonic() - 5)  # after a reboot: the wall clock
            self.assertGreater(jm.elapsed(info, "t"), 86400)
        self.assertIsNone(jm.elapsed({}, "t"))
        self.assertGreater(jm.elapsed({"t": "2000-01-01T00:00:00Z"}, "t"), 86400)  # old state: wall clock

    @unittest.skipIf(jm.IS_WIN, "logs are trimmed between runs only on Windows")
    def test_trim_log(self):
        d = tempfile.mkdtemp(prefix="jm-unit-")
        try:
            path = os.path.join(d, "log")
            with open(path, "wb") as f:
                f.write(b"x" * 3000)
            self.assertFalse(jm.trim_log(path, 0.01))  # 10 KB: under the cap
            self.assertTrue(jm.trim_log(path, 0.002))
            self.assertEqual(os.path.getsize(path), 0)
            self.assertEqual(os.path.getsize(path + ".1"), 3000)
            self.assertFalse(jm.trim_log(path, 0))  # 0 = no limit
        finally:
            shutil.rmtree(d, ignore_errors=True)


SPEC = """# gso_b top-5 run
id          = gso_b_top5                 # optional
name        = gso_b restart, best-by top5
env         = SET=gso_b
env         = DEEP_EXTRA_ARGS=--camera-fx 0.5 --best-by top5
start       = sh run.sh \\
              foreground
stop        =                            # default
stop_grace  = 60
progress    = grep 'epoch [0-9]*:' run.log | tail -n 1   # one line
cpu         = 4
ram         = 2500
disk        = 2000 @ /mnt/data
tokens      = trainer
tokens      = gpu:2
after       = gso_b_render
after_ok    = no
timeout     = 12h
on_boot     = hold
notify      = curl -s -d "x" https://ntfy.sh/topic\\#frag
"""


class TestSpec(unittest.TestCase):
    def test_parse_example(self):
        s = jm.parse_spec(SPEC)
        self.assertEqual(s["id"], "gso_b_top5")
        self.assertEqual(s["name"], "gso_b restart, best-by top5")
        self.assertEqual(s["env"], [("SET", "gso_b"), ("DEEP_EXTRA_ARGS", "--camera-fx 0.5 --best-by top5")])
        self.assertEqual(s["start"], "sh run.sh               foreground")
        self.assertIsNone(s["stop"])
        self.assertEqual(s["progress"], "grep 'epoch [0-9]*:' run.log | tail -n 1")
        self.assertEqual(s["cpu"], 4.0)
        self.assertEqual(s["disk"], (2000.0, "/mnt/data"))
        self.assertEqual(s["tokens"], [("trainer", 1), ("gpu", 2)])
        self.assertEqual(s["after"], ["gso_b_render"])
        self.assertFalse(s["after_ok"])
        self.assertEqual(s["timeout"], 43200)
        self.assertEqual(s["on_boot"], "hold")
        self.assertEqual(s["notify"], 'curl -s -d "x" https://ntfy.sh/topic#frag')
        # defaults
        self.assertEqual(s["retries"], 0)
        self.assertEqual(s["nice"], 5)
        self.assertTrue(jm.parse_spec("start = x")["after_ok"])

    def test_errors(self):
        for bad, frag in (("strat = x", "unknown key"), ("start = a\nstart = b", "given twice"),
                          ("cpu = many", "cpu"), ("on_boot = maybe", "on_boot"), ("just words", "key = value"),
                          ("tokens = a:b", "tokens"), ("env = 1X=2", "env"), ("drain = perhaps", "drain")):
            with self.assertRaises(jm.JmError) as cm:
                jm.parse_spec(bad)
            self.assertIn(frag, str(cm.exception))

    def test_hash_rules(self):
        s = jm.parse_spec("start = echo a#b # comment\nnote = \\# not a comment")
        self.assertEqual(s["start"], "echo a#b")
        self.assertEqual(s["note"], "# not a comment")

    def test_edit_keeps_comments(self):
        text = "# header\nname = a   # the name\nstart = x \\\n  y\nenv = A=1\n# tail\n"
        t2 = jm.edit_kv_text(text, "start", ["z # not comment"])
        self.assertIn("# header", t2)
        self.assertIn("# tail", t2)
        self.assertIn("name = a   # the name", t2)
        s = jm.parse_spec(t2)
        self.assertEqual(s["start"], "z # not comment")
        t3 = jm.edit_kv_text(t2, "env", ["B=2"], append=True)
        self.assertEqual(jm.parse_spec(t3)["env"], [("A", "1"), ("B", "2")])
        t4 = jm.edit_kv_text(t3, "env", [])
        self.assertEqual(jm.parse_spec(t4)["env"], [])
        t5 = jm.edit_kv_text(t4, "retries", ["3"])
        self.assertEqual(jm.parse_spec(t5)["retries"], 3)

    def test_config(self):
        c = jm.parse_config("cpus = 4\nram = 4000\nram_reserve = 500\ntokens = trainer:1\ntokens = gpu\n"
                            "drain_after = 30m\npoll = 5s\nbackfill = no\n")
        self.assertEqual(c["token_counts"], {"trainer": 1, "gpu": 1})
        self.assertFalse(c["backfill"])
        self.assertEqual(c["drain_after"], 1800)
        d = jm.parse_config("")
        self.assertEqual(d["cpus"], float(os.cpu_count() or 1))
        self.assertGreater(d["ram"], 0)
        self.assertRaises(jm.JmError, jm.parse_config, "cpu = 4")


class TestSecrets(unittest.TestCase):
    def test_names(self):
        for n in ("GITHUB_TOKEN", "GH_TOKEN", "API_KEY", "APIKEY", "MY_SECRET", "DB_PASSWORD", "AWS_CREDENTIALS",
                  "PETAI_DONGLE_SECRET_PASSPHRASE", "token"):
            self.assertTrue(jm.is_secret_name(n), n)
        for n in ("PATH", "MAX_TOKENS", "TOKENIZERS_PARALLELISM", "HOME", "SECRETARY"):
            self.assertFalse(jm.is_secret_name(n), n)
        cfg = jm.parse_config("secret_names = MYPASS\nnot_secret = JOB_TOKEN\n")
        self.assertTrue(jm.is_secret_name("MYPASS", cfg))
        self.assertFalse(jm.is_secret_name("JOB_TOKEN", cfg))

    def test_values(self):
        for v in ("ghp_abc", "github_pat_11ABC_def", "x=glpat-123", "sk-ant-api03-xyz", "sk-" + "a" * 24,
                  "xoxb-1-2", "AKIA" + "A" * 16, "AIza" + "b" * 35, "Bearer gho_q",
                  "-----BEGIN RSA PRIVATE KEY-----\nMIIx\n-----END RSA PRIVATE KEY-----"):
            self.assertTrue(jm.has_secret_value(v), v)
        for v in ("sk-learn", "hello", "/mnt/data", "ghp_", "task-ghp"):
            self.assertFalse(jm.has_secret_value(v), v)
        cfg = jm.parse_config("secret_prefixes = acme_\n")
        self.assertTrue(jm.has_secret_value("acme_123", cfg))

    def test_mask(self):
        line = "push with github_pat_11ABCDEF_xyz and ghp_secret123 then sk-" + "z" * 30 + " ok"
        m = jm.mask_secrets(line)
        self.assertEqual(m, "push with github_pat_*** and ghp_*** then sk-*** ok")
        pk = "a\n-----BEGIN PRIVATE KEY-----\nMIIEv\nQQQ\n-----END PRIVATE KEY-----\nb"
        self.assertEqual(jm.mask_secrets(pk), "a\n-----BEGIN PRIVATE KEY-----***\nb")


class TestProcessIdentity(unittest.TestCase):
    def test_self(self):
        pid = os.getpid()
        st = jm.proc_start_time(pid)
        self.assertIsNotNone(st)
        self.assertTrue(jm.proc_alive(pid, st))
        self.assertFalse(jm.proc_alive(pid, st + "x"))
        # The pid and start time match but the command line lacks the token: a reused pid, read as dead.
        self.assertFalse(jm.proc_alive(pid, st, "token-not-in-my-cmdline-0123456789"))

    def test_dead(self):
        p = subprocess.Popen([PY, "-c", "pass"])
        p.wait()
        self.assertIsNone(jm.proc_start_time(p.pid))
        self.assertFalse(jm.proc_alive(999999999, "1"))

    @unittest.skipIf(jm.IS_WIN, "a Windows process has its own command line from the start")
    def test_token_after_fork(self):
        # Right after the fork the child's command line is still its parent's: that miss must not count.
        pid, st = os.getpid(), jm.proc_start_time(os.getpid())
        seen = ["python parent", "python parent", "jm _run j1 tokfork"]
        real = jm.proc_cmdline
        jm.proc_cmdline = lambda p: seen.pop(0) if len(seen) > 1 else seen[0]
        try:
            self.assertTrue(jm.proc_alive(pid, st, "tokfork"))
        finally:
            jm.proc_cmdline = real

    def test_token(self):
        tok = "tok%d" % int(time.time())
        p = subprocess.Popen([PY, "-c", "import time; time.sleep(30)", tok])
        try:
            deadline = time.time() + 10
            st = None
            while st is None and time.time() < deadline:
                st = jm.proc_start_time(p.pid)
            self.assertTrue(jm.proc_alive(p.pid, st, tok))
        finally:
            p.kill()
            p.wait()


class TestExamples(unittest.TestCase):
    def test_examples_parse(self):
        d = os.path.join(os.path.dirname(jm.JM_PATH), "examples")
        names = [n for n in os.listdir(d) if n.endswith(".jm")]
        self.assertTrue(names)
        for n in names:
            jm.parse_spec(jm.read_text(os.path.join(d, n)), n)


class TestProgress(unittest.TestCase):
    # RDD_019: progress read from the token or from progress_re / progress_total.
    def spec(self, **keys):
        text = "start = true\n" + "".join("%s = %s\n" % kv for kv in keys.items())
        return jm.parse_spec(text)

    def test_progress_value(self):
        s = self.spec()
        self.assertEqual(jm.progress_value(s, "x jm:progress=3/40 y"), (3.0, 40.0))
        self.assertEqual(jm.progress_value(s, "jm:progress=12.5%"), (12.5, 100.0))
        self.assertIsNone(jm.progress_value(s, "epoch 3: loss 1"))
        s = self.spec(progress_re=r"epoch (\d+):")
        self.assertEqual(jm.progress_value(s, "[17] epoch 3: loss 1"), (3.0, None))
        s = self.spec(progress_re=r"epoch (\d+):", progress_total="40")
        self.assertEqual(jm.progress_value(s, "epoch 3: loss 1"), (3.0, 40.0))
        s = self.spec(progress_re=r"RENDER (\d+)/(\d+)")
        self.assertEqual(jm.progress_value(s, "RENDER 50/200 identities"), (50.0, 200.0))

    def test_progress_re_checked(self):
        cfg = jm.parse_config("cpus = 2\nram = 1000\n")
        with self.assertRaises(jm.JmError):
            jm.check_spec(self.spec(progress_re="epoch \\d+"), cfg)  # no group
        with self.assertRaises(jm.JmError):
            jm.check_spec(self.spec(progress_re="epoch (\\d+"), cfg)  # does not compile


class TestMisc(unittest.TestCase):
    def test_expand(self):
        env = {"A": "1", "B": "x"}
        self.assertEqual(jm.expand_vars("$A/${B}/$C/$$", env), "1/x//$")

    def test_version(self):
        out = subprocess.check_output([PY, jm.JM_PATH, "--version"], universal_newlines=True)
        self.assertIn(jm.VERSION, out)

    def test_usage_exit(self):
        p = subprocess.Popen([PY, jm.JM_PATH, "frobnicate"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p.communicate()
        self.assertEqual(p.returncode, 2)


if __name__ == "__main__":
    unittest.main()
