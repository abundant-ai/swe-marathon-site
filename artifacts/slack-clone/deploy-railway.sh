#!/usr/bin/env bash
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

for trial in trial-1 trial-2 trial-3; do
  name="swe-marathon-slack-${trial}"
  echo "==> Deploying ${name}"
  cd "${ROOT}/${trial}"

  if [ ! -d ".railway" ]; then
    railway init --name "${name}"
  fi

  railway up --detach --message "Deploy ${name}"
  railway domain --port 8000 || true
done

echo "Done. Copy each Railway domain into swe-marathon-site/src/data.js."
