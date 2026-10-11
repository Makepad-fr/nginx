#!/usr/bin/env bash
set -euo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# Adopt/reconcile the existing BetaCrew host without replacing the shared stack.
exec python3 "$repo_root/scripts/deploy-betacrew-ingress.py" "$@"
