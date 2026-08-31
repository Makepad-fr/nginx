#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd "${script_dir}/.." && pwd)
common="${repo_root}/sites/00-common.conf.template"
prod="${repo_root}/sites/amiary-prod.conf.template"
canary="${repo_root}/sites/amiary-canary.conf.template"
common_proxy="${repo_root}/sites/amiary-common-proxy.conf"
deploy_workflow="${repo_root}/.github/workflows/manual-deploy.yml"

for file in "${common}" "${prod}" "${canary}" "${common_proxy}" "${deploy_workflow}"; do
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

echo "Amiary proxy privacy policy is valid."
