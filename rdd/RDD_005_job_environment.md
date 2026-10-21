# RDD_005 - What a job's environment is made of

Date: 2026-10-21. Status: resolved (implementation; design sec 5 and 16); **confirmed by the owner, 2026-10-21**
(both the filtering of the job's environment and the use of the guard's name pattern).

## Decision

A job gets: the daemon's environment **minus every variable whose name is a secret name** (and minus `JM_*`),
then the config's `env` lines, the spec's `env` lines, `env_pass` names (from the daemon's environment),
`env_file` contents, and the `JM_*` variables. `env` values expand `$NAME` / `${NAME}` against what is built so far
(`$$` = `$`). So ordinary variables (PATH, LD_LIBRARY_PATH, CUDA settings) reach jobs as before, while a secret
reaches a job only by name through `env_pass` / `env_file`.

Extra variable: `JM_JM`, the command that runs this jm (`python /path/jm.py`), so a job can submit children
(`$JM_JM submit --parent "$JM_ID" ...`) without jm on PATH.

Secret names use the shell guard's pattern, `(^|_)(TOKEN|SECRET|PASSPHRASE|PASSWORD|PASSWD|API_?KEY|PRIVATE_KEY|
CREDENTIALS?)(_|$)` (any case), plus config `secret_names`; config `not_secret` lists harmless matches. This is
the "same rule set" design sec 16 asks for, and it avoids refusing `MAX_TOKENS`-like names. Secret values are
refused in `env` lines and in every command key of a spec.
