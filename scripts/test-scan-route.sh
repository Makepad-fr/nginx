#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/live/scan.makepad.fr"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=scan.makepad.fr -keyout "$tmp/live/scan.makepad.fr/privkey.pem" -out "$tmp/live/scan.makepad.fr/fullchain.pem" >/dev/null 2>&1
docker run --rm --network none -v "$tmp:/etc/letsencrypt:ro" -v "$root/sites/scan.conf.template:/etc/nginx/conf.d/default.conf:ro" nginx:1.30-alpine3.24 nginx -t
