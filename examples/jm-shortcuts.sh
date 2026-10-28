#
# Copyright (C) Aloysius Indrayanto
#               https://github.com/blue-wind-25/JobManager
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Short jm commands for an interactive shell on a worker, e.g. from a phone (docs/REMOTE.md sec 3).
# Copy next to jm and source it from ~/.bashrc (bash, zsh, dash and busybox sh all work):
#     . ~/bin/jm-shortcuts.sh
#
#   jl          the queue as a tree in run order: id, state, progress, aligned (narrow enough for a phone)
#   je          the same with the expected end of each job (eta, RDD_019) in place of progress
#   js [ID]     status
#   jf [ID]     follow the captured output (log -f), the children's too; ends when the jobs end, or Ctrl-C
#   ja [ID]     follow the job's own log (applog -f), the children's too
#   jt [ID] [N] the last N (default 30) lines of the captured output, the children's too
#   jr [ID] [N] the job's `report` (its own status summary; N reaches it as JM_LINES); no ID: every queued job's
#
# Without an ID they take the first running job at the top level of the queue (a group: the whole group).

_jm_id() {
    if [ -n "$1" ]; then
        echo "$1"
        return 0
    fi
    jm list --tree | awk -F'\t' '$2 == "running" && $1 !~ /^ / { print $1; f = 1; exit } END { exit !f }' && return 0
    echo "no running job; give an ID (jl lists them)" >&2
    return 1
}

jl() { jm list --tree --order --fields id,state,progress "$@"; }
je() { jm list --tree --order --fields id,state,eta "$@"; }
js() { _jm_i=$(_jm_id "$1") && jm status "$_jm_i"; }
jf() { _jm_i=$(_jm_id "$1") && jm log -f --children "$_jm_i"; }
ja() { _jm_i=$(_jm_id "$1") && jm applog -f --children "$_jm_i"; }
jt() { _jm_i=$(_jm_id "$1") && jm log -n "${2:-30}" --children "$_jm_i"; }
jr() {
    case "${1:-}" in
        '') jm report ;;
        *[!0-9]*) jm report "$1" ${2:+-n "$2"} ;;
        *) jm report -n "$1" ;;  # a number alone: every job, N lines
    esac
}
