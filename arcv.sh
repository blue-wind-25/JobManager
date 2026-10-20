#!/bin/bash
#
# Copyright (C) https://github.com/blue-wind-25/JobManager
# All rights reserved.
#
# Backup: the repo tree (minus .git and Python caches) as <repo>-<date>.tar.bz2 next to the repo.
# Adapted from HowToTrainYourPetAI's arcv.sh; this repo has no build output to clean and no
# out-of-repo data folders, so it archives the tree only. Runs from anywhere.
#
#   ./arcv.sh

set -eu

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_NAME="$(basename "$REPO_DIR")"
DATE_STAMP=`date +'%Y%m%d-%H%M'`
COPY_NAME=$REPO_NAME-$DATE_STAMP

echo

echo -e "Copying the project tree (excluding the '.git' directory) ..."
cd "$REPO_DIR/.."
rsync -av --exclude='.git/' --exclude='__pycache__/' --exclude='*.pyc' "$REPO_NAME/" "$COPY_NAME/"
echo

echo -e "Archiving files from the copied project tree ..."
tar -cjvpf "$COPY_NAME.tar.bz2" "$COPY_NAME" > /dev/null
echo -e "Done '$COPY_NAME.tar.bz2'"

echo
