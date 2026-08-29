#!/usr/bin/env bash
# Create the two rental-test pods via runpodctl.
#   >>> BILLS YOUR RUNPOD ACCOUNT (~$7.4/hr total while running) <<<
#   Tear down with scripts/rent-down.sh — per-second billing stops then.
#
# Prereq (once):  runpodctl config --apiKey=YOUR_KEY
#                 (key from runpod.io Console -> Settings -> API Keys)
set -euo pipefail
cd "$(dirname "$0")/.."

IMG="${IMG:-runpod/pytorch:1.0.3-cu1281-torch291-ubuntu2404}"
GPU="${GPU:-NVIDIA GeForce RTX 4090}"

pod_id() { python3 -c '
import json, sys
d = json.load(sys.stdin)
if isinstance(d, dict):
    print(d.get("id") or d.get("pod", {}).get("id") or "")
'; }

echo "==> Pod A (hub + 8 nodes): 8x $GPU, Secure Cloud, port 8000/http"
A_ID=$(runpodctl pod create --name cei-hub-a \
  --gpu-id "$GPU" --gpu-count 8 --cloud-type SECURE \
  --image "$IMG" --ports "8000/http,22/tcp" \
  --container-disk-in-gb 40 --wait -o json | pod_id)
[ -n "$A_ID" ] || { echo "!! could not parse Pod A id"; exit 1; }
echo "    Pod A: $A_ID"

echo "==> Pod B (the domain): 2x $GPU, Secure Cloud"
B_ID=$(runpodctl pod create --name cei-domain-b \
  --gpu-id "$GPU" --gpu-count 2 --cloud-type SECURE \
  --image "$IMG" --ports "22/tcp" \
  --container-disk-in-gb 40 --wait -o json | pod_id)
[ -n "$B_ID" ] || { echo "!! could not parse Pod B id (Pod A $A_ID is RUNNING and billing — rent-down.sh removes it)"; echo "$A_ID" > .pods; exit 1; }
echo "    Pod B: $B_ID"

echo "$A_ID $B_ID" > .pods
echo
echo "Both pods up (per-second billing has started)."
echo
echo "  Hub URL (after bring-up):  https://${A_ID}-8000.proxy.runpod.net"
echo
echo "Next: run the bring-up paste blocks from TESTPLAN-RENTAL.md"
echo "  Pod A: git clone https://github.com/prawalpokharel/cei-live-demo && cd cei-live-demo && DOMAIN_MATCH=b- ./demo.sh"
echo "  Pod B: (see TESTPLAN — one line, pulls the agent from the hub)"
echo
echo "When finished:  scripts/rent-down.sh"
