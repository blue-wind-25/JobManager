# RDD_011 - Logs: `jm log` for groups and separate streams, `jm applog` for a job's own log

Date: 2026-10-22. Status: resolved (owner request, 2026-10-22: "a custom command to retrieve a job's or group's
log, optionally with its children; and the stdout / stderr of a job or group, optionally with its children").
Extends design sec 4, 5 and 9.

Two different things are called "the log" of a job:
- what jm captured: the stdout and stderr of the job's commands, written by the wrapper into `jobs/<id>/log`
  (with jm's run banners). This is the only place their output goes when a job does not redirect it - and also
  where tracebacks and warnings end up when a job writes its real log to a file of its own (`--log-file x`)
  instead of redirecting stdout;
- the log the job keeps itself: a file under its run directory, several files, a journal, a remote service.
  Only the job knows where that is, so jm asks the spec for a command that prints it.

## Decision

**Captured output (`jm log`)** - extended, not a new command, since `jm log` already is the captured output:
- `--children` shows the job and then its descendants (depth first, the `jm list --tree` order), each job's output
  under a `==> ID <==` line, as `tail` does for several files. The line is written whenever the output switches
  to another job, so with `-f` the sections interleave as the jobs write. Jobs with no output so far show no
  section. With `--children` the header is always written (even for one job), so scripts see one format.
- `-f --children` picks up children submitted while it follows (dynamic groups) and ends when every job shown has
  ended (done / failed / cancelled / stopped) and the files are quiet, as `-f` on one job did (RDD_007).
- New spec key `split_stderr = yes` (default `no`): the wrapper writes the stderr of every command it runs for
  the job (`done_check`, `pre`, `start` / `resume`, `post`, `stop`) into `jobs/<id>/stderr` instead of `log`;
  the run banners go into both files, so runs can be told apart in either. `jm log --stderr` shows that file,
  `jm log --stdout` the `log`. Default stays merged: one file in time order is what is wanted most of the time,
  it keeps old state dirs and `cat jobs/<id>/log` unchanged, and two separate files lose the order between
  the streams. jm does not tee the streams through pipes (it would get both files plus a merged one) because
  the job then dies of SIGPIPE when its wrapper is killed - jobs write to their files directly so they survive
  the wrapper (design sec 3).
- On a job without `split_stderr`, `--stdout` warns that the log holds both streams; `--stderr` is an error
  (exit 1) for one job, and skips such jobs with a warning under `--children`. `notify` output (run by the
  daemon) stays in `log`. `log_max` rotates `stderr` like `log`.
- `jm status ID` shows the last lines of `stderr` too when the job splits.
- `jm log -f` now counts file positions in bytes (it compared a text-mode length, which is wrong for `\r\n` on
  Windows) and starts a file again from the top after `log_max` rotated it.

**The job's own log (`jm applog`)**:
- New spec key `applog` (a command, like `progress`): prints the job's own log to stdout, e.g.
  `applog = tail -n 200 out/train.log`. A group may have one too (e.g. a summary over its run directory).
  Children do not inherit it (each child's own `applog` shows its part; inheriting would print the same thing
  once per child).
- `jm applog ID [-n N] [-f] [--children] [--raw]` runs it - in the CLI, on demand, never by the daemon - in the
  job's `cwd`, in any job state, and prints its stdout + stderr, masked like `jm log` (`--raw` on a terminal
  only). With `--children` it runs the applog of the job and of each descendant that has one, with the same
  `==> ID <==` sections; jobs without one are skipped; none at all is an error (exit 1).
- The command gets `JM_LINES` (the `-n` value, or empty) and `JM_FOLLOW` (`1` with `-f`, else empty), so it can
  honour them itself: `applog = tail -n ${JM_LINES:-200} ${JM_FOLLOW:+-f} out/train.log`. Without `-f`, jm also
  keeps only the last N lines of what it printed, so `-n` works with a command that ignores `JM_LINES`.
- With `-f`, all the commands run at once and their lines are written as they arrive. Each command is ended
  (its whole process group) once its job has ended and it printed nothing for 1 s, so a `tail -f` applog
  stops by itself like `jm log -f`; jm also ends them on Ctrl-C or a closed pipe. Without `-f` they run one after
  the other.
- Its environment: the config's and the spec's `env` lines and `JM_ID`, `JM_NAME`, `JM_HOME`, `JM_RUN`,
  `JM_HOW`, `JM_PARENT`, `JM_STATE`, `JM_JM`, `JM_LINES`, `JM_FOLLOW` - **without** `env_pass` / `env_file`:
  its output is printed, often into a captured session, so it gets no secret it could print (design sec 16).
  An applog that needs a credential reads it itself.
- Exit code: 0; 1 when an applog could not start (missing `cwd`) or exited non-zero (named on stderr), or when
  no job has an applog; 3 no such job; 4 `--raw` without a terminal.
