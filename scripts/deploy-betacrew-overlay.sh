#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${repo_root}"
template=${BETACREW_NGINX_TEMPLATE:-./sites/betacrew-prod.conf.template}
[[ -f "${template}" ]] || { echo "BetaCrew Nginx template is missing: ${template}" >&2; exit 1; }

rendered=$(mktemp)
stack_file=$(mktemp)
cleanup() { find "${rendered}" "${stack_file}" -delete 2>/dev/null || true; }
trap cleanup EXIT HUP INT TERM

BETACREW_NGINX_TEMPLATE="${template}" docker compose \
  --env-file envs/production/.env.proxy \
  --env-file envs/production/.env.betacrew \
  -f compose.yml -f envs/production/compose.yml -f compose.betacrew.yml \
  config > "${rendered}"

python3 - "${rendered}" "${stack_file}" <<'PY'
import hashlib, re, sys
from pathlib import Path
source, target = map(Path, sys.argv[1:])
text = source.read_text().replace('published: "80"', 'published: 80').replace('published: "443"', 'published: 443')
text = re.sub(r'(?m)^(\s*cpus:)\s*([0-9]+(?:\.[0-9]+)?)\s*$', r'\1 "\2"', text)
revision = hashlib.sha256(text.encode()).hexdigest()[:12]
text = re.sub(r'(?m)^(\s+name: nginx_[a-z0-9_]+)$', rf'\1_{revision}', text)
text = "\n".join(line for line in text.splitlines() if line != "name: nginx") + "\n"
target.write_text(text)
PY

docker stack deploy --compose-file "${stack_file}" makepad-edge
for _ in $(seq 1 40); do
  [[ $(docker service ls --filter name=makepad-edge_nginx --format '{{.Replicas}}') == 1/1 ]] && exit 0
  sleep 3
done
docker service ps --no-trunc makepad-edge_nginx >&2
exit 1
