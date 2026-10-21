# claude-guards

Reusable Claude Code `PreToolUse` safety hooks (design: ../design/CLAUDE_GUARDS.md). Kept in the JobManager
repository until it gets its own; this directory already has that repository's layout.

| Guard | Refuses |
|---|---|
| `secret-echo` | a secret variable named where its value could print (`echo $GITHUB_TOKEN`, `printenv`, `os.environ["..."]`), whole-environment dumps, unkeyed environment reads, token-like values on a command line |
| `broad-search` | `find` / `grep -r` / `rg` / `ag` / `fd` / `ack` rooted at `/`, `/home`, `/usr`, `~` (configurable) |
| `protected-read` | reading a `protected_files` entry (Bash, Read, Grep, Glob); writing it is allowed |

Each guard fails open (no Python, bad input, any error = allow) and runs on Python 2.7 and 3.

```sh
sh install.sh --user                 # every repository on this machine (~/.claude)
sh install.sh --repo PATH            # one repository (PATH/.claude, commit it: cloud sessions get it)
sh install.sh --check --repo PATH    # installed vs this copy's version
```

Settings: `~/.claude/guards.conf` and `<repo>/.claude/guards.conf` (see `hooks/guards.conf.example`). Restart the
Claude Code session after installing if the hooks do not take effect.

Tests: `python3 tests/run.py` (and `python2 tests/run.py`), `sh tests/install_test.sh`.

Known limits: a name assembled at run time, or a variable read inside a script file (the hook sees only the
command line), gets through; the hooks stop commands, not output. The real defence is not exporting long-lived
credentials in shell profiles (prefer keyring logins, credential helpers, 0600 env files).
