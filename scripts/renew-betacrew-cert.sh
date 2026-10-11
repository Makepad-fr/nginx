#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

certbot renew --quiet \
  --config-dir /srv/makepad/nginx/betacrew-letsencrypt \
  --work-dir /srv/makepad/nginx/betacrew-certbot-work \
  --logs-dir /srv/makepad/nginx/betacrew-certbot-logs \
  --deploy-hook "python3 ${repo_root}/scripts/deploy-betacrew-ingress.py --reload-certificate"
