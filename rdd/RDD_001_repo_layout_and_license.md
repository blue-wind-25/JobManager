# RDD_001 - Repository layout and licence

Date: 2026-10-21. Status: resolved (owner direction).

## Decision

- Licence: **Apache License 2.0** (`LICENSE` at the root). Every source file carries the header
  `Copyright (C) Aloysius Indrayanto` / `https://github.com/blue-wind-25/JobManager` followed by the standard
  Apache 2.0 notice. The verbatim appendix of `design/CLAUDE_GUARDS.md` keeps the HowToTrainYourPetAI headers it
  quotes (it documents another repository's files and is not this repository's source).
- Documentation sub-directories:
  - `design/` - design notes (`JM_DESIGN.md`, `CLAUDE_GUARDS.md`), the proposals the code implements;
  - `rdd/` - resolved design decisions, one file per decision, `RDD_<nnn>_<topic>.md`, numbered in order;
  - `state/` - implementation state: what is built, what is tested, what is next (`state/IMPL_STATE.md`).
- The repository is `blue-wind-25/JobManager` (the design's working name was `jm`); the tool itself is still
  `jm`, one file, `jm.py`.

## Consequences

- A decision taken while building that the design leaves open, or that deviates from it, gets an RDD before or
  with the commit that implements it; `state/IMPL_STATE.md` links the RDDs.
