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

There is no network port and no web UI: remote use is `ssh host jm ...`.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/blue-wind-25/JobManager/main/jm.py -o ~/bin/jm && chmod +x ~/bin/jm
jm daemon start                 # or: jm daemon install systemd | launchd | windows
```

(Use a release tag instead of `main` in the URL once one exists, so a machine runs a known version.)
The state dir is `~/.jm`; set `JM_HOME` or `--home DIR` for another one (e.g. a writable data mount).

## Quick start

```sh
jm config cpus=4 ram=4000 ram_reserve=500 tokens=gpu:1
jm submit --name sleepy --cpu 1 --ram 200 -- sleep 600        # inline
jm submit examples/train.jm                                    # a spec file
ssh host jm submit - < examples/train.jm                       # a spec on stdin
jm list                                                        # id, state, name, reason, progress (tab-separated)
jm status j0001                                                # spec summary, runs, live RSS/CPU, log tail
jm log -f j0001
jm wait j0001; echo $?                                         # exit code = the job's
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
cpu         = 4
ram         = 2500
tokens      = gpu
retries     = 1
timeout     = 12h
```

Every key is described in [design/JM_DESIGN.md](design/JM_DESIGN.md) sec 5 (spec) and sec 6 (config).

## Commands

| Command | What it does |
|---|---|
| `jm daemon start \| stop \| restart \| status \| run` | background / foreground daemon (stop leaves jobs running) |
| `jm daemon install systemd \| launchd \| windows` | write a user service file and print how to enable it |
| `jm daemon bashrc` | a `~/.bashrc` line that starts the daemon if it is not running |
| `jm submit FILE \| - \| [opts] -- CMD...` | `--name --cpu --ram --disk --tokens --env --dep --set k=v --parent ID --top --after ID --end --hold` |
| `jm list [--all] [--tree] [--tag T]` | queue order, then recently finished jobs |
| `jm status [ID]` / `jm free` | one job / the daemon and free capacity |
| `jm log ID [-f] [-n N] [--raw]` | the log, secrets masked (`--raw` needs a terminal) |
| `jm wait ID [--timeout T]` | block until it ends |
| `jm move ID --top\|--bottom\|--up N\|--down N\|--before ID2\|--after ID2`, `jm move --by-priority`, `jm swap A B` | reorder |
| `jm hold ID` / `jm release ID` | park / unpark |
| `jm stop ID` / `jm restart ID` / `jm requeue ID [--top\|--end] [--fresh] [--spec-edit k=v]` / `jm cancel ID` | life cycle (`--children` for groups) |
| `jm edit ID` / `jm set ID k=v k+=v k=` | change a spec that is not running |
| `jm pause ID` / `jm cont ID` | SIGSTOP / SIGCONT (Unix) |
| `jm config [k=v ...]` / `jm clean [--older 7d]` / `jm artifacts ID` | machine config, drop finished jobs, list outputs |

Exit codes: 0 ok, 1 error, 2 usage, 3 no such job, 4 not allowed in that state.

Variables every job command sees: `JM_ID`, `JM_NAME`, `JM_HOME`, `JM_RUN`, `JM_HOW` (start / resume / restart),
`JM_PARENT`, `JM_JM` (the command that runs jm, for `$JM_JM submit --parent "$JM_ID" ...`), plus `JM_EXIT` in
`post` / `notify` and `JM_STATE` in `notify`.

## Secrets

jm never stores a secret: a spec whose `env` names a secret (`..._TOKEN`, `..._PASSWORD`, ...) or holds a
token-like value is refused. Pass secrets by name instead - `env_pass = NAME` (taken from the daemon's
environment at each start) or `env_file = /path/0600-file` - and they are kept out of every job that does not ask
for them. See design/JM_DESIGN.md sec 16.

## Repository layout

| Path | Contents |
|---|---|
| `jm.py` | the whole tool |
| `tests/` | `python3 -m unittest discover tests` (unit tests and daemon tests with `jm _sim` fake jobs) |
| `examples/` | sample specs |
| `guards/` | claude-guards: Claude Code safety hooks (secret echo, broad search, protected files), installed into `.claude/` |
| `design/` | design notes: `JM_DESIGN.md`, `CLAUDE_GUARDS.md` |
| `rdd/` | resolved design decisions taken while building |
| `state/` | implementation state: what is built, tested, next |
| `.github/workflows/ci.yml` | CI: Linux x86_64 / aarch64, macOS, Windows, Python 3.6, Alpine, Fedora, guards on Python 2.7 / 3 |

## Licence

Apache License 2.0 - see [LICENSE](LICENSE).
