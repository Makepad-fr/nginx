#!/usr/bin/env bash
set -euo pipefail

for command in docker openssl; do
  command -v "${command}" >/dev/null 2>&1 || { echo "${command} is required" >&2; exit 1; }
done

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "${script_dir}/.." && pwd)
common="${repo_root}/sites/00-common.conf.template"
prod="${repo_root}/sites/amiary-prod.conf.template"
canary="${repo_root}/sites/amiary-canary.conf.template"
common_proxy="${repo_root}/sites/amiary-common-proxy.conf"
deploy_workflow="${repo_root}/.github/workflows/manual-deploy.yml"
compose="${repo_root}/compose.yml"

for file in "${common}" "${prod}" "${canary}" "${common_proxy}" "${deploy_workflow}" "${compose}"; do
  test -s "${file}" || { echo "missing Amiary proxy input: ${file}" >&2; exit 1; }
done

require_text() {
  local file=$1 text=$2
  grep -Fq -- "${text}" "${file}" || { echo "${file} is missing: ${text}" >&2; exit 1; }
}

for file in "${prod}" "${canary}"; do
  require_text "${file}" 'access_log /dev/stdout amiary_json;'
  require_text "${file}" 'error_log /dev/stderr crit;'
  require_text "${file}" 'client_max_body_size 2m;'
  require_text "${file}" 'client_max_body_size 12m;'
  require_text "${file}" 'location = /.well-known/carddav'
  require_text "${file}" 'location ^~ /dav/'
  # shellcheck disable=SC2016
  require_text "${file}" 'add_header X-Request-ID $request_id always;'
  require_text "${file}" 'add_header X-Content-Type-Options nosniff always;'
  if [[ $(grep -Fc 'amiary_json;' "${file}") -ne 2 ]]; then
    echo "${file} must use the privacy log for both HTTP and HTTPS servers" >&2
    exit 1
  fi
  if grep -Fq '/var/log/nginx/amiary-' "${file}"; then
    echo "Amiary logs must use Docker-rotated stdout/stderr: ${file}" >&2
    exit 1
  fi
  if [[ $(grep -Fc 'error_log /dev/stderr crit;' "${file}") -ne 2 ]]; then
    echo "${file} must suppress URI-bearing error logs for both HTTP and HTTPS servers" >&2
    exit 1
  fi
done

require_text "${common_proxy}" 'proxy_set_header X-Forwarded-For "";'
require_text "${common_proxy}" 'proxy_set_header X-Real-IP "";'
require_text "${common_proxy}" 'proxy_request_buffering off;'
require_text "${common_proxy}" 'proxy_buffering off;'
# shellcheck disable=SC2016
require_text "${common_proxy}" 'proxy_set_header X-Request-ID $request_id;'
require_text "${deploy_workflow}" 'ensure_encrypted_amiary_network'
require_text "${deploy_workflow}" 'docker network create --driver overlay --attachable --opt encrypted'
require_text "${deploy_workflow}" 'index .Options "encrypted"'

amiary_log=$(awk '/log_format amiary_json/{capture=1} capture{print} capture && /};/{exit}' "${common}")
# shellcheck disable=SC2016
for forbidden in '$request_uri' '$uri' '$remote_addr' '$http_authorization' '$request_body'; do
  if grep -Fq -- "${forbidden}" <<<"${amiary_log}"; then
    echo "Amiary access log contains forbidden field: ${forbidden}" >&2
    exit 1
  fi
done

nginx_image=$(awk '/^[[:space:]]+image: nginx:/{print $2; exit}' "${compose}")
case "${nginx_image}" in nginx:*@sha256:*) ;; *) echo "Nginx image must be immutable" >&2; exit 1 ;; esac
render_dir=$(mktemp -d)
cleanup() {
  find "${render_dir}" -mindepth 1 -delete 2>/dev/null || true
  rmdir "${render_dir}" 2>/dev/null || true
}
trap cleanup EXIT
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -subj /CN=amiary.localhost \
  -keyout "${render_dir}/server.key" -out "${render_dir}/server.crt" >/dev/null 2>&1

docker run --rm --network none \
  -e 'NGINX_ENVSUBST_FILTER=^(AMIARY_|CATWLK_)' \
  -e AMIARY_PROD_SERVER_NAME=amiary.localhost \
  -e AMIARY_CANARY_SERVER_NAME=canary.amiary.localhost \
  -e AMIARY_PROD_UPSTREAM=http://amiary-prod-app:8080 \
  -e AMIARY_CANARY_UPSTREAM=http://amiary-canary-app:8080 \
  -e AMIARY_PROD_TLS_CERT_FILE=/run/amiary-test/server.crt \
  -e AMIARY_PROD_TLS_KEY_FILE=/run/amiary-test/server.key \
  -e AMIARY_CANARY_TLS_CERT_FILE=/run/amiary-test/server.crt \
  -e AMIARY_CANARY_TLS_KEY_FILE=/run/amiary-test/server.key \
  -e CATWLK_ACME_WEBROOT=/var/lib/letsencrypt \
  -v "${common}:/etc/nginx/templates/00-common.conf.template:ro" \
  -v "${prod}:/etc/nginx/templates/amiary-prod.conf.template:ro" \
  -v "${canary}:/etc/nginx/templates/amiary-canary.conf.template:ro" \
  -v "${common_proxy}:/etc/nginx/snippets/amiary-common-proxy.conf:ro" \
  -v "${render_dir}:/run/amiary-test:ro" \
  "${nginx_image}" nginx -t

echo "Amiary proxy privacy policy is valid."
