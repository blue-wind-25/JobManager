# Using jm from another machine (ssh)

jm runs one daemon per machine and has no network port: you drive a worker over ssh (decisions in RDD_012). The
commands are **the same as on the worker's own terminal**. Put `-H HOST` in front and jm sends the command over
ssh for you:

```sh
jm -H pi submit jobs/train.jm          # the spec file is on this machine: jm sends it, nothing to copy
jm -H pi list
jm -H pi log -f j0007                  # Ctrl-C stops following, as locally
jm -H pi applog --children -f j0007
jm -H pi stop j0007
jm -H pi@192.168.1.50 status           # any ssh target works too: user@ip, ip, an ~/.ssh/config alias
```

For the shortest command line, give each worker an alias in your shell's rc file:

```sh
alias jp='jm -H pi'                    # then: jp list, jp log -f j0007, jp submit jobs/train.jm
```

## 1. Set up once

On the workstation:
1. Copy `jm.py` onto PATH as `jm`, as on the workers (see the README). It needs no daemon and no state dir here.
2. Use ssh keys (or an agent): when the output is not a terminal (scripts), jm runs ssh with `BatchMode=yes`,
   which never asks for a password.
3. Optional: name the workers in `~/.jm/hosts` (or `$JM_HOME/hosts`), one per line:

   ```
   # NAME   SSH TARGET          JM ON THE WORKER     JM_HOME ON THE WORKER (optional)
   pi       pi@192.168.1.50     ~/bin/jm             /mnt/data/jm
   jetson   me@jetson.local     -                    # `-`: plain `jm` from the worker's PATH
   ```

   The third column matters: **ssh commands do not read `~/.bashrc`** (most `.bashrc` files stop early when not
   interactive), so a `jm` put on PATH there is not found (`jm` exits 127 and says so). Give its full path. The
   fourth column is the worker's state dir when it is not `~/.jm` (e.g. on a read-only root); `--home` on the
   command line still wins.
4. Optional, for speed: let ssh keep one connection open per worker, so each command starts at once
   (`~/.ssh/config`):

   ```
   Host pi
       HostName 192.168.1.50
       User pi
       ControlMaster auto
       ControlPath ~/.ssh/cm-%r@%h:%p
       ControlPersist 10m
   ```

## 2. What is the same, what differs

Everything a command prints, and its exit code, comes back unchanged (`jm -H pi wait j7; echo $?`). A few
things depend on where the command runs:

| | Locally on the worker | `jm -H HOST ...` |
|---|---|---|
| `submit FILE` | reads FILE | reads FILE **here** and sends it on stdin (`submit -`); nothing to copy |
| `submit GROUP_FILE` (with `children`) | reads the children next to it | children files are read on the worker, so the group must be there: the path is passed as is (relative to the worker's login directory, or absolute); jm warns |
| a spec without `cwd`, or `submit -- CMD` without `--cwd` | the job runs in the directory you submitted from | the job runs in **your login directory on the worker** - give `cwd` / `--cwd` |
| the job's environment | the daemon's environment (not yours) | the same: your workstation's variables never reach a job; `env_pass` names a variable of the worker's daemon |
| `log -f`, `applog -f`, `edit`, `stop` / `cancel` of a group (asks), `--raw` | need a terminal | jm adds `ssh -t` when you are at a terminal, so they work the same; in a script (output piped or redirected) no `-t`, so the output stays plain text with `\n` line ends and stderr separate |
| quoting | your shell | jm quotes every word for the worker's `sh`: `jm -H pi submit -- sh -c 'a; b'` arrives intact |
| exit codes | 0 1 2 3 4 | the same, plus 255 when ssh itself failed and 127 when jm was not found on the worker |

Not done over ssh: the daemon on the worker must be running (`jm -H pi daemon start`, or the service file
installed there with `jm -H pi daemon install systemd`); data and results stay on the worker (`jm -H pi
artifacts ID` lists them, `scp` fetches them).

## 3. Plain ssh

Without jm on the workstation (a phone, someone else's machine), plain ssh works with the same commands - and the
same caveats, handled by hand:

```sh
ssh pi jm list                          # `jm` must be on the worker's non-interactive PATH, else ~/bin/jm
ssh pi jm submit - < jobs/train.jm      # the spec on stdin
ssh -t pi jm log -f j0007               # -t: Ctrl-C reaches jm on the worker, which stops following
ssh pi "jm submit -- sh -c 'a; b'"      # one quoted string: ssh joins the words and the worker's shell splits them
```

Without `-t`, Ctrl-C ends only the local ssh; a `jm log -f` on the worker ends by itself when the job ends.

## 4. Limits

- The worker must have a POSIX shell (Linux, macOS): jm quotes for `sh`. A Windows workstation can drive Unix
  workers (`ssh.exe` is part of Windows 10 and later); Windows workers are driven with plain ssh.
- `JM_SSH` replaces the `ssh` command, e.g. `JM_SSH="ssh -p 2222"` (a port or key per host is better kept in
  `~/.ssh/config`).
- One host per command. Picking the host where a job fits (`--host any`) and copying data (`stage`) are design
  sec 12.4 (v3).
