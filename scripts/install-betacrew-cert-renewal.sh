#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cron_file=$(mktemp)
filtered=$(mktemp)
cleanup() { find "${cron_file}" "${filtered}" -delete 2>/dev/null || true; }
trap cleanup EXIT HUP INT TERM

crontab -l > "${cron_file}" 2>/dev/null || true
grep -v '# BetaCrew certificate renewal$' "${cron_file}" > "${filtered}" || true
printf '17 4,16 * * * %q # BetaCrew certificate renewal\n' \
  "${repo_root}/scripts/renew-betacrew-cert.sh" >> "${filtered}"
crontab "${filtered}"
