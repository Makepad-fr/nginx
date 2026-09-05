#!/usr/bin/env bash
set -Eeuo pipefail

readonly expected_node=app
readonly lease_user=brio-operation-lease
readonly lease_home=/var/lib/makepad/brio-operation-lease-user
readonly executable_directory=/usr/local/libexec/makepad
readonly lease_executable=${executable_directory}/brio-operation-lease
readonly dispatch_executable=${executable_directory}/brio-operation-lease-dispatch
readonly coordinator_executable=${executable_directory}/brio-operation-lease-coordinator
readonly config_directory=/etc/makepad/brio-operation-lease
readonly node_path=/etc/makepad/brio-operation-lease-node
readonly tmpfiles_path=/etc/tmpfiles.d/makepad-brio-operation-lease.conf
readonly sudoers_path=/etc/sudoers.d/brio-operation-lease-nginx
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
readonly repo_root

die() {
  printf 'Brio operation lease installer: %s\n' "$1" >&2
  exit 1
}

(( EUID == 0 )) || die 'run this installer as root on the Nginx host'
[[ $# == 5 ]] || die 'usage: install-brio-operation-lease.sh DEPLOY_USER PUBLIC_KEY_FILE COORDINATOR_CONFIG PRIVATE_KEY_FILE KNOWN_HOSTS_FILE'
deploy_user=$1
public_key_file=$2
coordinator_config=$3
private_key_file=$4
known_hosts_file=$5
[[ "${deploy_user}" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die 'deployment user is invalid'
id "${deploy_user}" >/dev/null 2>&1 || die 'deployment user does not exist'
[[ ! -e /run/makepad/brio-operation-lease/lease && ! -L /run/makepad/brio-operation-lease/lease ]] || \
  die 'install only when no Brio operation lease is active'

for source_file in \
  "${repo_root}/scripts/brio-operation-lease.py" \
  "${repo_root}/scripts/brio-operation-lease-dispatch.py" \
  "${repo_root}/scripts/brio-operation-lease-coordinator.py" \
  "${public_key_file}" "${coordinator_config}" "${private_key_file}" "${known_hosts_file}"; do
  [[ -f "${source_file}" && ! -L "${source_file}" ]] || die 'every installation source must be a regular non-symlink file'
done

read -r key_type key_body key_extra <"${public_key_file}"
[[ "${key_type}" == ssh-ed25519 && "${key_body}" =~ ^[A-Za-z0-9+/]+={0,3}$ && -z "${key_extra:-}" ]] || \
  die 'lease endpoint public key must be one comment-free Ed25519 key'
[[ $(wc -l <"${public_key_file}") -eq 1 ]] || die 'lease endpoint public key must contain exactly one line'
grep -Fqx -- '-----BEGIN OPENSSH PRIVATE KEY-----' <(head -n 1 "${private_key_file}") || \
  die 'coordinator private key must be OpenSSH format'

python3 - "${coordinator_config}" <<'PY'
import json
import pathlib
import re
import sys

value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(value, dict) or set(value) != {"nodes", "user", "version"}:
    raise SystemExit("invalid coordinator configuration schema")
if value["version"] != 1 or value["user"] != "brio-operation-lease":
    raise SystemExit("invalid coordinator identity")
nodes = value["nodes"]
if not isinstance(nodes, list) or [entry.get("name") for entry in nodes if isinstance(entry, dict)] != ["app", "identity", "database"]:
    raise SystemExit("coordinator nodes must use app, identity, database order")
host = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
for entry in nodes:
    if set(entry) != {"host", "name", "port"} or not isinstance(entry["host"], str) or host.fullmatch(entry["host"]) is None:
        raise SystemExit("invalid coordinator node")
    if not isinstance(entry["port"], int) or isinstance(entry["port"], bool) or not 1 <= entry["port"] <= 65535:
        raise SystemExit("invalid coordinator port")
PY

if ! id "${lease_user}" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "${lease_home}" --shell /bin/bash "${lease_user}"
fi
passwd --lock "${lease_user}" >/dev/null

install -d -o root -g root -m 0755 /usr/local/libexec "${executable_directory}" /etc/makepad
install -d -o root -g root -m 0700 "${config_directory}"
install -o root -g root -m 0755 "${repo_root}/scripts/brio-operation-lease.py" "${lease_executable}"
install -o root -g root -m 0755 "${repo_root}/scripts/brio-operation-lease-dispatch.py" "${dispatch_executable}"
install -o root -g root -m 0755 "${repo_root}/scripts/brio-operation-lease-coordinator.py" "${coordinator_executable}"

temporary_directory=$(mktemp -d)
[[ -d "${temporary_directory}" && ! -L "${temporary_directory}" ]] || die 'could not create private installation staging'
cleanup() {
  find "${temporary_directory}" -depth -mindepth 1 -delete
  rmdir "${temporary_directory}"
}
trap cleanup EXIT
printf '%s\n' "${expected_node}" >"${temporary_directory}/node"
printf '%s\n' \
  'd /run/makepad 0755 root root -' \
  'd /run/makepad/brio-operation-lease 0700 root root -' \
  'f /run/makepad/brio-operation-lease/guard 0600 root root -' \
  >"${temporary_directory}/tmpfiles"
printf 'restrict,command="/usr/bin/sudo -n %s" %s %s\n' \
  "${dispatch_executable}" "${key_type}" "${key_body}" >"${temporary_directory}/authorized_keys"
printf 'Defaults!%s env_keep += "SSH_ORIGINAL_COMMAND"\n' "${dispatch_executable}" >"${temporary_directory}/sudoers"
printf '%s ALL=(root) NOPASSWD: %s\n' "${lease_user}" "${dispatch_executable}" >>"${temporary_directory}/sudoers"
printf '%s ALL=(root) NOPASSWD: %s *, %s status * deployment\n' \
  "${deploy_user}" "${coordinator_executable}" "${lease_executable}" >>"${temporary_directory}/sudoers"
chmod 0440 "${temporary_directory}/sudoers"
visudo -cf "${temporary_directory}/sudoers" >/dev/null

install -o root -g root -m 0600 "${temporary_directory}/node" "${node_path}"
install -o root -g root -m 0600 "${coordinator_config}" "${config_directory}/coordinator.json"
install -o root -g root -m 0600 "${private_key_file}" "${config_directory}/id_ed25519"
install -o root -g root -m 0600 "${known_hosts_file}" "${config_directory}/known_hosts"
install -o root -g root -m 0644 "${temporary_directory}/tmpfiles" "${tmpfiles_path}"
install -o root -g root -m 0440 "${temporary_directory}/sudoers" "${sudoers_path}"
install -d -o "${lease_user}" -g "${lease_user}" -m 0700 "${lease_home}/.ssh"
install -o "${lease_user}" -g "${lease_user}" -m 0600 \
  "${temporary_directory}/authorized_keys" "${lease_home}/.ssh/authorized_keys"
systemd-tmpfiles --create "${tmpfiles_path}"

[[ "$(stat -c '%U:%G:%a' /run/makepad/brio-operation-lease)" == root:root:700 ]] || die 'runtime directory metadata is unsafe'
[[ "$(stat -c '%U:%G:%a' /run/makepad/brio-operation-lease/guard)" == root:root:600 ]] || die 'runtime guard metadata is unsafe'
[[ ! -L /run/makepad/brio-operation-lease/guard ]] || die 'runtime guard must not be a symlink'
[[ ! -e /run/makepad/brio-operation-lease/lease && ! -L /run/makepad/brio-operation-lease/lease ]] || \
  die 'install only when no Brio operation lease is active'
for installed in "${node_path}" "${config_directory}/coordinator.json" "${config_directory}/id_ed25519" "${config_directory}/known_hosts"; do
  [[ "$(stat -c '%U:%G:%a' "${installed}")" == root:root:600 && ! -L "${installed}" ]] || die 'installed root configuration metadata is unsafe'
done

printf 'Installed the Brio operation lease endpoint and coordinator for node %s.\n' "${expected_node}"
