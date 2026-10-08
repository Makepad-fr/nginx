#!/usr/bin/env bash
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

[[ $# -eq 0 ]] || { echo "usage: $0" >&2; exit 2; }
[[ "$(id -u)" -eq 0 ]] || { echo "Install the Brio Nginx control receipt as root." >&2; exit 1; }

for binary in install mktemp python3 stat; do
  command -v "${binary}" >/dev/null 2>&1 || {
    echo "Missing Brio Nginx control-receipt installation dependency: ${binary}" >&2
    exit 1
  }
done

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
source_helper="${script_dir}/brio-nginx-control-receipt.py"
target=/usr/local/libexec/makepad/brio-nginx-control-receipt
[[ -f "${source_helper}" && ! -L "${source_helper}" ]] || {
  echo "Refusing a non-regular Brio Nginx control-receipt source." >&2
  exit 1
}
python3 - "${source_helper}" <<'PY'
import pathlib
import sys

source = pathlib.Path(sys.argv[1])
compile(source.read_text(encoding="utf-8"), str(source), "exec")
PY

install -d -o root -g root -m 0755 /usr/local/libexec/makepad
temporary=$(mktemp /usr/local/libexec/makepad/.brio-nginx-control-receipt.XXXXXXXX)
trap 'rm -f -- "${temporary:-}"' EXIT
install -o root -g root -m 0755 "${source_helper}" "${temporary}"
mv -Tf -- "${temporary}" "${target}"
temporary=""
[[ "$(stat -c '%U:%G:%a' "${target}")" == root:root:755 ]]
trap - EXIT

printf 'Installed the fixed Brio Nginx control receipt.\n'
