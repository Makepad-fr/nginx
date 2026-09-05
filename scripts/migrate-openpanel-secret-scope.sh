#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C
umask 077

# Audit or remove the one legacy repository-scoped deployment secret. GitHub
# never returns secret values, so deletion is permitted only after an operator
# has re-mirrored the canonical Proton field into the protected production
# environment. A newer environment-secret timestamp is evidence of that
# explicit write; the operator confirmation binds it to the documented source.

readonly repository="Makepad-fr/nginx"
readonly environment_name="production"
readonly secret_name="MAKEPAD_PROXY_OPENPANEL_APP_NETWORK"
readonly canonical_source="Nginx · production overlay names/openpanel"
readonly api_version="2022-11-28"

die() {
  printf '%s\n' "$*" >&2
  exit 1
}

mode=${1:-}
case "${mode}" in
  --check)
    [[ $# -eq 1 ]] || die "usage: migrate-openpanel-secret-scope.sh --check < GITHUB_REPOSITORY_ADMIN_TOKEN"
    ;;
  --delete-repository-duplicate)
    [[ $# -eq 2 && "$2" == "${canonical_source}" ]] || \
      die "deletion requires exact canonical-source confirmation: ${canonical_source}"
    ;;
  *)
    die "usage: migrate-openpanel-secret-scope.sh <--check|--delete-repository-duplicate 'Nginx · production overlay names/openpanel'> < GITHUB_REPOSITORY_ADMIN_TOKEN"
    ;;
esac

for command_name in gh python3; do
  command -v "${command_name}" >/dev/null || die "${command_name} is required"
done

IFS= read -r administration_token || die "a repository-administration token is required on standard input"
[[ "${administration_token}" =~ ^(github_pat_|ghp_|gho_|ghs_|ghu_)[A-Za-z0-9_]+$ ]] || \
  die "administration token has an invalid format"
export GH_TOKEN="${administration_token}"
unset administration_token

temporary_directory=$(mktemp -d)
cleanup() {
  unset GH_TOKEN
  if [[ -d "${temporary_directory}" && ! -L "${temporary_directory}" ]]; then
    find "${temporary_directory}" -depth -mindepth 1 -delete
    rmdir -- "${temporary_directory}"
  fi
}
trap cleanup EXIT

readonly repository_json="${temporary_directory}/repository.json"
readonly environment_json="${temporary_directory}/environment.json"
readonly policies_json="${temporary_directory}/policies.json"
readonly repository_secrets_json="${temporary_directory}/repository-secrets.json"
readonly environment_secrets_json="${temporary_directory}/environment-secrets.json"

fetch_policy_and_inventory() {
  gh api --header "X-GitHub-Api-Version: ${api_version}" \
    "repos/${repository}" >"${repository_json}"
  gh api --header "X-GitHub-Api-Version: ${api_version}" \
    "repos/${repository}/environments/${environment_name}" >"${environment_json}"
  gh api --header "X-GitHub-Api-Version: ${api_version}" \
    "repos/${repository}/environments/${environment_name}/deployment-branch-policies?per_page=100" \
    >"${policies_json}"
  gh api --header "X-GitHub-Api-Version: ${api_version}" \
    "repos/${repository}/actions/secrets?per_page=100" >"${repository_secrets_json}"
  gh api --header "X-GitHub-Api-Version: ${api_version}" \
    "repos/${repository}/environments/${environment_name}/secrets?per_page=100" \
    >"${environment_secrets_json}"
}

validate_inventory() {
  local validation_mode=$1
  python3 - "${repository_json}" "${environment_json}" "${policies_json}" \
    "${repository_secrets_json}" "${environment_secrets_json}" "${secret_name}" \
    "${validation_mode}" <<'PY'
import datetime
import json
import pathlib
import sys


def load(index):
    return json.loads(pathlib.Path(sys.argv[index]).read_text())


repository = load(1)
environment = load(2)
policy_payload = load(3)
repository_payload = load(4)
environment_payload = load(5)
secret_name = sys.argv[6]
mode = sys.argv[7]

if (
    repository.get("full_name") != "Makepad-fr/nginx"
    or repository.get("private") is not False
    or repository.get("default_branch") != "main"
):
    raise SystemExit("the secret-scope audit is not bound to public Makepad-fr/nginx main")
if environment.get("name") != "production" or environment.get("deployment_branch_policy") != {
    "protected_branches": False,
    "custom_branch_policies": True,
}:
    raise SystemExit("production must use exact custom deployment-branch protection")
policies = policy_payload.get("branch_policies", [])
if policy_payload.get("total_count", len(policies)) != len(policies):
    raise SystemExit("production deployment-branch policy response is truncated")
if [(value.get("name"), value.get("type")) for value in policies] != [("main", "branch")]:
    raise SystemExit("production must permit deployments only from the exact main branch")

repository_secrets = repository_payload.get("secrets", [])
environment_secrets = environment_payload.get("secrets", [])
if repository_payload.get("total_count", len(repository_secrets)) != len(repository_secrets):
    raise SystemExit("repository secret response is truncated")
if environment_payload.get("total_count", len(environment_secrets)) != len(environment_secrets):
    raise SystemExit("production environment secret response is truncated")
repository_matches = [value for value in repository_secrets if value.get("name") == secret_name]
environment_matches = [value for value in environment_secrets if value.get("name") == secret_name]
if len(repository_matches) > 1 or len(environment_matches) != 1:
    raise SystemExit(f"{secret_name} must exist exactly once in production and at most once at repository scope")

if mode == "check":
    if repository_matches:
        raise SystemExit(f"forbidden repository-level deployment secret still exists: {secret_name}")
    print("0")
    raise SystemExit(0)
if mode != "migration":
    raise SystemExit("invalid secret-scope validation mode")
if not repository_matches:
    print("0")
    raise SystemExit(0)

def timestamp(value, scope):
    raw = value.get("updated_at")
    if not isinstance(raw, str):
        raise SystemExit(f"{scope} secret has no update timestamp")
    try:
        return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise SystemExit(f"{scope} secret has an invalid update timestamp") from error


if timestamp(environment_matches[0], "production") <= timestamp(repository_matches[0], "repository"):
    raise SystemExit(
        "production secret was not explicitly refreshed after the repository duplicate; "
        "re-mirror the canonical Proton field before deletion"
    )
print("1")
PY
}

fetch_policy_and_inventory
if [[ "${mode}" == --check ]]; then
  validate_inventory check >/dev/null
  printf 'Proved %s is present only in the protected production environment.\n' "${secret_name}"
  exit 0
fi

repository_copy_count=$(validate_inventory migration)
if [[ "${repository_copy_count}" == 1 ]]; then
  gh api --method DELETE --header "X-GitHub-Api-Version: ${api_version}" \
    "repos/${repository}/actions/secrets/${secret_name}" >/dev/null
fi

# A successful DELETE response is not sufficient evidence. Re-read every
# boundary and require the environment copy to remain while repository scope is
# absent. This makes the operation idempotent without widening its target.
fetch_policy_and_inventory
validate_inventory check >/dev/null
printf 'Proved %s is present only in the protected production environment.\n' "${secret_name}"
