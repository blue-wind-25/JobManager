# RDD_019 - Estimates, progress parsing and an expected end for every queued job

Date: 2026-10-29. Status: resolved (owner, 2026-10-29: "Build 1-6 as one release, then 7"; 7 = "ETA from
`progress` + `estimate`", design sec 12.2).

## Problem

A session planning the CM5 queue works out by hand when each job of a ten-job chain will end: past durations of
similar jobs from `jm status`, the epoch rate from the progress line, then a sum along the `after` chain. jm has
every input but shows none of it. Design sec 12.2 specifies progress parsing and an ETA; sec 12.1 the estimate
sources (`estimate`, `class` history) for a later EASY backfill.

## Decision

- **New spec keys** (all optional): `class` (jobs of one class share duration history), `progress_re` (a Python
  regex with one group, done, or two, done and total; checked at submit / set: it must compile and have a group),
  `progress_total` (a number, the total when the regex has one group). The existing `estimate` (a duration for the
  whole job) is now used.
- **Progress** is read from the stored progress samples (`jobs/<id>/progress`, the last 20, taken every
  `progress_every`): the token `jm:progress=D/T` or `jm:progress=P%` anywhere in the line, else `progress_re`. The
  **rate** spans the first appearance of the oldest and of the newest value among the samples, so a value that
  holds for several samples is not read as a slowdown.
- **History**: a job with a `class` that ends with exit 0 (not by `done_check`, not skipped, not a group) appends
  `<class> <seconds> <id>` to `STATE/history`, seconds = the sum of all its runs (a run cut off by a reboot counts
  up to when the daemon noticed: small for a reboot, long for a long power-off). The file survives `jm clean`.
  The class estimate is the median of the last 5: history lines first, then done jobs still in the state dir that
  carry the class and are not in the history (so `jm set OLD_JOB class=X` on finished jobs seeds it).
- **Estimate of a job** = its `estimate`, else its class median, else unknown.
- **Remaining time of a running job**: with a total and a rate, (total - done) x rate minus the time since the
  newest value appeared; with a total but no rate yet, on a first run only, elapsed x (total - done) / done; else
  its estimate minus the time already run; at least 1 minute; else unknown.
- **Expected end** (`forecast`) for each queued job in run order (RDD_017): a running job ends now + remaining; a
  queued / waiting job starts when every `after` job has ended (done ones: now) and ends start + estimate. Held
  and stopped jobs, and everything after them or after an unknown, are unknown. **Resources are ignored**: on one
  machine the `after` chains decide the order that matters, and simulating admission is sec 12.1's job.
- **Shown, never acted on**: `jm list --eta` or `--fields ...,eta` adds an `eta` column (`~MM-DD HH:MM` in the
  machine's local time, `?` unknown, `-` held / stopped, empty for finished jobs). The default columns are
  unchanged, so scripts that read `jm list` see the same five fields. `jm status ID` adds `eta: ...` with what it
  rests on (`done/total`, `per step`, `estimate ... (estimate | class X, N runs)`). Shortcut `je` = `jl` with the
  eta column instead of progress.

Deviations from design sec 12.1 / 12.2: `estimate` and the class history are per job, not per run (a job that
resumes after reboots or retries is what a person plans by); the ETA is a column of its own, not appended to the
progress text, so the default output keeps its five fields.

## Rationale

Every part is read-only for the scheduler: admission, backfill and the drain guard do not change, so a wrong
estimate costs nothing. The keys are new and optional; a spec without them behaves as before. A 1.5.0 jm refuses
a spec that uses them (unknown key), so a worker must run 1.6.0 before its specs use them; `STATE/history` is a new
file that an older jm ignores. Using done jobs that carry a class lets a machine with past runs (the CM5) get
estimates at once by setting `class` on them, without waiting for a new history.
