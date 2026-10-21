# RDD_002 - The queue file holds every unfinished job

Date: 2026-10-21. Status: resolved (implementation).

Design sec 4 says `queue` is "the order"; sec 7 says a failed job with retries left is "requeued at its old
position", and sec 6 skips held / stopped jobs while walking the queue.

## Decision

`queue` lists every job that is not in a terminal state (done, failed, cancelled): queued, waiting, held, stopped,
running and stopping jobs keep their line. A job leaves the queue when it becomes terminal and comes back (at the
end, or `--top`) when it is requeued. `jm list` prints the queue in order, then the finished jobs younger than
`keep`. Admission walks the queue and only considers queued / waiting jobs. A daemon pass repairs the file after
hand edits (unknown ids dropped, unfinished jobs missing from it appended).
