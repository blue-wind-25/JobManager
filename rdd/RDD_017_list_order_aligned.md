# RDD_017 - `jm list --order`, `--fields` and aligned columns

Date: 2026-10-28. Status: resolved (owner, 2026-10-28: "make jl list the jobs in run order", "the columns are not
aligned, fix it and apply it to the running jm").

## Problem

`jm list` prints the queue in queue order. A chain submitted in pieces (`after` added later with `jm set`, or a
job submitted after jobs that will wait for it) runs in an order that queue order does not show: on the CM5 the
owner saw arm C listed above the scaling jobs it waits for. And `jl` (`jm list --tree | cut -f1,2,5`) leaves the
columns where a terminal's tab stops put them, ragged for ids longer or shorter than a tab.

## Decision

- **`--order`**: finished jobs (oldest first, as now), then the queued ones in the order they are expected to
  start: running / stopping first, then repeatedly the first job in queue order whose `after` jobs are all done or
  already placed. A job whose `after` can never be met (a dependency gone, or failed / cancelled under
  `after_ok = yes`) is placed last. Resources, tokens and estimates are not modelled: on one machine the `after`
  chains decide the order that matters, and a simulation of the scheduler would be a v2 (sec 12.1) feature.
  `--tree` still nests children under their parent, both in this order.
- **`--fields`**: any of `id,state,name,reason,progress`, in the order given; an unknown name is a usage error.
- **Alignment**: on a terminal (stdout is a tty) the columns are padded with two spaces, each column but the last
  to its widest cell, capped at 40 so a long name or reason cannot push the rest off a phone (a longer cell is
  printed whole). Piped output stays tab-separated, so every existing `awk -F'\t'` / `cut` user is unchanged.
  `--align` / `--tabs` force either. Still no header (RDD_007).
- **`jl`** becomes `jm list --tree --order --fields id,state,progress "$@"`.

## Rationale

Tty detection keeps the machine format exactly as it was for scripts (HowToTrainYourPetAI's `jm_submit.sh list`,
`cm5_jm_update.sh status`, `_jm_id`), while a person at a terminal or on a phone gets aligned output without a
flag. The order is a prediction from data jm already has; a wrong guess costs nothing because nothing acts on it.

## Applying it

Client side only: no state format change, the daemon does not read it. A worker gets it with its next jm upgrade
(HowToTrainYourPetAI `tools/cm5/cm5_jm_update.sh apply v1.4.0`, which also installs the new `jm-shortcuts.sh`).
