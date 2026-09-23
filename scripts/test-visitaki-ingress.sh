#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
fixture=$(mktemp -d)
container=''
cleanup() { if [[ -n "$container" ]]; then docker rm -f "$container" >/dev/null 2>&1 || true; fi; rm -rf "$fixture"; }
trap cleanup EXIT
mkdir -p "$fixture/etc/letsencrypt/live/visitaki.com"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=visitaki.com' \
  -keyout "$fixture/etc/letsencrypt/live/visitaki.com/privkey.pem" \
  -out "$fixture/etc/letsencrypt/live/visitaki.com/fullchain.pem" >/dev/null 2>&1
{ printf 'events {}\nhttp {\n'; cat sites/visitaki-preview.conf.template; printf '\n}\n'; } > "$fixture/nginx.conf"
container=$(docker create --network none --entrypoint nginx \
 nginx:1.30-alpine3.24@sha256:97d490c12ba55b4946b01546d1c3ed324e8d41ab1c9fcb2a616aa470620e5b46 \
 -t -c /tmp/nginx.conf)
docker cp "$fixture/etc/letsencrypt" "$container:/etc/letsencrypt" >/dev/null
docker cp "$fixture/nginx.conf" "$container:/tmp/nginx.conf" >/dev/null
docker start -a "$container"
[[ $(docker inspect -f '{{.State.ExitCode}}' "$container") == 0 ]]
# These checks prevent accidental activation by the ordinary shared release.
python3 - <<'PY'
from pathlib import Path
base=Path('compose.yml').read_text()
assert 'visitaki' not in base, 'Preview must remain an explicit overlay until readiness passes'
route=Path('sites/visitaki-preview.conf.template').read_text()
assert 'access_log off;' in route and 'return 308 https://visitaki.com$request_uri;' in route
assert 'proxy_set_header X-Forwarded-For $remote_addr;' in route
print('Visitaki Nginx syntax, redirect and dormant-route checks passed.')
PY
