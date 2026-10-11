#!/usr/bin/env bash
set -euo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
exec python3 "${repo_root}/scripts/brio_release_guard.py" bash -euc '
mapfile -t containers < <(docker ps --filter label=com.docker.swarm.service.name=makepad-edge_nginx -q)
if [ "${#containers[@]}" -ne 1 ] || [[ -z "${containers[0]:-}" ]]; then
  echo "Expected one active Makepad ingress container." >&2
  exit 1
fi
docker exec "${containers[0]}" nginx -t
docker kill -s HUP "${containers[0]}" >/dev/null
'
