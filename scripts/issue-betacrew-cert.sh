#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${repo_root}"

certbot certonly --webroot \
  -w /srv/makepad/nginx/betacrew-acme \
  --config-dir /srv/makepad/nginx/betacrew-letsencrypt \
  --work-dir /srv/makepad/nginx/betacrew-certbot-work \
  --logs-dir /srv/makepad/nginx/betacrew-certbot-logs \
  --non-interactive --agree-tos --email hello@makepad.fr \
  --cert-name betacrew.app \
  -d betacrew.app -d www.betacrew.app

BETACREW_NGINX_TEMPLATE=./sites/betacrew-prod.conf.template \
  "${repo_root}/scripts/deploy-betacrew-overlay.sh"
