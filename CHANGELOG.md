# Changelog

## Unreleased (1.0.0.dev0)

- jm v1 (design/JM_DESIGN.md sections 1-11, 12.5 and 16): state dir and lock, spec / config parser, CLI, daemon
  loop, scheduler with backfill and the drain guard, detached per-job wrapper, process identity (pid + start time
  + token), reboot / crash adoption with `on_boot`, static groups and dynamic children, systemd / launchd /
  Task Scheduler service files, secret refusal / `env_pass` / `env_file` / log masking, `jm free`.
- claude-guards (design/CLAUDE_GUARDS.md) in `guards/`: secret-echo, broad-search and protected-read hooks, case
  tests on Python 2.7 / 3, `install.sh --user | --repo`; installed into this repository's `.claude/`.
- CI matrix: Linux x86_64 / aarch64, macOS arm64 / x86_64, Windows, Python 3.6, Alpine, Fedora (manual trigger).
- Machine hooks run outside the state lock; new config keys `hook_timeout` (120 s) and `idle_after` (60 s);
  `STATE/machine` records busy / idle; hooks get `JM_HOOK`. `cwd` expands `$NAME`. `jm submit --continue-on-fail`.
- docs/GUIDE.md (behaviour reference) and docs/PORTING.md (moving a project's job scripts to jm).
- Logs (RDD_011): `jm log --children` (a job and its descendants, `==> ID <==` sections, `-f` follows new
  children too), spec key `split_stderr` with `jm log --stdout / --stderr`; spec key `applog` (a command that
  prints the job's own log) and `jm applog ID [-f] [-n N] [--children]`, run without `env_pass` / `env_file`
  secrets. `jm log -f` counts bytes (Windows `\r\n`) and survives `log_max` rotation.
- docs/REMOTE.md (RDD_012): driving a worker with `ssh -t WORKER jm ...` (what differs over ssh, the
  non-interactive PATH, short host names and aliases) and from a phone: examples/jm-shortcuts.sh
  (`jl js jf ja jt`, the ID optional).
