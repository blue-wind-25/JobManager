# CLAUDE.md - working on JobManager (`jm`)

## What this is

`jm.py` is a single-file, single-machine job manager (queue, scheduler, monitor). `guards/` is claude-guards, a set
of Claude Code PreToolUse hooks, kept here until it gets its own repository and installed into this repository's
`.claude/`.

## Where things are

- `design/JM_DESIGN.md`, `design/CLAUDE_GUARDS.md` - the designs. Read the relevant section before changing
  behaviour.
- `rdd/RDD_<nnn>_<topic>.md` - resolved design decisions, one per file, numbered in order. A decision the design
  leaves open, or a deviation from it, gets a new RDD in the same commit as the code.
- `state/IMPL_STATE.md` - what is built, what is tested, what is next. Update it when a part changes status.
- `tests/` (jm, `unittest` only), `guards/tests/` (guard case lists + installer test).

## Hard rules

- **Python 3.6 floor, standard library only** for `jm.py` and its tests: no `subprocess.run(capture_output=...)`,
  no `text=`, no `dataclasses`, no walrus, no `shlex.join`, no `datetime.fromisoformat`, no argparse
  `required=` on subparsers. Check with `vermin -t=3.6- jm.py` when in doubt, or run the tests in
  `python:3.6-slim`.
- `guards/hooks/guard.py` and `guards/tests/run.py` must run on **Python 2.7 and 3**.
- Every state / config / spec file stays plain text; new state goes into new files (old state dirs must stay
  readable).
- Every source file starts with the Copyright / Apache 2.0 header (copy it from an existing file).
- jm never stores or prints a secret value (design sec 16). Keep `env` filtering, refusal and log masking intact.
- After changing `guards/hooks/*`, re-run `sh guards/install.sh --repo .` so `.claude/hooks/claude-guards/` matches
  (CI compares them).

## The guards are live in this repository

`.claude/settings.json` runs the guards on every Bash call (and protected-read on Read / Grep / Glob):
- Never print or dump the environment (`env`, `printenv`, `set`, `os.environ` without a key, ...) and never name
  a secret-looking variable (`*_TOKEN`, `*_SECRET`, `*_PASSWORD`, `*_API_KEY`, ...) in a command except to set,
  unset or hand it over by name. To search the code for such a word use the Grep tool, not `grep` in Bash.
- Text that must contain such names (docs quoting the pattern, test case lists) is written with the Write / Edit
  tools, not through a shell heredoc. Commit messages paraphrase them.
- Do not run `find` / `grep -r` / `rg` on `/`, `/home`, `/usr` or `~`.
- `gh`: log in with `gh auth login` (keyring), never through a token variable.

## Commands

```sh
python3 -m unittest discover tests              # jm: ~70 tests, ~90 s (each daemon test runs a real daemon)
python3 -m unittest discover -s tests -p 'test_units.py'   # fast unit tests only
python3 guards/tests/run.py                     # guard case lists (also: python2 guards/tests/run.py)
sh guards/tests/install_test.sh                 # installer
python3 -m pyflakes jm.py tests/*.py guards/hooks/guard.py
```

Tests run every job as `jm _sim` (a fake job: `--secs --exit --ram --progress --resumable --trap --print-env
--out`); each test has its own temporary JM_HOME and a daemon with `poll = 0.2s`. Set `JM_TEST_ARTIFACTS=DIR` to
keep the JM_HOME of failed tests. Job commands in tests must work in both `sh` and `cmd` (quote with `"`).

## Conventions

- Line length 120. Comments explain *why*; match the density of the surrounding code.
- Branches: in a session on the owner's PC, commit and push to `main` directly (owner direction, 2026-10-21); cloud
  sessions work on their assigned branch and open a PR.
- Commit and push after each meaningful slice; CI (`.github/workflows/ci.yml`, manual trigger for now:
  `gh workflow run ci.yml --ref <branch>`) runs the full matrix: Linux x86_64 / aarch64, macOS arm64 / x86_64,
  Windows, Python 3.6, Alpine (busybox), Fedora, and the guards on Python 2.7 / 3.
