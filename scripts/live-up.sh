#!/usr/bin/env bash
# One command to stand up the live product demo end to end:
#   rent a 3-host rig (orphan-reconciled) → bring up the CEI hub + agents +
#   job stream → print the hub URL to paste into gpu.iversoncloud.com/live.
#   >>> BILLS YOUR RUNPOD ACCOUNT until scripts/live-down.sh <<<
set -euo pipefail
cd "$(dirname "$0")/.."
PODFILE="${PODS_FILE:-.pods-live}"

echo "== renting rig (prefix=live) =="
POD_PREFIX=live PODS_FILE="$PODFILE" scripts/rent-up-flex.sh

echo "== bringing up hub + agents + job stream =="
scripts/bringup.sh "$PODFILE"
