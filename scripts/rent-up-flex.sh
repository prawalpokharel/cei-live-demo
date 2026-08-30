#!/usr/bin/env bash
# Flexible rental: 3 pods (hub-a: 4 GPUs, hub-a2: 4 GPUs, domain-b: 2 GPUs),
# trying a ladder of GPU types x any Secure datacenter until each pod lands.
#   >>> BILLS YOUR RUNPOD ACCOUNT while pods run; rent-down.sh stops it <<<
set -uo pipefail
cd "$(dirname "$0")/.."

IMG="${IMG:-runpod/pytorch:1.0.3-cu1281-torch291-ubuntu2404}"
# GPU ladder is overridable: GPU_LADDER="NVIDIA L40S|NVIDIA GeForce RTX 4090" (pipe-sep)
# POD_PREFIX renames the pods (default cei) so two rigs can coexist.
if [ -n "${GPU_LADDER:-}" ]; then
  IFS='|' read -r -a GPUS <<< "$GPU_LADDER"
else
  GPUS=("NVIDIA A40" "NVIDIA RTX A6000" "NVIDIA GeForce RTX 4090" "NVIDIA L40S" "NVIDIA A100 80GB PCIe")
fi
PFX="${POD_PREFIX:-cei}"

pod_id() { python3 -c '
import json, sys
raw = sys.stdin.read().strip()
try:
    d = json.loads(raw) if raw else {}
except Exception:
    d = {}
if isinstance(d, dict):
    print(d.get("id") or d.get("pod", {}).get("id") or "")
'; }

CREATED=()
make_pod() {  # name count extra_ports
  local name="$1" count="$2" ports="$3"
  for gpu in "${GPUS[@]}"; do
    echo "==> $name: trying ${count}x $gpu (Secure, any DC)" >&2
    local id
    id=$(runpodctl pod create --name "$name" \
      --gpu-id "$gpu" --gpu-count "$count" --cloud-type SECURE \
      --image "$IMG" --ports "$ports" \
      --container-disk-in-gb 40 --wait --wait-timeout 6m -o json 2>/dev/null | pod_id)
    if [ -n "$id" ]; then
      echo "    $name = $id (${count}x $gpu)" >&2
      CREATED+=("$id")
      echo "$id"
      return 0
    fi
  done
  return 1
}

cleanup() {
  echo "!! aborting — deleting anything created so nothing keeps billing" >&2
  for id in "${CREATED[@]:-}"; do runpodctl pod delete "$id" >&2 || true; done
  runpodctl pod list >&2
}

A_ID=$(make_pod "$PFX-hub-a" 4 "8000/http,22/tcp")   || { cleanup; exit 1; }
A2_ID=$(make_pod "$PFX-hub-a2" 4 "22/tcp")           || { cleanup; exit 1; }
B_ID=$(make_pod "$PFX-domain-b" 2 "22/tcp")          || { cleanup; exit 1; }

echo "$A_ID $A2_ID $B_ID" > "${PODS_FILE:-.pods}"
echo
echo "Three pods up (per-second billing started). Hub: https://${A_ID}-8000.proxy.runpod.net"
echo "Reconcile check (any cei-* pod NOT in .pods is an orphan to delete):"
runpodctl pod list 2>/dev/null | grep -o '"name": *"cei-[^"]*"' || true
echo "When finished:  scripts/rent-down.sh"
