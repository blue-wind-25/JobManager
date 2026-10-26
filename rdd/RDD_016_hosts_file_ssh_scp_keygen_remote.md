# RDD_016 - Hosts file `~/.jmhosts`; `jm ssh` / `scp` / `keygen` / `remote`

Date: 2026-10-27. Status: resolved (owner, 2026-10-27: replace the long ssh / scp option strings with `jm ssx` and
`jm scp` reading `~/.jmhosts`, first host by default, `--host NAME` otherwise; later `jm remote [--host NAME] ...`
for any jm command; a key generator; backward compatible; Windows and macOS tested in CI).

## Problem

Every project script that drives a worker repeats
`ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i ~/KEY user@host` (and the `scp -r` twin).
RDD_012 kept remote use as plain `ssh -t WORKER jm ...` for v1; design sec 12.4 planned `jm --host` for v3.

## Decision

- **Hosts file** `~/.jmhosts` (or `$JM_HOSTS`), INI sections in the owner's format:

  ```
  [cm5]
  User                  = pi
  Host                  = raspberrypi.local
  StrictHostKeyChecking = no
  UserKnownHostsFile    = /dev/null
  LogLevel              = ERROR
  SignInKey             = ~/ssh-pi@raspberrypi-key
  ```

  jm's own keys: `Host` (required), `User`, `Port`, `SignInKey` (`-i`, `~` expanded), `Jm` (jm's path on the host,
  default `jm`) and `JmHome` (`--home` on the host). **Every other key is passed through as `-o Key=Value`**, so
  jm needs no code per ssh option and ssh itself rejects a misspelt one; `/dev/null` becomes `NUL` on Windows. Key
  case is kept. The first section is the default host. A file writable by group or others is refused (it names a
  private key and decides where commands run). This replaces sec 12.4's `~/.jm/hosts` line format.
- **`jm ssh [--host NAME] [-t] [--] [COMMAND...]`** (alias `jm ssx`, the owner's first name for it) and
  **`jm scp [--host NAME] SRC... DST`**, where `:path` is on the host (a Windows drive path `C:\x` stays local) and
  `-r` is always passed. Exit code = the client's.
- **`jm remote [--host NAME] COMMAND...`**: `ssh HOST 'jm [--home JmHome] COMMAND...'`, the words quoted once for the
  host's POSIX shell; `-t` only when stdin and stdout are a terminal; `remote submit LOCAL_FILE` sends the spec on
  stdin as `submit -` (RDD_012's rule, done for the user). Group specs with children files still need the files on
  the host.
- **`jm keygen [--host NAME] [--file F] [--comment C] [--no-passphrase] [--install]`**: `ssh-keygen -t ed25519` into
  the host's `SignInKey`, else `~/.ssh/jm_<host>`; an existing file is never overwritten. jm does not edit the hosts
  file; it prints the `SignInKey = ...` line to add. `--install` appends the public key to the host's
  `~/.ssh/authorized_keys` once (POSIX hosts), signing in with the host's other `SignInKey` if there is one, else ssh
  asks for the host's **password on the user's terminal** - jm never sees or stores it. Without a terminal (Claude
  Code, CI) `--install` cannot answer a password prompt: run it in a console, or append the `.pub` by hand.
- **The system OpenSSH client**, not a Python library: the standard library has no SSH, and `ssh`, `scp` and
  `ssh-keygen` ship with Linux, macOS and Windows 10 1809+ / 11. These commands do not open the state dir.
- **Backward compatible**: new subcommands only; nothing changes without a hosts file; plain `ssh -t WORKER jm ...`
  keeps working (RDD_012 stays for the phone shortcuts). `jm --host` before every command (sec 12.4, with
  `--host any` and `stage`) stays v3.

## Tests

`tests/test_remote.py`: command lines and the hosts file on every CI platform, `keygen` where `ssh-keygen` exists
(Linux, macOS, Windows runners), and a round trip (ssh, scp both ways, `remote --version`, `remote submit` of a local
spec, list, cancel) against localhost sshd in the CI job `jm-ssh`, only with a throwaway `JmHome`. Checked by hand
against the CM5 on 2026-10-27 (ssh, scp both ways, `remote list`, exit codes).
