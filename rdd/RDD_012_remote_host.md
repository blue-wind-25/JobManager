# RDD_012 - Remote use is plain `ssh -t WORKER jm ...`; short commands for a phone

Date: 2026-10-22. Status: resolved (owner, 2026-10-22: "are the commands the same over ssh? document it; keep
the `ssh -t` command as short as possible; I will also check jobs from a phone").

## Decision

- **No `jm --host` in v1** (owner, 2026-10-22). `ssh -t WORKER jm <command>` runs the same commands with the same
  output and exit codes; design sec 12.4's `jm --host` stays v3, with `--host any` and `stage`. (A `-H` option
  was built and then dropped in the same PR: what it saved - quoting, sending a local spec, `-t` only at a
  terminal - is a few documented rules, not worth a second way to run every command.)
- docs/REMOTE.md documents what differs over ssh instead of hiding it:
  - jm and `JM_HOME` must be visible to non-interactive ssh commands: most `~/.bashrc` files return early
    for them, so put jm on a PATH directory that is always there (`/usr/local/bin`), or the exports above the
    early return. A check command is given.
  - the words are split again by the worker's shell (quote one more level);
  - a local spec goes on stdin, `ssh pi jm submit - < FILE`, without `-t`; group specs and their children files
    must be on the worker;
  - a job without `cwd` runs in the login directory on the worker;
  - `-t` for `-f`, prompts, `$EDITOR`, `--raw`; not in scripts (`\r\n`, stderr mixed in).
  - Shortest form on a workstation: `~/.ssh/config` host name (+ ControlMaster) and `alias jp='ssh -t pi jm'`.
- **From a phone** the ssh app logs into the worker, so short commands belong in the worker's shell, not in
  jm: `examples/jm-shortcuts.sh` (sourced from `~/.bashrc`) defines `jl js jf ja jt`, whose ID defaults to the
  first running top-level job (a group with its children). jm itself gets no "default job": a guessed job is
  fine to look at, not to stop or cancel, and the shell functions keep that choice visible and editable.
  Documented in docs/REMOTE.md sec 3 (ConnectBot post-login `jl`, Termux `ssh -t pi bash -ic jf`).
