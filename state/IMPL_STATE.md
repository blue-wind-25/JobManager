# Implementation state

Last update: 2026-10-23.

| Part | Status | Notes |
|---|---|---|
| Repository skeleton: licence, `design/`, `rdd/`, `state/`, README, CLAUDE.md, CHANGELOG, examples | done | RDD_001 |
| jm v1 - design/JM_DESIGN.md sec 1-11 | done | RDD_002 .. RDD_008 |
| jm secrets - sec 16 | done | RDD_005 |
| jm logs: `jm log --children / --stdout / --stderr` (`split_stderr`), `applog` and `jm applog` | done; CI matrix green (PR #3) | RDD_011 |
| Monotonic durations, default log cap (`log_max` 100 MB, trimmed during a run on Unix) | done | RDD_013 |
| docs/REMOTE.md (ssh use, phone); `examples/jm-shortcuts.sh` | done; shortcuts tested in sh (CI: also busybox on Alpine), CI matrix green (PR #3) | RDD_012 (no `jm --host` in v1) |
| jm tests and CI - sec 12.5 | done; full CI matrix green (PR #2) | see below |
| claude-guards - design/CLAUDE_GUARDS.md sec 2-5, 7 | done (in `guards/`, installed in `.claude/`) | RDD_009 |
| jm v2 - sec 12.2 progress parsing and ETA, estimates (`estimate`, `class`, `STATE/history`), `jm list --eta` | done (1.6.0) | RDD_019; `TestProgress`, `test_eta`, `test_history_and_running_eta` |
| jm v2 - sec 12.1 EASY backfill with estimates | not started | the estimates of RDD_019 are its input |
| jm v3 - sec 12.3 several users, sec 12.4 several machines (`jm free` already exists) | not started | |
| claude-guards in its own repository; adoption in HowToTrainYourPetAI (CLAUDE_GUARDS.md sec 6) | not started | `guards/` moves out as is |
| Release 1.0.0 (`VERSION`, CHANGELOG; tag `v1.0.0` on the release PR's merge) | done | |
| Read-only home while idle: `home_rw`, no writer held by an idle daemon | done (1.1.0) | RDD_014; `TestReadOnlyHome` |
| Spec key `report`, `jm report [ID] [-n N]`, shortcut `jr` | done (1.2.0) | RDD_015; `TestReport`, `TestShortcuts` |
| Hosts file, `jm ssh` / `scp` / `keygen` / `remote` | done (1.3.0) | RDD_016; `tests/test_remote.py`, CI `jm-ssh`; checked by hand against the CM5 |
| `jm list --order`, `--fields`, aligned columns; `jl` in run order | done (1.4.0) | RDD_017; `test_list_order_fields_align` |
| `jm hold` of a stopped job, `hold / release --chain`, `jm skip`, `jm deps`, `after` warnings, fuller `jm status` | done (1.5.0) | RDD_018; `test_hold_stopped_and_chain`, `test_skip`, `test_deps_status_and_after_warnings`, `test_skip_and_hold_stopped` |
| docs/OPERATIONS.md: every command per state, admission, `after` rules, run steps, recipes | done (docs only; 1.5.0 marked) | rules checked against 1.4.0 code and a scratch daemon (`after` on cancelled / stopped / missing jobs) |
| Dogfooding on the CM5 (JM_DESIGN.md sec 15.2) | in progress, 1.4.0 (HowToTrainYourPetAI RDD_646-653) | 1.0.0 showed RDD_014; passed: submit, run, reboot and a physical power cut mid-run (resumed by itself, systemd user unit + linger, GUIDE sec 9), logs and `report` from the phone, `after` chains of 10+ jobs with hold / stop / `jm set after=` re-pointing (2026-10-28/29); left: results fetched |

## Release procedure (every release)

1. CHANGELOG section + `VERSION` in jm.py; README's install URL to the new tag; full test suite green locally.
2. Commit, push `main`, run CI (`gh workflow run ci.yml --ref main`), wait until every job is green.
3. `gh release create vX.Y.Z --target main --title "jm X.Y.Z" --notes-file NOTES` (notes = the CHANGELOG section
   plus the CI run id); check `git rev-parse --short vX.Y.Z^{commit}` = `origin/main`.
4. **Install on the owner's PC** from the tag, never the working tree:
   `git show vX.Y.Z:jm.py > ~/bin/jm && chmod 755 ~/bin/jm && jm --version` (`~/bin` is on PATH; no daemon runs on
   the PC, so no restart).
5. Workers are upgraded by their projects, not from here: HowToTrainYourPetAI `tools/cm5/cm5_jm_update.sh` for the
   CM5 (owner runs it when the queue allows; it keeps the old file as `jm-<old version>`).

## Tested

- 2026-10-22, PR #3: the intermittent `test_token` failure was a real race (RDD_004 addendum): a wrapper checked
  between fork and exec still shows the daemon's command line, and the miss was cached. Fixed (a Unix miss is
  re-read for up to 1 s); 27 of 300 fresh processes missed before, 0 after; `test_token_after_fork` covers it.
- 2026-10-22, logs change (RDD_011) and phone shortcuts (RDD_012), clock and log cap (RDD_013): 81 tests pass on Python 3.13 (Linux);
  `vermin -t=3.6-` finds nothing newer than 3.6. CI matrix green on every runner for PR #3 (run 39711720510,
  commit 4104de7; push / PR triggers were on for that PR only, manual again after it).
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
on_first_start / on_idle hooks; pause / cont; progress sampling and `jm status` live usage; logs (RDD_011):
`jm log --children` (plain and `-f`), `split_stderr` with `--stdout / --stderr`, `jm applog` (group and
`--children`, `-n`, `JM_LINES`, no `env_pass` secret, a failing applog, `-f` ending with the job, `-f` on an
ended job with a slow applog); the phone shortcuts (`examples/jm-shortcuts.sh`) in sh; durations from a
boot-scoped monotonic stamp (`elapsed`), `trim_log`, a job trimmed while it writes past `log_max`.

## Known limits

- The guards judge the command text only: prose in a shell heredoc that quotes a secret-looking name, or a word
  like `env` between backticks, is refused too. Write such text with the Write / Edit tools (see CLAUDE.md).

## Decisions

RDD_001 .. RDD_010 reviewed by the owner on 2026-10-21, RDD_011 .. RDD_013 on 2026-10-22; RDD_014 (2026-10-23, read-only home), RDD_015 (`report`) and RDD_016 (hosts file, ssh / scp / keygen / remote) and RDD_017 (`list --order`, aligned columns) await review.
- Confirmed as built: RDD_005 (job environment minus secret names; the guard's name pattern), RDD_003
  (`jm restart` only for a running job, to the top of the queue; retries run `resume`, else `start`), RDD_006
  (`continue_on_fail`: recovery is the group's own business; no "stop running siblings" mode), RDD_007 (`jm list`
  has no header), RDD_008 (Windows stop).
- Changed after review: RDD_010 - machine hooks run outside the state lock, `hook_timeout` (120 s), `idle_after`
  (60 s), `STATE/machine`, `JM_HOOK`; `cwd` expands `$NAME` (RDD_005).
- CI stays manual-trigger only (owner direction, 2026-10-21).
- User documentation: docs/GUIDE.md (behaviour), docs/OPERATIONS.md (changing a live queue: commands per state,
  `after`, recipes), docs/REMOTE.md (driving workers over ssh), docs/PORTING.md (moving a project's own job
  scripts to jm).
- RDD_011 (logs) confirmed: `split_stderr` off by default; `applog` gets no `env_pass` / `env_file` secrets
  **and** its output is masked (documented; relax only if it causes problems).
- RDD_012 (owner): no `jm --host` in v1; plain `ssh -t WORKER jm ...`, documented in docs/REMOTE.md, plus the
  phone shortcuts.
- RDD_013 (owner: in PR #3): durations on the monotonic clock, logs capped by default.
- RDD_004 addendum: the fork / exec window in the wrapper identity check (fixed in PR #3, see Tested).

## Next

1. Finish the CM5 dogfood cycle (design sec 15.2): the chained job (gso_b run 2 `after` the tagger job) runs and
   both results are fetched; then the project retires its old queue scripts.
2. Then v2.
