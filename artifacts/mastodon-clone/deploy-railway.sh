#!/usr/bin/env bash
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

i=0
for trial in trial-1 trial-2 trial-3; do
  i=$((i+1))
  name="swe-marathon-mastodon-${trial}"
  echo "==> Deploying ${name}"
  cd "${ROOT}/${trial}"

  if ! railway status >/dev/null 2>&1; then
    [ $i -gt 1 ] && { echo "sleeping 35s for project-create rate limit"; sleep 35; }
    railway init --name "${name}" --workspace Abundant
  fi

  railway up --detach --message "Deploy ${name}"
  railway domain --port 8000 || true
done

echo "Done. Copy each Railway domain into swe-marathon-site/src/data.js."
