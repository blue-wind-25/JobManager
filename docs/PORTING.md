# Porting a project's own job scripts to jm

Many projects grow their own job management: a `queue.sh` that waits until the machine is free, `nohup` and pid
files, a "resume everything after reboot" script, `STOPPED` marker files, waiter loops that poll for another job,
settle sleeps. jm replaces all of that machinery; what stays in the project is **the work itself**, as commands jm
can start, stop and start again. This guide is the checklist (the design's version is
[design/JM_DESIGN.md](../design/JM_DESIGN.md) sec 15).

## 1. What moves where

| In the project's scripts today | With jm |
|---|---|
| `nohup ... &`, `setsid`, `disown`, pid files, `kill $(cat run.pid)` | nothing: jm's wrapper detaches the job; `jm stop` / `jm status` |
| `busy()` / "wait until the trainer is done" loops, `sleep 180` settle times | `cpu`, `ram`, `tokens` (e.g. `tokens = trainer`), `disk` in the spec; one queue, one lock |
| a waiter that polls for job A before starting job B | `after = A` in B's spec (`after_ok = no` to run B even if A fails) |
| `resume_all.sh` after a reboot | the daemon adopts running jobs and resumes cut-off ones (`on_boot = resume`, default) |
| `STOPPED` marker files so the resume script leaves a job alone | `jm stop ID` (state `stopped`, never resumed automatically) |
| "retry with other settings" scripts | `jm requeue ID --spec-edit key=value`, or `jm set` then `jm requeue` |
| a per-script queue file / ordering logic | `jm submit --top / --after ID`, `jm move`, `jm swap`, `jm hold` |
| polling for completion, `tail -f` on a log | `jm wait ID` (exit code = the job's), `jm log -f ID` |
| a chain script: step 1, then step 2, then step 3 | three specs with `after`, or a group with `children` and `parallel = 1` |
| "fan out one run per language / dataset" | a static group (`children = ...`, `parallel = N`) or a parent that submits children |
| remounting a read-only data disk around runs (scripts of any name) | `on_first_start` / `on_idle` in jm's config, set to your scripts ([GUIDE.md](GUIDE.md) sec 6) |
| notification `curl` at the end of a script | `notify = ...` in the spec (runs on every state change, with `JM_STATE`) |
| tokens exported in the script or in `~/.bashrc` for the job | `env_pass = NAME` / `env_file = /path` (mode 0600) |

## 2. Turn each script into a foreground, restartable command

A jm job's `start` command must:
1. **Run in the foreground** and exit with the work's status (0 = success). Remove `nohup`, `&`, `disown`,
   pid-file writing, and "already running?" checks: jm does those. A script with a background mode can keep it;
   give jm the foreground entry point (`sh train.sh foreground`).
2. **Be safe to start again.** Retries, reboots and `jm restart` run the command again. Skip steps that are
   already done, continue from the latest snapshot or checkpoint, write outputs to a temporary name and rename them
   when complete.
3. **Say how to tell it is already done**, with `done_check` (exit 0 = done, the run is skipped): e.g.
   `done_check = test -s out/model.bin`. This also covers a run that finished just before a crash.
4. **Optionally continue differently**: a `resume` command, or one command that looks at `$JM_HOW`
   (`start` / `resume` / `restart`) and `$JM_RUN`. Without `resume`, jm runs `start` again.
5. **Stop cleanly on TERM** (Unix), or provide a `stop` command (always needed for a clean stop on Windows):
   save a checkpoint and exit. After `stop_grace` (default 60 s) jm kills what is left.
6. **Not manage the machine**: no remounting, no killing other jobs, no waiting for other jobs, no calls to the
   machine's prepare / undo scripts (`data-rw.sh` / `data-ro.sh` or whatever they are called there) - jm's hooks
   run them; see [GUIDE.md](GUIDE.md) sec 6 for making such scripts jm-aware when the calls cannot be removed at
   once.

## 3. Write the specs

Keep them in the project, e.g. `jobs/<name>.jm`, committed:

```
name        = train extractor (gso_b)
cwd         = /mnt/data/project
env         = SET=gso_b
start       = sh tools/train.sh foreground
done_check  = test -s out/gso_b/extractor.bin
progress    = tail -n 1 out/gso_b/train.log
cpu         = 4
ram         = 2500
tokens      = trainer
retries     = 1
timeout     = 12h
```

Machine-specific values (data paths) go into the machine's jm config as `env = DATA=/mnt/data` lines, which every
job receives; the spec then uses `$DATA` (`cwd` and `env` values expand `$NAME`).

## 4. Configure the machine once

```sh
jm config cpus=4 ram=4000 ram_reserve=500 tokens=trainer:1 tokens=gpu:1
jm config on_first_start=/usr/local/bin/data-rw.sh on_idle=/usr/local/bin/data-ro.sh   # if needed; your own names
jm daemon install systemd        # or launchd / windows; or `jm daemon bashrc` for a ~/.bashrc line
```

On a read-only root, put the state dir on the writable mount: `JM_HOME=/mnt/data/jm` (or `--home`).

## 5. A thin helper to submit from a workstation

One small script per project, e.g. `tools/jm_submit.sh HOST SPEC [key=value ...]`:

```sh
#!/bin/sh
# Submit a committed spec on HOST after pulling the project there.
set -eu
host=$1; spec=$2; shift 2
sets=""; for kv in "$@"; do sets="$sets --set $kv"; done
ssh "$host" "cd /mnt/data/project && git pull -q && jm submit $spec $sets"
```

Status for the owner becomes `ssh host jm list`; logs `ssh -t host jm log -f ID`.

## 6. Switch over safely

1. Keep the old scripts until **one full cycle** has run under jm: submit, run, a reboot in the middle, the resume,
   a chained job, the results fetched.
2. Then delete the old queue / resume / waiter scripts and every `busy()`, `STOPPED` and settle sleep in one
   commit, and record the switch in the project's decision log.
3. Jobs that were started by the old scripts are not known to jm: let them finish (or stop them) before the
   first `jm submit` of the same work.
