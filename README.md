# JobManager (`jm`)

A small job queue, scheduler and monitor for long-running jobs - training runs, renders, benchmarks, downloads -
on **one machine**: a Raspberry Pi CM5, a Jetson, a Linux box, macOS, Windows.

- **One file**, `jm.py`, Python 3.6+ standard library only. No install step: copy it onto PATH.
- **Plain text everywhere**: specs, config and state are `key = value` / one-line files you can `cat`, `grep` and
  hand-edit.
- **Survives reboots**: jobs run under a detached wrapper, so they keep running when the daemon stops or crashes;
  on start the daemon adopts live jobs and resumes cut-off ones (`on_boot = resume | hold | fail`).
- **Fits jobs side by side** by declared `cpu`, `ram`, named `tokens` (e.g. `gpu:1`) and free `disk`, with
  backfill and a drain guard against starvation.
- **Change the queue at any time**: move, swap, hold, release, stop, restart, requeue, cancel, edit.
- **Sub-jobs**: static groups (`children = FILE`, `parallel = N`) and children a running job submits itself.
- **Secret-safe**: refuses secrets in specs, passes them by name (`env_pass`, `env_file`), masks tokens in logs.

There is no network port and no web UI: remote use is `ssh -t host jm ...`, with the same commands, or `jm remote ...`
/ `jm ssh` / `jm scp` with the hosts named once in `~/.jmhosts` ([docs/REMOTE.md](docs/REMOTE.md) sec 4).

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/blue-wind-25/JobManager/v1.6.1/jm.py -o ~/bin/jm && chmod +x ~/bin/jm
jm daemon start                 # or: jm daemon install systemd | launchd | windows
```

(A release tag in the URL, not `main`, so every machine runs a known version; `jm --version` shows it.)
**Upgrade:** the same line with the new tag, then `jm daemon restart` (or restart its service) where a daemon runs;
running jobs keep running (their wrappers do not depend on the daemon).
The state dir is `~/.jm`; set `JM_HOME` or `--home DIR` for another one (e.g. a writable data mount).

## Quick start

```sh
jm config cpus=4 ram=4000 ram_reserve=500 tokens=gpu:1
jm submit --name sleepy --cpu 1 --ram 200 -- sleep 600        # inline
jm submit examples/train.jm                                    # a spec file
ssh host jm submit - < examples/train.jm                       # a spec on stdin
jm list                                                        # id, state, name, reason, progress (aligned on a terminal,
                                                               #   tab-separated when piped)
jm list --order                                                # the queue in the order it will run
jm status j0001                                                # spec summary, runs, live RSS/CPU, log tail
jm log -f j0001
jm wait j0001; echo $?                                         # exit code = the job's
ssh -t pi jm log -f j0001                                      # any command, on worker `pi` (docs/REMOTE.md)
```

A spec (`examples/train.jm`):

```
name        = extractor training
cwd         = /mnt/data/project
env         = SET=gso_b
start       = sh tools/train.sh foreground
resume      = sh tools/train.sh foreground --resume
done_check  = test -s out/extractor.bin       # exit 0 = already done: skip, mark done
progress    = tail -n 1 out/train.log
applog      = tail -n ${JM_LINES:-200} ${JM_FOLLOW:+-f} out/train.log   # `jm applog ID`
cpu         = 4
ram         = 2500
tokens      = gpu
retries     = 1
timeout     = 12h
```

Every key is described in [design/JM_DESIGN.md](design/JM_DESIGN.md) sec 5 (spec) and sec 6 (config).

## Documentation

- [docs/GUIDE.md](docs/GUIDE.md): how jm behaves - job states, `start` / `resume` / `restart`, retries, groups
  and what happens when a child fails, stopping (and Windows), the `on_first_start` / `on_idle` machine hooks
  (read-only data mounts), secrets, logs (`jm log` / `jm applog`), starting the daemon at boot.
- [docs/OPERATIONS.md](docs/OPERATIONS.md): the operator's reference for changing a live queue - every command in
  every state, admission, what `after` does (and does not) propagate, one run step by step, recipes.
- [docs/REMOTE.md](docs/REMOTE.md): driving workers over ssh from a workstation (`ssh -t pi jm ...`, what differs)
  or a phone (two-letter shortcuts, [examples/jm-shortcuts.sh](examples/jm-shortcuts.sh)).
- [docs/PORTING.md](docs/PORTING.md): moving a project's own queue / resume / waiter scripts to jm.

## Commands

| Command | What it does |
|---|---|
| `jm daemon start \| stop \| restart \| status \| run` | background / foreground daemon (stop leaves jobs running) |
| `jm daemon install systemd \| launchd \| windows` | write a user service file and print how to enable it |
| `jm daemon bashrc` | a `~/.bashrc` line that starts the daemon if it is not running |
| `jm submit FILE \| - \| [opts] -- CMD...` | `--name --cpu --ram --disk --tokens --env --dep --set k=v --parent ID --top --after ID --end --hold --continue-on-fail` |
| `jm list [--all] [--tree] [--tag T] [--order] [--fields F,F] [--eta] [--align \| --tabs]` | queue order, then recently finished jobs; `--order`: finished jobs, then the expected run order (`after` chains followed); `--eta`: each job's expected end (from `estimate`, the `class` history, the progress rate); columns aligned on a terminal, tabs when piped |
| `jm status [ID]` / `jm free` | one job / the daemon and free capacity |
| `jm log ID [-f] [-n N] [--children] [--stdout\|--stderr] [--raw]` | captured stdout + stderr, secrets masked (`--raw` needs a terminal); `--children` adds the children's, one section each |
| `jm applog ID [-f] [-n N] [--children] [--raw]` | runs the spec's `applog` command: the log the job keeps itself |
| `jm report [ID] [-n N] [--raw]` | runs the spec's `report` command: the job's own status summary (no ID: every queued job's, one section each; `-n` = `JM_LINES`) |
| `jm wait ID [--timeout T]` | block until it ends |
| `jm move ID --top\|--bottom\|--up N\|--down N\|--before ID2\|--after ID2`, `jm move --by-priority`, `jm swap A B` | reorder |
| `jm hold ID` / `jm release ID` `[--chain]` | park / unpark (also a stopped job); `--chain`: also every job waiting for it through `after` |
| `jm skip ID` / `jm deps ID` | mark done without running (not a running job) / the jobs waiting for ID, as a tree |
| `jm stop ID` / `jm restart ID` / `jm requeue ID [--top\|--end] [--fresh] [--spec-edit k=v]` / `jm cancel ID` | life cycle (`--children` for groups) |
| `jm edit ID` / `jm set ID k=v k+=v k=` | change a spec that is not running |
| `jm pause ID` / `jm cont ID` | SIGSTOP / SIGCONT (Unix) |
| `jm config [k=v ...]` / `jm clean [--older 7d]` / `jm artifacts ID` | machine config, drop finished jobs, list outputs |
| `jm ssh [--host NAME] [-t] [--] [CMD...]` (alias `ssx`) | ssh to a host named in `~/.jmhosts` (first host by default) |
| `jm scp [--host NAME] SRC... DST` | copy to / from it; `:path` is on the host, `-r` always |
| `jm remote [--host NAME] COMMAND...` | any jm command on the host; `remote submit LOCAL_FILE` sends the spec on stdin |
| `jm keygen [--host NAME] [--file F] [--no-passphrase] [--install]` | a new ed25519 sign-in key (ssh-keygen); `--install` puts it on the host (ssh asks the password on your terminal) |

Exit codes: 0 ok, 1 error, 2 usage, 3 no such job, 4 not allowed in that state.

Variables every job command sees: `JM_ID`, `JM_NAME`, `JM_HOME`, `JM_RUN`, `JM_HOW` (start / resume / restart),
`JM_PARENT`, `JM_JM` (the command that runs jm, for `$JM_JM submit --parent "$JM_ID" ...`), plus `JM_EXIT` in
`post` / `notify`, `JM_STATE` in `notify` / `applog`, and `JM_LINES` / `JM_FOLLOW` in `applog`.

## Secrets

jm never stores a secret: a spec whose `env` names a secret (`..._TOKEN`, `..._PASSWORD`, ...) or holds a
token-like value is refused. Pass secrets by name instead - `env_pass = NAME` (taken from the daemon's
environment at each start) or `env_file = /path/0600-file` - and they are kept out of every job that does not ask
for them. `jm log`, `jm applog` and `jm status` mask token-like strings, and `applog` commands get no
secrets at all. See design/JM_DESIGN.md sec 16.

## Repository layout

| Path | Contents |
|---|---|
| `jm.py` | the whole tool |
| `tests/` | `python3 -m unittest discover tests` (unit tests and daemon tests with `jm _sim` fake jobs) |
| `examples/` | sample specs; `jm-shortcuts.sh` (short commands on a worker, e.g. from a phone); `jm-data-rw.sh` / `jm-data-ro.sh` (machine hooks for a read-only data mount) |
| `guards/` | claude-guards: Claude Code safety hooks (secret echo, broad search, protected files), installed into `.claude/` |
| `docs/` | user documentation: `GUIDE.md`, `OPERATIONS.md`, `REMOTE.md`, `PORTING.md` |
| `design/` | design notes: `JM_DESIGN.md`, `CLAUDE_GUARDS.md` |
| `rdd/` | resolved design decisions taken while building |
| `state/` | implementation state: what is built, tested, next |
| `.github/workflows/ci.yml` | CI (manual trigger for now): Linux x86_64 / aarch64, macOS, Windows, Python 3.6, Alpine, Fedora, guards on Python 2.7 / 3 |

## Licence

Apache License 2.0 - see [LICENSE](LICENSE).
