# RDD_013 - Durations on the monotonic clock; logs capped by default

Date: 2026-10-22. Status: resolved (owner, 2026-10-22: "fix both in this PR"), from the review before first real
use on the CM5.

## 1. Durations survive wall-clock jumps

A Pi without an RTC battery boots with a wrong date (often the last shutdown time, from fake-hwclock) and jumps
when NTP sets it. jm measured every duration with the wall clock, so a jump forward could fire a `timeout`, the
drain guard (`drain_after`) or `on_idle` at once, and a jump back could hold them off.

Decision:
- Durations that live in the daemon or a CLI process (`stop_grace` in the wrapper, `idle_after`,
  `progress_every`, `jm wait --timeout`, lock and start / stop waits, `applog -f`) use `time.monotonic()`.
- Durations stored in job files (`run_start` for `timeout` and the running time in `jm list`, `waiting_since` for
  `drain_after` and `head_wait`) keep their UTC text and get a second key, `<key>_mono = <boot id> <monotonic>`,
  on Linux (`/proc/sys/kernel/random/boot_id`). Within the same boot the elapsed time comes from the monotonic
  clock, also after a daemon restart; after a reboot, on macOS / Windows (which have an RTC and no boot id
  file), or for state written by an older jm, it falls back to the wall clock as before.
- `keep` (days, across reboots) and the times shown to people stay wall-clock UTC.

## 2. Logs are capped by default

`log_max` was 0 (no limit) by default and a job's log was rotated only when a run started, so one long run could
fill the disk; `daemon.log` was never rotated.

Decision:
- New config key `log_max` (MB, default 100) for every job log and `daemon.log`; a spec's `log_max` overrides it
  for that job; `0` = no limit. `log_max` applies to `stderr` (`split_stderr = yes`) too.
- Between runs (and when the daemon starts) a log over the cap becomes `<name>.1` (one older part is kept).
- During a run, on Unix, the wrapper checks every second: over the cap, the log is copied to `log.1` and emptied,
  with a banner line saying so. The job keeps writing: its file is open with O_APPEND, so it goes on at the new
  end. Bytes written between the copy and the cut are appended to the copy; only what lands in the last instant
  can be lost. The daemon trims `daemon.log` the same way after each pass.
- Not during a run on Windows: a writer there keeps its own file position, so emptying the file would leave a
  gap of NUL bytes up to that position. Windows logs rotate between runs and at daemon start only.
- `jm log -f` already starts a file again from the top when it shrinks.
