#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
fixture=$(mktemp -d)
container=''
cleanup() { if [[ -n "$container" ]]; then docker rm -f "$container" >/dev/null 2>&1 || true; fi; rm -rf "$fixture"; }
trap cleanup EXIT
mkdir -p "$fixture/etc/letsencrypt/live/staging.vif.io"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=staging.vif.io' \
  -keyout "$fixture/etc/letsencrypt/live/staging.vif.io/privkey.pem" \
  -out "$fixture/etc/letsencrypt/live/staging.vif.io/fullchain.pem" >/dev/null 2>&1
mkdir -p "$fixture/etc/letsencrypt/live/brio-staging.makepad.fr"
cp "$fixture/etc/letsencrypt/live/staging.vif.io/"*.pem "$fixture/etc/letsencrypt/live/brio-staging.makepad.fr/"
python3 - "$fixture/nginx.conf" <<'PY'
from pathlib import Path
import sys
common=Path('sites/00-common.conf.template').read_text().split('# Brio logs',1)[1]
route=Path('sites/vif-staging.conf.template').read_text().replace('http://brio-staging-app:8080','http://127.0.0.1:8081').replace('http://vif-platform-staging-app:8080','http://127.0.0.1:8082')
route+='\n'+Path('sites/brio-vif-redirects.conf.template').read_text().replace('http://brio-staging-app:8080','http://127.0.0.1:8081')
assert 'vif-staging.conf.template' not in Path('compose.yml').read_text()
stubs=''
for port, name in [(8081,'community'),(8082,'platform')]:
 stubs+='server { listen '+str(port)+'; add_header X-Fixture-Method $request_method always; add_header X-Fixture-Stripe $http_stripe_signature always; add_header X-Fixture-Tally $http_tally_signature always; add_header X-Fixture-Cookie $http_cookie always; add_header X-Fixture-Authorization $http_authorization always; location / { return 200 "'+name+'|$request_uri|$http_host|$http_x_forwarded_host|$http_forwarded|$http_x_forwarded_for|$http_x_real_ip"; } }\n'
Path(sys.argv[1]).write_text('events {}\nhttp {\n# Brio logs'+common+'\n'+route+'\n'+stubs+'\n}\n')
PY
container=$(docker create --network none --entrypoint nginx \
 nginx:1.30-alpine3.24@sha256:97d490c12ba55b4946b01546d1c3ed324e8d41ab1c9fcb2a616aa470620e5b46 \
 -c /tmp/nginx.conf -g 'daemon off;')
docker cp "$fixture/etc/letsencrypt" "$container:/etc/letsencrypt" >/dev/null
docker cp "$fixture/nginx.conf" "$container:/tmp/nginx.conf" >/dev/null
docker start "$container" >/dev/null
python3 - "$container" <<'PY'
import subprocess,sys,time
container=sys.argv[1]
def request(path,host='staging.vif.io',extra=()):
 r=subprocess.run(['docker','exec',container,'wget','-T','5','-S','-O','-','--no-check-certificate','--header','Host: '+host,'--header','X-Forwarded-Host: evil.example','--header','Forwarded: host=evil.example','--header','X-Forwarded-For: 203.0.113.8','--header','X-Real-IP: 203.0.113.9',*extra,'https://127.0.0.1'+path],capture_output=True,text=True,timeout=15)
 return r.stdout,r.stderr
for _ in range(30):
 if '204 No Content' in request('/')[1]:break
 time.sleep(.1)
else:raise AssertionError('Fixture did not become ready')
for path,name in [('/walking-club','community'),('/walking-club/events?day=test','community'),('/walking-club/admin/photo-library','community'),('/platform','platform'),('/platform/account','platform')]:
 body,headers=request(path)
 assert body==name+'|'+path+'|staging.vif.io|staging.vif.io|||',(path,body,headers)
 assert '200 OK' in headers and 'noindex, nofollow, noarchive' in headers
for path in ['/unknown','/walking-club-other','/platform-other','/walking-club/../../unknown']:
 assert any(code in request(path)[1] for code in ('404 Not Found','400 Bad Request')),path
for host in ['domains.staging.vif.io','evil.example']:
 assert '421' in request('/walking-club',host)[1],host
assert 'Disallow: /' in request('/robots.txt')[0]
for path, location in [('/events?day=test','https://staging.vif.io/walking-club/events?day=test'),('/auth/callback?code=synthetic&state=old','https://staging.vif.io/walking-club/auth/login')]:
 _,headers=request(path,'brio-staging.makepad.fr')
 assert 'Location: '+location+'\n' in headers,headers
 assert ('303' if path.startswith('/auth/callback') else '308') in headers
for provider in ['stripe','tally']:
 path='/webhooks/'+provider
 assert '405' in request(path,'brio-staging.makepad.fr')[1]
 body,headers=request(path,'brio-staging.makepad.fr',('--post-data','synthetic=payload','--header','Stripe-Signature: synthetic-stripe','--header','Tally-Signature: synthetic-tally','--header','Cookie: obsolete=session','--header','Authorization: Bearer obsolete'))
 assert 'X-Fixture-Method: POST' in headers and 'X-Fixture-Stripe: synthetic-stripe' in headers and 'X-Fixture-Tally: synthetic-tally' in headers,headers
 assert 'X-Fixture-Cookie:' not in headers and 'X-Fixture-Authorization:' not in headers,headers
 assert body=='community|/walking-club'+path+'|staging.vif.io|staging.vif.io|||',(body,headers)
assert '405' in request('/admin/events','brio-staging.makepad.fr',('--post-data','synthetic=payload'))[1]
print('Vif staging routing, exact path boundaries, forwarding-header sanitization, CNAME rejection and noindex checks passed.')
PY
