#!/usr/bin/env bash
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

for trial in trial-1 trial-2 trial-3; do
  name="swe-marathon-excel-${trial}"
  echo "==> Deploying ${name}"
  cd "${ROOT}/${trial}"

  railway status >/dev/null 2>&1 || railway init --name "${name}" --workspace Abundant

  railway up --detach --message "Deploy ${name}"
  railway domain --port 8000 || true
done

echo "Done. Copy each Railway domain into swe-marathon-site/src/data.js."
