# RDD_025 - A pause outlives a reboot, and paused time is not part of `timeout`

Date: 2026-11-16. Status: resolved (owner, 2026-11-16: "make it paused until I resume (like held but the job is already
running)... there is already pause and cont but a reboot will cont it automatically").

## Problem

`jm pause` (SIGSTOP) is the only way to park a running job without ending its run. Two gaps made it unreliable for
"pause until I say so":

1. A reboot or crash kills the paused process. The daemon then finds a cut-off run and `on_boot = resume` (default)
   requeues it at the top: the job runs again by itself, the opposite of what the pause asked for.
2. `timeout` counted wall time since the run started, pauses included, so a long pause failed the run as timed out
   (the design already called pause/cont "risky for timeouts").

## Decision

- A run cut off while paused goes to **held** (whatever `on_boot` says), reason "run cut off while paused (reboot
  or crash); held: jm release resumes it", with the next run a `resume`. The process cannot outlive the reboot; the
  intent can. A stop request (`jm stop` / `cancel`) still wins, as before.
- `timeout` measures run time minus paused time: `jm pause` stamps the pause (wall + monotonic, as every stamp),
  `jm cont` adds its length to `paused_total` in the job's info; a new run clears both. On Linux the monotonic stamps
  make this exact; elsewhere the stamps are whole-second wall-clock text, so run and pause times are each off by up to
  a second (harmless for real timeouts; the test keeps margins of seconds after macos-15-intel failed one of 0.5 s).
- `jm stop` + `jm requeue` stays the other way to park a job: it frees cpu, ram and tokens (a paused job keeps them,
  and a paused CUDA process keeps its GPU memory) but ends the run, so the job must resume from its own checkpoint.
