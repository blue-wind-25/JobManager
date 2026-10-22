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

# Queue operations and plain-text state, without a daemon: submit, list, move, swap, hold, set, edit, config,
# exit codes, service files.

import os
import sys
import unittest

from jmtest import JmTestCase, jm, sim, IS_WIN


class TestQueueOps(JmTestCase):
    daemon = False

    def test_submit_inline_and_ids(self):
        a = self.submit(None, "--name", "first", "--cpu", "1", "--ram", "200", "--", "echo", "hi there")
        b = self.submit("start = %s\n" % sim())
        c = self.submit("id = mine\nstart = %s\n" % sim())
        self.assertEqual((a, b, c), ("j0001", "j0002", "mine"))
        self.assertEqual(self.queue(), ["j0001", "j0002", "mine"])
        s = jm.parse_spec(self.jobfile(a, "spec"))
        self.assertEqual(s["name"], "first")
        self.assertEqual(s["ram"], 200)
        self.assertIn("hi there", s["start"])
        self.assertEqual(s["cwd"], self.work)  # added at submit
        self.assertEqual(self.state(a), "queued")
        rc, _, err = self.jm("submit", "-", input="id = mine\nstart = x\n")
        self.assertEqual(rc, 1)
        self.assertIn("already exists", err)
        rc, out, err = self.jm("list")
        self.assertEqual(rc, 0)
        rows = [r.split("\t") for r in out.splitlines()]
        self.assertEqual([r[0] for r in rows], ["j0001", "j0002", "mine"])
        self.assertEqual(rows[0][1:3], ["queued", "first"])
        self.assertIn("daemon is not running", self.jm("submit", "-", input="start = x")[2])

    def test_submit_file_and_positions(self):
        path = self.write("a.jm", "name = from file\nstart = %s\n" % sim())
        a = self.jm_ok("submit", path).split()[0]
        b = self.jm_ok("submit", path, "--top").split()[0]
        c = self.jm_ok("submit", path, "--after", b).split()[0]
        d = self.jm_ok("submit", path, "--hold", "--set", "retries=2").split()[0]
        self.assertEqual(self.queue(), [b, c, a, d])
        self.assertEqual(self.state(d), "held")
        self.assertEqual(jm.parse_spec(self.jobfile(d, "spec"))["retries"], 2)

    def test_submit_refusals(self):
        for text, frag in (("strat = x", "unknown key"), ("name = no start", "needs `start`"),
                           ("start = x\ntokens = gpu", "not in the config"),
                           ("start = x\ncpu = 64", "cpu 64 > cpus"),
                           ("start = x\nafter = nope", "no such job"),
                           ("start = x\nid = bad id", "use letters")):
            rc, _, err = self.jm("submit", "-", input=text)
            self.assertNotEqual(rc, 0, text)
            self.assertIn(frag, err, text)
        self.assertEqual(self.queue(), [])

    def test_move_and_swap(self):
        ids = [self.submit("start = %s\n" % sim()) for _ in range(5)]
        a, b, c, d, e = ids
        self.jm_ok("move", e, "--top")
        self.assertEqual(self.queue(), [e, a, b, c, d])
        self.jm_ok("move", e, "--bottom")
        self.assertEqual(self.queue(), [a, b, c, d, e])
        self.jm_ok("move", d, "--up", "2")
        self.assertEqual(self.queue(), [a, d, b, c, e])
        self.jm_ok("move", a, "--down")
        self.assertEqual(self.queue(), [d, a, b, c, e])
        self.jm_ok("move", e, "--before", a)
        self.assertEqual(self.queue(), [d, e, a, b, c])
        self.jm_ok("move", d, "--after", c)
        self.assertEqual(self.queue(), [e, a, b, c, d])
        self.jm_ok("swap", e, d)
        self.assertEqual(self.queue(), [d, a, b, c, e])
        self.jm_ok("set", c, "priority=5")
        self.jm_ok("set", e, "priority=9")
        self.jm_ok("move", "--by-priority")
        self.assertEqual(self.queue(), [e, c, d, a, b])
        self.assertEqual(self.jm("move", "nope", "--top")[0], 3)
        self.assertEqual(self.jm("move", a)[0], 2)

    def test_hold_release_stop_cancel_without_daemon(self):
        a = self.submit("start = %s\n" % sim())
        self.jm_ok("hold", a)
        self.assertEqual(self.state(a), "held")
        self.jm_ok("release", a)
        self.assertEqual(self.state(a), "queued")
        self.assertEqual(self.jm("release", a)[0], 4)
        self.jm_ok("stop", a)
        self.assertEqual(self.state(a), "stopped")
        self.assertIn(a, self.queue())
        self.jm_ok("requeue", a)
        self.assertEqual(self.state(a), "queued")
        self.jm_ok("cancel", a)
        self.assertEqual(self.state(a), "cancelled")
        self.assertNotIn(a, self.queue())
        self.assertEqual(self.jm("cancel", a)[0], 4)
        self.assertEqual(self.jm("restart", a)[0], 4)
        self.assertEqual(self.jm("status", "nope")[0], 3)
        events = self.jobfile(a, "events")
        for word in ("submitted", "held", "queued", "stopped", "cancelled"):
            self.assertIn(word, events)
        # still listed (finished within `keep`)
        self.assertIn(a, self.jm_ok("list"))

    def test_set_keeps_text(self):
        a = self.submit("# my comment\nname = x   # trailing\nstart = %s\nenv = A=1\n" % sim())
        self.jm_ok("set", a, "name=renamed", "env+=B=2", "retries=1")
        text = self.jobfile(a, "spec")
        self.assertIn("# my comment", text)
        s = jm.parse_spec(text)
        self.assertEqual(s["name"], "renamed")
        self.assertEqual(s["env"], [("A", "1"), ("B", "2")])
        self.assertEqual(s["retries"], 1)
        self.assertEqual(self.jm("set", a, "id=other")[0], 4)
        self.assertEqual(self.jm("set", a, "nokey=1")[0], 1)
        self.assertEqual(self.jm("set", a, "name+=x")[0], 1)

    def test_edit(self):
        a = self.submit("name = before\nstart = %s\n" % sim())
        editor = self.write("ed.py", "import sys\np = sys.argv[1]\ns = open(p).read()\n"
                            "open(p, 'w').write(s.replace('before', 'after'))\n")
        self.jm_ok("edit", a, env={"EDITOR": '"%s" "%s"' % (sys.executable, editor), "VISUAL": ""})
        self.assertEqual(jm.parse_spec(self.jobfile(a, "spec"))["name"], "after")
        bad = self.write("bad.py", "import sys\nopen(sys.argv[1], 'a').write('bogus = 1\\n')\n")
        rc, _, err = self.jm("edit", a, env={"EDITOR": '"%s" "%s"' % (sys.executable, bad), "VISUAL": ""})
        self.assertEqual(rc, 1)
        self.assertIn("unknown key", err)

    def test_hand_edit_round_trip(self):
        a = self.submit("name = one\nstart = %s\n" % sim())
        b = self.submit("name = two\nstart = %s\n" % sim())
        # Hand edits while no daemon runs: reorder the queue file, rename in the spec.
        with open(os.path.join(self.home, "queue"), "w") as f:
            f.write("%s\n%s\n" % (b, a))
        spec = os.path.join(self.home, "jobs", a, "spec")
        with open(spec) as f:
            text = f.read()
        with open(spec, "w") as f:
            f.write(text.replace("name = one", "name = uno  # renamed by hand"))
        rows = [r.split("\t") for r in self.jm_ok("list").splitlines()]
        self.assertEqual([(r[0], r[2]) for r in rows], [(b, "two"), (a, "uno")])

    def test_config(self):
        self.jm_ok("config", "tokens=gpu:2", "ram_reserve=100", "backfill=no")
        out = self.jm_ok("config")
        self.assertIn("tokens = gpu:2", out)
        self.assertIn("backfill = no", out)
        self.assertIn("poll = 0.2s", out)
        self.assertEqual(self.jm("config", "nokey=1")[0], 1)
        self.assertEqual(self.jm("config", "env=GITHUB_TOKEN=x")[0], 1)
        self.assertIn("cpus 4/4 ram 3900/3900 tokens gpu:2/2 queue 0", self.jm_ok("free"))
        self.assertIn("daemon: not running", self.jm_ok("status"))
        self.assertEqual(self.jm("daemon", "status")[0], 1)

    def test_service_files(self):
        for kind, name, needles in (
                ("systemd", "jm.service", ["ExecStart=", "daemon run", "KillMode=process", self.home]),
                ("launchd", "jm.plist", ["<string>daemon</string>", "AbandonProcessGroup", "RunAtLoad"]),
                ("windows", "jm.xml", ["LogonTrigger", "daemon run", "<Command>"])):
            path = os.path.join(self.work, name)
            out = self.jm_ok("daemon", "install", kind, "--out", path)
            self.assertIn("enable it with", out)
            enc = "utf-16" if kind == "windows" else "utf-8"
            with open(path, encoding=enc) as f:
                text = f.read()
            for n in needles:
                self.assertIn(n, text, kind)
        self.assertIn("daemon start --quiet", self.jm_ok("daemon", "bashrc"))
        self.assertEqual(self.jm("daemon", "install")[0], 2)


FAKE_SSH = """import subprocess, sys
a = sys.argv[1:]
while a[0].startswith("-"):
    a = a[2:] if a[0] == "-o" else a[1:]
sys.exit(subprocess.call(["sh", "-c", a[1]]))
"""


@unittest.skipIf(IS_WIN, "the fake ssh runs the remote command with sh")
class TestRemote(JmTestCase):
    """`jm -H HOST ...` through a fake ssh that runs the remote command line here (RDD_012)."""
    daemon = False

    def test_host(self):
        local = os.path.join(self.work, "local")
        os.makedirs(local)
        wrapper = self.write("jm-remote", '#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, jm.JM_PATH), 0o755)
        with open(os.path.join(local, "hosts"), "w") as f:
            f.write("box  me@box  %s\n" % wrapper)
        env = {"JM_HOME": local, "JM_SSH": '"%s" "%s"' % (sys.executable, self.write("fake_ssh.py", FAKE_SSH))}
        spec = self.write("s.jm", "name = from afar\ncwd = /tmp\nstart = sleep 1\n")
        a = self.jm_ok("-H", "box", "submit", spec, "--hold", env=env).strip()
        self.assertEqual(self.state(a), "held")
        self.assertIn("name = from afar", self.jobfile(a, "spec"))
        b = self.jm_ok("-H", "box", "submit", "--name", "two words", "--", "sh", "-c", "echo 1; echo $HOME",
                       env=env).strip()
        self.assertEqual(jm.parse_spec(self.jobfile(b, "spec"))["start"], "sh -c 'echo 1; echo $HOME'")
        self.assertEqual(len(self.jm_ok("-H", "box", "list", env=env).splitlines()), 2)
        self.assertEqual(self.jm("-H", "box", "status", "nosuch", env=env)[0], 3)  # exit codes come through


class TestSecretsSubmit(JmTestCase):
    daemon = False

    def test_refused(self):
        for text in ("start = x\nenv = GITHUB_TOKEN=x\n", "start = x\nenv = X=ghp_abc\n",
                     "start = curl -H 'Authorization: token ghp_abcdef' x\n",
                     "start = x\nenv_file = relative/path\n"):
            rc, _, err = self.jm("submit", "-", input=text)
            self.assertEqual(rc, 1, text)
        rc, _, err = self.jm("submit", "-", input="start = x\nenv = GITHUB_TOKEN=x\n")
        self.assertIn("env_pass", err)
        self.assertEqual(self.queue(), [])

    @unittest.skipIf(IS_WIN, "file modes are not checked on Windows")
    def test_env_file_mode(self):
        path = self.write("secrets.env", "API_TOKEN=abc\n", 0o644)
        rc, _, err = self.jm("submit", "-", input="start = x\nenv_file = %s\n" % path)
        self.assertEqual(rc, 1)
        self.assertIn("chmod 600", err)
        os.chmod(path, 0o600)
        self.submit("start = x\nenv_file = %s\n" % path)

    def test_raw_log_needs_terminal(self):
        a = self.submit("start = x\n")
        rc, _, err = self.jm("log", a, "--raw")
        self.assertEqual(rc, 4)
        self.assertIn("terminal", err)


if __name__ == "__main__":
    unittest.main()
