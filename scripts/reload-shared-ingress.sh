#!/usr/bin/env bash
set -euo pipefail
mapfile -t containers < <(docker ps --filter label=com.docker.swarm.service.name=makepad-edge_nginx -q)
if [ "${#containers[@]}" -ne 1 ]; then
  echo 'Expected one active Makepad ingress container.' >&2
  exit 1
fi
docker exec "${containers[0]}" nginx -t
docker kill -s HUP "${containers[0]}" >/dev/null
