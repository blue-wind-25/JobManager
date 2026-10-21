# RDD_006 - Sub-job rules left open by the design

Date: 2026-10-21. Status: resolved (implementation; design sec 8).

## Decision

- A child is eligible only while its parent is `running` (a static group's parent with no `start` becomes running,
  holding no resources, as soon as its own `after` dependencies allow). Children of a held / stopped / queued
  parent wait.
- Without `continue_on_fail`, the first child that fails for good fails the group: its queued / held siblings are
  cancelled ("sibling X failed"); siblings already running finish; the parent becomes failed once its own command
  and the running children have ended. With `continue_on_fail = yes`, the parent ends when every child has ended,
  failed if any failed. A cancelled child does not fail its parent.
- When a parent fails or is cancelled, its unfinished descendants that are not running are cancelled.
- Children inherit the parent's `cwd` when they give none. Static children get auto ids unless their spec has `id`.
- `jm stop / cancel` of a parent with unfinished children asks on a terminal and refuses otherwise (exit 4) unless
  `--children` is given. `jm requeue --children` requeues the unfinished children too.
