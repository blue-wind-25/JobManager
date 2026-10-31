# RDD_021 - protected-read allows copy-onto, in-place edits and --exclude=; broad-search refuses ls -R

Date: 2026-11-01. Status: resolved (owner, 2026-11-01: archive HowToTrainYourPetAI's and RobotCoding's own guard
hooks in favour of claude-guards, after taking over what they did better).

## Problem

HowToTrainYourPetAI's CLAUDE.md documents three ways to update its write-only `_handoff.txt`: `cp scratch
_handoff.txt`, `sed -i` on it, and repo-wide searches with `--exclude=_handoff.txt`. Its own hook
(block-handoff-read.sh) allows them; claude-guards' protected-read refused all three, so replacing the old hook
would have broken the documented routes. Its block-broad-search.sh also refuses `ls -R` on a broad root, which
claude-guards did not.

## Decision

- protected-read allows an occurrence of a protected file that is: the last word of a `cp` / `mv` / `install` /
  `rsync` / `scp` / `dd` segment (the destination); in a segment that edits in place (`sed -i`, `perl -i`,
  `--in-place`); or right after `--exclude...=`. Any other occurrence in a copier segment (the file as a source),
  archivers (`tar`, `base64`, ...) and readers stay refused, as before.
- broad-search treats `ls` with `-R` (in any short-option cluster) or `--recursive` as a search.
- 12 protected-read and 5 broad-search cases added; `guards/tests/run.py`: 202 cases, 0 mismatches on Python 3.13.

## Rationale

Each allowed form writes the file without printing it; the old hook ran with these rules in HowToTrainYourPetAI
since 2026-09 without a leak. `sed -i` cannot print the file to the session (its output goes back into the file).
