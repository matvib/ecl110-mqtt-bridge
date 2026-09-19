#!/usr/bin/env bash
# Pull latest from GitHub and restart the service only if something changed.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
before=$(git rev-parse HEAD)
git pull --ff-only
after=$(git rev-parse HEAD)
if [ "$before" != "$after" ]; then
    echo "Updated ${before:0:7} -> ${after:0:7}, restarting ecl110"
    sudo systemctl restart ecl110
else
    echo "Already up to date"
fi
