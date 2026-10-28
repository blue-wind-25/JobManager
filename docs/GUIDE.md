# jm user guide

How jm behaves in the cases where "what exactly happens?" matters. The full design is
[design/JM_DESIGN.md](../design/JM_DESIGN.md); the decisions behind each rule are in [rdd/](../rdd/). To change a
live queue (hold, re-point, requeue, skip), [OPERATIONS.md](OPERATIONS.md) has every command per state in one place.

## 1. Job states

```
queued ──► waiting ──► running ──► done
   ▲          │           │  │
   │          │           │  └──► failed ──(retries left)──► queued (same place in the queue)
   │          ▼           ▼
   └────── held      stopping ──► stopped          cancelled (out of the queue)
```

| State | Meaning |
|---|---|
| queued / waiting | in the queue, not started yet; `reason` says why (`jm list`, 4th column) |
| running | its command runs (or, for a group, its children do) |
| stopping | a stop / restart / requeue / cancel / timeout is in progress |
| done | exit 0, or `done_check` passed before starting |
| failed | non-zero exit, `pre` failed, timeout, or a run cut off with `on_boot = fail` - and no retries left |
| held | parked by hand (`jm hold`); never started until `jm release` |
| stopped | stopped by hand; **never resumed automatically**, not even after a reboot; `jm requeue` brings it back |
| cancelled | removed by hand (`jm cancel`) |

The queue (`STATE/queue`) holds every job that is not done / failed / cancelled, in order. `jm list` prints it,
then the finished jobs of the last `keep` (7 days). There is **no header line**: every row is
`id <TAB> state <TAB> name <TAB> reason <TAB> progress`, ready for `cut` / `awk`.

## 2. start, resume, restart

A job has one command it must have, `start`, and an optional `resume`. Every run gets `JM_HOW`:

| Run | `JM_HOW` | Command used |
|---|---|---|
| first run, `jm requeue --fresh` | `start` | `start` |
| automatic retry after a failure (`retries`) | `resume` | `resume`, **or `start` when there is no `resume`** |
| a run cut off by a reboot / crash (`on_boot = resume`) | `resume` | `resume`, or `start` |
| `jm restart`, `jm requeue` (without `--fresh`) | `restart` / `resume` | `resume`, or `start` |

So a job without `resume` simply runs `start` again; when it needs to, it can still tell a fresh start from a
second attempt by `$JM_HOW` (and `$JM_RUN`, the run number). Jobs should be **safe to start again**: skip
finished steps, continue from a snapshot, and give a `done_check` so a run that finished but was not recorded counts
as done.

**`jm restart ID` is for a running job only.** It stops the job (the normal stop: `stop` command or TERM, then
KILL after `stop_grace`) and starts it again at the **top of the queue**, so it gets its resources back before
anything else starts. On a job that is not running (queued, waiting, held, stopping, stopped, finished, or a
group whose own command has ended) it fails with exit code 4 and changes nothing; use `jm requeue` for those.

**Put the work in a script; keep the spec's commands one short line.** `start = sh tools/train.sh` is safer
than a whole pipeline in the spec:
- a spec value is not shell text: `#` after a space starts a comment even inside quotes (`echo "a #b"` becomes
  `echo "a`; write `\#`), a long command needs `\` line continuations, and `$NAME` in a command is expanded by
  the shell at run time, not by jm;
- the same line must work in `sh` and, on Windows, in `cmd`; over ssh it is quoted once more (docs/REMOTE.md);
- a script lives in the project's repository, is reviewed and versioned with the code, and can be run and
  debugged by hand without jm (`JM_HOW=resume sh tools/train.sh`);
- `start` and `resume` can be the same script, deciding by `$JM_HOW`; `set -eu` makes a failing step fail the
  job instead of running on;
- tokens in a command line are refused at submit (sec 7); a script reads them from the `env_pass` / `env_file`
  variables.

Short, one-word-ish commands are fine inline: `done_check = test -s out/model.bin`,
`progress = tail -n 1 out/train.log`, `applog = tail -n ${JM_LINES:-200} ${JM_FOLLOW:+-f} out/train.log`.
End the script with `exec` of the real program, so no shell sits between it and jm: its exit code is the job's,
and `JM_PID` (given to a `stop` command) is the program itself. A template is in [PORTING.md](PORTING.md) sec 2.

## 3. Retries and timeouts

- `retries = N`: after a failure the job goes back to the queue (same place) up to N times, as a `resume` run.
- `timeout = 12h`: a run that takes longer is stopped and counts as a failure (retries apply).
- Running time is measured on the monotonic clock (on Linux also across a daemon restart), so the date being set
  after boot - NTP on a Pi without RTC battery - neither fires a `timeout` nor the drain guard early (RDD_013).
- `jm hold` on a running job: the job is held instead of retried if it fails.

## 4. Groups and their children

- A **static group** is a spec with `children = FILE` lines and optionally `parallel = N`; a **dynamic** child is
  submitted by a running job: `$JM_JM submit --parent "$JM_ID" ...`.
- A child starts only while its parent is `running`. The parent ends when its own command (if any) and all its
  children have ended: **done** if every child is done, otherwise **failed**.

When a child fails for good (no retries left):

| Group setting | What jm does with the other children |
|---|---|
| default | cancels the ones still waiting ("sibling X failed"); the ones already running finish; the group fails |
| `continue_on_fail = yes` (or `jm submit --continue-on-fail`) | nothing: every other child runs as normal; the group still ends **failed** |

**With `continue_on_fail`, recovery is the group's own business.** jm only runs the children and reports which
failed (`jm status GROUP`, the group's reason). It cannot know whether the other children still make sense,
whether partial results must be cleaned up, or how to redo the failed part - only the job can. Typical ways to
handle it:
- give the failing child `retries` (and a `resume` that continues where it stopped);
- make each child independent of its siblings' results (then a failure only loses that one result);
- after the group, run a job with `after = GROUP` and `after_ok = no` that inspects what exists and repairs or
  resubmits (`jm requeue CHILD`, or a new `jm submit`);
- in a dynamic group, let the parent's own command check its children (`jm wait CHILD`) and decide.

jm never stops running siblings because one child failed: a running job is only stopped by hand
(`jm stop / cancel --children`) or by its own `timeout`.

## 5. Stopping a job

`jm stop ID` (and restart / requeue / cancel of a running job) asks the job's wrapper to stop it:
1. the spec's `stop` command runs, if there is one (it gets `JM_PID`, the job's main process); otherwise the
   job's process group gets **TERM** (Unix);
2. after `stop_grace` (default 60 s) whatever is left of the process group gets **KILL**.

**On Windows** there is no TERM for console programs. jm first asks `taskkill /T` (which closes GUI programs);
when Windows refuses that - as it does for console programs - jm kills the process tree at once
(`taskkill /T /F`), without waiting `stop_grace`. A job that must shut down cleanly on Windows (flush a
checkpoint, release a lock) needs a `stop` command, e.g. one that creates a file the job watches for, or calls
the program's own shutdown command. `jm pause` / `jm cont` do not exist on Windows.

## 6. Machine hooks: on_first_start and on_idle

For a machine that must prepare something before work and undo it after - e.g. a read-only root file system
whose data mount is made writable with `data-rw.sh` and read-only again with `data-ro.sh` - the config has two
hooks:

```
on_first_start = /usr/local/bin/data-rw.sh   # the machine goes from idle to busy
on_idle        = /usr/local/bin/data-ro.sh   # nothing has run for idle_after
idle_after     = 60s                         # default
hook_timeout   = 120s                        # default; a hook running longer is killed (exit 124)
```

**Any command works.** jm has no built-in script names and runs nothing by default: both hooks are empty until
the config sets them. `data-rw.sh` / `data-ro.sh` are just the names used in these examples; use your own scripts,
any path, arguments, or a short shell command (`on_idle = sudo mount -o remount,ro /mnt/data`). Each hook is one
command line run with `sh -c` (`cmd /c` on Windows), from the state dir; for several steps, call a script.

How they run:
- **Before the first job starts** on an idle machine, the daemon runs `on_first_start` and waits for it; only
  then are jobs started. If it fails (non-zero exit or timeout), no job starts: the waiting jobs show
  "on_first_start failed (exit N)" and the hook is tried again every poll.
- When nothing has run for `idle_after`, the daemon runs `on_idle`. A job chain (`after`, retries, groups,
  requeues) that starts the next job within `idle_after` keeps the machine busy: no read-only / writable flip
  between the jobs.
- Hooks run **outside the state lock**: `jm list`, `jm submit`, etc. never wait for a hook. Only the daemon waits.
- The state is kept in `STATE/machine` (`busy <UTC>` or `idle <UTC>`, also shown by `jm status`), so it
  survives a daemon restart and other scripts can read it.
- Hooks get the config's `env` lines, `JM_HOME`, and `JM_HOOK` (`on_first_start` or `on_idle`); they do **not**
  get `JM_ID`. Their output goes into `daemon.log` (`<hook> output:` lines) after they end.

**The state dir on the mount that on_idle makes read-only** (a read-only root, the state dir on the data mount,
nothing else writable; RDD_014): set **`home_rw`** to the command that makes it writable again, usually the same
script as `on_first_start`:

```
home_rw        = /usr/local/bin/data-rw.sh   # a jm command that must write finds its home read-only: run this first
```

- Commands that only read (`list`, `status`, `log`, `applog`, `wait`, `free`, `artifacts`, `config`,
  `daemon status`) work on the read-only home; `submit`, every queue change, `config k=v`, `daemon start` and
  `daemon stop` run `home_rw` first (and fail with its output if the home stays read-only).
- The daemon writes `machine idle` before on_idle, then waits without writing until a command has made the home
  writable again; it is not restarted. A daemon started after a boot, with the mount still read-only, runs
  `home_rw` itself, so a `~/.bashrc` line `jm daemon start` needs nothing before it.
- An idle daemon holds no file open for writing on its home (Unix): a remount read-only fails (EBUSY) while one is.
  A hook that leaves a background process holding its output open makes the daemon wait for `hook_timeout`.

**Jobs that call the hook scripts themselves.** jm cannot stop a job from running the read-only script (whatever
its name) in the middle of other jobs' work. When adopting jm, make the scripts jm-aware instead, using the
variables above. For a script called `data-ro.sh` (the same lines work at the top of a script of any name):

```sh
# data-ro.sh (or your own read-only script)
if [ -n "$JM_ID" ]; then                 # called from inside a jm job: the machine is not idle
    echo "data-ro.sh: skipped inside jm job $JM_ID (jm's on_idle hook does it)"; exit 0
fi
if [ -z "$JM_HOOK" ] && grep -q '^busy' "${JM_HOME:-$HOME/.jm}/machine" 2>/dev/null; then
    echo "data-ro.sh: jm is busy; not now"; exit 0   # called by hand / cron while jm jobs run
fi
# ... remount read-only ...
```

Ready-made versions of both, named so they never replace the machine's own scripts:
[examples/jm-data-rw.sh](../examples/jm-data-rw.sh) and [examples/jm-data-ro.sh](../examples/jm-data-ro.sh)
(jm-aware as above, the mount named by `DATA_MOUNT`).

The writable script (`data-rw.sh` here) called from a job is harmless (the mount is already writable), so it needs
no change; removing such calls from job scripts is still cleaner (see [PORTING.md](PORTING.md)). The same applies
to any other pair of "prepare / undo" scripts used as hooks (power a disk up and down, start and stop a service,
switch a fan profile): the names and what they do are the machine owner's choice.

## 7. Secrets

- A spec may not hold a secret: an `env` line whose name looks secret (`..._TOKEN`, `..._PASSWORD`,
  `..._API_KEY`, ...) or whose value looks like a token is refused, and so is a token in any command.
- Jobs get the daemon's environment **minus every secret-looking variable**. A job that needs one names it:
  `env_pass = NAME` (taken from the daemon's environment at each start) or `env_file = /path` (a `KEY=VALUE`
  file, mode 0600, read at each start, never copied).
- `jm log`, `jm applog` and `jm status` mask token-looking strings; `--raw` shows them, on a terminal only.
- An `applog` command (sec 8) gets **no** `env_pass` / `env_file` secrets, and its output is masked too: masking
  only recognises token-shaped values, so a plain password it printed would show. An applog that needs a
  credential reads it itself (e.g. from its own 0600 file). This is deliberately strict and will be relaxed if
  it gets in the way (RDD_011).

## 8. Logs: `jm log` and `jm applog`

A job has two kinds of log, and jm has a command for each (decisions in RDD_011):

| | `jm log ID` | `jm applog ID` |
|---|---|---|
| shows | what jm **captured**: the stdout and stderr of the job's commands, with jm's run banners | the log the job **keeps itself**, printed by the spec's `applog` command |
| stored in | `jobs/<id>/log` (and `jobs/<id>/stderr` with `split_stderr = yes`) | wherever the job writes it; jm stores nothing |
| typical content | everything a job prints when it does not redirect, plus tracebacks and warnings of a job that writes its real log to a file of its own | `out/train.log`, a run directory, several files, a journal |

Both take `-n N` (last N lines), `-f` (follow), `--raw` (unmasked, terminal only) and **`--children`**: the job
and then all its descendants (depth first, as `jm list --tree`), each under a `==> ID <==` line, as `tail` prints
several files. With `-f` the line is repeated whenever the output switches to another job; `-f` ends once every
job shown has ended and is quiet, and picks up children a running group submits meanwhile.

```sh
jm log G --children -f            # follow a whole group's captured output
jm log J --stderr                 # only the stderr (needs split_stderr = yes)
jm applog J -n 50                 # the last 50 lines of the job's own log
jm applog G --children -f         # every applog in the group, until the jobs end
```

**stdout and stderr.** By default both streams go into one file, `log`, in the order they were written. With
`split_stderr = yes` in the spec, the stderr of every command jm runs for the job (`done_check`, `pre`, `start` /
`resume`, `post`, `stop`) goes to `jobs/<id>/stderr` instead; the run banners are in both files. Then
`jm log --stdout` shows `log` and `jm log --stderr` shows `stderr` (and `jm status` shows the tail of each). On
a job without `split_stderr`, `--stdout` warns that the log holds both, and `--stderr` fails (one job) or skips it
with a warning (`--children`). Splitting loses the order between the two streams, so leave it off unless the
stderr is wanted on its own (a job that prints its progress to stdout and only errors to stderr). `notify` output
always goes to `log`.

**Size.** Every log is capped at `log_max` MB: the config's `log_max` (default 100) or the spec's, `0` for no
limit. Over the cap the older part moves to `log.1` (`stderr.1`, `daemon.log.1`; one older part is kept) - between
runs, and on Linux / macOS also while the job runs: the wrapper checks every second, copies the log to `log.1`,
empties it and writes a `==== ... over log_max ...` line; the job writes on undisturbed. On Windows logs rotate
only between runs and at daemon start. `jm log` shows the current file; `cat jobs/<id>/log.1` the part before.

**The job's own log: `applog`.** Any command that prints the log to stdout:

```
applog = tail -n ${JM_LINES:-200} ${JM_FOLLOW:+-f} out/train.log     # sh
applog = cat out/*/summary.txt                                         # a group: the summary of every part
applog = journalctl --user -u my-render -n ${JM_LINES:-100} ${JM_FOLLOW:+-f} --no-pager
```

- It runs when you call `jm applog` (never by the daemon), in the job's `cwd`, in any state - also queued,
  failed or long done.
- It gets `JM_LINES` (the `-n` value, or empty) and `JM_FOLLOW` (`1` with `-f`, else empty) to pass on, as
  above. Without `-f` jm also keeps only the last N lines of its output itself, so `-n` works with a plain `cat`.
- With `-f`, jm ends the command (its whole process group) once the job has ended and the command printed
  nothing for a second, so `tail -f` stops by itself; Ctrl-C ends it at once.
- With `--children`, every job in the tree that has an `applog` runs its own (children do not inherit the
  parent's); without `-f` they run one after the other, with `-f` all at once.
- It gets the config's and the spec's `env` lines and the `JM_*` variables (`JM_ID`, `JM_RUN`, `JM_STATE`, ...),
  but **not** `env_pass` / `env_file`: what it prints is shown, often in a captured session, so it is given no
  secret to print. Its output is masked like `jm log`.
- Exit code 1 when a command exited non-zero or could not start (it is named on stderr), or when no job shown
  has an `applog`.

**The job's own status: `report`** (RDD_015). Where `applog` prints a log, `report` prints a summary the job's
own tools compute: a training run's per-language table, the last epochs, how far a render is. Any command:

```
report = sh tools/status.sh train ${JM_LINES:-8}      # e.g. the run's table and its last N epochs
```

- `jm report ID [-n N]` runs it for one job; `jm report [-n N]` for every job in the queue (queued, waiting,
  running, held, stopped) that has one, in queue order, a `==> ID <==` section each. `-n` reaches the command as
  `JM_LINES` (empty without `-n`); what it means is the command's choice. There is no `-f`: a report is a snapshot.
- Same rules as `applog`: run by the CLI only, in the job's `cwd`, with the `env` lines and `JM_*` variables but no
  `env_pass` / `env_file` secrets, output masked; exit code 1 when a command exited non-zero (named on stderr).
- On a worker, `jr [ID] [N]` (examples/jm-shortcuts.sh): `jr` every job, `jr 100` every job with N = 100,
  `jr ID 100` one job.

## 9. Starting the daemon at boot

Jobs survive a reboot only as far as the daemon comes back: a cut-off job is resumed (`on_boot = resume`) when the
daemon next starts, not before. Three ways to start it, from most to least robust:

| Way | Starts at boot without a login | Restarts a daemon that died | Needs |
|---|---|---|---|
| service file (`jm daemon install systemd \| launchd \| windows`) | yes (systemd: with linger) | yes (`Restart=on-failure`, 10 s) | the commands it prints |
| cron `@reboot` line | yes | no | cron |
| `jm daemon bashrc` line in `~/.bashrc` | no: at the first interactive login | at the next login | nothing |

Prefer the service file: it is what jm generates and tests, it brings back a daemon that crashed, and on systemd its
output goes to the journal, not to a file. Jobs run in their own sessions, so restarting or stopping the service
leaves them running (`KillMode=process`). The `~/.bashrc` line can stay beside it: it does nothing while a daemon
runs. A cron line (`@reboot /usr/local/bin/jm --home DIR daemon start --quiet`) is one line, but it starts the
daemon once and never again if it dies.

**systemd (Linux), once per machine:**

```sh
jm daemon stop                              # a daemon started by hand or by ~/.bashrc; running jobs keep running
jm daemon install systemd                   # writes ~/.config/systemd/user/jm.service
systemctl --user daemon-reload
systemctl --user enable --now jm.service
sudo loginctl enable-linger "$USER"         # start the user's services at boot, with nobody logged in
```

Stop the hand-started daemon first: a second daemon on the same state dir refuses to start ("already running"),
and the service would retry it every 10 s. Check after a reboot without logging in interactively:
`ssh host jm daemon status` and `jm list` (resumed jobs show `run N (resume)`).

**On a read-only root** (the unit file, its enable link and the linger flag all live on the root):

```sh
sudo mount -o remount,rw /
# ... the five commands above ...
sudo mount -o remount,ro /
```

- `loginctl enable-linger` can fail with "Read-only file system" even after the remount: systemd-logind runs in its
  own mount namespace, made while the root was read-only at boot. The flag is only an empty file, read at the next
  boot, so create it directly: `sudo mkdir -p /var/lib/systemd/linger && sudo touch /var/lib/systemd/linger/$USER`.
- If the remount back to read-only says "mount point is busy", some process has a file on the root open for
  writing: `sudo lsof / | awk '$4 ~ /[0-9]+[wu]/'`. On Raspberry Pi OS that is often rpi-swap, which attaches its
  swap file to a loop device whenever the root becomes writable (`Mechanism=none` in
  `/etc/rpi/swap.conf.d/*.conf`, then stop `dev-zram0.swap` and `rpi-setup-loop@var-swap.service`).
- With the state dir on a data mount that is read-only after boot, set `home_rw` (sec 6): the service's daemon runs
  it at start-up (daemon.log: `home_rw: ... exited 0`), then resumes the cut-off jobs.

## 10. Other machines

jm drives one machine. To use a worker from your workstation, name it once in `~/.jmhosts` and use `jm remote
COMMAND`, `jm ssh`, `jm scp` and `jm keygen` (1.3.0, RDD_016), or plain `ssh -t host jm ...`: docs/REMOTE.md.
