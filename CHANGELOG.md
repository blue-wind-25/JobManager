# Changelog

## Unreleased (1.0.0.dev0)

- jm v1 (design/JM_DESIGN.md sections 1-11, 12.5 and 16): state dir and lock, spec / config parser, CLI, daemon
  loop, scheduler with backfill and the drain guard, detached per-job wrapper, process identity (pid + start time
  + token), reboot / crash adoption with `on_boot`, static groups and dynamic children, systemd / launchd /
  Task Scheduler service files, secret refusal / `env_pass` / `env_file` / log masking, `jm free`.
- claude-guards (design/CLAUDE_GUARDS.md) in `guards/`: secret-echo, broad-search and protected-read hooks, case
  tests on Python 2.7 / 3, `install.sh --user | --repo`; installed into this repository's `.claude/`.
- CI matrix: Linux x86_64 / aarch64, macOS arm64 / x86_64, Windows, Python 3.6, Alpine, Fedora.
