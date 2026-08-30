#!/usr/bin/env bash
# Bring up a rented rig for the LIVE product demo: CEI-build hub on the first
# pod + exactly one agent per host, then start a long job stream so the
# gpu.iversoncloud.com /live cockpit (heatmap, energy, value-at-risk, live
# recommendations) stays populated. Idempotent-ish: safe to re-run.
#
#   scripts/bringup.sh <podfile>       # podfile holds: "<A_ID> <A2_ID> <B_ID>"
#   scripts/bringup.sh .pods-live
#
# Every lesson paid for is baked in here so a redeploy is one command:
#   * clone is CLEAN + pinned to master + retried (a partial clone left a
#     broken, cei.py-less repo and crashed the hub import);
#   * agents are launched fetched-file + `setsid --fork nohup … </dev/null &
#     echo` — the only pattern that survives the ssh session closing on this
#     pod fleet (repo-path invocations and trailing `sleep`/`pgrep` hang or die);
#   * exactly ONE agent per host (duplicates race on job ids);
#   * ssh uses no known_hosts (parallel connections raced on it).
set -uo pipefail
cd "$(dirname "$0")/.."

PODFILE="${1:-.pods-live}"
REPO="${REPO:-https://github.com/prawalpokharel/cei-live-demo}"
BRANCH="${BRANCH:-master}"
KEY="$HOME/.runpod/ssh/runpodctl-ssh-key"
SSHO="-i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=20"
# hub env — long stream keeps the demo alive for hours:
HUBENV="SETPOINT_C=${SETPOINT_C:-45} DOMAIN_MATCH=${DOMAIN_MATCH:-b-} TOTAL_JOBS=${TOTAL_JOBS:-3000} ARRIVAL_P=${ARRIVAL_P:-0.15} JOB_MIN_S=${JOB_MIN_S:-60} JOB_MAX_S=${JOB_MAX_S:-180}"

read -r A_ID A2_ID B_ID < "$PODFILE"
[ -n "${B_ID:-}" ] || { echo "podfile must hold three ids: <A> <A2> <B>"; exit 1; }
HUB="https://${A_ID}-8000.proxy.runpod.net"

ep() { runpodctl ssh info "$1" 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["ip"], d.get("port") or d.get("sshPort"))'; }
read -r AIP APORT   <<< "$(ep "$A_ID")"
read -r A2IP A2PORT <<< "$(ep "$A2_ID")"
read -r BIP BPORT   <<< "$(ep "$B_ID")"
echo "hub=$HUB  a=$AIP:$APORT  a2=$A2IP:$A2PORT  b=$BIP:$BPORT"

# ── hub host: deps + robust clone + hub ────────────────────────────────────
ssh $SSHO root@"$AIP" -p "$APORT" "
python3 -m pip install -q --break-system-packages fastapi 'uvicorn[standard]' 2>&1 | tail -1
pkill -9 -f 'server.app:ap[p]' 2>/dev/null; pkill -9 -f 'node_agent.p[y]' 2>/dev/null; sleep 1
for i in 1 2 3; do rm -rf /root/cei-live-demo; git clone -q -b $BRANCH $REPO /root/cei-live-demo && break; sleep 3; done
test -f /root/cei-live-demo/server/cei.py || { echo CLONE_MISSING_CEI; exit 1; }
cd /root/cei-live-demo
setsid --fork nohup env $HUBENV python3 -u -m uvicorn server.app:app --host 0.0.0.0 --port 8000 >/root/hub.log 2>&1 </dev/null & echo HUB_FIRED
" </dev/null || { echo HUB_SSH_FAILED; exit 1; }

printf 'waiting for hub'
for i in $(seq 1 48); do
  curl -s -m5 -A chk "$HUB/metrics" 2>/dev/null | grep -q '"cei"' && { echo " up"; break; }
  printf '.'; sleep 5
done
curl -s -A chk "$HUB/metrics" 2>/dev/null | grep -q '"cei"' || { echo " HUB_DOWN (no CEI build)"; exit 1; }

# ── one agent per host (fetched-file pattern; the survivor) ────────────────
agent() { # ip port host localhubopt
  local ip=$1 port=$2 host=$3 hubarg=$4
  ssh $SSHO root@"$ip" -p "$port" "pkill -9 -f 'node_agent.p[y]' 2>/dev/null; sleep 1; rm -rf /tmp/cei-jobs-* /root/na.py; curl -s -A cei-setup $hubarg/agent -o /root/na.py; head -1 /root/na.py | grep -q python && setsid --fork nohup env HUB=$hubarg HOST=$host python3 /root/na.py >/root/agent.log 2>&1 </dev/null & echo ${host}_FIRED" </dev/null | tail -1
}
agent "$AIP"  "$APORT"  a  "http://localhost:8000"
agent "$A2IP" "$A2PORT" a2 "$HUB"
agent "$BIP"  "$BPORT"  b  "$HUB"

echo "waiting for nodes..."; sleep 15
N=$(curl -s -A chk "$HUB/metrics" | python3 -c 'import json,sys; print(len([n for n in json.load(sys.stdin)["measured"]["nodes"] if not n.get("stale")]))')
echo "fresh nodes: $N"
[ "$N" = "10" ] || echo "!! expected 10 nodes, got $N — check agent logs"

# ── start the job stream ──────────────────────────────────────────────────
curl -s -X POST -A chk "$HUB/control/start" -d '{}' >/dev/null
sleep 20
curl -s -A chk "$HUB/metrics" | python3 -c '
import json,sys
m=json.load(sys.stdin); f=[n for n in m["measured"]["nodes"] if not n.get("stale")]
print("running",m["jobs"]["running"],"| cei",len(m.get("cei",{})),"| goodput",len(m.get("goodput",{})),"| kW",round(sum(n["watts"] for n in f)/1000,2))
'
echo
echo "LIVE. Sign in at gpu.iversoncloud.com, open /live, connect:"
echo "  $HUB"
echo "BRINGUP_DONE"
