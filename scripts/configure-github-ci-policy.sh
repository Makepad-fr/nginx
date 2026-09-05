#!/usr/bin/env bash
set -euo pipefail

# Reconcile the repository-side trust boundary. A required result can be
# emitted only by the Checks App after the hypervisor's Ed25519 teardown proof
# has been verified against authoritative GitHub run, job, PR, and runner data.

readonly repository="Makepad-fr/nginx"
readonly api_version="2022-11-28"

die() {
  printf '%s\n' "$*" >&2
  exit 1
}

[[ $# -eq 4 ]] || die "usage: configure-github-ci-policy.sh PR_CHECK_APP_ID LAUNCHER_APP_SENDER_ID APPROVED_BASE_SHA256 ATTESTATION_PUBLIC_KEY_FILE < GITHUB_REPOSITORY_ADMIN_TOKEN"
readonly app_id=$1
readonly launcher_sender_id=$2
readonly approved_base_digest=$3
readonly attestation_public_key_file=$4
for command_name in gh grep openssl python3; do
  command -v "${command_name}" >/dev/null || die "${command_name} is required"
done
[[ "${app_id}" =~ ^[1-9][0-9]*$ ]] || die "PR_CHECK_APP_ID must be a positive integer"
[[ "${launcher_sender_id}" =~ ^[1-9][0-9]*$ ]] || die "LAUNCHER_APP_SENDER_ID must be a positive integer"
[[ "${approved_base_digest}" =~ ^[a-f0-9]{64}$ ]] || die "APPROVED_BASE_SHA256 must be lowercase SHA-256"
[[ -f "${attestation_public_key_file}" && ! -L "${attestation_public_key_file}" ]] || die "ATTESTATION_PUBLIC_KEY_FILE must be a regular file"
openssl pkey -pubin -in "${attestation_public_key_file}" -text -noout 2>/dev/null | grep -Fq ED25519 || die "attestation public key must be Ed25519"

IFS= read -r administration_token || die "a repository-administration token is required on standard input"
[[ "${administration_token}" =~ ^(github_pat_|ghp_|gho_|ghs_|ghu_)[A-Za-z0-9_]+$ ]] || die "administration token has an invalid format"
export GH_TOKEN="${administration_token}"
unset administration_token

temporary_files=()
cleanup() {
  for temporary_file in "${temporary_files[@]}"; do
    if [[ -f "${temporary_file}" && ! -L "${temporary_file}" ]]; then
      rm -f -- "${temporary_file}"
    fi
  done
  unset GH_TOKEN
}
trap cleanup EXIT

repository_json=$(mktemp)
protection_json=$(mktemp)
signatures_json=$(mktemp)
variables_json=$(mktemp)
launcher_sender_json=$(mktemp)
repository_secrets_json=$(mktemp)
temporary_files+=("${repository_json}" "${protection_json}" "${signatures_json}" "${variables_json}" "${launcher_sender_json}" "${repository_secrets_json}")

gh api --header "X-GitHub-Api-Version: ${api_version}" "repos/${repository}" >"${repository_json}"
python3 - "${repository_json}" <<'PY'
import json
import pathlib
import sys

repository = json.loads(pathlib.Path(sys.argv[1]).read_text())
if (
    repository.get("full_name") != "Makepad-fr/nginx"
    or repository.get("private") is not False
    or repository.get("default_branch") != "main"
    or repository.get("has_issues") is not True
):
    raise SystemExit("Nginx must remain public, issue-enabled, and use main as its default branch")
PY

gh api --header "X-GitHub-Api-Version: ${api_version}" \
  "user/${launcher_sender_id}" >"${launcher_sender_json}"
python3 - "${launcher_sender_json}" "${launcher_sender_id}" <<'PY'
import json
import pathlib
import sys

sender = json.loads(pathlib.Path(sys.argv[1]).read_text())
if (
    str(sender.get("id")) != sys.argv[2]
    or sender.get("type") != "Bot"
    or not str(sender.get("login", "")).endswith("[bot]")
    or sender.get("site_admin") is not False
):
    raise SystemExit("LAUNCHER_APP_SENDER_ID does not identify a non-site-admin GitHub App bot")
PY

configure_environment() {
  local environment_name=$1
  local environment_json environment_branches_json
  environment_json=$(mktemp)
  environment_branches_json=$(mktemp)
  temporary_files+=("${environment_json}" "${environment_branches_json}")

  printf '%s' '{"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}' | \
    gh api --method PUT --header "X-GitHub-Api-Version: ${api_version}" \
      "repos/${repository}/environments/${environment_name}" --input - >/dev/null
  gh api --header "X-GitHub-Api-Version: ${api_version}" \
    "repos/${repository}/environments/${environment_name}/deployment-branch-policies?per_page=100" \
    >"${environment_branches_json}"
  while IFS= read -r policy_id; do
    [[ -z "${policy_id}" ]] || gh api --method DELETE --header "X-GitHub-Api-Version: ${api_version}" \
      "repos/${repository}/environments/${environment_name}/deployment-branch-policies/${policy_id}" >/dev/null
  done < <(python3 - "${environment_branches_json}" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text())
policies = payload.get("branch_policies", [])
if payload.get("total_count", len(policies)) > len(policies):
    raise SystemExit("more than 100 deployment branch policies require explicit pagination")
for policy in policies:
    if (policy.get("name"), policy.get("type")) != ("main", "branch"):
        policy_id = policy.get("id")
        if not isinstance(policy_id, int) or policy_id <= 0:
            raise SystemExit("deployment policy has no valid ID")
        print(policy_id)
PY
  )
  main_policy_count=$(python3 - "${environment_branches_json}" <<'PY'
import json
import pathlib
import sys

policies = json.loads(pathlib.Path(sys.argv[1]).read_text()).get("branch_policies", [])
print(sum((policy.get("name"), policy.get("type")) == ("main", "branch") for policy in policies))
PY
  )
  if [[ "${main_policy_count}" == 0 ]]; then
    printf '%s' '{"name":"main","type":"branch"}' | gh api --method POST \
      --header "X-GitHub-Api-Version: ${api_version}" \
      "repos/${repository}/environments/${environment_name}/deployment-branch-policies" --input - >/dev/null
  elif [[ "${main_policy_count}" != 1 ]]; then
    die "${environment_name} has duplicate main policies"
  fi

  gh api --header "X-GitHub-Api-Version: ${api_version}" \
    "repos/${repository}/environments/${environment_name}" >"${environment_json}"
  gh api --header "X-GitHub-Api-Version: ${api_version}" \
    "repos/${repository}/environments/${environment_name}/deployment-branch-policies?per_page=100" \
    >"${environment_branches_json}"
  python3 - "${environment_json}" "${environment_branches_json}" "${environment_name}" <<'PY'
import json
import pathlib
import sys

environment = json.loads(pathlib.Path(sys.argv[1]).read_text())
payload = json.loads(pathlib.Path(sys.argv[2]).read_text())
policies = payload.get("branch_policies", [])
if environment.get("name") != sys.argv[3] or environment.get("deployment_branch_policy") != {
    "protected_branches": False,
    "custom_branch_policies": True,
}:
    raise SystemExit(f"{sys.argv[3]} must use exact custom deployment branches")
if payload.get("total_count", len(policies)) != 1:
    raise SystemExit(f"{sys.argv[3]} must contain exactly one deployment branch policy")
if [(policy.get("name"), policy.get("type")) for policy in policies] != [("main", "branch")]:
    raise SystemExit(f"{sys.argv[3]} must permit only main")
PY
}

configure_environment release-nginx

# Numeric App identity, reviewed image digest, and the public half of the
# teardown key are non-secret trust anchors. The private Ed25519 key remains
# only on the hypervisor; the Checks App private key remains environment-scoped.
gh api --header "X-GitHub-Api-Version: ${api_version}" \
  "repos/${repository}/actions/variables?per_page=100" >"${variables_json}"
python3 - "${variables_json}" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text())
variables = payload.get("variables", [])
if payload.get("total_count", len(variables)) > len(variables):
    raise SystemExit("more than 100 repository variables require explicit pagination")
PY

set_repository_variable() {
  local name=$1
  local value_file=$2
  local count payload_file method endpoint
  count=$(python3 - "${variables_json}" "${name}" <<'PY'
import json
import pathlib
import sys

items = json.loads(pathlib.Path(sys.argv[1]).read_text()).get("variables", [])
print(sum(item.get("name") == sys.argv[2] for item in items))
PY
  )
  [[ "${count}" == 0 || "${count}" == 1 ]] || die "${name} exists more than once"
  payload_file=$(mktemp)
  temporary_files+=("${payload_file}")
  python3 - "${name}" "${value_file}" "${payload_file}" <<'PY'
import json
import pathlib
import sys

value = pathlib.Path(sys.argv[2]).read_text().strip()
pathlib.Path(sys.argv[3]).write_text(json.dumps({"name": sys.argv[1], "value": value}, separators=(",", ":")))
PY
  if [[ "${count}" == 0 ]]; then
    method=POST
    endpoint="repos/${repository}/actions/variables"
  else
    method=PATCH
    endpoint="repos/${repository}/actions/variables/${name}"
  fi
  gh api --method "${method}" --header "X-GitHub-Api-Version: ${api_version}" \
    "${endpoint}" --input "${payload_file}" >/dev/null
}

launcher_sender_value=$(mktemp)
check_app_id_value=$(mktemp)
base_digest_value=$(mktemp)
temporary_files+=("${launcher_sender_value}" "${check_app_id_value}" "${base_digest_value}")
printf '%s\n' "${launcher_sender_id}" >"${launcher_sender_value}"
printf '%s\n' "${app_id}" >"${check_app_id_value}"
printf '%s\n' "${approved_base_digest}" >"${base_digest_value}"
set_repository_variable NGINX_CI_LAUNCHER_APP_SENDER_ID "${launcher_sender_value}"
set_repository_variable NGINX_PR_CHECK_APP_ID "${check_app_id_value}"
set_repository_variable NGINX_CI_APPROVED_BASE_IMAGE_SHA256 "${base_digest_value}"
set_repository_variable NGINX_CI_ATTESTATION_PUBLIC_KEY "${attestation_public_key_file}"

protection_payload=$(python3 - "${app_id}" <<'PY'
import json
import sys

print(json.dumps({
    "required_status_checks": {
        "strict": True,
        "checks": [{"context": "policy-and-render", "app_id": int(sys.argv[1])}],
    },
    "enforce_admins": True,
    "required_pull_request_reviews": {
        "dismiss_stale_reviews": True,
        "require_code_owner_reviews": True,
        "required_approving_review_count": 1,
        "require_last_push_approval": True,
        "bypass_pull_request_allowances": {"users": [], "teams": [], "apps": []},
    },
    "restrictions": None,
    "required_linear_history": True,
    "allow_force_pushes": False,
    "allow_deletions": False,
    "block_creations": False,
    "required_conversation_resolution": True,
    "lock_branch": False,
    "allow_fork_syncing": False,
}, separators=(",", ":")))
PY
)
printf '%s' "${protection_payload}" | gh api --method PUT \
  --header "X-GitHub-Api-Version: ${api_version}" \
  "repos/${repository}/branches/main/protection" --input - >/dev/null
# Signed commits are configured through a separate branch-protection endpoint.
gh api --method POST --header "X-GitHub-Api-Version: ${api_version}" \
  "repos/${repository}/branches/main/protection/required_signatures" >/dev/null
printf '%s' '{"default_workflow_permissions":"read","can_approve_pull_request_reviews":false}' | \
  gh api --method PUT --header "X-GitHub-Api-Version: ${api_version}" \
    "repos/${repository}/actions/permissions/workflow" --input - >/dev/null

gh api --header "X-GitHub-Api-Version: ${api_version}" \
  "repos/${repository}/branches/main/protection" >"${protection_json}"
gh api --header "X-GitHub-Api-Version: ${api_version}" \
  "repos/${repository}/branches/main/protection/required_signatures" >"${signatures_json}"
python3 - "${protection_json}" "${signatures_json}" "${app_id}" <<'PY'
import json
import pathlib
import sys

protection = json.loads(pathlib.Path(sys.argv[1]).read_text())
signatures = json.loads(pathlib.Path(sys.argv[2]).read_text())
app_id = int(sys.argv[3])
checks = protection.get("required_status_checks", {}).get("checks", [])
reviews = protection.get("required_pull_request_reviews", {})
if [(check.get("context"), check.get("app_id")) for check in checks] != [("policy-and-render", app_id)]:
    raise SystemExit(f"main has unexpected App-bound checks: {checks!r}")
required_true = (
    (protection.get("required_status_checks", {}).get("strict"), "strict status checks"),
    (protection.get("enforce_admins", {}).get("enabled"), "administrator enforcement"),
    (reviews.get("dismiss_stale_reviews"), "stale-review dismissal"),
    (reviews.get("require_code_owner_reviews"), "code-owner review"),
    (reviews.get("require_last_push_approval"), "last-push approval"),
    (protection.get("required_linear_history", {}).get("enabled"), "linear history"),
    (protection.get("required_conversation_resolution", {}).get("enabled"), "conversation resolution"),
)
for enabled, name in required_true:
    if enabled is not True:
        raise SystemExit(f"main does not enforce {name}")
if reviews.get("required_approving_review_count") != 1:
    raise SystemExit("main must require exactly one approving review")
allowances = reviews.get("bypass_pull_request_allowances", {})
if any(allowances.get(kind, []) for kind in ("users", "teams", "apps")):
    raise SystemExit("main unexpectedly permits pull-request review bypasses")
if protection.get("allow_force_pushes", {}).get("enabled") is not False:
    raise SystemExit("main unexpectedly permits force pushes")
if protection.get("allow_deletions", {}).get("enabled") is not False:
    raise SystemExit("main unexpectedly permits deletion")
if signatures.get("enabled") is not True:
    raise SystemExit("main does not require signed commits")
PY

workflow_permissions=$(gh api --header "X-GitHub-Api-Version: ${api_version}" \
  "repos/${repository}/actions/permissions/workflow")
python3 - "${workflow_permissions}" <<'PY'
import json
import sys

permissions = json.loads(sys.argv[1])
if permissions != {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False}:
    raise SystemExit(f"unexpected Actions workflow-token policy: {permissions!r}")
PY

[[ "$(gh api --header "X-GitHub-Api-Version: ${api_version}" \
  "repos/${repository}/actions/variables/NGINX_CI_LAUNCHER_APP_SENDER_ID" --jq .value)" == "${launcher_sender_id}" ]] || \
  die "Launcher App sender ID failed repository-variable read-back"
[[ "$(gh api --header "X-GitHub-Api-Version: ${api_version}" \
  "repos/${repository}/actions/variables/NGINX_PR_CHECK_APP_ID" --jq .value)" == "${app_id}" ]] || \
  die "Checks App ID failed repository-variable read-back"
[[ "$(gh api --header "X-GitHub-Api-Version: ${api_version}" \
  "repos/${repository}/actions/variables/NGINX_CI_APPROVED_BASE_IMAGE_SHA256" --jq .value)" == "${approved_base_digest}" ]] || \
  die "approved base-image digest failed repository-variable read-back"
observed_public_key=$(mktemp)
temporary_files+=("${observed_public_key}")
gh api --header "X-GitHub-Api-Version: ${api_version}" \
  "repos/${repository}/actions/variables/NGINX_CI_ATTESTATION_PUBLIC_KEY" --jq .value >"${observed_public_key}"
python3 - "${attestation_public_key_file}" "${observed_public_key}" <<'PY'
import pathlib
import sys

if pathlib.Path(sys.argv[1]).read_text().strip() != pathlib.Path(sys.argv[2]).read_text().strip():
    raise SystemExit("attestation public key failed repository-variable read-back")
PY

# Deployment credentials must never silently fall back from a protected
# environment to repository scope. The dedicated migration helper performs the
# guarded deletion; this policy reconciler only fails closed if it remains.
gh api --header "X-GitHub-Api-Version: ${api_version}" \
  "repos/${repository}/actions/secrets?per_page=100" >"${repository_secrets_json}"
python3 - "${repository_secrets_json}" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text())
secrets = payload.get("secrets", [])
if payload.get("total_count", len(secrets)) != len(secrets):
    raise SystemExit("repository secret response is truncated")
forbidden = [
    item.get("name")
    for item in secrets
    if item.get("name") == "MAKEPAD_PROXY_OPENPANEL_APP_NETWORK"
]
if forbidden:
    raise SystemExit(
        "repository-level MAKEPAD_PROXY_OPENPANEL_APP_NETWORK is forbidden; "
        "run the guarded Proton-first migration"
    )
PY

printf 'Protected %s main with signed teardown, Checks App %s, and Launcher sender %s.\n' \
  "${repository}" "${app_id}" "${launcher_sender_id}"
