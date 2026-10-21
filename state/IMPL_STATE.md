# Implementation state

Last update: 2026-10-21.

| Part | Status | Notes |
|---|---|---|
| Repository skeleton: licence, `design/`, `rdd/`, `state/`, README, CLAUDE.md, CHANGELOG, examples | done | RDD_001 |
| jm v1 - design/JM_DESIGN.md sec 1-11 | done | RDD_002 .. RDD_008 |
| jm secrets - sec 16 | done | RDD_005 |
| jm tests and CI - sec 12.5 | done locally; CI matrix added | see below |
| claude-guards - design/CLAUDE_GUARDS.md sec 2-5, 7 | done (in `guards/`, installed in `.claude/`) | RDD_009 |
| jm v2 - sec 12.1 estimates / EASY backfill, sec 12.2 progress parsing and ETA | not started | `progress` samples are already stored in the v2 format |
| jm v3 - sec 12.3 several users, sec 12.4 several machines (`jm free` already exists) | not started | |
| claude-guards in its own repository; adoption in HowToTrainYourPetAI (CLAUDE_GUARDS.md sec 6) | not started | `guards/` moves out as is |
| Dogfooding on the CM5 (JM_DESIGN.md sec 15.2) | not started | needs a v1.0.0 tag first |

## Tested

- `python3 -m unittest discover tests`: 69 tests, all passing on Python 3.13 (this cloud container), Python 3.6.15
  (`python:3.6-slim`) and Python 3.12 on Alpine / busybox (`python:3.12-alpine`).
- `guards/tests/run.py`: 167 cases, 0 mismatches on Python 3.13 and Python 2.7 (`python:2.7-slim`);
  `guards/tests/install_test.sh` passes.
- Not run locally: macOS, Windows, aarch64, Fedora - covered by CI (`.github/workflows/ci.yml`); see the PR's checks.

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

## Next

1. Get the CI matrix green (expect Windows / macOS fixes), then tag `v1.0.0`.
2. Dogfood on the CM5 (design sec 15.2), then v2.
