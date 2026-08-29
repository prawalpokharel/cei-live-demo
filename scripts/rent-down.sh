#!/usr/bin/env bash
# Delete the rental-test pods created by rent-up.sh (billing stops).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .pods ]; then
  echo "no .pods file — listing everything so you can delete by id:"
  runpodctl pod list
  echo "delete with: runpodctl pod delete <id>"
  exit 1
fi

for id in $(cat .pods); do
  echo "==> deleting pod $id"
  runpodctl pod delete "$id" || echo "!! delete failed for $id — check 'runpodctl pod list'"
done
rm -f .pods
echo
echo "==> remaining pods (should not include cei-hub-a / cei-domain-b):"
runpodctl pod list
