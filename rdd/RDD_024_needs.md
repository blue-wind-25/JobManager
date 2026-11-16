# RDD_024 - `jm needs ID`: what a job waits for, upward

Date: 2026-11-16. Status: resolved (owner, 2026-11-16: "add it as a jm command, also is it possible to add something
like jm needs that will show all not done/canceled (is it a good idea?)").

## Problem

`jm deps ID` (RDD_018) shows what waits for a job. The other question, "what is this job waiting for, all the way
up?", had no command: `jm status` names only the direct `after` jobs, and `jm list` gives one reason per job. A long
chain (here an E2 bench behind two trainings, two joins, an E1 bench and its trainings) had to be walked by hand.

## Decision

- `jm needs ID [--all] [--align | --tabs]`: the jobs ID waits for through `after`, upward, as a tree in `after`
  order, each job once (as `deps`); columns id, state, reason.
- By default only what is still in the way: an `after` job that does not meet the rule yet - the rule the daemon
  applies (`deps_state`): done, or with `after_ok = no` any finished state - and under it that job's own. A met job
  and everything above it are left out (they no longer matter), so an empty answer means nothing is in the way.
- Failed and cancelled jobs are not "done" and stay in the tree when the waiting job has `after_ok = yes`: the reason
  column says "never met". Hiding them (the owner's first idea, "all not done / cancelled") would hide the one job
  that holds the chain forever. An `after` naming no job prints as state `missing`.
- `--all` prints every `after` job, met or not, with its state (the whole chain).

## Why not a `jm list` flag

The list is about every job; this is one job's chain. `deps` and `needs` are the two directions of the same tree.
