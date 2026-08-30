#!/usr/bin/env bash
# Tear down the live demo rig and VERIFY nothing is left billing. Deletes the
# pods in the podfile AND any stray pod carrying the "live" prefix (belt and
# suspenders against orphans), then asserts `runpodctl pod list` is empty.
set -uo pipefail
cd "$(dirname "$0")/.."
PODFILE="${PODS_FILE:-.pods-live}"

ids=""
[ -f "$PODFILE" ] && ids="$(cat "$PODFILE")"
# also catch anything still named live-*
stray="$(runpodctl pod list -o json 2>/dev/null | python3 -c '
import json,sys
for p in json.load(sys.stdin):
    if str(p.get("name","")).startswith("live"):
        print(p["id"])
' 2>/dev/null || true)"

for id in $ids $stray; do
  [ -n "$id" ] && { echo "deleting $id"; runpodctl pod delete "$id" || true; }
done
sleep 4

left="$(runpodctl pod list -o json 2>/dev/null | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null || echo '?')"
echo "pods remaining: $left"
if [ "$left" = "0" ]; then
  rm -f "$PODFILE"
  echo "ALL CLEAR — nothing billing."
else
  echo "!! $left pod(s) still present — run: runpodctl pod list"
fi