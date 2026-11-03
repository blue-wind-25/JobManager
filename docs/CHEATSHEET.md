# jm cheat sheet

The shortcuts are shell functions in [examples/jm-shortcuts.sh](../examples/jm-shortcuts.sh). Each one only runs a plain
`jm` command, so it cannot do anything `jm` cannot. Setup: [REMOTE.md](REMOTE.md) sec 3.

## The shortcuts

| Type | Really runs | What you see | Stops by itself? |
|---|---|---|---|
| `jl` | `jm list --tree --order --fields id,state,eta,progress` | The queue in run order, with expected end and progress | Yes |
| `je` | `jm list --tree --order --fields id,state,eta` | The same without progress; narrowest, for a phone | Yes |
| `js [ID]` | `jm status ID` | One job: spec, runs, live RAM and CPU, last 10 log lines | Yes |
| `jf [ID]` | `jm log -f --children ID` | Follows what the job prints; children too | When the job ends, or Ctrl-C |
| `ja [ID]` | `jm applog -f --children ID` | Follows the log the job keeps itself; children too | Ctrl-C, or the spec's `applog` ending |
| `jt [ID] [N]` | `jm log -n N --children ID` | Last N lines (30) of what the job printed | Yes |
| `jr` | `jm report` | The `report` of every queued job | Yes |
| `jr ID [N]` | `jm report ID [-n N]` | The `report` of one job | Yes |
| `jr N` | `jm report -n N` | Every queued job's `report`, with N lines | Yes |

None of them changes anything. All are read-only.

**No ID:** `js`, `jf`, `ja` and `jt` use the first running top-level job. A group means the whole group. With nothing
running they print `no running job; give an ID` and fail.

**Extra words:** `jl` and `je` pass them on to `jm list`, e.g. `jl --all`. The others ignore them.

**`jr N` vs `jr ID`:** a word of digits only counts as N. A job whose id is all digits needs `jm report ID`.

## Three logs, which one?

| Want | Use | Source |
|---|---|---|
| What the job printed, with jm's start and end banners | `jt`, `jf`, `jm log` | jm captured it |
| The log the job writes itself | `ja`, `jm applog` | The spec's `applog` command |
| A short "how is it doing" summary | `jr`, `jm report` | The spec's `report` command |

`applog` and `report` need that key in the spec. Without it there is nothing to show.

## Common tasks

| Task | Command |
|---|---|
| Queue a spec | `jm submit FILE` |
| Queue one command | `jm submit --name x --cpu 1 --ram 200 -- sleep 600` |
| Who waits for this job? | `jm deps ID` |
| Do not start this job yet | `jm hold ID` (`--chain`: its dependents too) |
| Let it start again | `jm release ID` (`--chain`: its dependents too) |
| Stop it now, keep it listed | `jm stop ID` |
| Run a stopped job again | `jm requeue ID` (`--fresh`: `start`, not `resume`) |
| Remove it | `jm cancel ID` |
| Treat it as done, without running it | `jm skip ID` |
| Change a waiting job | `jm set ID key=value` |
| Run it next | `jm move ID --top` |
| Wait for it in a script | `jm wait ID` (exit code = the job's) |
| Free cores, RAM and tokens | `jm free` |
| The machine's limits | `jm config` |
| A job on another machine | `jm remote list`, `jm remote log -f ID` |

## Traps

- `done_check` runs only before a start. A start that exits 0 counts as done, even if the work is incomplete.
- `jm stop` never restarts by itself, not even after a reboot. `jm requeue` it.
- Nothing passes along `after`. Dependents of a stopped, held or cancelled job just wait. A cancelled one means forever.
- `jm set` and `jm edit` refuse a running job.
- A clean poweroff counts as a reboot: running jobs resume at boot, at the top of the queue (`on_boot = resume`).

Every command in every state: [OPERATIONS.md](OPERATIONS.md). Every spec and config key: [GUIDE.md](GUIDE.md) sec 11.
