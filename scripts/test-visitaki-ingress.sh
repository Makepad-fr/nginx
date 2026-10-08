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
cp -R "$fixture/etc/letsencrypt/live/visitaki.com" "$fixture/etc/letsencrypt/live/auth.visitaki.com"
{ printf 'events {}\nhttp {\n'; cat sites/visitaki-preview.conf.template sites/visitaki-identity.conf.template; printf '\n}\n'; } > "$fixture/nginx.conf"
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
docker rm "$container" >/dev/null
container=''
# Exercise routing against an isolated local stub, without public ports or DNS.
python3 - "$fixture/nginx.conf" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
s = p.read_text().replace('http://keycloak-visitaki:8080', 'http://127.0.0.1:8080')
stub = 'server { listen 8080; location / { return 200 "identity fixture"; } }'
p.write_text(s.replace('http {', 'http {\n' + stub, 1))
PY
container=$(docker create --network none --entrypoint nginx \
 nginx:1.30-alpine3.24@sha256:97d490c12ba55b4946b01546d1c3ed324e8d41ab1c9fcb2a616aa470620e5b46 \
 -c /tmp/nginx.conf -g 'daemon off;')
docker cp "$fixture/etc/letsencrypt" "$container:/etc/letsencrypt" >/dev/null
docker cp "$fixture/nginx.conf" "$container:/tmp/nginx.conf" >/dev/null
docker start "$container" >/dev/null
python3 - "$container" <<'PY'
import subprocess, sys, time
container = sys.argv[1]
def status(path):
    result = subprocess.run(['docker', 'exec', container, 'wget', '-T', '5', '-S', '-O', '/dev/null',
        '--no-check-certificate', '--header', 'Host: auth.visitaki.com',
        'https://127.0.0.1' + path], capture_output=True, text=True, timeout=15)
    return result.stderr
for attempt in range(30):
    if '200 OK' in status('/realms/visitaki/.well-known/openid-configuration'):
        break
    time.sleep(.1)
else:
    raise SystemExit('Identity fixture did not become ready')
for path in ['/realms/visitaki/protocol/openid-connect/auth', '/realms/visitaki/broker/apple/endpoint', '/resources/theme/login.css']:
    assert '200 OK' in status(path), path
for path in ['/', '/admin/', '/admin/realms/visitaki', '/realms/master/', '/realms/other/', '/health/ready', '/metrics', '/realms/visitaki/../../admin/']:
    assert '404 Not Found' in status(path), path
print('Visitaki identity routes pass; administration, other realms and management endpoints are private.')
PY
