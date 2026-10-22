# RDD_014 - A state dir that goes read-only while idle (home_rw)

Date: 2026-10-23. Status: resolved (owner, 2026-10-23: "if you need to modify the JM source, do it"), found while
installing jm on the CM5 (design sec 15.2), where `JM_HOME=/mnt/data/pi/jm` sits on the data mount that
`on_idle = data-ro.sh` remounts read-only, and the machine has no other writable storage.

## Problem

With 1.0.0, tested with a home made read-only by the on_idle hook:
- the daemon died on its next write after the hook (`daemon.log`, `machine`; PermissionError / EROFS);
- every command failed on a read-only home, readers included: `jm list` / `jm status` open the state lock
  `O_RDWR | O_CREAT`;
- a daemon started after a boot (the mount comes up read-only) died at once;
- on a real remount the hook could not even succeed: `mount -o remount,ro` fails with EBUSY while any file on the
  mount is open for writing, and the daemon held three - its `daemon.lock` (`O_RDWR`), its stdout / stderr
  (`daemon.log`, inherited from `jm daemon start`) and the hook's own output file (`daemon.log` again).

## Decision

- New config key **`home_rw`** (a command, empty by default): any jm command that must write and finds its home
  read-only runs it first (`JM_HOOK=home_rw`, `JM_HOME`, the config's env; `hook_timeout`), then checks the home is
  writable, else fails with the command's output. Without `home_rw` the error says to set it. Writers are
  everything that goes through the state transaction (submit, every queue / job change, config changes, clean),
  `daemon start` (only when no daemon runs) and `daemon stop`.
- Commands that only read (`list`, `status`, `log`, `applog`, `wait`, `free`, `artifacts`, `config` without
  assignments, `daemon status`) work on a read-only home as it is; `Store.init()` creates nothing there.
- **No writer is held on the home** by an idle daemon (Unix): lock files are opened read-only (flock needs no write
  access; a read-only fd does not count as a writer for a remount); once started, the daemon points its stdout /
  stderr at `/dev/null` when they are a file on the home's file system (start-up errors still reach daemon.log);
  hook output goes through a pipe into `daemon.log` lines (`<hook> output:`) instead of the file itself. Windows
  keeps its `O_RDWR` lock files (no remount there).
- **on_idle order:** the daemon writes `machine idle` *before* it runs on_idle. Then, while the home is read-only,
  it skips its passes and writes nothing; `daemon.log` lines that cannot be written are dropped. Nothing can be
  queued meanwhile (a writer makes the home writable first), so the next pass after `home_rw` sees the new work and
  runs `on_first_start` as usual.
- A lock file that cannot be created on a read-only home is granted without a file (nothing can change the state).

## Rejected

- Moving `daemon.lock` to `/tmp` (owner's suggestion): the read-only fd already lets the remount through, keeps
  the lock beside the state it guards (one home, one lock, no `/tmp` name per home) and survives reboots the same
  way.
- Reusing `on_first_start` to make the home writable from the CLI: that hook means "the machine goes busy"; a
  submit that is not admitted at once must not flip it. Most machines set both to the same script.
- A hook output file kept open only during the hook: the on_idle hook is exactly the one that remounts.

## Consequences

The CM5 recipe (design sec 15.2) is `home_rw = on_first_start = data-rw.sh`, `on_idle = data-ro.sh`; a
`~/.bashrc` line `jm daemon start` needs no `data-rw.sh` before it. A hook that leaves a background process holding
its stdout makes the daemon wait for `hook_timeout` (the pipe stays open); redirect such a process's output.
