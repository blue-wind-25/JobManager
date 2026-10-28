# Using jm from another machine or a phone (ssh)

jm runs one daemon per machine and has no network port: you drive a worker over ssh (RDD_012). The commands
are **the same as on the worker's own terminal**; put `ssh -t WORKER` in front:

```sh
ssh -t pi jm list
ssh -t pi jm log -f j0007              # Ctrl-C stops following, as locally
ssh -t pi jm applog --children -f j0007
ssh -t pi jm stop j0007
ssh pi jm submit - < jobs/train.jm     # a spec from this machine, on stdin: no -t (see sec 2)
```

`-t` gives the remote jm a terminal: Ctrl-C reaches it, and so do the prompts (`stop` / `cancel` of a group),
`jm edit` ($EDITOR) and `--raw`. Leave it out in scripts and pipes (`ssh pi jm list | cut -f1,2`): with `-t`
the output gets `\r\n` line ends and stderr mixed into stdout. Exit codes come through
(`ssh pi jm wait j0007; echo $?`); 255 means ssh itself failed.

The shortest form is an alias per worker on your workstation, plus a short host name (sec 1):

```sh
alias jp='ssh -t pi jm'                # then: jp list, jp log -f j0007, jp stop j0007
```

## 1. Set up once

1. **Make `jm` (and `JM_HOME`) visible to ssh commands on the worker.** `ssh pi jm ...` runs without an
   interactive login, and most `~/.bashrc` files stop at their first lines in that case
   (`case $- in *i*) ;; *) return;; esac`), so a PATH or `JM_HOME` set further down is missing:
   `bash: jm: command not found` (exit 127), or jm looking at `~/.jm` instead of your state dir. Either
   - put jm in a directory that is always on PATH: `sudo ln -s ~/bin/jm /usr/local/bin/jm`; or
   - move the `export PATH=...` / `export JM_HOME=...` lines to the **top** of the worker's `~/.bashrc`, above
     that `case` line (zsh: `~/.zshenv`).

   Check from the workstation: `ssh pi 'command -v jm; jm status'` must print jm's path and the right `home:`.
2. **Key login** (`ssh-copy-id pi@192.168.1.50`), so no password is asked on every command.
3. **A short host name** and one kept-open connection, so each command is short and starts at once
   (`~/.ssh/config` on the workstation):

   ```
   Host pi
       HostName 192.168.1.50
       User pi
       ControlMaster auto
       ControlPath ~/.ssh/cm-%r@%h:%p
       ControlPersist 10m
   ```

## 2. What is the same, what differs

| | Locally on the worker | Over ssh |
|---|---|---|
| every command, its output, its exit code | | the same |
| quoting | your shell splits the words once | ssh joins the words and the worker's shell splits them **again**: quote a command with spaces or `;` as one more level, `ssh pi "jm submit -- sh -c 'a; b'"` |
| `submit FILE` | reads FILE | FILE is a path **on the worker**; for a spec on your machine use stdin: `ssh pi jm submit - < FILE` (no `-t`: stdin carries the spec) |
| a group spec (`children = FILE`) | children read next to it | the group and its children files must be on the worker (`scp -r`, or the project checkout there) |
| a spec without `cwd`, or `submit -- CMD` without `--cwd` | the job runs in the directory you submitted from | the job runs in **your login directory on the worker**: give `cwd` / `--cwd` |
| the job's environment | the daemon's (not yours) | the same: your workstation's variables never reach a job; `env_pass` names a variable of the worker's daemon |
| `log -f`, `applog -f`, `edit`, group `stop` / `cancel` prompts, `--raw` | need a terminal | need `ssh -t` |
| Ctrl-C during `log -f` | stops it | with `-t` the same; without, only the local ssh ends (the remote `log -f` ends when the job ends) |

A dropped connection only ends what you were following: jobs run under jm's detached wrappers, never under your
ssh session. The daemon on the worker is started there (`ssh pi jm daemon start`, or once with
`ssh pi jm daemon install systemd` and the commands it prints). Results stay on the worker: `ssh pi jm artifacts
ID` lists them, `scp` fetches them.

## 3. From a phone

A phone ssh app (ConnectBot, JuiceSSH, Termux, ...) logs into the worker itself, so jm runs there and the
commands are the local ones; the work is in keeping them short to type. Set up the worker once:

1. Copy [examples/jm-shortcuts.sh](../examples/jm-shortcuts.sh) next to jm (e.g. `~/bin/`) and source it from the
   worker's `~/.bashrc`, after the line that puts jm on PATH:

   ```sh
   . ~/bin/jm-shortcuts.sh
   ```

2. That gives these commands; **the ID is optional**: without it they take the first running job at the top
   of the queue, and for a group the whole group (`--children`):

   | Type | Runs | |
   |---|---|---|
   | `jl` | `jm list --tree --order --fields id,state,progress` | the queue in run order: id, state, progress (narrow) |
   | `je` | `jm list --tree --order --fields id,state,eta` | the same with each job's expected end (1.6.0) |
   | `js [ID]` | `jm status ID` | state, runs, live RSS / CPU, last log lines |
   | `jf [ID]` | `jm log -f --children ID` | follow what the job prints; ends when the job ends |
   | `ja [ID]` | `jm applog -f --children ID` | follow the job's own log (`applog`) |
   | `jt [ID] [N]` | `jm log -n N --children ID` | the last N (30) lines, without following |
   | `jr [ID] [N]` | `jm report [ID] [-n N]` | the job's own status summary (`report`); no ID: every queued job's; `jr N`: all, N lines |

   Everything else is the plain command (`jm stop j0007`, `jm list` for the full rows with names and reasons).
   Short ids help too: `jm submit --id t5 ...` instead of the automatic `j0007`.

3. In the app, save the worker as a host with key login. In ConnectBot: *Manage Pubkeys* creates a key; append
   its public half to the worker's `~/.ssh/authorized_keys`. Then put `jl` in the host's *Post-login automation*
   (other apps call it a startup or login command): connecting shows the queue at once, and `jf` follows the
   running job. Stop following with Ctrl-C (the app's Ctrl key, then `c`), or let it end with the job.

With Termux, one command per action also works, from the phone's own shell: `ssh -t pi bash -ic jf` runs the
shortcut in an interactive shell (which reads all of `~/.bashrc`), so `alias pf='ssh -t pi bash -ic jf'` in
Termux's `~/.bashrc` makes it two letters.

## 4. `jm ssh`, `jm scp`, `jm remote`, `jm keygen` (1.3.0, RDD_016)

Instead of repeating the ssh options in every script, name the workers once in `~/.jmhosts` (or the file
`$JM_HOSTS` names), one INI section each; the first one is the default:

```
[cm5]
User                  = pi
Host                  = raspberrypi.local
StrictHostKeyChecking = no
UserKnownHostsFile    = /dev/null
LogLevel              = ERROR
SignInKey             = ~/ssh-pi@raspberrypi-key
Jm                    = /usr/local/bin/jm          # optional: jm's path on the host (sec 1)

[jetson]
Host = jetson.local
```

`Host`, `User`, `Port`, `SignInKey` (the `-i` key), `Jm` and `JmHome` are jm's; **any other key is handed to ssh /
scp as `-o Key=Value`**, so every ssh option works and ssh reports a misspelt one. `/dev/null` is `NUL` on Windows.
`chmod 600 ~/.jmhosts`: jm refuses a file others can change.

```sh
jm ssh uname -a                        # the first host; `jm ssx` is the same command
jm ssh --host jetson -- ls -la /data   # `--` before a command whose words start with -
jm ssh -t                              # an interactive login
jm scp model.bin :/mnt/data/pi/in/     # `:path` = on the host; -r always
jm scp --host cm5 :/mnt/data/pi/run/extractor.bin .
jm remote list                         # any jm command, run on the host (-t only at a terminal)
jm remote --host cm5 log -f j0007
jm remote submit examples/train.jm     # a local spec: sent on stdin as `submit -`
```

Exit codes are the client's (255 = ssh itself failed). jm uses the system OpenSSH client (`ssh`, `scp`,
`ssh-keygen`): Linux, macOS, and Windows 10 1809+ / 11 have it (Windows: Settings > Optional features > OpenSSH
Client).

**A new sign-in key:** `jm keygen [--host NAME]` runs `ssh-keygen -t ed25519` into the host's `SignInKey` (else
`~/.ssh/jm_<host>`; an existing file is never overwritten) and prints the `SignInKey = ...` line to add if needed.
`--no-passphrase` makes an unencrypted key for unattended scripts. Then, **in a terminal**,
`jm keygen --install [--host NAME] [--file KEY]` appends the public key to the host's `~/.ssh/authorized_keys`
(POSIX hosts): ssh asks for the host's password itself (jm never sees it), or signs in with the host's other
`SignInKey` if one works. Where nothing can answer a password prompt (scripts, CI, an AI agent's shell), append
`KEY.pub` to the host's `authorized_keys` by another route instead.

## 5. Limits

- Windows workers: the same commands work over Windows' OpenSSH server, but its default shell is `cmd`, so the
  quoting rules are cmd's, and the shortcuts (sec 3) are for sh-like shells only.
- One worker per command. Choosing the worker where a job fits (`jm --host any`) and copying data (`stage`) are
  design sec 12.4 (v3).
