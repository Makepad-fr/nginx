#!/usr/bin/env bash
set -Eeuo pipefail

die() {
  printf 'Brio operation lease remote client: %s\n' "$1" >&2
  exit 64
}

[[ $# == 2 ]] || die 'usage: brio-operation-lease-remote.sh acquire|status|release OWNER'
readonly action=$1
readonly owner=$2
[[ "${action}" =~ ^(acquire|status|release)$ ]] || die 'action is invalid'
[[ "${owner}" =~ ^[0-9a-f]{64}$ ]] || die 'owner is invalid'

: "${BRIO_LEASE_REMOTE_HOST:?set BRIO_LEASE_REMOTE_HOST}"
: "${BRIO_LEASE_REMOTE_USER:?set BRIO_LEASE_REMOTE_USER}"
: "${BRIO_LEASE_SSH_DIRECTORY:?set BRIO_LEASE_SSH_DIRECTORY}"
[[ "${BRIO_LEASE_REMOTE_HOST}" =~ ^[A-Za-z0-9][A-Za-z0-9.-]{0,252}[A-Za-z0-9]$ ]] || die 'remote host is invalid'
[[ "${BRIO_LEASE_REMOTE_USER}" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die 'remote user is invalid'
readonly remote_port=${BRIO_LEASE_REMOTE_PORT:-22}
if [[ ! "${remote_port}" =~ ^[1-9][0-9]{0,4}$ ]] || (( remote_port > 65535 )); then
  die 'remote port is invalid'
fi
[[ "${BRIO_LEASE_SSH_DIRECTORY}" == /* && -d "${BRIO_LEASE_SSH_DIRECTORY}" && ! -L "${BRIO_LEASE_SSH_DIRECTORY}" ]] || die 'SSH directory is invalid'
readonly private_key=${BRIO_LEASE_SSH_DIRECTORY}/id_ed25519
readonly known_hosts=${BRIO_LEASE_SSH_DIRECTORY}/known_hosts
for source in "${private_key}" "${known_hosts}"; do
  [[ -f "${source}" && ! -L "${source}" && -r "${source}" ]] || die 'SSH material is unavailable or unsafe'
  [[ "$(stat -c '%a' "${source}")" == 600 ]] || die 'SSH material must use mode 0600'
done

exec /usr/bin/ssh \
  -F /dev/null -T \
  -o BatchMode=yes \
  -o IdentitiesOnly=yes \
  -o PasswordAuthentication=no \
  -o KbdInteractiveAuthentication=no \
  -o StrictHostKeyChecking=yes \
  -o "UserKnownHostsFile=${known_hosts}" \
  -o GlobalKnownHostsFile=/dev/null \
  -o ConnectTimeout=10 \
  -o ConnectionAttempts=1 \
  -i "${private_key}" \
  -p "${remote_port}" \
  "${BRIO_LEASE_REMOTE_USER}@${BRIO_LEASE_REMOTE_HOST}" \
  "/usr/bin/sudo -n /usr/local/libexec/makepad/brio-operation-lease-coordinator ${action} ${owner} deployment"
