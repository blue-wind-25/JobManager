# Changelog

## 1.5.0 - 2026-10-29

- `jm hold` takes a stopped job (RDD_018): held, its next run a resume; `jm release` queues it.
- `jm hold --chain ID` / `jm release --chain ID`: also every job that waits for ID through `after`, directly or not.
  hold parks the chain's queued / waiting jobs only; release queues every held one. With `--chain` ID itself may be
  running or finished (park what comes after this run). Each job changed is printed.
- `jm skip ID`: mark a queued, waiting, held, stopped, failed or cancelled job done without running it (reason
  "skipped by hand (was STATE)"); not for a running job or one with unfinished children.
- `jm deps ID`: the jobs waiting for ID through `after`, as a tree (id, state, reason). `jm status` adds
  `needed by:` and prints `done_check`, `pre`, `post`, `stop`.
- `jm set` / `jm edit` / `jm requeue --spec-edit` warn about an `after` that names no job or makes a cycle; the
  change is still applied (exit 0).
- docs/OPERATIONS.md: the operator's reference (every command per state, admission, `after`, recipes).
- No state format change; a 1.4.0 daemon reads a 1.5.0 state dir.

## 1.4.0 - 2026-10-28

- `jm list --order` (RDD_017): finished jobs, then the queue in the order it is expected to run: running jobs first,
  then queue order with each job moved after the jobs its `after` names; a job whose `after` can never be met last.
  Works with `--tree`. Resources and estimates are not modelled.
- `jm list --fields id,state,name,reason,progress` picks and orders the columns.
- `jm list` pads its columns with spaces on a terminal (each but the last to its widest cell, at most 40); piped
  output stays tab-separated as before. `--align` / `--tabs` force either.
- Shortcut `jl` = `jm list --tree --order --fields id,state,progress`: run order, aligned (was `cut -f1,2,5`, which
  a terminal's tab stops left ragged).

## 1.3.0 - 2026-10-27

- Hosts file `~/.jmhosts` (or `$JM_HOSTS`), INI sections, first = default (RDD_016): `Host`, `User`, `Port`,
  `SignInKey`, `Jm`, `JmHome`; any other key goes to ssh / scp as `-o Key=Value`.
- `jm ssh` (alias `ssx`), `jm scp` (`:path` = on the host), `jm remote` (any jm command on the host; a local spec
  is sent on stdin), `jm keygen` (ed25519 via ssh-keygen, `--install` appends it to the host's authorized_keys,
  the password asked by ssh on your terminal). All take `--host NAME`. Uses the system OpenSSH client.
- CI job `jm-ssh`: a localhost round trip; the command-line and keygen tests run on every OS. docs/REMOTE.md sec 4.

## 1.2.0 - 2026-10-23

- Spec key `report` and `jm report [ID] [-n N]` (RDD_015): a command that prints the job's own status summary (a
  run's table, its last epochs), for one job or every queued one in queue order, a `==> ID <==` section each;
  `-n` reaches it as `JM_LINES`; the environment, secret rules and masking of `applog`. `jm status` shows it.
  Phone shortcut `jr [ID] [N]` in examples/jm-shortcuts.sh.
- docs/GUIDE.md sec 9: starting the daemon at boot (service file vs cron vs ~/.bashrc), with the read-only-root
  steps (remount, the linger flag when logind cannot write, a busy root on Raspberry Pi OS).

## 1.1.0 - 2026-10-23

- A state dir that goes read-only while idle (RDD_014): new config key `home_rw`, a command any jm command that
  must write runs first when its home is read-only; read-only commands work there as it is; the daemon writes
  `machine idle` before `on_idle`, then waits without writing; `daemon start` on a read-only home runs `home_rw`.
- Fix (Unix): an idle daemon held files open for writing on its home (`daemon.lock`, its stdout / stderr on
  `daemon.log`, the hook output file), so an `on_idle` remount read-only failed with EBUSY. Lock files are opened
  read-only, the daemon lets go of a stdout / stderr on its home once started, hook output goes through a pipe
  into `daemon.log` lines.
- examples/jm-data-rw.sh, examples/jm-data-ro.sh: machine hooks for a data mount that is read-only when idle (the
  read-only one jm-aware: skips inside a job and while jm is busy).

## 1.0.0 - 2026-10-22

- jm v1 (design/JM_DESIGN.md sections 1-11, 12.5 and 16): state dir and lock, spec / config parser, CLI, daemon
  loop, scheduler with backfill and the drain guard, detached per-job wrapper, process identity (pid + start time
  + token), reboot / crash adoption with `on_boot`, static groups and dynamic children, systemd / launchd /
  Task Scheduler service files, secret refusal / `env_pass` / `env_file` / log masking, `jm free`.
- claude-guards (design/CLAUDE_GUARDS.md) in `guards/`: secret-echo, broad-search and protected-read hooks, case
  tests on Python 2.7 / 3, `install.sh --user | --repo`; installed into this repository's `.claude/`.
- CI matrix: Linux x86_64 / aarch64, macOS arm64 / x86_64, Windows, Python 3.6, Alpine, Fedora (manual trigger).
- Machine hooks run outside the state lock; new config keys `hook_timeout` (120 s) and `idle_after` (60 s);
  `STATE/machine` records busy / idle; hooks get `JM_HOOK`. `cwd` expands `$NAME`. `jm submit --continue-on-fail`.
- docs/GUIDE.md (behaviour reference) and docs/PORTING.md (moving a project's job scripts to jm).
- Logs (RDD_011): `jm log --children` (a job and its descendants, `==> ID <==` sections, `-f` follows new
  children too), spec key `split_stderr` with `jm log --stdout / --stderr`; spec key `applog` (a command that
  prints the job's own log) and `jm applog ID [-f] [-n N] [--children]`, run without `env_pass` / `env_file`
  secrets. `jm log -f` counts bytes (Windows `\r\n`) and survives `log_max` rotation.
- docs/REMOTE.md (RDD_012): driving a worker with `ssh -t WORKER jm ...` (what differs over ssh, the
  non-interactive PATH, short host names and aliases) and from a phone: examples/jm-shortcuts.sh
  (`jl js jf ja jt`, the ID optional).
- Fix: the wrapper identity check could take a just-started wrapper for dead (its command line is the daemon's
  until exec) and cached that for the daemon's life; a Unix miss is now re-read for up to 1 s (RDD_004).
- Durations on the monotonic clock (RDD_013): `timeout`, `drain_after`, `idle_after`, `stop_grace` and the running
  time no longer jump with the wall clock (NTP setting the date on a Pi without RTC); job files keep a
  boot-scoped monotonic stamp beside the UTC time (Linux).
- Logs capped by default (RDD_013): config `log_max` (100 MB) for job logs and `daemon.log`, overridable per spec,
  `0` = no limit; the older part goes to `*.1`; on Unix also trimmed while the job runs.
- Docs: put a job's work in a script and keep the spec's commands one short line (GUIDE sec 2, PORTING sec 2,
  with a template script).
