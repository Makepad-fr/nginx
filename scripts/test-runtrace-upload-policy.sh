#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
work_dir=$(mktemp -d)
suffix="${RANDOM}-$$"
network="runtrace-nginx-test-${suffix}"
upstream="runtrace-upstream-${suffix}"
proxy="runtrace-proxy-${suffix}"
nginx_image="nginx:1.28-alpine@sha256:a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236"

for expected in \
  'zone=runtrace_general:10m rate=30r/s' \
  'zone=runtrace_telemetry:10m rate=2r/s' \
  "limit_conn_zone \$binary_remote_addr zone=runtrace_connections:10m" \
  'log_format runtrace_json escape=json' \
  '"request_id":"$request_id"'; do
  if ! grep -Fq -- "${expected}" "${repo_root}/sites/00-common.conf.template"; then
    echo "Runtrace shared proxy policy is missing: ${expected}" >&2
    exit 1
  fi
done
for expected in \
  'limit_req_status 429' \
  'limit_conn_status 429' \
  'limit_conn runtrace_connections 40' \
  'limit_req zone=runtrace_telemetry burst=5 nodelay' \
  'limit_req zone=runtrace_general burst=60 nodelay' \
  'access_log /dev/stdout runtrace_json' \
  'add_header X-Request-ID $request_id always' \
  'proxy_set_header X-Request-ID $request_id' \
  'proxy_hide_header X-Request-ID'; do
  if ! grep -Fq -- "${expected}" "${repo_root}/sites/runtrace-prod.conf.template"; then
    echo "Runtrace virtual host policy is missing: ${expected}" >&2
    exit 1
  fi
done

cleanup() {
  docker rm -f "${proxy}" "${upstream}" >/dev/null 2>&1 || true
  docker network rm "${network}" >/dev/null 2>&1 || true
  rm -rf "${work_dir}"
}
trap cleanup EXIT

mkdir -p "${work_dir}/proxy-conf" "${work_dir}/upstream-conf" "${work_dir}/certs" "${work_dir}/acme"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -subj "/CN=runtrace.localhost" \
  -keyout "${work_dir}/certs/privkey.pem" \
  -out "${work_dir}/certs/fullchain.pem" >/dev/null 2>&1

export RUNTRACE_PROD_SERVER_NAME=runtrace.localhost
export RUNTRACE_PROD_UPSTREAM="http://${upstream}:80"
export RUNTRACE_TLS_CERT_FILE=/etc/nginx/test-certs/fullchain.pem
export RUNTRACE_TLS_KEY_FILE=/etc/nginx/test-certs/privkey.pem
export CATWLK_ACME_WEBROOT=/var/lib/letsencrypt

envsubst "\${RUNTRACE_PROD_SERVER_NAME} \${RUNTRACE_PROD_UPSTREAM} \${RUNTRACE_TLS_CERT_FILE} \${RUNTRACE_TLS_KEY_FILE} \${CATWLK_ACME_WEBROOT}" \
  < "${repo_root}/sites/runtrace-prod.conf.template" \
  > "${work_dir}/proxy-conf/runtrace.conf"
cp "${repo_root}/sites/00-common.conf.template" "${work_dir}/proxy-conf/00-common.conf"
printf '%s\n' \
  'server {' \
  '    listen 80;' \
  '    client_max_body_size 70m;' \
  '    add_header X-Upstream-Request-ID $http_x_request_id always;' \
  '    location / { return 204; }' \
  '}' > "${work_dir}/upstream-conf/default.conf"

docker network create "${network}" >/dev/null
docker run -d --name "${upstream}" --network "${network}" \
  -v "${work_dir}/upstream-conf:/etc/nginx/conf.d:ro" \
  "${nginx_image}" >/dev/null
docker run -d --name "${proxy}" --network "${network}" \
  -p 127.0.0.1::443 \
  -v "${work_dir}/proxy-conf:/etc/nginx/conf.d:ro" \
  -v "${work_dir}/certs:/etc/nginx/test-certs:ro" \
  -v "${work_dir}/acme:/var/lib/letsencrypt:ro" \
  "${nginx_image}" >/dev/null

docker exec "${proxy}" nginx -t >/dev/null
proxy_port=$(docker port "${proxy}" 443/tcp | awk -F: 'NR == 1 {print $NF}')
if [[ -z "${proxy_port}" ]]; then
  echo "Unable to determine disposable proxy port." >&2
  exit 1
fi

wait_for_proxy() {
  local attempts=0
  until curl -ksS --resolve "runtrace.localhost:${proxy_port}:127.0.0.1" \
    -o /dev/null "https://runtrace.localhost:${proxy_port}/healthz"; do
    attempts=$((attempts + 1))
    if (( attempts >= 30 )); then
      echo "Disposable Runtrace proxy did not become ready." >&2
      exit 1
    fi
    sleep 0.2
  done
}

post_bytes() {
  local path=$1
  local mebibytes=$2
  dd if=/dev/zero bs=1048576 count="${mebibytes}" 2>/dev/null \
    | curl -ksS --http1.1 --resolve "runtrace.localhost:${proxy_port}:127.0.0.1" \
      -H 'Content-Type: application/octet-stream' \
      -H 'Expect:' \
      --data-binary @- \
      -o /dev/null \
      -w '%{http_code}' \
      "https://runtrace.localhost:${proxy_port}${path}"
}

assert_status() {
  local expected=$1
  local actual=$2
  local description=$3
  if [[ "${actual}" != "${expected}" ]]; then
    echo "${description}: expected HTTP ${expected}, got ${actual}." >&2
    exit 1
  fi
}

wait_for_proxy
headers_file="${work_dir}/response-headers"
curl -ksS --resolve "runtrace.localhost:${proxy_port}:127.0.0.1" \
  -D "${headers_file}" \
  -o /dev/null \
  "https://runtrace.localhost:${proxy_port}/healthz?organizationSlug=must-not-be-logged"
request_id=$(awk 'tolower($1) == "x-request-id:" {gsub("\r", "", $2); print $2}' "${headers_file}" | tail -1)
upstream_request_id=$(awk 'tolower($1) == "x-upstream-request-id:" {gsub("\r", "", $2); print $2}' "${headers_file}" | tail -1)
if [[ ! "${request_id}" =~ ^[a-f0-9]{32}$ || "${request_id}" != "${upstream_request_id}" ]]; then
  echo "Runtrace request ID was not consistently propagated through the proxy (response=${request_id:-missing}, upstream=${upstream_request_id:-missing})." >&2
  exit 1
fi

assert_status 204 "$(post_bytes /telemetry-batches 1)" "1 MiB telemetry upload"
assert_status 204 "$(post_bytes /telemetry-batches 10)" "10 MiB telemetry upload"
assert_status 204 "$(post_bytes /telemetry-batches 64)" "64 MiB telemetry upload"
assert_status 413 "$(post_bytes /telemetry-batches 65)" "65 MiB telemetry upload"
assert_status 413 "$(post_bytes /admin/settings 5)" "5 MiB general request"

sleep 0.2
proxy_logs=$(docker logs "${proxy}" 2>&1)
access_logs=$(printf '%s\n' "${proxy_logs}" | awk '/^\{/')
if ! printf '%s\n' "${access_logs}" | grep -Fq '"request_id":"'; then
  echo "Runtrace proxy did not emit structured request correlation logs." >&2
  exit 1
fi
for forbidden in 'organizationSlug' 'must-not-be-logged' '/healthz' '/telemetry-batches' '/admin/settings'; do
  if printf '%s\n' "${access_logs}" | grep -Fq "${forbidden}"; then
    echo "Runtrace JSON access log exposed a URL or query value: ${forbidden}" >&2
    exit 1
  fi
done
printf '%s\n' "${access_logs}" | python3 -c '
import json
import sys

records = [json.loads(line) for line in sys.stdin if line.startswith("{")]
if not records:
    raise SystemExit("no Runtrace JSON access records found")
required = {
    "timestamp",
    "request_id",
    "method",
    "status",
    "request_bytes",
    "response_bytes",
    "request_time_seconds",
    "upstream_status",
    "upstream_response_time_seconds",
}
if any(set(record) != required for record in records):
    raise SystemExit("Runtrace JSON access record has an unexpected field set")
'

echo "Runtrace nginx upload policy passed."
