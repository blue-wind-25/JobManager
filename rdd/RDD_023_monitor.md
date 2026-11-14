# RDD_023 - `monitor`: commands that run beside a job and end with it

Date: 2026-11-15. Status: resolved (owner, 2026-11-15: "check if jm can be improved to start/stop monitoring
script (in this case NVMe monitor) automatically").

## Problem

The owner's PC guards every job with a drive-temperature script (it stops the job when an NVMe drive runs hot).
It was started by hand with `systemd-run` after each `jm submit`, one per job. A guard was forgotten or started
with a relative path (the job ran unguarded). A requeued job needed a new guard. A held job's guard kept
running, because the script only ended on done / failed / cancelled / stopped. None of this is the script's
fault: jm knows when a run starts and ends, the script does not.

## Decision

- Spec key `monitor` (a list: each line is one command) and the same key in the machine config. The config's
  monitors run for every job, then the spec's own; `config_monitor = no` in a spec skips the config's.
- The wrapper starts the monitors right before `start` / `resume`, after `done_check` and `pre`. So a run that
  `done_check` skips, or whose `pre` fails, starts none. They stop when that command ends, before `post`: TERM
  to each monitor's process group, KILL after 10 s, then anything left in its group goes too. On Windows it is
  the process tree, forced, as for `stop`.
- A monitor gets the job's environment, cwd and log (its output goes into the job's log, after a
  `monitor N started` banner), plus `JM_MONITOR` (its number, 1 first). Its exit status never changes the job's.
  One that ends by itself is logged (`monitor N had ended by itself, exit R`).
- A monitor that has to act on the job calls jm, e.g. `$JM_JM stop $JM_ID`. That is a normal stop: the job ends
  stopped, and the monitor is stopped with it.
- Every run starts its own monitors: a retry, a requeue, a resume after a reboot. Held, waiting and stopped jobs
  have none. A daemon restart does not touch them, because the wrapper owns them, as it owns the job.
- `jm status` lists the monitors a job gets (masked like the other commands); monitor lines are refused when
  they hold a secret-looking value, like every other command key.

## Rationale

The wrapper already owns the job's life: it is the one place that sees each run start and end, survives a daemon
restart, and dies with a reboot as the job does. A monitor there cannot outlive its run or miss one.
A config key, not only a spec key, because a machine-wide guard should not depend on each spec remembering it.
The opt-out is for jobs that must not be guarded that way, e.g. one that only copies files over the network.
Starting after `pre` keeps a monitor off runs that never do the work. A monitor's exit status is ignored,
because a watcher that crashes must not fail a 10-hour run. A guard that must stop the job says so with `jm stop`.
A fixed 10 s grace, not a key: a monitor is a small script, and nothing so far needs a longer one.
