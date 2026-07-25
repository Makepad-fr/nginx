#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
suffix=$$
network="vestiaire-routing-test-${suffix}"
api_container="vestiaire-routing-api-${suffix}"
web_container="vestiaire-routing-web-${suffix}"
developer_container="vestiaire-routing-developer-${suffix}"
proxy_container="vestiaire-routing-proxy-${suffix}"
tmp_dir="${RUNNER_TEMP:-/tmp}/vestiaire-routing-test-${suffix}"

cleanup() {
  docker stop "${proxy_container}" "${developer_container}" "${web_container}" "${api_container}" >/dev/null 2>&1 || true
  docker network rm "${network}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

mkdir -p "${tmp_dir}/certs"

write_mock_config() {
  local destination=$1
  local upstream=$2
  cat > "${destination}" <<EOF
events {}
http {
    server {
        listen 8080;
        location / {
            add_header X-Test-Upstream "${upstream}" always;
            add_header X-Test-Authorization \$http_authorization always;
            add_header X-Test-Cookie \$http_cookie always;
            return 204;
        }
    }
}
EOF
}

write_mock_config "${tmp_dir}/api.conf" api
write_mock_config "${tmp_dir}/web.conf" web
write_mock_config "${tmp_dir}/developer.conf" developer
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -subj /CN=vestiaire.localhost \
  -keyout "${tmp_dir}/certs/privkey.pem" \
  -out "${tmp_dir}/certs/fullchain.pem" >/dev/null 2>&1

docker network create "${network}" >/dev/null
docker run --rm -d \
  --name "${api_container}" \
  --network "${network}" \
  --network-alias vestiaire-catalog-api \
  -v "${tmp_dir}/api.conf:/etc/nginx/nginx.conf:ro" \
  nginx:1.28-alpine >/dev/null
docker run --rm -d \
  --name "${web_container}" \
  --network "${network}" \
  --network-alias vestiaire-web \
  -v "${tmp_dir}/web.conf:/etc/nginx/nginx.conf:ro" \
  nginx:1.28-alpine >/dev/null
docker run --rm -d \
  --name "${developer_container}" \
  --network "${network}" \
  --network-alias vestiaire-developer-platform \
  -v "${tmp_dir}/developer.conf:/etc/nginx/nginx.conf:ro" \
  nginx:1.28-alpine >/dev/null
docker run --rm -d \
  --name "${proxy_container}" \
  --network "${network}" \
  -p 127.0.0.1::443 \
  -e VESTIAIRE_PROD_SERVER_NAME=vestiaire.localhost \
  -e VESTIAIRE_WEB_PROD_UPSTREAM=http://vestiaire-web:8080 \
  -e VESTIAIRE_CATALOG_API_PROD_UPSTREAM=http://vestiaire-catalog-api:8080 \
  -e VESTIAIRE_DEVELOPER_PLATFORM_PROD_UPSTREAM=http://vestiaire-developer-platform:8080 \
  -e VESTIAIRE_TLS_CERT_FILE=/certs/fullchain.pem \
  -e VESTIAIRE_TLS_KEY_FILE=/certs/privkey.pem \
  -e CATWLK_ACME_WEBROOT=/var/lib/letsencrypt \
  -v "${repo_root}/sites/00-common.conf.template:/etc/nginx/templates/00-common.conf.template:ro" \
  -v "${repo_root}/sites/vestiaire-prod.conf.template:/etc/nginx/templates/vestiaire-prod.conf.template:ro" \
  -v "${tmp_dir}/certs:/certs:ro" \
  nginx:1.28-alpine >/dev/null

published_address=$(docker port "${proxy_container}" 443/tcp)
test_port=${published_address##*:}

for _ in $(seq 1 30); do
  if curl --insecure --silent --output /dev/null \
    --resolve "vestiaire.localhost:${test_port}:127.0.0.1" \
    "https://vestiaire.localhost:${test_port}/"; then
    break
  fi
  sleep 1
done

VESTIAIRE_TEST_PORT=${test_port} python3 - <<'PY'
import http.client
import json
import os
import ssl

context = ssl._create_unverified_context()


def request(path, accept):
    connection = http.client.HTTPSConnection(
        "127.0.0.1",
        int(os.environ["VESTIAIRE_TEST_PORT"]),
        context=context,
        timeout=10,
    )
    connection.request(
        "GET",
        path,
        headers={
            "Host": "vestiaire.localhost",
            "Accept": accept,
            "Authorization": "Bearer test-token",
            "Cookie": "session=test-session",
        },
    )
    response = connection.getresponse()
    body = response.read().decode()
    result = response.status, dict(response.getheaders()), body
    connection.close()
    return result


status, headers, _ = request("/", "text/html")
assert status == 204 and headers["X-Test-Upstream"] == "web"
assert "X-Test-Authorization" not in headers
assert headers["X-Test-Cookie"] == "session=test-session"

status, headers, _ = request("/product-offers?brand=zara", "application/json")
assert status == 204 and headers["X-Test-Upstream"] == "api"
assert headers["X-Test-Authorization"] == "Bearer test-token"
assert "X-Test-Cookie" not in headers
assert "Accept" in headers["Vary"]
assert headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"

status, headers, _ = request("/product-offers", "text/html")
assert status == 204 and headers["X-Test-Upstream"] == "web"
assert "X-Test-Authorization" not in headers
assert headers["X-Test-Cookie"] == "session=test-session"
assert "Accept" in headers["Vary"]
assert headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"

status, headers, _ = request("/product-offers", "*/*")
assert status == 204 and headers["X-Test-Upstream"] == "api"

status, headers, body = request("/product-offers", "image/png")
assert status == 406 and json.loads(body)["error"] == "not_acceptable"
assert "Accept" in headers["Vary"]

status, headers, _ = request("/product-offers/facets", "text/html")
assert status == 204 and headers["X-Test-Upstream"] == "api"

valid_offer_id = "a" * 64
status, headers, _ = request(f"/product-offers/{valid_offer_id}", "text/html")
assert status == 204 and headers["X-Test-Upstream"] == "web"

status, headers, _ = request("/product-offers/not-an-offer-id", "text/html")
assert status == 204 and headers["X-Test-Upstream"] == "api"

status, headers, _ = request("/readyz", "text/html")
assert status == 204 and headers["X-Test-Upstream"] == "api"

status, headers, _ = request("/developers", "text/html")
assert status == 204 and headers["X-Test-Upstream"] == "developer"
assert headers["X-Test-Authorization"] == "Bearer test-token"
assert headers["X-Test-Cookie"] == "session=test-session"

status, headers, _ = request(
    "/developers/login/oauth2/code/keycloak?code=oauth-secret-code&state=opaque",
    "text/html",
)
assert status == 204 and headers["X-Test-Upstream"] == "developer"

for path in (
    "/current-user",
    "/organizations",
    "/organizations/test/api-credentials",
):
    status, headers, _ = request(path, "application/json")
    assert status == 204 and headers["X-Test-Upstream"] == "developer"
    assert headers["X-Test-Authorization"] == "Bearer test-token"
    assert headers["X-Test-Cookie"] == "session=test-session"

print("Vestiaire ingress routing contract passed")
PY

docker stop "${developer_container}" >/dev/null

VESTIAIRE_TEST_PORT=${test_port} python3 - <<'PY'
import http.client
import json
import os
import ssl


def request(path, accept):
    connection = http.client.HTTPSConnection(
        "127.0.0.1",
        int(os.environ["VESTIAIRE_TEST_PORT"]),
        context=ssl._create_unverified_context(),
        timeout=10,
    )
    connection.request(
        "GET",
        path,
        headers={"Host": "vestiaire.localhost", "Accept": accept},
    )
    response = connection.getresponse()
    body = response.read().decode()
    result = response.status, dict(response.getheaders()), body
    connection.close()
    return result


status, headers, body = request("/developers", "text/html")
assert status == 503
assert headers["Retry-After"] == "60"
assert headers["Cache-Control"] == "no-store"
assert "Developer portal temporarily unavailable" in body

status, headers, body = request("/organizations", "application/json")
assert status == 503
assert headers["Retry-After"] == "60"
assert headers["Cache-Control"] == "no-store"
assert json.loads(body)["code"] == "service_unavailable"

print("Vestiaire developer-platform unavailable fallbacks passed")
PY

docker stop "${web_container}" >/dev/null

VESTIAIRE_TEST_PORT=${test_port} python3 - <<'PY'
import http.client
import json
import os
import ssl

context = ssl._create_unverified_context()


def request(path, accept):
    connection = http.client.HTTPSConnection(
        "127.0.0.1",
        int(os.environ["VESTIAIRE_TEST_PORT"]),
        context=context,
        timeout=10,
    )
    connection.request(
        "GET",
        path,
        headers={"Host": "vestiaire.localhost", "Accept": accept},
    )
    response = connection.getresponse()
    body = response.read().decode()
    result = response.status, dict(response.getheaders()), body
    connection.close()
    return result


for path in ("/", "/product-offers"):
    status, headers, body = request(path, "text/html")
    assert status == 503
    assert headers["Retry-After"] == "60"
    assert headers["Cache-Control"] == "no-store"
    assert headers["Content-Type"].startswith("text/html")
    assert "Service temporarily unavailable" in body

status, headers, _ = request("/product-offers", "application/json")
assert status == 204 and headers["X-Test-Upstream"] == "api"

print("Vestiaire web-unavailable fallback passed")
PY

docker stop "${api_container}" >/dev/null

VESTIAIRE_TEST_PORT=${test_port} python3 - <<'PY'
import http.client
import json
import os
import ssl

connection = http.client.HTTPSConnection(
    "127.0.0.1",
    int(os.environ["VESTIAIRE_TEST_PORT"]),
    context=ssl._create_unverified_context(),
    timeout=10,
)
connection.request(
    "GET",
    "/product-offers",
    headers={"Host": "vestiaire.localhost", "Accept": "application/json"},
)
response = connection.getresponse()
body = response.read().decode()
headers = dict(response.getheaders())
connection.close()

assert response.status == 503
assert headers["Retry-After"] == "60"
assert headers["Cache-Control"] == "no-store"
assert headers["Content-Type"].startswith("application/json")
assert json.loads(body)["code"] == "service_unavailable"

print("Vestiaire catalog-unavailable fallback passed")
PY

valid_offer_id=$(printf 'a%.0s' {1..64})
logs=$(docker logs "${proxy_container}" 2>&1)
if grep -Fq "Bearer test-token" <<< "${logs}" \
  || grep -Fq "brand=zara" <<< "${logs}" \
  || grep -Fq "${valid_offer_id}" <<< "${logs}" \
  || grep -Fq "not-an-offer-id" <<< "${logs}" \
  || grep -Fq "oauth-secret-code" <<< "${logs}"; then
  echo "Vestiaire ingress logs contain sensitive request data" >&2
  exit 1
fi
grep -Fq "route=/product-offers/:offerId" <<< "${logs}"
grep -Fq "route=/product-offers/:subresource" <<< "${logs}"
