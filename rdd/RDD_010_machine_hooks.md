# RDD_010 - Machine hooks run outside the lock, once per busy period

Date: 2026-10-21. Status: resolved (owner review of RDD_007's first version, 2026-10-21). Supersedes the hook
part of RDD_007.

The first version ran `on_first_start` / `on_idle` inside the daemon's pass, holding the state lock for up to
600 s, so every `jm` command waited for a slow `data-rw.sh`. It also ran `on_idle` the moment the last job
ended, so a chain of jobs flipped the mount read-only and writable between every job.

## Decision

- No built-in script names and no default hooks: `on_first_start` / `on_idle` are empty until the config sets
  them, and each is any command line. `data-rw.sh` / `data-ro.sh` appear in the docs as examples only (owner,
  2026-10-21).
- The daemon runs a hook **after** its pass, with the lock released: CLI commands never wait for a hook. Only the
  daemon waits (its next pass follows at once when `on_first_start` succeeded).
- `on_first_start` runs before the first job starts on an idle machine. If it fails (non-zero, or killed after
  `hook_timeout`, exit 124), no job starts; the waiting jobs' reason says so and the hook is retried every poll.
- `on_idle` runs when nothing has run for `idle_after` (config, default 60 s), so chained jobs keep the machine
  busy. Without `on_idle` the machine just becomes idle after `idle_after`.
- `hook_timeout` (config, default 120 s) replaces the fixed 600 s.
- The busy / idle state is a plain-text file, `STATE/machine` (`busy <UTC>` / `idle <UTC>`): it survives a daemon
  restart (no second `on_first_start` while jobs keep running), `jm status` shows it, and outside scripts can read
  it.
- Hooks get `JM_HOOK` (`on_first_start` / `on_idle`) and no `JM_ID`; jobs get `JM_ID`. jm cannot prevent a job
  from running `data-ro.sh` itself, so the documented answer is to make such scripts jm-aware with these
  variables and the `machine` file (docs/GUIDE.md sec 6), and to remove such calls when porting
  (docs/PORTING.md).
