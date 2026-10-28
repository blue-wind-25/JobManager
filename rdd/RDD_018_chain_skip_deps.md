# RDD_018 - Holding stopped jobs, `--chain`, `jm skip`, `jm deps`, `after` warnings, fuller `jm status`

Date: 2026-10-29. Status: resolved (owner, 2026-10-29: "Build 1-6 as one release", from the gaps found while
writing docs/OPERATIONS.md).

## Problem

Running the CM5's ten-job V-30 chains showed six gaps, each worked around by hand:
1. A stopped job cannot be held (`jm hold` refuses it), so "parked, to go on later" and "a run I ended" cannot be
   told apart, and turning one into the other needs a requeue race.
2. Parking a chain means one `jm hold` per job, and later one `jm release` per job (the 9k arm and its eval).
3. "What waits for X?" has no answer but reading every `after: X` reason in `jm list`.
4. `jm set ID after=TYPO` is accepted silently and the job waits forever; so is a cycle.
5. No way to mark a job done by hand: skipping one in a chain needs `done_check=true` or re-pointing every
   dependent.
6. `jm status` leaves out `done_check`, `pre`, `post`, `stop`: the stored spec file has to be read for them.

## Decision

1. **`jm hold` on a stopped job** makes it held, with the next run a resume and the used retries reset (what
   `jm requeue` of the stopped job would do), reason "held by hand (was stopped)". `jm release` then queues it.
   The refusal text of `release` on a stopped job now names both ways (`jm requeue or jm hold`).
2. **`--chain`** on `hold` / `release`: the job, then every job that waits for it through `after`, directly or
   not (depth first, each once). `hold --chain` parks only the chain's queued / waiting jobs ("held with ID
   (--chain)"); running, stopped, held and finished ones are left as they are. `release --chain` queues every held
   job in the chain, also ones held by hand. With `--chain` the job itself may be in any state: a running or
   finished job's chain can be parked ("let this run finish, then stop"). Each job changed in the chain is printed
   (`held ID` / `released ID`).
3. **`jm deps ID [--align | --tabs]`**: the jobs that wait for ID through `after`, as a tree indented like
   `jm list --tree`, columns id, state, reason (all states, finished too). A job reached twice is shown once.
   **`jm status ID`** prints `needed by: ID:state ...` (direct dependents only).
4. **Warnings, not refusals**, from `jm set` (when it changes `after`), `jm requeue --spec-edit` (same) and
   `jm edit`: an `after` id that names no job, and a cycle back to the job (the shortest one found, breadth
   first). The change is applied and the exit code is 0, so scripts that set `after` before submitting the job it
   names keep working.
5. **`jm skip ID`**: a job that is queued, waiting, held, stopped, failed or cancelled becomes done without a run,
   reason "skipped by hand (was STATE)". Refused for a running / stopping job (stop it first), a done job, and a
   job with unfinished children (skip or cancel them first). `after_ok = yes` dependents may then start.
6. **`jm status ID`** also prints `done_check`, `pre`, `post` and `stop`, masked like `start`.

## Rationale

Every change adds a command, a flag or an output line; nothing an existing script or spec relies on changes: no
state file format, no new state, no change to admission, `jm list` output or exit codes of existing calls. A
1.4.0 daemon reads a state dir written by 1.5.0 (a skipped job is an ordinary done job; a held stopped job is an
ordinary held one with `how = resume`). Warnings instead of refusals in (4) keep `jm set` usable for a dependency
submitted later. `skip` records its origin in the reason, so a skipped job is never mistaken for one that ran.
