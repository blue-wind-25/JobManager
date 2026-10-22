# RDD_004 - The daemon is identified by a lock, not by its pid file

Date: 2026-10-21. Status: resolved (implementation).

## Decision

The daemon holds an exclusive lock on `STATE/daemon.lock` for its whole life (flock / msvcrt). "A daemon is
running" = that lock cannot be taken; a second daemon refuses to start. `daemon.pid` is informational
(`<pid> <start time> <jm version>`). `jm daemon stop` writes `STATE/daemon.stop` (works on Windows, where there is
no SIGTERM) and falls back to SIGTERM after 30 s on Unix. Jobs keep the design's identity rule: the wrapper's
`pid` file is `<pid> <start time> <random id>`, with that random id on the wrapper's command line.

Addendum 2026-10-22 (PR #3): the daemon writes the wrapper's `pid` file right after the fork, and between fork and
exec a Unix child still shows its parent's command line. A check in that window read "random id missing" = not
alive, and the answer was cached for the daemon's life, so a live wrapper could be taken for a cut-off run
(`test_token` failed now and then; 27 of 300 fresh processes in a loop). Now a miss on Unix is re-read for up to
1 s before it counts (and is cached); a hit is cached at once. Windows sets the command line at creation: no
retry there.
