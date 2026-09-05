#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
workflow="${repo_root}/.github/workflows/manual-deploy.yml"
ci_workflow="${repo_root}/.github/workflows/ci.yml"

for expected in \
  'log_format brio_privacy escape=json' \
  'zone=brio_staging_apply:10m rate=5r/m' \
  'zone=brio_staging_general:10m rate=15r/s' \
  'zone=brio_staging_connections:10m'; do
  grep -Fq -- "${expected}" "${repo_root}/sites/00-common.conf.template" || {
    echo "Brio shared proxy policy is missing: ${expected}" >&2
    exit 1
  }
done

for expected in \
  'runs-on: [self-hosted, Linux, X64, makepad]' \
  "if: github.repository == 'Makepad-fr/nginx' && github.ref == 'refs/heads/main'" \
  'ref: ${{ github.sha }}' \
  '[[ "$(git rev-parse HEAD)" == "${GITHUB_SHA}" ]]' \
  'UserKnownHostsFile=${known_hosts_file}' \
  '-F /dev/null' \
  'GlobalKnownHostsFile=/dev/null' \
  'IdentitiesOnly=yes' \
  'Remove job-scoped deployment material' \
  'if: always()' \
  'REMOTE_HOST: ${{ vars.NGINX_DEPLOY_HOST }}' \
  'REMOTE_PORT: ${{ vars.NGINX_DEPLOY_PORT }}' \
  'REMOTE_USER: ${{ vars.NGINX_DEPLOY_USER }}' \
  'REMOTE_DIR: ${{ vars.NGINX_DEPLOY_REMOTE_DIR }}' \
  'STACK_NAME: ${{ vars.NGINX_DEPLOY_STACK_NAME }}' \
  'MAKEPAD_PROXY_BRIO_STAGING_APP_NETWORK must be makepad_brio_staging_app' \
  'MAKEPAD_PROXY_MAILDEV_BRIO_STAGING_WEB_NETWORK must be makepad_brio_staging_maildev_web' \
  'require_brio_network "${brio_app_network}" false Makepad-fr/brio app-edge' \
  'require_brio_network "${brio_maildev_network}" true Makepad-fr/maildev maildev-web' \
  'deploy its owning service first' \
  'openssl x509 -in "${certificate}" -noout -checkhost "${host}"' \
  'openssl x509 -in "${certificate}" -noout -checkend 604800' \
  'openssl verify -purpose sslserver -verify_hostname "${host}"' \
  '"${nginx_image}" nginx -t' \
  'docker service rollback "${nginx_service}"' \
  'wait_for_nginx_convergence' \
  'wait_for_status "https://${brio_host}/livez" 204' \
  'wait_for_status "https://${maildev_host}/" 302'; do
  grep -Fq -- "${expected}" "${workflow}" || {
    echo "Brio deployment control is missing: ${expected}" >&2
    exit 1
  }
done

# These are literal workflow patterns, not shell paths.
# shellcheck disable=SC2088
for forbidden in \
  'secrets.DEPLOY_SSH_HOST' \
  'secrets.DEPLOY_SSH_PORT' \
  'secrets.DEPLOY_SSH_USER' \
  'secrets.DEPLOY_REMOTE_DIR' \
  'secrets.DEPLOY_STACK_NAME' \
  '${HOME}/.ssh' \
  '$HOME/.ssh' \
  '~/.ssh' \
  'StrictHostKeyChecking=accept-new'; do
  if grep -Fq -- "${forbidden}" "${workflow}"; then
    echo "Nginx deploy workflow contains forbidden state or trust fallback: ${forbidden}" >&2
    exit 1
  fi
done

for expected in \
  'pull_request:' \
  "github.event.pull_request.head.repo.full_name == github.repository" \
  "github.event.pull_request.base.ref == 'main'" \
  'runs-on: [self-hosted, Linux, X64, makepad]' \
  'persist-credentials: false'; do
  grep -Fq -- "${expected}" "${ci_workflow}" || {
    echo "Nginx CI control is missing: ${expected}" >&2
    exit 1
  }
done
if grep -Fq -- 'secrets.' "${ci_workflow}"; then
  echo "Nginx pull-request CI must not receive secrets." >&2
  exit 1
fi

python3 - "${repo_root}/.github/workflows" <<'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
actual = sorted(path.name for path in root.iterdir() if path.suffix in {".yml", ".yaml"})
if actual != ["ci.yml", "manual-deploy.yml"]:
    raise SystemExit(f"unexpected Nginx workflow inventory: {actual!r}")
PY

grep -Fq -- 'test: ["CMD", "nginx", "-t"]' "${repo_root}/compose.yml" || {
  echo "Nginx service is missing its configuration healthcheck." >&2
  exit 1
}

if sed -n '/require_brio_network()/,/^          }/p' "${workflow}" | grep -Fq -- 'docker network create'; then
  echo "Nginx must not create application-owned Brio networks." >&2
  exit 1
fi

python3 - "${repo_root}/sites/brio-staging.conf.template" "${repo_root}/sites/maildev-brio-staging.conf.template" <<'PY'
import pathlib
import sys

for raw_path in sys.argv[1:]:
    path = pathlib.Path(raw_path)
    text = path.read_text()
    start = text.index("server {")
    depth = 0
    end = None
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end is None:
        raise SystemExit(f"unterminated HTTP server block in {path}")
    redirect_server = text[start:end]
    for required in ("access_log /dev/stdout brio_privacy;", "error_log /dev/stderr crit;"):
        if required not in redirect_server:
            raise SystemExit(f"HTTP redirect server in {path} is missing privacy logging control: {required}")
PY

for forbidden in '$request_uri' '$remote_addr' '$http_referer' '$http_user_agent'; do
  if sed -n '/log_format brio_privacy/,/};/p' "${repo_root}/sites/00-common.conf.template" | grep -Fq -- "${forbidden}"; then
    echo "Brio privacy log exposes forbidden field: ${forbidden}" >&2
    exit 1
  fi
done

for expected in \
  'server_name ${BRIO_STAGING_SERVER_NAME}' \
  'location = /applications' \
  'client_max_body_size 1m' \
  'access_log /dev/stdout brio_privacy' \
  'error_log /dev/stderr crit' \
  'add_header Referrer-Policy "strict-origin-when-cross-origin" always' \
  'add_header Cache-Control "private, no-store" always' \
  'proxy_set_header X-Forwarded-For ""' \
  'proxy_set_header X-Real-IP ""' \
  'proxy_set_header Forwarded ""' \
  'add_header X-Robots-Tag "noindex, nofollow, noarchive" always'; do
  grep -Fq -- "${expected}" "${repo_root}/sites/brio-staging.conf.template" || {
    echo "Brio application virtual-host policy is missing: ${expected}" >&2
    exit 1
  }
done

for expected in \
  'server_name ${MAILDEV_BRIO_STAGING_SERVER_NAME}' \
  'auth_request /oauth2/auth' \
  'location ~ ^/(?:api/)?email/[^/]+/relay(?:/|$)' \
  'return 403' \
  'error_log /dev/stderr crit' \
  'add_header Cache-Control "private, no-store" always' \
  'proxy_set_header X-Forwarded-For ""'; do
  grep -Fq -- "${expected}" "${repo_root}/sites/maildev-brio-staging.conf.template" || {
    echo "Brio MailDev virtual-host policy is missing: ${expected}" >&2
    exit 1
  }
done

work_dir=$(mktemp -d)
suffix="${RANDOM}-$$"
network="brio-nginx-test-${suffix}"
app_upstream="brio-app-${suffix}"
mail_upstream="brio-mail-${suffix}"
auth_upstream="brio-auth-${suffix}"
proxy="brio-proxy-${suffix}"
nginx_image='nginx:1.30-alpine3.24@sha256:97d490c12ba55b4946b01546d1c3ed324e8d41ab1c9fcb2a616aa470620e5b46'

cleanup() {
  docker rm -f "${proxy}" "${app_upstream}" "${mail_upstream}" "${auth_upstream}" >/dev/null 2>&1 || true
  docker network rm "${network}" >/dev/null 2>&1 || true
  if [[ -d "${work_dir}" && ! -L "${work_dir}" ]]; then
    find "${work_dir}" -depth -mindepth 1 -delete
    rmdir -- "${work_dir}"
  fi
}
trap cleanup EXIT

mkdir -p "${work_dir}/proxy-conf" "${work_dir}/upstream-conf" "${work_dir}/auth-conf" "${work_dir}/certs" "${work_dir}/acme"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -subj '/CN=brio-staging.localhost' \
  -addext 'subjectAltName=DNS:brio-staging.localhost,DNS:maildev-brio-staging.localhost' \
  -keyout "${work_dir}/certs/privkey.pem" \
  -out "${work_dir}/certs/fullchain.pem" >/dev/null 2>&1

export BRIO_STAGING_SERVER_NAME=brio-staging.localhost
export BRIO_STAGING_UPSTREAM="http://${app_upstream}:80"
export BRIO_STAGING_TLS_CERT_FILE=/etc/nginx/test-certs/fullchain.pem
export BRIO_STAGING_TLS_KEY_FILE=/etc/nginx/test-certs/privkey.pem
export MAILDEV_BRIO_STAGING_SERVER_NAME=maildev-brio-staging.localhost
export MAILDEV_BRIO_STAGING_UPSTREAM="http://${mail_upstream}:80"
export MAILDEV_BRIO_STAGING_AUTH_UPSTREAM="http://${auth_upstream}:80"
export MAILDEV_BRIO_STAGING_TLS_CERT_FILE=/etc/nginx/test-certs/fullchain.pem
export MAILDEV_BRIO_STAGING_TLS_KEY_FILE=/etc/nginx/test-certs/privkey.pem
export CATWLK_ACME_WEBROOT=/var/lib/letsencrypt

envsubst '${BRIO_STAGING_SERVER_NAME} ${BRIO_STAGING_UPSTREAM} ${BRIO_STAGING_TLS_CERT_FILE} ${BRIO_STAGING_TLS_KEY_FILE} ${CATWLK_ACME_WEBROOT}' \
  < "${repo_root}/sites/brio-staging.conf.template" > "${work_dir}/proxy-conf/brio.conf"
envsubst '${MAILDEV_BRIO_STAGING_SERVER_NAME} ${MAILDEV_BRIO_STAGING_UPSTREAM} ${MAILDEV_BRIO_STAGING_AUTH_UPSTREAM} ${MAILDEV_BRIO_STAGING_TLS_CERT_FILE} ${MAILDEV_BRIO_STAGING_TLS_KEY_FILE} ${CATWLK_ACME_WEBROOT}' \
  < "${repo_root}/sites/maildev-brio-staging.conf.template" > "${work_dir}/proxy-conf/maildev.conf"
cp "${repo_root}/sites/00-common.conf.template" "${work_dir}/proxy-conf/00-common.conf"

printf '%s\n' 'server {' ' listen 80;' \
  ' add_header X-Upstream-Forwarded-For "$http_x_forwarded_for" always;' \
  ' add_header X-Upstream-Real-IP "$http_x_real_ip" always;' \
  ' add_header X-Upstream-Forwarded "$http_forwarded" always;' \
  ' location / { return 204; }' '}' > "${work_dir}/upstream-conf/default.conf"
printf '%s\n' 'server {' ' listen 80;' \
  ' location = /oauth2/auth {' \
  '  if ($http_cookie != "maildev_auth=allowed") { return 401; }' \
  '  return 202;' ' }' ' location / { return 204; }' '}' > "${work_dir}/auth-conf/default.conf"

docker network create "${network}" >/dev/null
docker run -d --name "${app_upstream}" --network "${network}" -v "${work_dir}/upstream-conf:/etc/nginx/conf.d:ro" "${nginx_image}" >/dev/null
docker run -d --name "${mail_upstream}" --network "${network}" -v "${work_dir}/upstream-conf:/etc/nginx/conf.d:ro" "${nginx_image}" >/dev/null
docker run -d --name "${auth_upstream}" --network "${network}" -v "${work_dir}/auth-conf:/etc/nginx/conf.d:ro" "${nginx_image}" >/dev/null
docker run -d --name "${proxy}" --network "${network}" -p 127.0.0.1::443 \
  -v "${work_dir}/proxy-conf:/etc/nginx/conf.d:ro" \
  -v "${work_dir}/certs:/etc/nginx/test-certs:ro" \
  -v "${work_dir}/acme:/var/lib/letsencrypt:ro" "${nginx_image}" >/dev/null

docker exec "${proxy}" nginx -t >/dev/null
proxy_port=$(docker port "${proxy}" 443/tcp | awk -F: 'NR == 1 {print $NF}')
[[ -n "${proxy_port}" ]]

request() {
  local host=$1 path=$2 cookie=${3:-}
  local args=(-ksS --http1.1 --resolve "${host}:${proxy_port}:127.0.0.1" -D - -o /dev/null)
  [[ -z "${cookie}" ]] || args+=(-H "Cookie: ${cookie}")
  curl "${args[@]}" "https://${host}:${proxy_port}${path}"
}

for _ in $(seq 1 30); do
  request brio-staging.localhost /livez >/dev/null 2>&1 && break
  sleep 0.2
done
request brio-staging.localhost /livez >/dev/null

app_headers=$(request brio-staging.localhost '/livez?token=must-not-be-logged')
mail_headers=$(request maildev-brio-staging.localhost '/?email=must-not-be-logged' 'maildev_auth=allowed')
for header in 'strict-transport-security:' 'x-content-type-options: nosniff' 'x-frame-options: DENY' 'x-robots-tag: noindex, nofollow, noarchive'; do
  printf '%s\n' "${app_headers}" | tr '[:upper:]' '[:lower:]' | grep -Fq -- "$(printf '%s' "${header}" | tr '[:upper:]' '[:lower:]')"
done
printf '%s\n' "${mail_headers}" | tr '[:upper:]' '[:lower:]' | grep -Fq 'cache-control: private, no-store'
printf '%s\n' "${app_headers}" | tr '[:upper:]' '[:lower:]' | grep -Fq 'cache-control: private, no-store'

for protected_path in / /api/email '/socket.io/?EIO=4&transport=polling'; do
  unauthenticated=$(request maildev-brio-staging.localhost "${protected_path}" | awk 'NR == 1 {print $2}')
  authenticated=$(request maildev-brio-staging.localhost "${protected_path}" 'maildev_auth=allowed' | awk 'NR == 1 {print $2}')
  [[ "${unauthenticated}" == 302 && "${authenticated}" == 204 ]]
done
for relay_path in /email/example/relay /api/email/example/relay; do
  status=$(curl -ksS --http1.1 --resolve "maildev-brio-staging.localhost:${proxy_port}:127.0.0.1" \
    -o /dev/null -w '%{http_code}' "https://maildev-brio-staging.localhost:${proxy_port}${relay_path}")
  [[ "${status}" == 403 ]]
done
for leaked_header in x-upstream-forwarded-for x-upstream-real-ip x-upstream-forwarded; do
  ! printf '%s\n' "${app_headers}" | tr '[:upper:]' '[:lower:]' | grep -Eq "^${leaked_header}: .+"
done

sleep 0.2
access_logs=$(docker logs "${proxy}" 2>&1 | awk '/^\{/')
printf '%s\n' "${access_logs}" | python3 -c '
import json, sys
records = [json.loads(line) for line in sys.stdin if line.startswith("{")]
required = {"timestamp", "request_id", "method", "status", "response_bytes", "request_time_seconds", "upstream_status", "upstream_response_time_seconds"}
if not records or any(set(record) != required for record in records):
    raise SystemExit("Brio privacy access records are absent or have unexpected fields")
'
for forbidden in must-not-be-logged /livez /api/email 127.0.0.1; do
  ! printf '%s\n' "${access_logs}" | grep -Fq -- "${forbidden}"
done

echo 'Brio nginx staging policy passed.'
