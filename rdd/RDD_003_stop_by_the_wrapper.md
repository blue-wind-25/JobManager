# RDD_003 - Stops are carried out by the wrapper, finished by the daemon

Date: 2026-10-21. Status: resolved (implementation).

## Decision

- `jm stop / restart / requeue / cancel` of a running job writes `jobs/<id>/stop_req` (`after = stopped |
  restart | requeue | cancel`, plus a reason) and sets the state to `stopping`; the CLI returns at once.
- The wrapper (`jm _run`) polls for `stop_req`: it runs the spec's `stop` command (with `JM_PID`) or sends TERM
  to the job's process group, waits `stop_grace`, then KILLs the group. It keeps watching the group after the
  shell has exited, so a process that ignores TERM cannot survive the stop.
- The daemon, on seeing the `exit` file, applies `after`. A timeout is the same request with `after = fail`
  (retries apply).
- Consequence: a stop works with the daemon down; only the final state change waits for the daemon.
- `restart` puts the job at the top of the queue so it gets its resources back first. It is only for a job whose
  own command is running; in any other state (including `stopping`, and a group whose command has ended) it exits
  4 and changes nothing - `jm requeue` is for those. **Confirmed by the owner, 2026-10-21.**
- Retries after a failure run with `JM_HOW=resume` (the resume command), like a cut-off run; a job without a
  `resume` command runs `start` again (it can still read `JM_HOW` / `JM_RUN`). **Confirmed by the owner,
  2026-10-21.**
- Documented for users in docs/GUIDE.md sec 2 and 5.
