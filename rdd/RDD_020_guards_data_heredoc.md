# RDD_020 - secret-echo: a quoted heredoc that only feeds a file writer is data; `environ` matched as a word

Date: 2026-11-01. Status: resolved (owner, 2026-11-01: make the guards global and take over the best of the
repository hooks; port of HowToTrainYourPetAI RDD_672, 2026-10-24).

## Problem

claude-guards copied HowToTrainYourPetAI's secret checker on 2026-10-21 (design/CLAUDE_GUARDS.md appendix A).
Three days later that checker stopped refusing prose (RDD_672 there); claude-guards still refused it. Installed
at user level on the owner's PC, the guard refused ordinary commands: a commit message or a note written through
a quoted heredoc that contains the words "env" or "set", and any command whose text contains the English word
"environment" (rule 3 matched `environ` as a substring).

## Decision

- Rule 2 (no whole-environment dump) skips the body of a heredoc that is pure data: its delimiter is quoted
  (`<<'EOF'` / `<<"EOF"`, so the shell expands nothing in it) and it feeds only a file writer - `cat > F`,
  `cat >> F`, `tee [-a] F`, or `git commit ... -F -` - with nothing after the heredoc on that line; an earlier
  `cd X &&` on the same line is allowed and still scanned. An unquoted delimiter, a pipe onward, any other
  consumer (sh, bash, python3, ssh ...) or an unterminated heredoc keeps the body under rule 2.
- Rules 1 (secret names), 3 (unkeyed environment reads) and the value prefixes still scan the whole command,
  heredoc bodies included.
- Rule 3 matches `environ` / `ENVIRON` as words (`os.environ`, `os.environb`, `/proc/1/environ`), not inside
  "environment".
- `guards/tests/cases_secret.tsv` gains the 17 cases RDD_672 added to the original list (6 allowed prose cases, 11
  refused: unquoted `$(env)` heredoc, `cat <<'EOF' | sh`, `sh <<'EOF'`, a python heredoc running env, a command
  after the heredoc, an unterminated heredoc, a secret name or `os.environ` inside a data heredoc, `os.environb`,
  `/proc/self/environ`); `guards/tests/run.py` reads `<NL>` in a case as a newline.

## Rationale

With a quoted delimiter the body is never expanded and a file writer never runs it, so nothing the shell executes
changes; what could leak (a secret's name, an environment read) is still caught by rules 1 and 3. Executing the
written file later is a separate command, and script files are already outside the hook's reach (known limit).
Checked 2026-11-01: `guards/tests/run.py` 184 cases, 0 mismatches on Python 3.13 (python2 is not installed on the
owner's PC; the CI job claude-guards covers 2.7, and the ported code ran on 2.7 in HowToTrainYourPetAI).
