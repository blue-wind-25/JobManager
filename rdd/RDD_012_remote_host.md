# RDD_012 - `jm -H HOST`: the remote part of design sec 12.4 in v1

Date: 2026-10-22. Status: resolved (owner request, 2026-10-22: "are the commands the same over ssh? document it,
or add helper scripts that take the worker's address; keep the `ssh -t` command as short as possible").

Plain `ssh host jm ...` runs the same commands, but five things differ silently: `jm` is often not on the
non-interactive PATH (ssh commands skip most of `~/.bashrc`), the remote shell splits the words again (quoting),
a spec file named by its local path is not on the worker, a job without `cwd` runs in the remote login
directory, and `-t` is needed for `-f`, prompts, `$EDITOR` and `--raw` but breaks stdin and plain-text output.
Per-project helper scripts would each re-solve these.

## Decision

- The global option `-H HOST` / `--host HOST` (before the command) runs any jm command on HOST over ssh, with
  the same arguments. This is the `jm --host` of design sec 12.4, built now; `--host any`, `hosts_for` and
  `stage` stay v3. No separate helper script: `jm.py` itself is the client, so a workstation needs only the one
  file. The shortest form is a shell alias per worker (`alias jp='jm -H pi'`), documented in docs/REMOTE.md.
- HOST is a name from the hosts file - `$JM_HOME/hosts`, default `~/.jm/hosts`, lines
  `NAME SSH_TARGET [JM [JM_HOME]]` as in sec 12.4 (`-` keeps a column's default, `#` comments) - or else used as
  the ssh target itself (`user@ip`, an ip, an `~/.ssh/config` alias), so a worker's address works without setup.
  `JM` is the jm command on the worker (default `jm`); `JM_HOME` becomes `--home` unless the command line has one.
- Every argument is quoted for the worker's POSIX `sh` (a leading `~/` or `$HOME/` is left for it to expand).
  Windows workers are not supported by `-H` (cmd quoting); a Windows workstation can drive Unix workers.
- `submit FILE` with a local FILE sends the spec on stdin (`submit -`). A group spec (with `children`) is passed
  as a path on the worker, with a warning: its children files are read where the spec is.
- `-t` is added exactly when the local stdin and stdout are both terminals and no spec goes on stdin: a person
  then gets Ctrl-C, prompts, `$EDITOR` and `--raw` as locally; a script gets plain output (`\n`, stderr apart)
  and `BatchMode=yes` (never a password prompt). This replaces sec 12.4's "`-t` added for `log -f`".
- ssh's exit code is returned as is (the remote jm's code, 255 for an ssh failure); 255 and 127 (jm not found)
  get a hint on stderr. `JM_SSH` replaces the `ssh` command (e.g. `ssh -p 2222`).
- From a phone (owner, 2026-10-22) the ssh app logs into the worker, so short commands belong in the worker's
  shell, not in jm: `examples/jm-shortcuts.sh` (sourced from `~/.bashrc`) defines `jl js jf ja jt`, whose ID
  defaults to the first running top-level job (a group with its children). jm itself gets no "default job":
  a command acting on a guessed job is fine to look at, not to stop or cancel, and the shell functions keep
  that choice visible and editable. Documented in docs/REMOTE.md sec 4 (ConnectBot post-login `jl`, Termux).
- The job's cwd default stays "the directory of the submit": over ssh that is the login directory. Documented
  (docs/REMOTE.md sec 2) instead of guessed: specs for remote use should give `cwd`.
