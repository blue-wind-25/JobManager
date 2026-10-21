# jm user guide

How jm behaves in the cases where "what exactly happens?" matters. The full design is
[design/JM_DESIGN.md](../design/JM_DESIGN.md); the decisions behind each rule are in [rdd/](../rdd/).

## 1. Job states

```
queued ──► waiting ──► running ──► done
   ▲          │           │  │
   │          │           │  └──► failed ──(retries left)──► queued (same place in the queue)
   │          ▼           ▼
   └────── held      stopping ──► stopped          cancelled (out of the queue)
```

| State | Meaning |
|---|---|
| queued / waiting | in the queue, not started yet; `reason` says why (`jm list`, 4th column) |
| running | its command runs (or, for a group, its children do) |
| stopping | a stop / restart / requeue / cancel / timeout is in progress |
| done | exit 0, or `done_check` passed before starting |
| failed | non-zero exit, `pre` failed, timeout, or a run cut off with `on_boot = fail` - and no retries left |
| held | parked by hand (`jm hold`); never started until `jm release` |
| stopped | stopped by hand; **never resumed automatically**, not even after a reboot; `jm requeue` brings it back |
| cancelled | removed by hand (`jm cancel`) |

The queue (`STATE/queue`) holds every job that is not done / failed / cancelled, in order. `jm list` prints it,
then the finished jobs of the last `keep` (7 days). There is **no header line**: every row is
`id <TAB> state <TAB> name <TAB> reason <TAB> progress`, ready for `cut` / `awk`.

## 2. start, resume, restart

A job has one command it must have, `start`, and an optional `resume`. Every run gets `JM_HOW`:

| Run | `JM_HOW` | Command used |
|---|---|---|
| first run, `jm requeue --fresh` | `start` | `start` |
| automatic retry after a failure (`retries`) | `resume` | `resume`, **or `start` when there is no `resume`** |
| a run cut off by a reboot / crash (`on_boot = resume`) | `resume` | `resume`, or `start` |
| `jm restart`, `jm requeue` (without `--fresh`) | `restart` / `resume` | `resume`, or `start` |

So a job without `resume` simply runs `start` again; when it needs to, it can still tell a fresh start from a
second attempt by `$JM_HOW` (and `$JM_RUN`, the run number). Jobs should be **safe to start again**: skip
finished steps, continue from a snapshot, and give a `done_check` so a run that finished but was not recorded counts
as done.

**`jm restart ID` is for a running job only.** It stops the job (the normal stop: `stop` command or TERM, then
KILL after `stop_grace`) and starts it again at the **top of the queue**, so it gets its resources back before
anything else starts. On a job that is not running (queued, waiting, held, stopping, stopped, finished, or a
group whose own command has ended) it fails with exit code 4 and changes nothing; use `jm requeue` for those.

## 3. Retries and timeouts

- `retries = N`: after a failure the job goes back to the queue (same place) up to N times, as a `resume` run.
- `timeout = 12h`: a run that takes longer is stopped and counts as a failure (retries apply).
- `jm hold` on a running job: the job is held instead of retried if it fails.

## 4. Groups and their children

- A **static group** is a spec with `children = FILE` lines and optionally `parallel = N`; a **dynamic** child is
  submitted by a running job: `$JM_JM submit --parent "$JM_ID" ...`.
- A child starts only while its parent is `running`. The parent ends when its own command (if any) and all its
  children have ended: **done** if every child is done, otherwise **failed**.

When a child fails for good (no retries left):

| Group setting | What jm does with the other children |
|---|---|
| default | cancels the ones still waiting ("sibling X failed"); the ones already running finish; the group fails |
| `continue_on_fail = yes` (or `jm submit --continue-on-fail`) | nothing: every other child runs as normal; the group still ends **failed** |

**With `continue_on_fail`, recovery is the group's own business.** jm only runs the children and reports which
failed (`jm status GROUP`, the group's reason). It cannot know whether the other children still make sense,
whether partial results must be cleaned up, or how to redo the failed part - only the job can. Typical ways to
handle it:
- give the failing child `retries` (and a `resume` that continues where it stopped);
- make each child independent of its siblings' results (then a failure only loses that one result);
- after the group, run a job with `after = GROUP` and `after_ok = no` that inspects what exists and repairs or
  resubmits (`jm requeue CHILD`, or a new `jm submit`);
- in a dynamic group, let the parent's own command check its children (`jm wait CHILD`) and decide.

jm never stops running siblings because one child failed: a running job is only stopped by hand
(`jm stop / cancel --children`) or by its own `timeout`.

## 5. Stopping a job

`jm stop ID` (and restart / requeue / cancel of a running job) asks the job's wrapper to stop it:
1. the spec's `stop` command runs, if there is one (it gets `JM_PID`, the job's main process); otherwise the
   job's process group gets **TERM** (Unix);
2. after `stop_grace` (default 60 s) whatever is left of the process group gets **KILL**.

**On Windows** there is no TERM for console programs. jm first asks `taskkill /T` (which closes GUI programs);
when Windows refuses that - as it does for console programs - jm kills the process tree at once
(`taskkill /T /F`), without waiting `stop_grace`. A job that must shut down cleanly on Windows (flush a
checkpoint, release a lock) needs a `stop` command, e.g. one that creates a file the job watches for, or calls
the program's own shutdown command. `jm pause` / `jm cont` do not exist on Windows.

## 6. Machine hooks: on_first_start and on_idle

For a machine that must prepare something before work and undo it after - e.g. a read-only root file system
whose data mount is made writable with `data-rw.sh` and read-only again with `data-ro.sh` - the config has two
hooks:

```
on_first_start = /usr/local/bin/data-rw.sh   # the machine goes from idle to busy
on_idle        = /usr/local/bin/data-ro.sh   # nothing has run for idle_after
idle_after     = 60s                         # default
hook_timeout   = 120s                        # default; a hook running longer is killed (exit 124)
```

How they run:
- **Before the first job starts** on an idle machine, the daemon runs `on_first_start` and waits for it; only
  then are jobs started. If it fails (non-zero exit or timeout), no job starts: the waiting jobs show
  "on_first_start failed (exit N)" and the hook is tried again every poll.
- When nothing has run for `idle_after`, the daemon runs `on_idle`. A job chain (`after`, retries, groups,
  requeues) that starts the next job within `idle_after` keeps the machine busy: no read-only / writable flip
  between the jobs.
- Hooks run **outside the state lock**: `jm list`, `jm submit`, etc. never wait for a hook. Only the daemon waits.
- The state is kept in `STATE/machine` (`busy <UTC>` or `idle <UTC>`, also shown by `jm status`), so it
  survives a daemon restart and other scripts can read it.
- Hooks get the config's `env` lines, `JM_HOME`, and `JM_HOOK` (`on_first_start` or `on_idle`); they do **not**
  get `JM_ID`.

**Jobs that call the hook scripts themselves.** jm cannot stop a job from running `data-ro.sh` in the middle of
other jobs' work. When adopting jm, make the scripts jm-aware instead, using the variables above:

```sh
# data-ro.sh
if [ -n "$JM_ID" ]; then                 # called from inside a jm job: the machine is not idle
    echo "data-ro.sh: skipped inside jm job $JM_ID (jm's on_idle hook does it)"; exit 0
fi
if [ -z "$JM_HOOK" ] && grep -q '^busy' "${JM_HOME:-$HOME/.jm}/machine" 2>/dev/null; then
    echo "data-ro.sh: jm is busy; not now"; exit 0   # called by hand / cron while jm jobs run
fi
# ... remount read-only ...
```

`data-rw.sh` called from a job is harmless (the mount is already writable), so it needs no change; removing such
calls from job scripts is still cleaner (see [PORTING.md](PORTING.md)).

## 7. Secrets

- A spec may not hold a secret: an `env` line whose name looks secret (`..._TOKEN`, `..._PASSWORD`,
  `..._API_KEY`, ...) or whose value looks like a token is refused, and so is a token in any command.
- Jobs get the daemon's environment **minus every secret-looking variable**. A job that needs one names it:
  `env_pass = NAME` (taken from the daemon's environment at each start) or `env_file = /path` (a `KEY=VALUE`
  file, mode 0600, read at each start, never copied).
- `jm log` and `jm status` mask token-looking strings; `jm log --raw` shows them, on a terminal only.
