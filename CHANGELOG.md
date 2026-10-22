# Changelog

## 1.1.0 - 2026-10-23

- A state dir that goes read-only while idle (RDD_014): new config key `home_rw`, a command any jm command that
  must write runs first when its home is read-only; read-only commands work there as it is; the daemon writes
  `machine idle` before `on_idle`, then waits without writing; `daemon start` on a read-only home runs `home_rw`.
- Fix (Unix): an idle daemon held files open for writing on its home (`daemon.lock`, its stdout / stderr on
  `daemon.log`, the hook output file), so an `on_idle` remount read-only failed with EBUSY. Lock files are opened
  read-only, the daemon lets go of a stdout / stderr on its home once started, hook output goes through a pipe
  into `daemon.log` lines.
- examples/data-rw.sh, examples/data-ro.sh: machine hooks for a data mount that is read-only when idle (the
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
