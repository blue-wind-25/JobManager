# jm operator's reference

Everything needed to decide how to change a live queue - hold, re-point, requeue, skip, re-run - without reading
`jm.py`. Each rule here is what the code does in 1.6.0; what is new in 1.5.0 (RDD_018) is marked **(1.5)**, in
1.6.0 (RDD_019) **(1.6)**, so a machine on an older jm lacks it (`jm --version`). [GUIDE.md](GUIDE.md) explains the why and the features,
[REMOTE.md](REMOTE.md) the `jm remote` / `jm ssh` side. When this file and the code disagree, the code is right
and this file has a bug.

## 1. States in one table

| State | In the queue | Can start | Who puts it there | Who takes it out |
|---|---|---|---|---|
| queued | yes | yes, at the next pass | submit, release, requeue, a retry, a reboot resume | the daemon (waiting / running) |
| waiting | yes | yes, once the `reason` clears | the daemon: deps, resources, parent, hook | the daemon |
| running | yes | - | the daemon | the job's end, or stop / cancel / restart / requeue |
| stopping | yes | - | stop / cancel / restart / requeue / timeout on a running job | the daemon, once the wrapper ends |
| held | yes | **no**, never by itself | `jm hold`, `submit --hold`, `on_boot = hold`, a retry after `hold` on a running job | `jm release`, `jm requeue`, stop, cancel, skip |
| stopped | yes | **no**, never by itself (not after a reboot either) | `jm stop` | `jm requeue`, `jm hold` **(1.5)**, `jm cancel`, skip |
| done | no | - | exit 0, `done_check` passed, `jm skip` **(1.5)** | `jm requeue` (runs it again) |
| failed | no | - | non-zero exit, `pre` failed, timeout, `on_boot = fail`, with no retries left | `jm requeue` |
| cancelled | no | - | `jm cancel`, a failed / cancelled parent, a failed sibling | `jm requeue` |

- **Finished** = done, failed, cancelled. Everything else is "in the queue" and shows in `jm list` above the
  finished rows. Finished jobs stay listed for `keep` (default 7 days); `jm clean` deletes their directories.
- held and stopped are both "parked". The difference: held is meant to go on (`release`), stopped is a run you
  ended (`requeue`). `jm release` refuses a stopped job. `jm hold` turns a stopped job into a held one whose next
  run is a resume **(1.5)**; 1.4.0 refuses it.
- A parked job never blocks other jobs from being admitted: the admission pass skips it (sec 3).

## 2. What each command does, per state

`-` = refused with exit code 4 and nothing changes. "running" includes "stopping".

| Command | queued / waiting | running | held | stopped | done / failed / cancelled |
|---|---|---|---|---|---|
| `hold ID` | -> held | stays running; **held instead of retried** if this run fails | no change | -> held, resumes **(1.5)** | - |
| `release ID` | - | cancels a pending "hold after this run" (else -) | -> queued | - (use requeue) | - |
| `hold/release --chain ID` **(1.5)** | as above, then the chain | as above, then the chain | as above, then the chain | as above, then the chain | the chain only |
| `skip ID` **(1.5)** | -> done | - (stop first) | -> done | -> done | done: -; failed / cancelled -> done |
| `stop ID` | -> stopped | stop it -> stopped | -> stopped | no change | - |
| `cancel ID` | -> cancelled | stop it -> cancelled | -> cancelled | -> cancelled | - |
| `restart ID` | - | stop it -> queued at the **top**, `JM_HOW=restart` | - | - | - |
| `requeue ID` | stays, retries reset | stop it -> queued, `JM_HOW=resume` | -> queued | -> queued | -> queued at the end |
| `set ID k=v`, `edit ID` | changed | - (ended first) | changed | changed | changed (used by a later requeue) |
| `move`, `swap` | moves it | moves it | moves it | moves it | - (not in the queue) |
| `pause` / `cont` (Unix) | - | SIGSTOP / SIGCONT its command's process group | - | - | - |

Details that matter:
- **Stopping a running job** runs the spec's `stop` command (with `JM_PID`), or sends TERM to its process group;
  after `stop_grace` (60 s) the group gets KILL. The state is `stopping` until the wrapper has ended.
- **requeue** keeps the job's place when it is still in the queue; otherwise it goes to the end. `--top` / `--end`
  choose. It resets the used retries and sets the next run to `resume` (a job with no `resume` key runs `start`
  anyway). `--fresh` runs `start` instead of `resume` (`JM_HOW=start`). `--spec-edit
  k=v` changes the spec in the same step (the job must have ended first). `--children` also requeues its
  unfinished children (a group).
- **hold on a running job** does not stop it. If it ends with exit 0 it is done; if it fails with retries left it
  goes to held instead of queued; if it fails with no retries left it is failed.
- **stop / cancel with children** asks first on a terminal; give `--children` in a script (without it and without a
  terminal the command is refused).
- **restart** is only for a running job. For anything else use requeue.
- **`--chain`** **(1.5)** = the job, then every job that waits for it through `after`, directly or not. `hold
  --chain` parks only the chain's queued / waiting jobs (running, stopped, held, finished ones stay as they are);
  `release --chain` queues every held job in the chain, also ones held by hand. With `--chain` the job itself may
  be in any state, so `jm hold --chain RUNNING_JOB` lets this run finish and parks what comes after it. It prints
  `held ID` / `released ID` for each chain job it changed.
- **`skip`** **(1.5)** marks the job done without a run, reason "skipped by hand (was STATE)"; `after_ok = yes`
  dependents may then start. Refused for a running job, a done one and a job with unfinished children.

## 3. Admission: when a queued job starts

The daemon wakes on every command and every `poll` (5 s). Each pass:
1. Running jobs first: finished (record the run, move on), cut off (reboot / crash, `on_boot` decides), or
   over `timeout` (stopped, counted as a failure).
2. Then the queue **top to bottom**. held, stopped and running rows are skipped. For each queued / waiting job,
   the first of these that is not met becomes its `reason` (`jm list`, 4th column) and it stays waiting:
   - every `after` job finished as required (sec 4);
   - a child: its parent is running, and the group's `parallel` limit is not reached;
   - it could ever fit: `cpu` <= `cpus`, `ram` <= `ram - ram_reserve`, each token configured with enough count
     ("never fits: ..." otherwise - fix the spec);
   - it fits now, next to what runs: free cpu, ram, tokens, and `disk` MB free on its path.
3. A job that passes starts at once; the pass goes on to the next row (several jobs can start in one pass).
4. **Backfill**: a job that does not fit only because of resources becomes the *head*; later rows can still start
   if they fit, unless the head has `drain = yes`, the head has waited longer than `drain_after` (30 min), or the
   config says `backfill = no`. Then nothing below the head starts until it does ("head X waits").
5. On an idle machine the first startable job waits for `on_first_start` to succeed first.

So queue order decides who gets resources first; `after` decides who may start at all. A job blocked by `after`
or by "never fits" is not a head and does not block anyone.

## 4. Dependencies (`after`)

| The `after` job is | `after_ok = yes` (default) | `after_ok = no` |
|---|---|---|
| done | may start | may start |
| failed / cancelled | **waits forever**: `after: X failed (after_ok = yes)` | may start |
| queued, waiting, running, held, stopped | waits: `after: X <state>` | waits |
| missing (no such id) | waits: `after: X does not exist` | waits |

- **Nothing propagates along `after` by itself.** Holding, stopping, cancelling or failing a job changes nothing
  in the jobs that name it: they keep waiting, with the reason saying why. So a held job already parks its whole
  chain; holding the dependents too is only for clarity. `--chain` **(1.5)** does both in one command, and
  `release --chain` undoes it in one.
- A dependency is checked only **when the dependent is about to start**. If X is done, Y started and later X is
  requeued and re-run, Y is not re-run.
- Several `after` lines: all must be met.
- `submit` refuses an `after` id that does not exist (unless it is in the same submission). **`jm set` / `jm edit`
  accept it**: a typo leaves the job waiting with "does not exist", and a cycle (A after B, B after A) waits
  forever. **(1.5)** they print a warning for both (`after: no such job X`, `after: cycle A -> B -> A`) and still
  apply the change (exit 0).
- **(1.5)** `jm deps X` lists what waits for X (a tree, every state); `jm status X` has a `needed by:` line.
- Parent / child is different: a **failed or cancelled parent cancels** its unfinished children (also held and
  stopped ones), and a child that fails for good cancels its waiting siblings unless the group has
  `continue_on_fail = yes` (GUIDE sec 4).
- `jm list --order` (and `jl`) prints the queue in expected start order: running first, then each job after the
  jobs it names; a job whose `after` can never be met goes last.

## 5. One run, step by step

The wrapper runs, in the job's `cwd`:
1. `done_check`, if set. **Exit 0 = the job is done; nothing else runs.** It runs before *every* run: first start,
   retry, resume after reboot, requeue, `--fresh` included.
2. `pre`, if set. Non-zero = the run failed (retries apply).
3. `resume` when `JM_HOW` is resume / restart and the spec has one; otherwise `start`.
4. `post`, if set, with `JM_EXIT` (always, whatever the exit code).

The job is **done when step 3 exits 0** - jm cannot tell a finished job from one that exited 0 too early, so a
script must fail (`set -e`, explicit `exit 1`) when its work is incomplete. A stop request is honoured in any step.

`JM_HOW`: `start` (first run, `requeue --fresh`), `resume` (retry, reboot resume, `requeue`, `release` of a job
that had run), `restart` (`jm restart`). `JM_RUN` counts runs from 1.

## 6. Failures, retries, timeouts, reboots

- A run fails on: non-zero exit, `pre` failing, `timeout` reached, the wrapper unable to start. With
  `retries = N` it goes back to the queue **at its old place** as a resume run, up to N times; then failed.
- A retry after `jm hold` on the running job goes to held instead.
- Reboot / crash / daemon killed: running jobs whose process is gone are *cut off*. `on_boot = resume` (default)
  requeues them at the **top** as resume runs; `hold` parks them; `fail` fails them. A cut-off run does not use up a
  retry. A job that is still alive when the daemon comes back (the daemon alone restarted) is adopted, not re-run.
- A clean `poweroff` is a reboot for jm: same as above. stopped jobs stay stopped; held stay held.

## 7. Recipes

| I want to | Do this |
|---|---|
| Park a chain | `jm hold FIRST`; the rest waits by itself (sec 4). **(1.5)** `jm hold --chain FIRST` parks every job in it, `jm release --chain FIRST` unparks them. |
| Park a running job without losing work | `jm stop ID` (the job must resume from its own checkpoint); later `jm requeue ID` (resume), or **(1.5)** `jm hold ID` now and `jm release ID` later. |
| Let the current run finish, then park | **(1.5)** `jm hold --chain ID`. On 1.4.0: hold its dependents one by one (`jm hold ID` on the running job only catches a failure). |
| Insert a job into a chain A -> C | submit B with `after = A`, then `jm set C after=B`. |
| Re-point a dependent | `jm set C after=NEW` (replaces every `after` line; `after+=X` adds one). Not while C runs. |
| Skip a job in a chain | **(1.5)** `jm skip B` (stop it first if it runs). On 1.4.0: `jm set C after=OTHER` on each dependent, then `jm cancel B`; or `jm set B done_check=true`, and it is done at its next start. |
| Run a done job again | `jm requeue ID` (resume) or `--fresh` (start). Its `done_check` still runs first: remove its output, or `jm set ID done_check=` first. Dependents that already ran are not re-run. |
| Change a waiting job's command or env | `jm set ID start=...` / `env+=K=V` / `env=` (clears the list). Takes effect at its next start. |
| Change a running job | Not possible (`set` refused). Stop it, `jm requeue ID --spec-edit k=v`, or change the waiting jobs after it. |
| Make a job start next | `jm move ID --top` (it still waits for its `after` and resources). |
| Turn stopped into held | **(1.5)** `jm hold ID`. On 1.4.0: `jm set ID after+=SOME_HELD_JOB` then `jm requeue ID` (it waits). |
| Find what waits on X | **(1.5)** `jm deps X`. On 1.4.0: `jm list --order` and read the `after: X` reasons. |
| Power the machine down | `sudo poweroff`. Running jobs resume at boot (`on_boot = resume`), at the top of the queue. |

## 8. Spec changes (`set`, `edit`, `requeue --spec-edit`)

- Allowed in every state except running / stopping. The `id` never changes.
- `key=value` replaces every line of that key; `key+=value` appends to a list key (`env`, `after`, `tokens`,
  `env_pass`, `env_file`, `children`, `artifacts`, `tags`); `key=` resets it to its default (empty list for
  lists).
- The changed spec is checked like a submitted one (syntax, secrets, `start` present), except `after` ids: those
  only warn **(1.5)** (sec 4).
- The daemon reads the spec at each admission pass, the wrapper at each run start: a change applies from the next
  pass / run. The stored spec is `$JM_HOME/jobs/ID/spec`. `jm status ID` prints needs, `after`, `cwd`, `start`,
  `resume`, `applog`, `report`, the env *names* and **(1.5)** `done_check`, `pre`, `post`, `stop`; never env values
  (read the file for those).
- A change is logged in the job's events (`jm status ID`): "spec changed: KEY ...".

## 9. Reading the state quickly

| Question | Command |
|---|---|
| What runs, what waits and why | `jm list` (4th column is the reason); `jm list --order` for start order |
| One job: runs, exit codes, progress, events | `jm status ID` |
| What waits for it | `jm deps ID` **(1.5)** |
| When each job should end | `jm list --order --eta` (shortcut `je`), `jm status ID` line `eta:` **(1.6)** (RDD_019: from `estimate`, the `class` history and the progress rate; resources ignored, shown only) |
| Its spec as stored | `cat $JM_HOME/jobs/ID/spec` |
| Its captured output / its own log / its summary | `jm log ID -n 50`, `jm applog ID -n 50`, `jm report ID` |
| Free cpu, ram, tokens | `jm free` |
| Block until it ends (exit code = the job's) | `jm wait ID [--timeout 2h]` |
| From another machine | `jm remote list`, `jm remote status ID`, ... (REMOTE.md) |

Exit codes of the CLI: 0 ok, 2 usage, 3 no such job, 4 not allowed in this state, 1 other errors.
