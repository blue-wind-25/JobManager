# Implementation state

Last update: 2026-10-21.

| Part | Status | Notes |
|---|---|---|
| Repository skeleton: licence, `design/`, `rdd/`, `state/`, README, CLAUDE.md, CHANGELOG, examples | done | RDD_001 |
| jm v1 - design/JM_DESIGN.md sec 1-11 | done | RDD_002 .. RDD_008 |
| jm secrets - sec 16 | done | RDD_005 |
| jm tests and CI - sec 12.5 | done; full CI matrix green (PR #2) | see below |
| claude-guards - design/CLAUDE_GUARDS.md sec 2-5, 7 | done (in `guards/`, installed in `.claude/`) | RDD_009 |
| jm v2 - sec 12.1 estimates / EASY backfill, sec 12.2 progress parsing and ETA | not started | `progress` samples are already stored in the v2 format |
| jm v3 - sec 12.3 several users, sec 12.4 several machines (`jm free` already exists) | not started | |
| claude-guards in its own repository; adoption in HowToTrainYourPetAI (CLAUDE_GUARDS.md sec 6) | not started | `guards/` moves out as is |
| Dogfooding on the CM5 (JM_DESIGN.md sec 15.2) | not started | needs a v1.0.0 tag first |

## Tested

- `python3 -m unittest discover tests`: 73 tests (Python 3.13 and 3.6, 2026-10-21, after the hook changes). CI run 39576098266 (2026-10-21, commit 451f745) is green on
  every runner: ubuntu-24.04, ubuntu-24.04-arm, macos-latest (arm64), macos-15-intel, windows-latest, and the
  python:3.6-slim, alpine and fedora containers.
- `guards/tests/run.py`: 167 cases, 0 mismatches on Python 3 and Python 2.7; `guards/tests/install_test.sh` passes
  (CI job claude-guards).
- Fixes needed to get there (PR #2): the test harness resolves its temp dir (macOS `/var` -> `/private/var`),
  `test_backfill` holds its blocker job until it cancels it (slow process start on Windows),
  `test_stop_never_resumed` waits for the queue rewrite, container artifacts are made readable by the runner.
  All were test or workflow issues; jm.py itself needed no change.
- CI is manual-trigger only: `gh workflow run ci.yml --ref <branch>`.

## Coverage of the design's test list (sec 12.5, 16)

submit / list / order ops; admission by cpu, ram, tokens, disk (faked with `JM_TEST_DISK_FREE`); backfill, the
drain guard (`drain = yes` and `drain_after`), `backfill = no`; `after` / `after_ok`; hold / release / stop /
restart / requeue / cancel; retries (failing and succeeding on retry); timeout; `done_check`, `pre`, `post`,
`notify`, `stop` command, `stop_grace` kill; daemon SIGKILLed while jobs run (job finishing meanwhile, and adopted
alive); wrapper killed with `on_boot` resume / hold / fail; PID reuse (pid file pointing at a live unrelated
process); static groups (`parallel`, a failing child, `continue_on_fail`), dynamic children, cancelling a parent;
plain-text hand-edit round trip; secrets: refused names and values in `env` lines, `env_pass` reaches the job
while other secrets do not, `env_file` mode check, log masking, `--raw` refused without a terminal; service files;
on_first_start / on_idle hooks; pause / cont; progress sampling and `jm status` live usage.

## Known limits

- The guards judge the command text only: prose in a shell heredoc that quotes a secret-looking name, or a word
  like `env` between backticks, is refused too. Write such text with the Write / Edit tools (see CLAUDE.md).

## Decisions

All reviewed by the owner on 2026-10-21; none open.
- Confirmed as built: RDD_005 (job environment minus secret names; the guard's name pattern), RDD_003
  (`jm restart` only for a running job, to the top of the queue; retries run `resume`, else `start`), RDD_006
  (`continue_on_fail`: recovery is the group's own business; no "stop running siblings" mode), RDD_007 (`jm list`
  has no header), RDD_008 (Windows stop).
- Changed after review: RDD_010 - machine hooks run outside the state lock, `hook_timeout` (120 s), `idle_after`
  (60 s), `STATE/machine`, `JM_HOOK`; `cwd` expands `$NAME` (RDD_005).
- CI stays manual-trigger only (owner direction, 2026-10-21).
- User documentation: docs/GUIDE.md (behaviour), docs/PORTING.md (moving a project's own job scripts to jm).

## Next

1. Tag `v1.0.0` (CI is green).
2. Dogfood on the CM5 (design sec 15.2), then v2.
