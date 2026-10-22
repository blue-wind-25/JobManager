#!/bin/sh
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

# Example machine hook (docs/GUIDE.md sec 6), jm-aware: remount a data mount read-only, unless jm still works on
# it. For jm's config: jm config on_idle=~/bin/jm-data-ro.sh (with jm-data-rw.sh as on_first_start / home_rw).
# Skips, exit 0:
#   - inside a jm job (JM_ID set): the machine is not idle; jm's on_idle hook does it later;
#   - called by hand, cron or an older script while jm's machine file says busy.
# jm's own on_idle call (JM_HOOK set) and a call while jm is idle remount: jm >= 1.1.0 waits on a read-only state
# dir and makes it writable again itself (home_rw, RDD_014). FORCE=1 remounts anyway. DATA_MOUNT (default
# /mnt/data) names the mount; JM_HOME (default ~/.jm) the state dir whose `machine` file is read. Needs passwordless
# sudo for mount. Install: see jm-data-rw.sh.
if [ "${FORCE:-0}" != 1 ]; then
    if [ -n "${JM_ID:-}" ]; then
        echo "jm-data-ro.sh: skipped inside jm job $JM_ID (jm's on_idle hook does it)"; exit 0
    fi
    if [ -z "${JM_HOOK:-}" ] && grep -q '^busy' "${JM_HOME:-$HOME/.jm}/machine" 2>/dev/null; then
        echo "jm-data-ro.sh: jm is busy; not now"; exit 0
    fi
fi
exec sudo mount -o remount,ro "${DATA_MOUNT:-/mnt/data}"
