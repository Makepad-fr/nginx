#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
for host in scan.makepad.fr pluck.makepad.fr; do
mkdir -p "$tmp/live/$host"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN="$host" -keyout "$tmp/live/$host/privkey.pem" -out "$tmp/live/$host/fullchain.pem" >/dev/null 2>&1
done
docker run --rm --network none -v "$tmp:/etc/letsencrypt:ro" -v "$root/sites/scan.conf.template:/etc/nginx/conf.d/default.conf:ro" -v "$root/sites/pluck.conf.template:/etc/nginx/conf.d/pluck.conf:ro" nginx:1.30-alpine3.24 nginx -t
