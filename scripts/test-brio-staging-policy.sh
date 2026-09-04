#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
work_dir=$(mktemp -d)
suffix="${RANDOM}-$$"
network="brio-nginx-test-${suffix}"
app_upstream="brio-app-${suffix}"
mail_upstream="brio-mail-${suffix}"
auth_upstream="brio-auth-${suffix}"
proxy="brio-proxy-${suffix}"
nginx_image="nginx:1.30-alpine3.24@sha256:97d490c12ba55b4946b01546d1c3ed324e8d41ab1c9fcb2a616aa470620e5b46"

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
  'openssl x509 -in "${certificate}" -noout -checkhost "${host}"' \
  'openssl x509 -in "${certificate}" -noout -checkend 604800' \
  'openssl verify -purpose sslserver -verify_hostname "${host}"' \
  'MAKEPAD_PROXY_BRIO_STAGING_APP_NETWORK must be makepad_brio_staging_app' \
  'MAKEPAD_PROXY_MAILDEV_BRIO_STAGING_WEB_NETWORK must be makepad_brio_staging_maildev_web' \
  'Brio deployment bundle contains non-canonical network names' \
  'wait_for_nginx_convergence' \
  'docker service ps --no-trunc --filter desired-state=running' \
  'wait_for_status "https://${brio_host}/livez" 204' \
  'wait_for_status "https://${maildev_host}/" 302' \
  '"${nginx_image}" nginx -t'; do
  grep -Fq -- "${expected}" "${repo_root}/.github/workflows/manual-deploy.yml" || {
    echo "Brio deployment TLS preflight is missing: ${expected}" >&2
    exit 1
  }
  done

for expected in \
  'nginx-deploy-ssh-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}' \
  'nginx-deploy-bundle-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}' \
  'UserKnownHostsFile=${known_hosts_file}' \
  '-F /dev/null' \
  'GlobalKnownHostsFile=/dev/null' \
  'IdentitiesOnly=yes' \
  'Remove job-scoped deployment material' \
  'if: always()'; do
  grep -Fq -- "${expected}" "${repo_root}/.github/workflows/manual-deploy.yml" || {
    echo "Self-hosted Nginx deploy cleanup control is missing: ${expected}" >&2
    exit 1
  }
done

# These are literal workflow patterns, not shell paths.
# shellcheck disable=SC2088
for forbidden in '${HOME}/.ssh' '$HOME/.ssh' '~/.ssh' 'add-ssh-host-key-action'; do
  if grep -Fq -- "${forbidden}" "${repo_root}/.github/workflows/manual-deploy.yml"; then
    echo "Self-hosted Nginx deploy workflow persists SSH state via ${forbidden}." >&2
    exit 1
  fi
done

grep -Fq -- 'test: ["CMD", "nginx", "-t"]' "${repo_root}/compose.yml" || {
  echo "Nginx service is missing its configuration healthcheck." >&2
  exit 1
}

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
  'proxy_set_header X-Forwarded-For ""' \
  'proxy_set_header X-Real-IP ""' \
  'proxy_set_header Forwarded ""' \
  'add_header X-Robots-Tag "noindex, nofollow, noarchive" always'; do
  grep -Fq -- "${expected}" "${repo_root}/sites/brio-staging.conf.template" || {
    echo "Brio application virtual host policy is missing: ${expected}" >&2
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
    echo "Brio MailDev virtual host policy is missing: ${expected}" >&2
    exit 1
  }
done

cleanup() {
  docker rm -f "${proxy}" "${app_upstream}" "${mail_upstream}" "${auth_upstream}" >/dev/null 2>&1 || true
  docker network rm "${network}" >/dev/null 2>&1 || true
  rm -rf "${work_dir}"
}
trap cleanup EXIT

mkdir -p "${work_dir}/proxy-conf" "${work_dir}/upstream-conf" "${work_dir}/auth-conf" "${work_dir}/certs" "${work_dir}/acme"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -subj "/CN=brio-staging.localhost" \
  -addext "subjectAltName=DNS:brio-staging.localhost,DNS:maildev-brio-staging.localhost" \
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
  < "${repo_root}/sites/brio-staging.conf.template" \
  > "${work_dir}/proxy-conf/brio.conf"
envsubst '${MAILDEV_BRIO_STAGING_SERVER_NAME} ${MAILDEV_BRIO_STAGING_UPSTREAM} ${MAILDEV_BRIO_STAGING_AUTH_UPSTREAM} ${MAILDEV_BRIO_STAGING_TLS_CERT_FILE} ${MAILDEV_BRIO_STAGING_TLS_KEY_FILE} ${CATWLK_ACME_WEBROOT}' \
  < "${repo_root}/sites/maildev-brio-staging.conf.template" \
  > "${work_dir}/proxy-conf/maildev.conf"
cp "${repo_root}/sites/00-common.conf.template" "${work_dir}/proxy-conf/00-common.conf"

printf '%s\n' \
  'server {' \
  '    listen 80;' \
  '    add_header X-Upstream-Forwarded-For "$http_x_forwarded_for" always;' \
  '    add_header X-Upstream-Real-IP "$http_x_real_ip" always;' \
  '    add_header X-Upstream-Forwarded "$http_forwarded" always;' \
  '    location / { return 204; }' \
  '}' > "${work_dir}/upstream-conf/default.conf"
printf '%s\n' \
  'server {' \
  '    listen 80;' \
  '    location = /oauth2/auth {' \
  '        if ($http_cookie != "maildev_auth=allowed") { return 401; }' \
  '        return 202;' \
  '    }' \
  '    location / { return 204; }' \
  '}' > "${work_dir}/auth-conf/default.conf"

docker network create "${network}" >/dev/null
docker run -d --name "${app_upstream}" --network "${network}" -v "${work_dir}/upstream-conf:/etc/nginx/conf.d:ro" "${nginx_image}" >/dev/null
docker run -d --name "${mail_upstream}" --network "${network}" -v "${work_dir}/upstream-conf:/etc/nginx/conf.d:ro" "${nginx_image}" >/dev/null
docker run -d --name "${auth_upstream}" --network "${network}" -v "${work_dir}/auth-conf:/etc/nginx/conf.d:ro" "${nginx_image}" >/dev/null
docker run -d --name "${proxy}" --network "${network}" -p 127.0.0.1::443 \
  -v "${work_dir}/proxy-conf:/etc/nginx/conf.d:ro" \
  -v "${work_dir}/certs:/etc/nginx/test-certs:ro" \
  -v "${work_dir}/acme:/var/lib/letsencrypt:ro" \
  "${nginx_image}" >/dev/null

docker exec "${proxy}" nginx -t >/dev/null
proxy_port=$(docker port "${proxy}" 443/tcp | awk -F: 'NR == 1 {print $NF}')
test -n "${proxy_port}"

request() {
  local host=$1
  local path=$2
  local cookie=${3:-}
  local args=(-ksS --http1.1 --resolve "${host}:${proxy_port}:127.0.0.1" -D - -o /dev/null)
  if [[ -n "${cookie}" ]]; then
    args+=(-H "Cookie: ${cookie}")
  fi
  curl "${args[@]}" "https://${host}:${proxy_port}${path}"
}

attempts=0
until request brio-staging.localhost /livez >/dev/null 2>&1; do
  attempts=$((attempts + 1))
  if (( attempts >= 30 )); then
    echo "Disposable Brio proxy did not become ready." >&2
    exit 1
  fi
  sleep 0.2
done

app_headers=$(request brio-staging.localhost '/livez?token=must-not-be-logged')
mail_headers=$(request maildev-brio-staging.localhost '/?email=must-not-be-logged' 'maildev_auth=allowed')
for header in 'strict-transport-security:' 'x-content-type-options: nosniff' 'x-frame-options: DENY' 'x-robots-tag: noindex, nofollow, noarchive'; do
  printf '%s\n' "${app_headers}" | tr '[:upper:]' '[:lower:]' | grep -Fq -- "$(printf '%s' "${header}" | tr '[:upper:]' '[:lower:]')" || {
    echo "Brio response is missing security header: ${header}" >&2
    exit 1
  }
done
printf '%s\n' "${mail_headers}" | tr '[:upper:]' '[:lower:]' | grep -Fq 'cache-control: private, no-store' || {
  echo "MailDev response is missing no-store cache policy." >&2
  exit 1
}

for protected_path in / /api/email '/socket.io/?EIO=4&transport=polling'; do
  unauthenticated_status=$(request maildev-brio-staging.localhost "${protected_path}" | awk 'NR == 1 {print $2}')
  authenticated_status=$(request maildev-brio-staging.localhost "${protected_path}" 'maildev_auth=allowed' | awk 'NR == 1 {print $2}')
  if [[ "${unauthenticated_status}" != "302" || "${authenticated_status}" != "204" ]]; then
    echo "MailDev route ${protected_path} did not enforce the OAuth gate (unauthenticated=${unauthenticated_status}, authenticated=${authenticated_status})." >&2
    exit 1
  fi
done

for relay_path in /email/example/relay /api/email/example/relay; do
  relay_status=$(curl -ksS --http1.1 --resolve "maildev-brio-staging.localhost:${proxy_port}:127.0.0.1" \
    -o /dev/null -w '%{http_code}' "https://maildev-brio-staging.localhost:${proxy_port}${relay_path}")
  if [[ "${relay_status}" != "403" ]]; then
    echo "MailDev relay endpoint ${relay_path} was not denied (HTTP ${relay_status})." >&2
    exit 1
  fi
done

for leaked_header in x-upstream-forwarded-for x-upstream-real-ip x-upstream-forwarded; do
  if printf '%s\n' "${app_headers}" | tr '[:upper:]' '[:lower:]' | grep -Eq "^${leaked_header}: .+"; then
    echo "Brio upstream received forbidden forwarding identity header: ${leaked_header}" >&2
    exit 1
  fi
done

sleep 0.2
access_logs=$(docker logs "${proxy}" 2>&1 | awk '/^\{/')
printf '%s\n' "${access_logs}" | python3 -c '
import json
import sys

records = [json.loads(line) for line in sys.stdin if line.startswith("{")]
if not records:
    raise SystemExit("no Brio privacy access records found")
required = {
    "timestamp", "request_id", "method", "status", "response_bytes",
    "request_time_seconds", "upstream_status", "upstream_response_time_seconds",
}
if any(set(record) != required for record in records):
    raise SystemExit("Brio privacy access record has an unexpected field set")
'
for forbidden in 'must-not-be-logged' '/livez' '/api/email' '127.0.0.1'; do
  if printf '%s\n' "${access_logs}" | grep -Fq -- "${forbidden}"; then
    echo "Brio privacy access log exposed forbidden data: ${forbidden}" >&2
    exit 1
  fi
done

echo "Brio nginx staging policy passed."
