# jm - a single-machine job manager

Design notes for `jm`, a small job queue / scheduler / monitor for long-running jobs (training runs, renders,
benchmarks, downloads) on one machine: a Raspberry Pi CM5, a Jetson, a normal Linux box, and if possible macOS and
Windows. Written from experience with ad-hoc CM5 queue scripts (per-job `busy()` waits, reboot-resume scripts,
STOPPED markers, PID-reuse bugs, post-train waiters) that each re-implemented part of what follows.

Status: design, not built. Decisions taken with the owner are marked **[decided]**; the rest are proposals.

---

## 1. Goals and non-goals

Goals
- Queue jobs, run as many as fit side by side, survive reboots, and resume interrupted jobs automatically.
- Change the queue at any time: reorder, hold, cancel, stop, restart in place, requeue.
- Jobs with sub-jobs (static groups, or children a running job submits itself).
- One file, Python 3.6+ standard library only **[decided]**; every state and config file is plain text **[decided]**.
- Run as a daemon started by systemd, launchd, a Windows scheduled task, or a `~/.bashrc` guard.

Non-goals (for now)
- Several machines: one daemon per machine; you pick the machine and run `jm` on it (over ssh) **[decided]**.
- Several users: single user **[decided]**; the state dir belongs to that user.
- A network server or web UI: there is no listening port. Remote use is `ssh host jm ...`.
- Measuring resource use to decide admission: requirements are declared (measured use is only reported).

## 2. Runtime and platforms

- Python >= 3.6, standard library only. No f-string tricks beyond 3.6, no `dataclasses`, no walrus, no `tomllib`.
- Linux (aarch64 and x86_64), macOS: full support. Windows: supported with the platform notes in section 10.
- One file, `jm` (or `jm.py`), executable, no install step: copy it anywhere on PATH.

## 3. Architecture

```
  jm CLI  ──(lock + text files)──►  STATE DIR  ◄──(lock + text files)──  jm daemon
                                        ▲                                    │ starts
                                        │ writes exit code, pid             ▼
                                   jm _run <id>  (per-job wrapper, own session / process group)
                                        │
                                        └──► the job's start / resume command
```

- **No sockets.** CLI and daemon share the state dir. Every mutation takes the state lock (`STATE/lock`, flock on
  Unix, `msvcrt.locking` on Windows), writes files atomically (write `x.tmp`, fsync, rename), releases. The CLI then
  touches `STATE/wake` so the daemon reacts at once instead of on its next poll (default every 5 s).
- **The daemon is not the jobs' parent.** Each job runs under a tiny wrapper, `jm _run <id>`, started detached
  (own session on Unix, `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS` on Windows, all fds closed, stdin from
  /dev/null). The wrapper starts the command, waits for it, and writes `exit`. So jobs keep running and their exit
  codes are still recorded while the daemon is stopped, upgraded or crashed. This also removes the "ssh launch hangs"
  trap: nothing a job starts holds the ssh session's descriptors.
- **Process identity = pid + start time + token.** The wrapper writes `pid` as `<pid> <start time> <token>`. Alive
  means: the pid exists, its start time matches, and its command line holds the token. A PID reused after a reboot
  can never pass as the job.

## 4. State dir layout

Default `~/.jm`; override with `JM_HOME` or `--home` (e.g. a writable data mount when the root file system is
read-only).

```
STATE/
  config              machine capacity and defaults (section 6)
  lock                the state lock
  wake                touched by the CLI to wake the daemon
  daemon.pid          "<pid> <start time> <token>" of the running daemon
  daemon.log          daemon events, appended
  queue               the order: one job id per line, top = first to run
  next_id             counter for auto ids
  jobs/<id>/
    spec              the job spec as submitted (section 5), edited by `jm edit`
    state             one word (section 7)
    reason            one line: why it is in that state ("waiting: ram 3000 > free 1200", "stopped by hand: ...")
    pid               "<pid> <start time> <token>" of its wrapper while running
    exit              exit code of the last run (absent while running)
    runs              one line per start: "<n> <UTC start> <UTC end> <exit> <how: start|resume|restart>"
    events            append-only "<UTC time> <event>" lines (submitted, moved, held, started, ...)
    log               the job's stdout + stderr, appended; a banner line on every (re)start
    stderr            only with `split_stderr = yes`: the stderr, which then is not in `log` (RDD_011)
```

Everything is readable with `cat`, `tail -f` and `grep`. Hand edits are allowed while the daemon is stopped.

## 5. Job spec (plain text)

`key = value`, one per line; `#` starts a comment; blank lines ignored; a repeated key adds to a list; a line ending
in `\` continues on the next. Commands are shell strings run with `sh -c` (Unix) or `cmd /c` (Windows); anything
longer belongs in a script the command calls. Unknown keys are an error (typos must not pass silently).

```
# gso_b top-5 run on the CM5
id          = gso_b_top5                 # optional; default: auto (j0001, j0002, ...)
name        = gso_b restart, best-by top5
cwd         = /mnt/data/pi/HowToTrainYourPetAI
env         = SET=gso_b
env         = DEEP_EXTRA_ARGS=--camera-fx 0.5 --best-by top5

start       = sh tools/cm5/cm5_train_extractor_gso_deep_run.sh foreground
resume      = sh tools/cm5/cm5_train_extractor_gso_deep_run.sh foreground   # default: = start
stop        =                            # default: TERM to the process group, KILL after stop_grace
stop_grace  = 60                         # seconds
done_check  = test -s $RUN_DIR/extractor.bin      # exit 0 = already done: skip / mark done, do not run
progress    = grep 'epoch [0-9]*:' run.log | tail -n 1   # one line shown by `jm status`
applog      = tail -n ${JM_LINES:-200} ${JM_FOLLOW:+-f} run.log   # the job's own log, for `jm applog`
split_stderr = no                        # yes: stderr into jobs/<id>/stderr, not into the log (RDD_011)

cpu         = 4                          # cores (float allowed)
ram         = 2500                       # MB
disk        = 2000 @ /mnt/data           # MB free needed on that path at start
tokens      = trainer                    # named exclusive resources (name or name:count)

after       = gso_b_render               # dependencies (ids); repeated key = several
after_ok    = yes                        # yes: only after they succeed (default); no: after they end in any state
priority    = 0                          # only breaks ties when moving jobs with `jm move --by-priority`
retries     = 1                          # automatic restarts after a failure (default 0)
timeout     =                            # wall-clock limit, e.g. 12h (default none)
on_boot     = resume                     # resume (default) | hold | fail: a run cut off by a reboot / crash
drain       = no                         # yes: as soon as this job heads the queue, admit nothing else

pre         = data-rw.sh                 # run before every (re)start; failure = the start fails
post        = data-ro.sh                 # run after every end (exit code in $JM_EXIT)
notify      = curl -s -d "$JM_ID $JM_STATE" https://ntfy.sh/mytopic   # on every state change
artifacts   = /mnt/data/pi/pettrain_runs/$RUN/extractor.bin            # `jm artifacts <id>` lists them
note        = restart from epoch 7, RDD_640
```

Variables available to every command: `JM_ID`, `JM_NAME`, `JM_HOME`, `JM_RUN` (run number), `JM_HOW`
(start | resume | restart), `JM_PARENT` (parent id or empty), `JM_EXIT` (in `post` / `notify`), `JM_STATE`
(in `notify` and `applog`), `JM_LINES` / `JM_FOLLOW` (in `applog`: `-n` and `-f` of `jm applog`), plus the spec's
`env` lines. `applog` runs in the CLI and gets no `env_pass` / `env_file` secrets (RDD_011).

**What else is worth carrying** (proposed, all optional): `gpu` as a token (`tokens = gpu`), `nice` (default 5),
`log_max` (rotate the log past N MB), `keep` (how long a finished job stays in `jm list`, default 7d),
`estimate` (expected duration; only shown, and usable later by a smarter backfill), `tags` (filter in `list`).

Submitting: `jm submit FILE`, `jm submit -` (spec on stdin, handy over ssh), or inline:
`jm submit --name x --cpu 1 --ram 200 -- sleep 600`.

## 6. Resources and scheduling

`STATE/config`:

```
cpus         = 4           # default: os.cpu_count()
ram          = 4000        # MB; default: total RAM
ram_reserve  = 500         # MB never handed out (OS, sshd, the daemon)
tokens       = trainer:1   # named exclusive resources and their counts
tokens       = gpu:1
tokens       = bench_jvm:2
backfill     = yes         # default yes [decided: default on, with the drain guard below]
drain_after  = 30m         # head job waiting longer than this -> stop backfilling until it starts
poll         = 5s
```

Admission, each poll (and on every `wake`):
1. Walk the queue top to bottom. Skip held / stopped jobs and jobs whose `after` jobs are not finished.
2. A job **fits** when: its cpu + the running jobs' cpu <= cpus; ram likewise <= ram - ram_reserve; every token it
   names has a free count; and `disk` MB are free on its path (checked with `shutil.disk_usage` at that moment).
3. The first eligible job (the **head**) starts if it fits.
4. **Backfill:** if the head does not fit, later eligible jobs that fit may start, unless the head has waited longer
   than `drain_after` or has `drain = yes`; then nothing else is admitted until the head starts (running jobs end,
   space frees up, the head goes). This is the starvation guard; it needs no run-time estimates.
5. With `backfill = no`, only the head can start (strict FIFO).

Requirements are declared, not measured. `jm status` shows each running job's real RSS and CPU (from `/proc`, `ps`,
or `tasklist`) beside its declared numbers, so a wrong declaration is visible.

## 7. Job states

```
queued ──► waiting ──► running ──► done
   ▲          │           │  │
   │          │           │  └──► failed ──(retries left)──► queued (requeued at its old position)
   │          ▼           ▼
   └────── held      stopping ──► stopped          cancelled (removed from `queue`, kept in jobs/ until `keep`)
```

- **queued**: in the queue, not yet considered or not eligible yet.
- **waiting**: eligible but does not fit, or its `after` jobs are pending (`reason` says which).
- **running** / **stopping**: wrapper alive / a stop is in progress.
- **done**: exit 0, or `done_check` passed before starting.
- **failed**: non-zero exit, `pre` failed, or timeout; retried automatically while `retries` allow.
- **held**: parked by hand (`jm hold`); kept in its queue position, never started until `jm release`.
- **stopped**: stopped by hand; never resumed automatically (the old `STOPPED` marker).
- **cancelled**: removed by hand.

**Reboot / daemon crash** **[decided: auto-resume]**: on start the daemon checks every `running` / `stopping`
job. Wrapper alive (pid + start time + token) -> it adopts it. Dead with an `exit` file -> done / failed as recorded.
Dead with no `exit` file -> the run was cut off: `on_boot = resume` (default) puts it back at the top of the queue
and starts it with its `resume` command (`JM_HOW=resume`); `hold` parks it; `fail` marks it failed. Jobs must be
resumable for this (snapshots, skip-if-done steps); `done_check` makes a finished-but-unrecorded job count as done.

## 8. Sub-jobs

Two forms, both shown as a tree in `jm list`:

- **Static group:** a spec with `children = FILE` lines (each a full spec) and `parallel = N` (how many children of
  this group may run at once, on top of the machine limits). The parent itself may have no `start`; it is done when
  every child is done, failed when one fails (`continue_on_fail = yes` lets the others go on).
- **Dynamic:** a running job submits children itself: `jm submit --parent $JM_ID spec` (e.g. "language zh trained ->
  gate + export zh"). Children queue right after their parent's position unless `--end` is given. The parent counts
  as finished only when its own command and all its children have ended.

Every child is a normal job: it can be held, requeued, restarted or cancelled on its own (the "retry zh as try2"
case: `jm requeue zh_train --spec-edit size=...`). Stopping or cancelling a parent asks before touching children
(`--children` to include them without asking).

## 9. CLI

```
jm daemon start | stop | status | restart     run in the background (stop leaves running jobs alone)
jm daemon run                                  run in the foreground (for systemd / launchd / a terminal)
jm daemon install systemd | launchd | windows  write a user service file and print how to enable it
jm daemon bashrc                               print a ~/.bashrc line: start the daemon if it is not running

jm submit FILE | - | --name N [--cpu C] [--ram MB] [...] -- COMMAND...
          [--parent ID] [--top | --after ID | --end] [--hold]
jm list [--all] [--tree] [--tag T]             queue order, state, reason, progress line
jm status [ID]                                 one job: spec summary, runs, live RSS/CPU, last log lines
jm log ID [-f] [-n N] [--children] [--stdout | --stderr]
                                               the job's captured output (follow like tail -f); --children
                                               adds its children's, one `==> ID <==` section each (RDD_011)
jm applog ID [-f] [-n N] [--children]          run the spec's `applog`: the log the job keeps itself
jm wait ID [--timeout T]                       block until the job ends; exit code = the job's

jm move ID --top | --bottom | --up [N] | --down [N] | --before ID2 | --after ID2
jm swap ID1 ID2
jm hold ID / jm release ID                     park / unpark (a running job: hold after it ends)
jm stop ID                                     stop now -> stopped (stays listed, never auto-resumed)
jm restart ID                                  stop, then start again in place (JM_HOW=restart; resume cmd)
jm requeue ID [--top | --end] [--fresh]        stop if running, back to the queue (--fresh: start, not resume)
jm cancel ID [--children]                      stop if running, remove from the queue
jm edit ID                                     open spec in $EDITOR (queued / held / stopped jobs only)
jm set ID key=value ...                        change spec keys without an editor
jm pause ID / jm cont ID                       SIGSTOP / SIGCONT (Unix only; optional, risky for timeouts)
jm config [key=value ...]                      show / change machine capacity and defaults
jm clean [--older 7d]                          drop finished jobs' dirs
```

Exit codes: 0 ok, 1 error, 2 usage, 3 no such job, 4 not allowed in that state. `--json` is deliberately absent
(plain text only); every list prints one job per line with tab-separated fields for `cut` / `awk`.

Remote use (the intended way for now **[decided]**): copy files with scp, then `ssh host jm submit spec`,
`ssh host jm list`, `ssh -t host jm log -f ID`.

## 10. Platform notes

| | Linux (CM5, Jetson, PC) | macOS | Windows |
|---|---|---|---|
| Detach | `setsid` via `start_new_session=True` | same | `CREATE_NEW_PROCESS_GROUP \| DETACHED_PROCESS` |
| Stop default | `killpg(TERM)`, then `KILL` | same | `taskkill /T /PID` (no TERM: give jobs a `stop` command) |
| Start time | `/proc/<pid>/stat` field 22 | `ps -o lstart= -p` | `wmic` / PowerShell `Get-CimInstance Win32_Process` |
| RSS / CPU | `/proc/<pid>/status`, `stat` | `ps -o rss,%cpu` | `tasklist /FO CSV` |
| Lock | `fcntl.flock` | same | `msvcrt.locking` |
| Service | systemd user unit | launchd agent | Task Scheduler (at logon) |
| Shell | `sh -c` | `sh -c` | `cmd /c` |

Read-only root (the CM5): `JM_HOME` on the writable mount; per-job `pre` / `post` or global hooks for
`data-rw.sh` / `data-ro.sh`. Global hooks in `config`: `on_first_start` (the machine goes from idle to busy) and
`on_idle` (the last job ended), so the mount is made writable once and read-only again only when nothing runs.

## 11. Lessons carried over from the CM5 scripts

| Pain | How jm handles it |
|---|---|
| Every queue script had its own `busy()` | one scheduler; declared cpu / ram / tokens |
| `cm5_resume_all.sh` after a reboot, one step per job | daemon start adopts or resumes every job (`on_boot`) |
| `STOPPED` files so resume leaves a run alone | state `stopped`, never auto-resumed |
| PID reused after a reboot looked alive | pid + start time + token |
| ssh background launch hangs | detached wrapper, all fds closed |
| post-train waiter polling for the trainer | `after = train_job` |
| requeue a language with saved settings | `jm requeue` with `jm set` on the queued job's spec |
| 180 s "settle" sleeps to avoid races between queues | one queue, one lock, no race |
| editing a running sh script breaks it | the daemon runs from its own copy; `jm daemon restart` to upgrade, jobs keep running |

## 12. Formerly open items, now specified

Each is a later version (section 13), but its rules are fixed here so it can be built without new decisions.

### 12.1 Backfill with run-time estimates (v2)

Spec keys: `estimate = 6h` (expected duration of one run) and `class = gso_train` (jobs of one class share history).
Estimate used, in order: the spec's `estimate`; else the median duration of the last 5 successful runs of the same
`class` (from `jobs/*/runs`, finished jobs kept for this until `keep` expires; a `STATE/history` file of
`<class> <seconds>` lines survives `jm clean`); else **unknown**.

Rule (EASY backfill), replacing step 4 of section 6 when every running job has a known estimate:
1. The **shadow time** = the earliest moment the head would fit, walking the running jobs by expected end
   (`start + estimate`, at least now + 1 min for an overrun job) and freeing their resources one by one.
2. A later job may start now if it fits now **and** either its own estimate ends before the shadow time, or it
   leaves enough of every resource free that the head still fits at the shadow time beside it.
3. A job with an unknown estimate, or any running job with an unknown estimate, falls back to the v1 rule
   (backfill until `drain_after`, then drain).
`jm list` shows the head's shadow time ("head expected to start ~14:20") so the decision is visible.

### 12.2 Progress and ETA (v2)

The spec's `progress` command prints one line; jm reads progress from it with, in order:
- a token `jm:progress=<done>/<total>` or `jm:progress=<percent>%` anywhere in the line (a job can print it itself);
- else `progress_re` (a Python regex with one or two groups: done, and total if no `progress_total`) and
  `progress_total` (a number), e.g. `progress_re = epoch (\d+):` and `progress_total = 40`.
ETA = elapsed time of this run / fraction done x (1 - fraction); shown in `jm list` / `jm status` as
`37% eta 16:40`. With no total (early-stopping training), only the rate is shown: `epoch 14, 29m/epoch`, from the
last two progress changes (jm samples the progress command every `progress_every`, default 60 s, and keeps the
last 20 samples in `jobs/<id>/progress`).

### 12.3 Several users (v3)

v1 is single-user. The multi-user design keeps one daemon **per user** (no root daemon, no privilege switching) and
adds one shared file:
- `capacity_file = /var/lib/jm/capacity` in each user's `config` (group-writable dir, group `jm`).
- The file holds one line per running job of every user: `<user> <job id> <cpu> <ram> <tokens>`. A daemon takes
  the file's flock, checks that the job fits against the machine totals minus every line, appends its line, releases;
  it removes its line when the job ends. A stale line (that user's daemon pid dead or the job's wrapper dead) is
  dropped by whichever daemon reads it next.
- `jm list --all-users` reads the other users' state dirs if they are readable (group `jm`), else shows their lines
  from the capacity file only.
- Fairness: none beyond first come (the head of each user's queue competes for the same free capacity).

### 12.4 Several machines (v3)

Still one daemon per machine; the CLI gains a host list:
- `~/.jm/hosts`: `<name> <ssh target> [jm path] [JM_HOME]` per line, e.g. `cm5 pi@raspberrypi.local`.
- `jm --host cm5 <any command>` = `ssh -o BatchMode=yes cm5-target jm <command>` (`-t` added for `log -f`). Specs
  given as a file are sent on stdin (`jm submit -`), so nothing needs copying for the spec itself.
- `jm free` (new, also useful locally) prints the machine's free capacity and queue size:
  `cpus 1.5/4 ram 1400/3500 tokens trainer:0/1 gpu:1/1 queue 3 head_wait 12m`.
- `jm --host any submit SPEC` asks every host's `jm free` and submits to the host where the job fits now, else the
  one with the shortest queue; hosts can be limited with `hosts_for = cm5,jetson` in the spec.
- Data is still the job's business: an optional `stage = <local path> <remote path>` spec key makes the CLI copy
  (scp, or rsync if present) before submitting; nothing is copied back automatically (`jm artifacts` lists paths).

### 12.5 Tests and CI (v1)

- `tests/` with `unittest` only (no pytest), run by `python3 -m unittest discover tests`.
- `jm _sim --secs S --ram MB --exit N [--progress] [--resumable FILE]`: a fake job that sleeps, allocates MB,
  prints `jm:progress=` lines, and (with `--resumable`) continues from a counter in FILE. All tests use it.
- Each test gets its own temporary `JM_HOME` and a daemon started with `poll = 0.2s`.
- Cases: submit / list / order ops; admission by cpu, ram, tokens, disk (disk faked through `disk` on a path with
  a test override); backfill and the drain guard; `after` / `after_ok`; hold / release / stop / restart / requeue /
  cancel in every state; retries and timeout; daemon killed (SIGKILL) while jobs run, then restarted: adoption;
  wrapper killed: `on_boot` resume / hold / fail; PID reuse (a fake pid file pointing at a live unrelated process
  must read as dead); static groups and dynamic children; plain-text files survive a hand edit round trip.
- CI (GitHub Actions), one workflow, `workflow_dispatch` + push + pull_request:

  | Runner / container | Covers |
  |---|---|
  | `ubuntu-24.04`, Python 3.12 | main Linux path, systemd unit generation (file only, not enabled) |
  | `ubuntu-24.04-arm`, Python 3.12 | aarch64: the CM5 / Jetson CPU family (free for public repos) |
  | `macos-latest` (arm64), `macos-13` (x86_64), Python 3.12 | `ps`-based start time and RSS, launchd plist |
  | `windows-latest`, Python 3.12 | process groups, `taskkill`, `msvcrt` lock, `cmd /c`, Task Scheduler XML |
  | container `python:3.6-slim` (Debian) | the 3.6 floor: no newer syntax or stdlib calls slipped in |
  | container `alpine` + `apk add python3` | musl, busybox `ps` / `sh` (jm must use `/proc`, not `ps -o lstart`) |
  | container `fedora` | another libc / coreutils set |

  Each job runs `python -m unittest discover -v tests` and uploads the temporary `JM_HOME` dirs of failed tests as
  an artifact (state files and logs are text, so a failure is readable from the artifact alone). A test that cannot
  run on a platform (pause/cont on Windows, systemd on containers) is skipped with a reason, never silently.
- Public repo: all runners above are free. Private repo: macOS minutes count 10x and Windows 2x against the free
  quota, so run macOS / Windows only on `workflow_dispatch` and on PRs, not on every push.

## 13. Versions

- **v1:** sections 1-11 and 12.5: single machine, single user, backfill with the drain guard, auto-resume.
- **v2:** 12.1 estimates and EASY backfill, 12.2 progress and ETA.
- **v3:** 12.3 several users, 12.4 several machines.
Each version keeps the earlier state-dir layout readable (new files only; an unknown key in an old spec is still an
error, a missing new key has a default).

### 13.1 Size and implementation plan

Rough size (Python, comments included):

| Part | jm.py lines | test lines |
|---|---|---|
| v1: state dir + lock, spec parser, CLI, daemon loop, scheduler + backfill/drain, wrapper, process identity, reboot adoption, groups / children, service files | 1800-2500 | 1200-1800 |
| v2: estimates + EASY backfill, progress / ETA | +400-600 | +300-500 |
| v3: several users, several machines | +600-900 | +400-600 |

**`gh` login: never through a token variable.** `gh` must be logged in the normal way (`gh auth login`, an
interactive browser / device-code login run by the owner, e.g. `! gh auth login` in a Claude Code session).
Authenticating `gh` through `GH_TOKEN` / `GITHUB_TOKEN` does not work for this owner's repos (the token in the
session environment lacks the needed rights). Before the first `gh` call, check `gh auth status`: if it says the
login comes from `GH_TOKEN` or `GITHUB_TOKEN`, run `gh` with those unset (`env -u GH_TOKEN -u GITHUB_TOKEN gh ...`)
and, if that shows no login, ask the owner to log in rather than working around it. (Where a hook refuses commands
containing `env -u`, the same works as `sh -c 'unset GH_TOKEN GITHUB_TOKEN; gh ...'`.)

Work loop, one session on a Linux workstation with `gh` logged in:
1. Create the repo (owner confirms name, visibility, licence), push a skeleton with the CI workflow first.
2. Build v1 in slices, each with its tests, run locally on Linux (`python3 -m unittest`, plus a 3.6 container if
   docker/podman is available): spec + state files; CLI queue ops; scheduler; wrapper + process identity; daemon +
   adoption; groups; service files.
3. After each slice: push a branch, `gh workflow run ci.yml --ref <branch>`, `gh run watch`, and on failure
   `gh run view --log-failed` / download the artifact; fix and repeat. Expect most iterations on Windows (process
   groups, file locking, path and shell quoting) and a few on macOS (`ps` formats) and Alpine (busybox).
4. Open a PR when all runners are green; the owner reviews and merges; tag `v1.0.0`.
5. Dogfood on the CM5 per section 15.2 before v2.

Expectation: v1 with its tests and a green matrix fits in **one long session** (most wall time is CI round trips of
a few minutes each); v2 and v3 one shorter session each. A cloud session works the same way if it has `gh` and
push access to the repo; the only hand-off then is the PR review.

## 14. Distribution: a small repo, not a gist

A gist cannot carry tests, CI, tagged releases or issues, and jm needs all four. So: a small public (or private)
repository, e.g. `jm`:

```
jm.py                 the whole tool, executable; `jm --version` prints the tag it was released from
tests/                unittest suite (section 12.5)
examples/             sample specs (training run, bench after it, a group with per-language children)
.github/workflows/    the CI matrix
README.md             install, quick start, the CLI table
LICENSE
CHANGELOG.md
```

Install on a machine = copy one file, as easy as a gist:
`curl -fsSL https://raw.githubusercontent.com/<owner>/jm/v1.0.0/jm.py -o ~/bin/jm && chmod +x ~/bin/jm`
(a tag in the URL, so a machine runs a known version). `jm daemon install systemd|launchd|windows` does the rest.
Python floor 3.6 matches CentOS 7's base-repo `python3`, so even that box runs it with base packages only.

## 15. Adopting jm in a project

### 15.1 The general pattern (any repository)

1. **Do not vendor jm.** Each machine installs one tagged version; the project only says which minimum it needs
   (`jm --version` checked by its helper script).
2. **Job specs live in the project**, e.g. `jobs/<name>.jm`, committed. They use `cwd` / `env` to point at the
   machine's checkout and data dirs; machine-specific values come from env vars set in the machine's `jm config`
   (`env = DATA=/mnt/data/pi` lines in `config` are passed to every job).
3. **Scripts become foreground, resumable commands.** A job's `start` runs the work in the foreground and exits with
   its status; detaching, pid files, waiting for a free machine, chaining and reboot handling all move to jm.
   Every job must be safe to start again (skip finished steps, resume from a snapshot) and should have a
   `done_check`.
4. **One thin helper per project** to submit from a workstation, e.g. `tools/jm_submit.sh HOST SPEC [key=value ...]`:
   `git pull` on the host, then `jm --host HOST submit` with the overrides (or `ssh host jm submit -` before v3).
5. **Keep the old scripts until one full cycle has run under jm** (submit, run, reboot in the middle, resume,
   chained job, result fetched), then delete them in one commit.

### 15.2 This repository (HowToTrainYourPetAI), when jm v1 exists

A decision record (RDD) first; it binds the steps below. Then:
1. **CM5 install:** `~/bin/jm` from a tag; `JM_HOME=/mnt/data/pi/jm` (the root is read-only);
   `config`: `cpus = 4`, `ram = 4000`, `ram_reserve = 500`, `tokens = trainer:1`, `tokens = bench_jvm:2`,
   `tokens = godot:1`, `on_first_start = data-rw.sh`, `on_idle = data-ro.sh`; `jm daemon install systemd` if the
   read-only root allows a user unit, else the `~/.bashrc` guard (`jm daemon bashrc`).
2. **Foreground modes** in the CM5 scripts that lack one: `cm5_train_extractor.sh foreground` (the trainer without
   nohup / run.pid), `cm5_post_train.sh run` (exists), `cm5_gso_render.sh` steps, `nx7c.sh` per language
   (train one language, then its gate + export as a child job).
3. **Specs in `tools/cm5/jobs/`:** extractor training (`tokens = trainer`, `done_check = test -s .../extractor.bin`),
   its V-29 bench (`after = <train job>`, `tokens = bench_jvm:2`), GSO render (`tokens = godot`), the NX-7(c)
   tagger as a group with one child per language (`parallel = 2`, `cpu = 2` each), retries via `jm requeue` +
   `jm set` instead of `nx7c.sh requeue`.
4. **Retire,** once 15.1 step 5 passes: `cm5_resume_all.sh`, `nx7c_queue_retry.sh`, `cm5_gso_deep_queue.sh`,
   the chain scripts' waiting logic (`cm5_gso_chain.sh`, `cm5_gso_b_chain.sh`), `cm5_post_train.sh queue` /
   `__wait__`, `cm5_gso_b_rescue.sh queue`, and every `busy()` / `STOPPED` / settle-sleep in them.
5. **PC side:** `tools/cm5/jm_submit.sh` replaces "pull on the CM5, then run the `_run` script" in the hand-offs;
   status for the owner becomes one command, `ssh pi jm list`.
6. **Docs:** `tools/README.md` rows for the specs and the helper, the retired scripts removed; the project's own
   shell ceiling (CentOS 7 `sh`) is unaffected, since jm is Python and lives outside the repository.

### 15.3 Other repositories

Same as 15.1. A project with no long jobs gains nothing; the fit is any repo that today has its own "wait until the
machine is free" or "resume after reboot" script.

## 16. Secrets

jm stores specs, state and logs as plain text and prints them in `list` / `status` / `log`, often inside an AI
coding session whose output is kept. So jm must never hold a secret value, and must make the safe way the easy way.

**Secret = by name or by value.** A name matches when it contains `TOKEN`, `SECRET`, `PASSPHRASE`, `PASSWORD`,
`PASSWD`, `API_KEY`, `APIKEY`, `PRIVATE_KEY` or `CREDENTIAL` (any case), or equals one of `config`'s
`secret_names = NAME` lines. A value matches when it starts with a known token prefix (`ghp_`, `gho_`, `ghs_`,
`ghu_`, `github_pat_`, `glpat-`, `sk-`, `sk-ant-`, `xox[abpr]-`, `AKIA`, `ASIA`, `AIza`) or is listed in
`config`'s `secret_prefixes = PREFIX` lines. The rule set is the same as the shell guard's (CLAUDE_GUARDS.md sec 3).

**What jm does:**
1. **Refuses** a spec whose `env` line has a secret name or a secret value (exit 1), naming the two ways below.
2. `env_pass = NAME` (repeatable): the job gets `NAME` from the **daemon's** environment at start; the spec and
   state files hold only the name. Missing in the daemon's environment = the job fails to start with that reason.
3. `env_file = PATH` (repeatable): `KEY=VALUE` lines read at each start; the file must be owned by the user and not
   readable by group or others (mode 0600 / 0400; on Windows: no check, a warning in `jm status`). Never copied
   into the state dir; `jm status` shows the path only.
4. **Never prints an environment**: no command shows a job's environment; `jm status` lists `env` keys, `env_pass`
   names and `env_file` paths only.
5. **Masks on display**: `jm log` and the log tail in `jm status` replace any token-prefix match with
   `<prefix>***`; `--raw` shows the log unmasked (refused when stdout is not a terminal, so an agent's captured
   output stays masked). The log file on disk is left as the job wrote it.
6. **Hooks and notify** (`pre`, `post`, `notify`, `on_first_start`, `on_idle`) get the same environment as the job,
   so a webhook secret goes through `env_pass` / `env_file` too, never inline in the command.
7. **The repo ships the shell guard** (CLAUDE_GUARDS.md) in its own `.claude/`, so a cloud session working on jm
   is protected even without the owner's user-level settings.

Tests (12.5) add: a spec with `env = GITHUB_TOKEN=x` or `env = X=ghp_abc` is refused; `env_pass` reaches the job;
an `env_file` with mode 0644 is refused on Unix; `jm log` masks `github_pat_...` in a log line; `--raw` without a
terminal is refused.
