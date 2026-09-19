#!/usr/bin/env bash
# Pull latest from GitHub and reinstall/restart only if something changed.
# Re-uses setup.sh -y, which keeps the existing config.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
before=$(git rev-parse HEAD)
git pull --ff-only
after=$(git rev-parse HEAD)
if [ "$before" != "$after" ]; then
    echo "Updated ${before:0:7} -> ${after:0:7}, reinstalling"
    sudo ./setup.sh -y
else
    echo "Already up to date"
fi
