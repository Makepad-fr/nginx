#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
fixture=$(mktemp -d)
container=''
nginx_image=$(awk '$1 == "image:" { print $2; exit }' compose.yml)
[[ "$nginx_image" == nginx:*@sha256:* ]]
cleanup() { if [[ -n "$container" ]]; then docker rm -f "$container" >/dev/null 2>&1 || true; fi; rm -rf "$fixture"; }
trap cleanup EXIT
mkdir -p "$fixture/etc/letsencrypt/live/visitaki.com"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=visitaki.com' \
  -keyout "$fixture/etc/letsencrypt/live/visitaki.com/privkey.pem" \
  -out "$fixture/etc/letsencrypt/live/visitaki.com/fullchain.pem" >/dev/null 2>&1
cp -R "$fixture/etc/letsencrypt/live/visitaki.com" "$fixture/etc/letsencrypt/live/auth.visitaki.com"
{ printf 'events {}\nhttp {\n'; cat sites/00-common.conf.template sites/visitaki-preview.conf.template sites/visitaki-identity.conf.template; printf '\n}\n'; } > "$fixture/nginx.conf"
container=$(docker create --network none --add-host auth.visitaki.com:127.0.0.1 --entrypoint nginx \
 "$nginx_image" \
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
s = s.replace('http://visitaki-preview-api:8080', 'http://127.0.0.1:8080')
stub = '''server {
    listen 8080;
    # Model the API's larger header capacity behind the TLS ingress.
    large_client_header_buffers 4 32k;
    location = /healthz { return 204; }
    location / { return 200 "identity fixture"; }
}
server {
    listen 443 ssl;
    server_name neighbor.test;
    ssl_certificate /etc/letsencrypt/live/visitaki.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/visitaki.com/privkey.pem;
    access_log off;
    error_log /dev/stderr crit;
    return 204;
}'''
p.write_text(s.replace('http {', 'http {\n' + stub, 1))
PY
container=$(docker create --network none --add-host auth.visitaki.com:127.0.0.1 \
 --add-host visitaki.com:127.0.0.1 --add-host www.visitaki.com:127.0.0.1 \
 --add-host neighbor.test:127.0.0.1 --entrypoint nginx \
 "$nginx_image" \
 -c /tmp/nginx.conf -g 'daemon off;')
docker cp "$fixture/etc/letsencrypt" "$container:/etc/letsencrypt" >/dev/null
docker cp "$fixture/nginx.conf" "$container:/tmp/nginx.conf" >/dev/null
docker start "$container" >/dev/null
python3 - "$container" <<'PY'
import subprocess, sys, time
container = sys.argv[1]
def status(path, host='auth.visitaki.com', proof_bytes=0, tls=True):
    command = ['docker', 'exec', container, 'wget', '-T', '5', '-S', '-O', '/dev/null',
        '--no-check-certificate', '--header', 'Host: ' + host]
    if proof_bytes:
        command += ['--header', 'X-Visitaki-App-Transaction: ' + 'x' * proof_bytes,
                    '--header', 'Authorization: Bearer ' + 'y' * 3072]
    command += [('https://' if tls else 'http://') + host + path]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15)
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
# Exercise the rendered TLS server configuration, including SNI selection, with
# synthetic data only. The API separately verifies proof contents and size.
assert '204 No Content' in status('/healthz', 'visitaki.com', 16384), '16 KiB proof header must reach Visitaki'
assert '400 Bad Request' in status('/healthz', 'visitaki.com', 32768), 'Oversized single header must remain bounded'
for host, path in [('auth.visitaki.com', '/realms/visitaki/.well-known/openid-configuration'),
                   ('www.visitaki.com', '/'), ('neighbor.test', '/')]:
    assert '400 Bad Request' in status(path, host, 16384), host + ' must retain its default header limit'
assert '400 Bad Request' in status('/healthz', 'visitaki.com', 16384, tls=False), 'HTTP must retain its default header limit'
print('Visitaki accepts a 16 KiB proof header plus Authorization, rejects a 32 KiB header, and preserves other host limits.')
PY
