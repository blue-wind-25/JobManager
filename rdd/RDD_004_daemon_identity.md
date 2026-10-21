# RDD_004 - The daemon is identified by a lock, not by its pid file

Date: 2026-10-21. Status: resolved (implementation).

## Decision

The daemon holds an exclusive lock on `STATE/daemon.lock` for its whole life (flock / msvcrt). "A daemon is
running" = that lock cannot be taken; a second daemon refuses to start. `daemon.pid` is informational
(`<pid> <start time> <jm version>`). `jm daemon stop` writes `STATE/daemon.stop` (works on Windows, where there is
no SIGTERM) and falls back to SIGTERM after 30 s on Unix. Jobs keep the design's identity rule: the wrapper's
`pid` file is `<pid> <start time> <random id>`, with that random id on the wrapper's command line.
