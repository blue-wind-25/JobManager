# RDD_026 - Backfill with run-time estimates (EASY), design sec 12.1

Date: 2026-11-20. Status: resolved (owner, 2026-11-20: "lets see what can be improved with JobManager, then make
release"; sec 12.1 was the one v2 item left, its rules fixed in the design).

## Problem

v1 backfill lets any later job that fits start while the head waits, until the head has waited `drain_after`
(30 min); then nothing below it starts. Without durations that is the only protection: a long backfilled job can
hold the head back for hours, while the drain can idle the machine although a short job would have finished in
time. Since 1.6.0 (RDD_019) jm knows most durations: the spec's `estimate`, else the median of the job's `class`.

## Decision

As design sec 12.1, with these details:
- Each pass, once the head is found (and `backfill = yes`, the head has no `drain = yes`): if **every** running job
  has an expected remaining time (`remaining_seconds`: progress rate, else the estimate minus the time run, at least
  1 min for an overrun job), the **shadow time** is when the head would first fit, freeing the running jobs' cpu, ram
  and tokens in order of expected end. The head's reason gets "expected to start ~MM-DD HH:MM (estimates)".
- A later job that fits now starts only if its own estimate ends before the shadow time, or if the head still fits
  at the shadow time beside it; then it uses up part of that spare. Otherwise it waits: "backfill would delay head X
  (expected to start ~...)".
- While the plan holds, `drain_after` does not apply (the plan protects the head). Any running job without an
  estimate, a head that already fits by cpu / ram / tokens (it waits on disk), or a head that freeing every running
  job would not let fit: the v1 rule, unchanged.
- `disk` is not part of the shadow walk: it is not freed predictably at a job's end. A backfilled job still needs
  disk now, as before.
- Config key `backfill_estimates` (default yes); `no` gives exactly the v1 rule. An estimate that is wrong only
  moves the head's start; it never stops anything that already runs.

## Why default on

Every job with an estimate or a class on the owner's machines already reports ETAs; the PC and the CM5 queues are
mostly `after` chains, where nothing changes. Where it changes something it is the case the design wanted: a long
side job no longer slips in front of a waiting GPU or large-RAM head.

## Tests

`TestAdmission.test_easy_backfill_protects_head` (a short job runs ahead, a long one waits with the reason),
`test_easy_backfill_spare` (a long job that leaves the head's token free may start), `test_easy_backfill_off`,
`test_easy_backfill_unknown_estimate` (the v1 rule).
