#!/usr/bin/env bash
# One-command demo: real GPUs if nvidia-smi exists, mock physics otherwise.
#   ./demo.sh            -> auto-detect
#   MOCK=1 ./demo.sh     -> force mock (rehearse the flow on any laptop, $0)
set -euo pipefail
cd "$(dirname "$0")"

if [ "${MOCK:-}" != "1" ] && command -v nvidia-smi >/dev/null 2>&1; then
  echo "==> Real GPUs detected:"
  nvidia-smi --query-gpu=index,name,power.draw --format=csv,noheader
  if nvidia-smi --query-gpu=power.draw --format=csv,noheader | grep -q "N/A"; then
    echo "!! A GPU reports power.draw = N/A — you were given a MIG/vGPU slice."
    echo "!! Destroy the pod and rent WHOLE cards, or the demo has no watts."
    exit 1
  fi
  if [ ! -x gpu-burn/gpu_burn ]; then
    echo "==> Building gpu-burn (wilicc/gpu-burn)..."
    git clone --depth 1 https://github.com/wilicc/gpu-burn && (cd gpu-burn && make)
  fi
else
  export MOCK=1
  echo "==> No GPUs (or MOCK=1): running the mock thermal model."
fi

python3 -m pip install --quiet fastapi "uvicorn[standard]" 2>/dev/null || \
  python3 -m pip install --quiet --user fastapi "uvicorn[standard]" 2>/dev/null || \
  python3 -m pip install --quiet --break-system-packages fastapi "uvicorn[standard]"

PORT="${PORT:-8000}"
SESSION=cei-demo
HOSTNAME_SHORT="${HOST:-a}"
if command -v tmux >/dev/null 2>&1; then
  tmux kill-session -t "$SESSION" 2>/dev/null || true
  tmux new-session -d -s "$SESSION" \
    "MOCK=${MOCK:-0} DOMAIN_MATCH=${DOMAIN_MATCH:-} python3 -m uvicorn server.app:app --host 0.0.0.0 --port $PORT"
  if [ "${MOCK:-0}" != "1" ]; then
    sleep 2
    tmux new-window -t "$SESSION" \
      "HUB=http://localhost:$PORT HOST=$HOSTNAME_SHORT python3 agents/node_agent.py"
    echo "==> Hub + local agent (HOST=$HOSTNAME_SHORT) in tmux session '$SESSION'."
  else
    echo "==> Mock hub in tmux session '$SESSION' (survives your SSH dropping)."
  fi
else
  echo "==> tmux not found; running hub in foreground."
  MOCK=${MOCK:-0} DOMAIN_MATCH=${DOMAIN_MATCH:-} python3 -m uvicorn server.app:app --host 0.0.0.0 --port "$PORT" &
fi

sleep 2
echo
echo "   Dashboard:  http://localhost:$PORT"
if [ -n "${RUNPOD_POD_ID:-}" ]; then
  echo "   Public:     https://${RUNPOD_POD_ID}-${PORT}.proxy.runpod.net"
fi
echo
echo "   Stage flow: Start job stream -> FIXED 0.85 (pain) -> kill + save ghost"
echo "   -> Reset -> AUTO (recovery) -> ARM (a) -> hand over the button."
