#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C
umask 077

# Idempotent crash/reboot recovery for one controller-owned JIT identity. The
# root-owned durable ledger supplies NGINX_CI_RESOURCE_ID before any launcher
# resource can be created. Local teardown deliberately precedes all networking.

readonly organization="Makepad-fr"
readonly api_version="2022-11-28"

die() {
  printf '%s\n' "$*" >&2
  exit 1
}

[[ $# -eq 1 && ( "$1" == local || "$1" == registration ) ]] || \
  die "usage: reconcile-nginx-ci-jit.sh <local|registration>"
readonly phase=$1
resource_id=${NGINX_CI_RESOURCE_ID:-}
[[ "${resource_id}" =~ ^[a-f0-9]{32}$ ]] || die "NGINX_CI_RESOURCE_ID must be 128-bit lowercase hex"

readonly vm_name="nginx-ci-${resource_id}"
readonly runner_name="nginx-ci-jit-${resource_id}"
readonly network_name="ngxci-${resource_id}"
readonly bridge_name="ngx${resource_id:0:10}"
readonly nft_table="ngxci_${resource_id}"

[[ "$(id -u)" -eq 0 ]] || die "JIT resource reconciliation must run as root on the dedicated CI hypervisor"

if [[ "${phase}" == registration ]]; then
  command -v gh >/dev/null || die "gh is required"
  IFS= read -r controller_token || die "a dedicated Launcher App installation token is required on standard input"
  [[ "${controller_token}" =~ ^ghs_[A-Za-z0-9_]{20,}$ ]] || die "the Launcher App token has an invalid format"

  runner_ids=$(GH_TOKEN="${controller_token}" gh api --paginate \
    --header "X-GitHub-Api-Version: ${api_version}" \
    "orgs/${organization}/actions/runners?per_page=100" \
    --jq ".runners[] | select(.name == \"${runner_name}\") | .id") || \
    die "failed to inspect the interrupted JIT runner registration"
  observed_runner_id=""
  runner_id_count=0
  while IFS= read -r candidate_runner_id; do
    [[ -z "${candidate_runner_id}" ]] && continue
    [[ "${candidate_runner_id}" =~ ^[1-9][0-9]*$ ]] || die "GitHub returned an invalid interrupted runner ID"
    runner_id_count=$((runner_id_count + 1))
    [[ "${runner_id_count}" -le 1 ]] || die "interrupted JIT runner registration identity is ambiguous"
    observed_runner_id=${candidate_runner_id}
  done < <(printf '%s\n' "${runner_ids}")
  if [[ -n "${observed_runner_id}" ]]; then
    GH_TOKEN="${controller_token}" gh api --method DELETE \
      --header "X-GitHub-Api-Version: ${api_version}" \
      "orgs/${organization}/actions/runners/${observed_runner_id}" >/dev/null || \
      die "failed to delete interrupted JIT runner registration ${observed_runner_id}"
  fi

  remaining_runner_ids=$(GH_TOKEN="${controller_token}" gh api --paginate \
    --header "X-GitHub-Api-Version: ${api_version}" \
    "orgs/${organization}/actions/runners?per_page=100" \
    --jq ".runners[] | select(.name == \"${runner_name}\") | .id") || \
    die "failed to prove interrupted JIT runner registration absence"
  unset controller_token
  while IFS= read -r runner_id; do
    [[ -z "${runner_id}" ]] && continue
    [[ "${runner_id}" =~ ^[1-9][0-9]*$ ]] || die "GitHub returned an invalid remaining runner ID"
    die "interrupted JIT runner registration still exists"
  done < <(printf '%s\n' "${remaining_runner_ids}")
  printf 'Proved interrupted JIT runner registration %s absent.\n' "${runner_name}"
  exit 0
fi

for command_name in find grep ip nft python3 stat virsh; do
  command -v "${command_name}" >/dev/null || die "${command_name} is required"
done

job_root=${NGINX_CI_JOB_ROOT:-/var/lib/makepad/nginx-ci/jobs}
python3 - "${job_root}" <<'PY'
import os
import pathlib
import stat
import sys

path = pathlib.Path(sys.argv[1])
if not path.is_absolute() or os.path.normpath(path) != str(path) or not str(path).startswith("/var/lib/makepad/nginx-ci/"):
    raise SystemExit("NGINX_CI_JOB_ROOT is outside the root-owned Nginx CI tree")
if ".." in path.parts:
    raise SystemExit("NGINX_CI_JOB_ROOT contains parent traversal")
for component in [pathlib.Path("/")] + list(reversed(list(path.parents)[:-1])) + [path]:
    if not os.path.lexists(component):
        break
    value = os.lstat(component)
    if not stat.S_ISDIR(value.st_mode) or stat.S_ISLNK(value.st_mode) or value.st_uid != 0 or value.st_mode & 0o022:
        raise SystemExit(f"insecure JIT job-root path component: {component}")
PY
readonly job_directory="${job_root}/nginx-ci-jit-${resource_id}"

cleanup_failed=false
domain_absent=false
network_absent=false
bridge_absent=false

# A failed control-plane query is uncertainty, never evidence of absence.
domain_list=$(virsh list --all --name 2>/dev/null) || domain_list_status=$?
domain_list_status=${domain_list_status:-0}
if [[ "${domain_list_status}" -ne 0 ]]; then
  printf 'Cannot query libvirt domains during interrupted-job reconciliation.\n' >&2
  cleanup_failed=true
elif grep -Fxq -- "${vm_name}" <<<"${domain_list}"; then
  virsh destroy "${vm_name}" >/dev/null 2>&1 || true
  virsh undefine "${vm_name}" --nvram >/dev/null 2>&1 || \
    virsh undefine "${vm_name}" >/dev/null 2>&1 || true
  remaining_domains=$(virsh list --all --name 2>/dev/null) || remaining_domains_status=$?
  remaining_domains_status=${remaining_domains_status:-0}
  if [[ "${remaining_domains_status}" -ne 0 ]] || grep -Fxq -- "${vm_name}" <<<"${remaining_domains}"; then
    printf 'Interrupted runner domain %s still exists.\n' "${vm_name}" >&2
    cleanup_failed=true
  else
    domain_absent=true
  fi
else
  domain_absent=true
fi

network_list=$(virsh net-list --all --name 2>/dev/null) || network_list_status=$?
network_list_status=${network_list_status:-0}
if [[ "${network_list_status}" -ne 0 ]]; then
  printf 'Cannot query libvirt networks during interrupted-job reconciliation.\n' >&2
  cleanup_failed=true
elif grep -Fxq -- "${network_name}" <<<"${network_list}"; then
  virsh net-destroy "${network_name}" >/dev/null 2>&1 || true
  virsh net-undefine "${network_name}" >/dev/null 2>&1 || true
  remaining_networks=$(virsh net-list --all --name 2>/dev/null) || remaining_networks_status=$?
  remaining_networks_status=${remaining_networks_status:-0}
  if [[ "${remaining_networks_status}" -ne 0 ]] || grep -Fxq -- "${network_name}" <<<"${remaining_networks}"; then
    printf 'Interrupted runner network %s still exists.\n' "${network_name}" >&2
    cleanup_failed=true
  else
    network_absent=true
  fi
else
  network_absent=true
fi

if [[ "${network_absent}" == true ]]; then
  bridge_list=$(ip -j link show 2>/dev/null | python3 -c '
import json
import sys

payload = json.load(sys.stdin)
if not isinstance(payload, list):
    raise SystemExit("link inventory must be a list")
for value in payload:
    name = value.get("ifname") if isinstance(value, dict) else None
    if not isinstance(name, str):
        raise SystemExit("link inventory contains an invalid name")
    print(name)
') || bridge_list_status=$?
  bridge_list_status=${bridge_list_status:-0}
  if [[ "${bridge_list_status}" -ne 0 ]]; then
    printf 'Cannot query host links during interrupted-job reconciliation.\n' >&2
    cleanup_failed=true
  elif grep -Fxq -- "${bridge_name}" <<<"${bridge_list}"; then
    ip link delete dev "${bridge_name}" >/dev/null 2>&1 || true
    remaining_bridges=$(ip -j link show 2>/dev/null | python3 -c '
import json
import sys

payload = json.load(sys.stdin)
if not isinstance(payload, list):
    raise SystemExit("link inventory must be a list")
for value in payload:
    name = value.get("ifname") if isinstance(value, dict) else None
    if not isinstance(name, str):
        raise SystemExit("link inventory contains an invalid name")
    print(name)
') || remaining_bridges_status=$?
    remaining_bridges_status=${remaining_bridges_status:-0}
    if [[ "${remaining_bridges_status}" -ne 0 ]] || grep -Fxq -- "${bridge_name}" <<<"${remaining_bridges}"; then
      printf 'Interrupted runner bridge %s still exists.\n' "${bridge_name}" >&2
      cleanup_failed=true
    else
      bridge_absent=true
    fi
  else
    bridge_absent=true
  fi
else
  printf 'Retaining the interrupted runner bridge until network absence is proven.\n' >&2
  cleanup_failed=true
fi

# Preserve containment whenever the VM, network, or bridge remains. Removing
# the firewall is safe only after all three are authoritatively absent.
if [[ "${domain_absent}" == true && "${network_absent}" == true && "${bridge_absent}" == true ]]; then
  nft_tables=$(nft list tables 2>/dev/null) || nft_tables_status=$?
  nft_tables_status=${nft_tables_status:-0}
  if [[ "${nft_tables_status}" -ne 0 ]]; then
    printf 'Cannot query nftables during interrupted-job reconciliation.\n' >&2
    cleanup_failed=true
  else
    if grep -Fxq -- "table inet ${nft_table}" <<<"${nft_tables}"; then
      nft delete table inet "${nft_table}" >/dev/null 2>&1 || true
    fi
    remaining_nft_tables=$(nft list tables 2>/dev/null) || remaining_nft_status=$?
    remaining_nft_status=${remaining_nft_status:-0}
    if [[ "${remaining_nft_status}" -ne 0 ]] || grep -Fxq -- "table inet ${nft_table}" <<<"${remaining_nft_tables}"; then
      printf 'Interrupted runner firewall table %s still exists.\n' "${nft_table}" >&2
      cleanup_failed=true
    fi
  fi
else
  printf 'Retaining the interrupted runner firewall until VM, network, and bridge absence is proven.\n' >&2
  cleanup_failed=true
fi

if [[ "${domain_absent}" == true ]]; then
  if [[ -e "${job_directory}" || -L "${job_directory}" ]]; then
    if [[ ! -d "${job_directory}" || -L "${job_directory}" || "${job_directory}" != "${job_root}"/nginx-ci-jit-* || "$(stat -c '%u:%a' "${job_directory}")" != 0:710 ]]; then
      printf 'Interrupted runner directory is not a safe controller-owned directory: %s\n' "${job_directory}" >&2
      cleanup_failed=true
    else
      find "${job_directory}" -depth -mindepth 1 -delete || cleanup_failed=true
      rmdir -- "${job_directory}" || cleanup_failed=true
    fi
  fi
  if [[ -e "${job_directory}" || -L "${job_directory}" ]]; then
    printf 'Interrupted runner disk or seed remains in %s.\n' "${job_directory}" >&2
    cleanup_failed=true
  fi
else
  printf 'Retaining interrupted runner disk and seed until domain absence is proven.\n' >&2
  cleanup_failed=true
fi

[[ "${cleanup_failed}" == false ]] || die "interrupted local JIT teardown remains uncertain"
printf 'Proved interrupted VM, network, bridge, firewall, disk, and seed for %s absent.\n' "${resource_id}"
