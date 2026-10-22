# RDD_015 - `report`: a job's own status summary, `jm report [ID] [-n N]`

Date: 2026-10-23. Status: resolved (owner, 2026-10-23: "make JM support calling" the project's status commands, the
count configurable), found while checking CM5 jobs from a phone.

## Problem

`jm status` shows jm's view (state, runs, RSS, a progress line) and `jm applog` the job's own log. Neither shows the
summary a job's own tools compute - a training run's per-language table, the last N epochs of a trainer - so the
owner typed project scripts with long paths and arguments over ssh from a phone, per job.

## Decision

- New spec key `report`: a command that prints the job's status summary. Shown by `jm status`.
- `jm report ID [-n N]` runs it for one job; `jm report [-n N]` for every job in the queue that has one (any state
  in the queue), in queue order, each under `==> ID <==`. `-n` is passed as `JM_LINES` (empty without it); the
  command decides what N means.
- It reuses `applog`'s rules: run by the CLI, never by the daemon; the job's `cwd`; `env` lines and `JM_*`
  variables, no `env_pass` / `env_file` secrets; output masked; exit 1 if a command exited non-zero.
- No `-f`: a report is a snapshot; following is `applog`'s job.
- Phone shortcut `jr [ID] [N]`: no argument = every job, a number alone = every job with N, else that job.

## Rejected

- Pointing `applog` at a status script: it breaks `-f` and the meaning of "the job's own log".
- Free-form "named commands" per job (`jm run ID NAME`): more surface for one need; a second need can extend it.
