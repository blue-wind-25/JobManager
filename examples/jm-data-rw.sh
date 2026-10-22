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

# Example machine hook (docs/GUIDE.md sec 6): make a data mount that is read-only in /etc/fstab writable. For jm's
# config, on a machine whose state dir (JM_HOME) is on that mount, as both hooks that prepare it:
#     jm config on_first_start=~/bin/jm-data-rw.sh home_rw=~/bin/jm-data-rw.sh
# Harmless when the mount is already writable, so scripts outside jm may call it too. Needs passwordless sudo for
# mount. DATA_MOUNT (default /mnt/data) names the mount (a config `env = DATA_MOUNT=...` line reaches the hooks).
# Named jm-* so it never replaces a machine's own data-rw.sh / data-ro.sh. Install next to jm (a directory on the
# data mount itself works: running a script from a read-only mount is fine):
#     cp jm-data-rw.sh jm-data-ro.sh ~/bin/ && chmod 755 ~/bin/jm-data-r[ow].sh
# No such script on a machine? A hook can be the command itself: home_rw = sudo mount -o remount,rw /mnt/data
exec sudo mount -o remount,rw "${DATA_MOUNT:-/mnt/data}"
