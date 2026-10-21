# RDD_009 - claude-guards lives in `guards/` until it gets its own repository

Date: 2026-10-21. Status: resolved (implementation; design/CLAUDE_GUARDS.md, design/JM_DESIGN.md sec 16.7).

## Decision

- `guards/` has the layout of the future claude-guards repository (`hooks/`, `tests/`, `install.sh`, `VERSION`),
  so it can be moved out with `git mv` and no edits. This repository installs it into its own `.claude/` with
  `sh guards/install.sh --repo .` (committed: cloud sessions get the hooks). CI checks that the installed copy
  equals `guards/hooks/`.
- Name detection: a word counts as a secret name when it is in `secret_names`, or it matches the pattern **and**
  is written like a variable name (upper case) or used as one (`$x`, `${x}`, `os.environ[...]`, `getenv(...)`,
  `printenv x`, ...). So `git commit -m "rotate the API token"` passes while `echo $token` is refused. An
  upper-case code identifier that matches (e.g. jm's `TOKEN_RE`) is refused too, as the old guard refused any
  mention; list such identifiers under `not_secret`.
- protected-read is also registered for the Read / Grep / Glob tools (matched on the path) - the old repository
  used `permissions.deny` rules for that second layer.
- broad-search also refuses `cd ROOT && find .` / `rg` style searches.
- `CLAUDE_GUARDS_CONF` (env) names one more settings file, read last (used by the tests).
- `.claude/guards.conf` here lists jm's own code identifiers that match the pattern under `not_secret`.
- Live effect: Claude Code picked the hooks up mid-session when `.claude/settings.json` appeared (the design
  expected a restart to be needed). Text that quotes the pattern itself is written with the Write tool.
