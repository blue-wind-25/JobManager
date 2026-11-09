# RDD_022 - `jm list --order` prints parked jobs before the running ones

Date: 2026-11-10. Status: resolved (owner, 2026-11-10: "finished -> stopped -> held -> running -> waiting").

## Problem

RDD_017 ordered the queue by expected start, but ranked stopped and held jobs as if they would start. They sat
between the running job and the waiting ones, although neither state ever starts by itself (OPERATIONS sec 1).

## Decision

`--order` prints finished jobs, then stopped, then held (each in queue order), then running / stopping, then the
rest in RDD_017's start order. Parked jobs are not "placed" for the `after` check, so a job waiting on one still
goes last. Plain `jm list` is unchanged.

## Rationale

The list then reads past, parked, now, next. Stopped comes before held because held is meant to go on and is
closer to running again. Nothing acts on the order; it is display only.

## Applying it

Client side only: no state format change, the daemon does not read it. A worker gets it with its next jm upgrade.
