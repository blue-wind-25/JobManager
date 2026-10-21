# RDD_007 - CLI details

Date: 2026-10-21. Status: resolved (implementation; design sec 9).

## Decision

- `jm submit` positions are `--top | --after ID | --end` as in the design; an `after` dependency is given inline
  with `--dep ID` (or `--set after=ID`). `--set key=value` sets any spec key; `--name --cpu --ram --disk --timeout
  --retries --cwd --tokens --env --id` are shorthands. A spec without `cwd` gets the submit directory, written into
  the stored spec.
- `jm set ID key=value | key+=value | key=` replaces, appends to a list key, or resets to the default. `jm set` and
  `jm edit` work in every state except running / stopping (also on done / failed jobs, for a requeue).
  `jm requeue ID --spec-edit key=value` does both in one step.
- `jm move --by-priority` stable-sorts the queue by `priority` (higher first): the only use of `priority`.
- `jm list` prints no header: one job per line, tab-separated `id state name reason progress`; `--tree` indents
  children under their parent.
- `jm status` without an id shows the daemon, the free capacity and the job counts; `jm free` (design 12.4) is
  in v1 because it is cheap.
- `jm wait`: exit 0 for done; for failed / stopped / cancelled the last run's exit code if 1..255, else 1;
  `--timeout` expiry exits 1 with a message on stderr.
- `jm log -f` stops following once the job has ended and the log is quiet.
- `#` starts a comment at a line start or after white space (so URL fragments survive); `\#` is a literal `#`.
- The daemon samples each running job's `progress` command every `progress_every` (config, default 60 s) into
  `jobs/<id>/progress` (last 20 `<UTC> <line>` lines, the v2 format of sec 12.2); `jm list` shows the last line.
- `on_first_start` / `on_idle` run synchronously in the daemon (600 s limit), so the mount is writable before the
  first job starts.
