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

# jm ssh / scp / keygen / remote (RDD_016). The command lines are checked on every platform; keygen runs where
# ssh-keygen exists; the round trip runs only when JM_TEST_SSH_HOSTS names a hosts file whose first host is a
# reachable sshd with jm on it (CI: the `jm-ssh` job, localhost).

import os
import shutil
import subprocess
import tempfile
import unittest

from jmtest import jm, JM, PY, IS_WIN

OWNER = """[cm5]
User                  = pi
Host                  = raspberrypi.local
StrictHostKeyChecking = no
UserKnownHostsFile    = /dev/null
LogLevel              = ERROR
SignInKey             =  ~/ssh-pi@raspberrypi-key

[jetson]
Host = jetson.local
Port = 2222
Jm   = /opt/jm/bin/jm    # an inline comment
JmHome = /data/jm
"""


def run(args, hosts):
    env = dict(os.environ)
    env["JM_HOSTS"] = hosts
    p = subprocess.Popen([PY, JM] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                         env=env)
    o, e = p.communicate()
    return p.returncode, o.decode("utf-8", "replace"), e.decode("utf-8", "replace")


class RemoteTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="jm-remote-")
        self.hosts = os.path.join(self.dir, "jmhosts")
        with open(self.hosts, "w") as f:
            f.write(OWNER)
        os.chmod(self.hosts, 0o600)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class TestHostsFile(RemoteTestCase):
    def test_order_and_case(self):
        hosts = jm.load_hosts(self.hosts)
        self.assertEqual([h[0] for h in hosts], ["cm5", "jetson"])
        self.assertEqual(hosts[0][1]["SignInKey"], "~/ssh-pi@raspberrypi-key")
        self.assertEqual(jm.pick_host(None, self.hosts)[0], "cm5")
        self.assertEqual(jm.pick_host("jetson", self.hosts)[1]["Port"], "2222")
        with self.assertRaises(jm.JmError):
            jm.pick_host("nope", self.hosts)

    def test_missing_file_and_host_key(self):
        self.assertEqual(jm.load_hosts(os.path.join(self.dir, "none")), [])
        bad = os.path.join(self.dir, "bad")
        with open(bad, "w") as f:
            f.write("[x]\nUser = pi\n")
        os.chmod(bad, 0o600)
        with self.assertRaises(jm.JmError):
            jm.load_hosts(bad)

    @unittest.skipIf(IS_WIN, "POSIX permission bits")
    def test_writable_by_others_refused(self):
        os.chmod(self.hosts, 0o666)
        with self.assertRaises(jm.JmError):
            jm.load_hosts(self.hosts)


class TestCommandLines(RemoteTestCase):
    def test_ssh(self):
        keys = jm.pick_host(None, self.hosts)[1]
        argv = jm.ssh_argv(keys, ["ls", "-la"])
        key = os.path.expanduser("~/ssh-pi@raspberrypi-key")
        self.assertEqual(argv, ["ssh", "-i", key, "-o", "LogLevel=ERROR", "-o", "StrictHostKeyChecking=no",
                                "-o", "UserKnownHostsFile=" + os.devnull, "pi@raspberrypi.local", "ls", "-la"])
        self.assertIn("-t", jm.ssh_argv(keys, [], tty=True))

    def test_port_and_no_user(self):
        keys = jm.pick_host("jetson", self.hosts)[1]
        self.assertEqual(jm.ssh_argv(keys, [])[:4], ["ssh", "-p", "2222", "jetson.local"])
        self.assertEqual(jm.scp_argv(keys, ["a", ":b"]), ["scp", "-r", "-P", "2222", "a", "jetson.local:b"])

    def test_scp_paths(self):
        keys = jm.pick_host(None, self.hosts)[1]
        argv = jm.scp_argv(keys, ["C:\\data\\x.bin", "rel/y", ":"])
        self.assertEqual(argv[-3:], ["C:\\data\\x.bin", "rel/y", "pi@raspberrypi.local:"])

    def test_remote_words(self):
        keys = jm.pick_host("jetson", self.hosts)[1]
        self.assertEqual(jm.remote_jm_argv(keys, ["log", "my job", "-n", "5"]),
                         ["/opt/jm/bin/jm --home /data/jm log 'my job' -n 5"])
        self.assertEqual(jm.remote_jm_argv({"Host": "h"}, ["list"]), ["jm list"])

    def test_split_host_args(self):
        self.assertEqual(jm.split_host_args(["--host", "a", "-t", "--", "-x"], ("-t",)), ("a", {"-t"}, ["-x"]))
        self.assertEqual(jm.split_host_args(["--host=b", "ls", "--host", "c"]), ("b", set(), ["ls", "--host", "c"]))

    def test_keygen_argv(self):
        self.assertEqual(jm.keygen_argv("k", "c", False)[-2:], ["-N", ""])
        self.assertNotIn("-N", jm.keygen_argv("k", "c", None))

    def test_cli_without_hosts(self):
        rc, _, err = run(["ssh", "true"], os.path.join(self.dir, "none"))
        self.assertEqual(rc, jm.EXIT_USAGE)
        self.assertIn("no hosts", err)
        rc, _, _ = run(["scp", "a", "b"], self.hosts)
        self.assertEqual(rc, jm.EXIT_USAGE)


@unittest.skipIf(shutil.which("ssh-keygen") is None, "no ssh-keygen on PATH")
class TestKeygen(RemoteTestCase):
    def test_new_key(self):
        k = os.path.join(self.dir, "sub", "k")
        rc, out, err = run(["keygen", "--host", "jetson", "--file", k, "--no-passphrase"], self.hosts)
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.isfile(k) and os.path.isfile(k + ".pub"))
        with open(k + ".pub") as f:
            self.assertTrue(f.read().startswith("ssh-ed25519 "))
        self.assertIn("SignInKey = " + k, out)
        rc, _, err = run(["keygen", "--host", "jetson", "--file", k, "--no-passphrase"], self.hosts)
        self.assertEqual(rc, 1)
        self.assertIn("exists", err)


@unittest.skipUnless(os.environ.get("JM_TEST_SSH_HOSTS"), "JM_TEST_SSH_HOSTS not set (needs a reachable sshd)")
class TestRoundTrip(unittest.TestCase):
    def setUp(self):
        self.hosts = os.environ["JM_TEST_SSH_HOSTS"]
        # The test submits, cancels and stops a daemon: only ever in a throwaway JmHome, never a real queue.
        if not jm.pick_host(None, self.hosts)[1].get("JmHome"):
            self.skipTest("the first host in JM_TEST_SSH_HOSTS has no JmHome (a scratch state dir)")
        self.dir = tempfile.mkdtemp(prefix="jm-remote-")

    def tearDown(self):
        run(["remote", "daemon", "stop"], self.hosts)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_ssh_scp_remote(self):
        rc, out, err = run(["ssh", "echo", "hello"], self.hosts)
        self.assertEqual((rc, out.strip()), (0, "hello"), err)
        self.assertEqual(run(["ssh", "--", "sh", "-c", "'exit 3'"], self.hosts)[0], 3)
        src = os.path.join(self.dir, "a.txt")
        with open(src, "w") as f:
            f.write("payload\n")
        remote = ":" + os.path.join(self.dir, "b.txt")
        self.assertEqual(run(["scp", src, remote], self.hosts)[0], 0)
        back = os.path.join(self.dir, "c.txt")
        self.assertEqual(run(["scp", remote, back], self.hosts)[0], 0)
        with open(back) as f:
            self.assertEqual(f.read(), "payload\n")
        rc, out, err = run(["remote", "--version"], self.hosts)
        self.assertEqual(rc, 0, err)
        self.assertIn("jm ", out)
        spec = os.path.join(self.dir, "t.jm")
        with open(spec, "w") as f:
            f.write("id = remote_t\nstart = true\n")
        rc, out, err = run(["remote", "submit", spec, "--hold"], self.hosts)
        self.assertEqual(rc, 0, err)
        rc, out, err = run(["remote", "list"], self.hosts)
        self.assertIn("remote_t", out, err)
        self.assertEqual(run(["remote", "cancel", "remote_t"], self.hosts)[0], 0)


if __name__ == "__main__":
    unittest.main()
