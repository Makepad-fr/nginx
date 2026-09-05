#!/usr/bin/env bash

# A caller may have enabled xtrace before invoking this script. Disable it
# before a Proton field can be materialized. Field values move only through
# anonymous pipes: never argv, exported environment, shell variables, or files.
set +x
set -Eeuo pipefail
umask 077
IFS=$' \t\n'
export LANG=C
export LC_ALL=C
unset DEBUG GH_DEBUG PASS_CLI_DEBUG

readonly repository=Makepad-fr/nginx
readonly repository_id=1200300778
readonly vault=Makepad
readonly api_version=2022-11-28
readonly required_check_context=policy-and-render
readonly max_value_bytes=49152
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
readonly repo_root
readonly inventory=${repo_root}/deploy/credential-inventory.json
readonly provider_contract=${repo_root}/deploy/github-app-contracts.json
readonly provider_contract_validator=${repo_root}/scripts/validate-github-provider-contract.py
readonly environment_policy=${repo_root}/scripts/github_environment_policy.py

usage() {
  printf '%s\n' \
    'usage: sync-github-credentials.sh [--check|--sync] [--scope SCOPE]' \
    '' \
    'Scopes: production, release-nginx, repository-variables, host-boundaries.' \
    '  --check  Audit names and policy only; never read a Proton field value (default).' \
    '  --sync   Preflight and stream exactly one writable scope to GitHub.'
}

die() {
  printf 'credential sync: %s\n' "$*" >&2
  exit 1
}

mode=check
mode_selected=0
selected_scope=
while (( $# > 0 )); do
  case "$1" in
    --check|--sync)
      (( mode_selected == 0 )) || die 'select exactly one mode'
      mode=${1#--}
      mode_selected=1
      ;;
    --scope)
      (( $# >= 2 )) || die '--scope requires a value'
      [[ -z "${selected_scope}" ]] || die '--scope may be supplied only once'
      selected_scope=$2
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unsupported argument: $1"
      ;;
  esac
  shift
done

case "${selected_scope}" in
  ''|host-boundaries|production|release-nginx|repository-variables) ;;
  *) die 'scope is not in the immutable Nginx inventory' ;;
esac
if [[ "${mode}" == sync && -z "${selected_scope}" ]]; then
  die '--sync requires one explicit --scope to bound the write set'
fi
if [[ "${mode}" == sync && "${selected_scope}" == host-boundaries ]]; then
  die 'host-boundaries is audit-only; install root/operator material outside GitHub'
fi

for command_name in pass-cli gh python3 sort grep mktemp find; do
  command -v "${command_name}" >/dev/null || die "${command_name} is required"
done
[[ -f "${inventory}" && ! -L "${inventory}" ]] || die 'credential inventory is missing or is a symbolic link'
[[ -f "${provider_contract}" && ! -L "${provider_contract}" ]] || die 'GitHub provider contract is missing or is a symbolic link'
[[ -f "${provider_contract_validator}" && ! -L "${provider_contract_validator}" ]] || \
  die 'GitHub provider contract validator is missing or is a symbolic link'
[[ -f "${environment_policy}" && ! -L "${environment_policy}" ]] || die 'environment policy helper is missing or is a symbolic link'
PYTHONDONTWRITEBYTECODE=1 python3 "${provider_contract_validator}" "${provider_contract}" || \
  die 'GitHub provider settings do not match the immutable reviewed contract'

status_root=$(mktemp -d "${TMPDIR:-/tmp}/nginx-credential-sync.XXXXXXXX")
[[ -d "${status_root}" && ! -L "${status_root}" ]] || die 'could not create a private status directory'
chmod 0700 "${status_root}"
readonly status_root
readonly github_entries_file=${status_root}/github-entries.tsv
readonly host_entries_file=${status_root}/host-entries.tsv
readonly selected_sources_file=${status_root}/selected-sources.tsv
readonly proton_items_file=${status_root}/proton-items.txt
readonly repository_secrets_file=${status_root}/github-repository-secret.txt
readonly repository_variables_file=${status_root}/github-repository-variable.txt
readonly repository_id_file=${status_root}/repository-id.txt
readonly repository_policy_file=${status_root}/repository-policy.json
readonly required_check_app_id_file=${status_root}/required-check-app-id.txt
readonly source_hmac_key_file=${status_root}/source-hmac.key

python3 - "${source_hmac_key_file}" <<'PY'
import os
import pathlib
import sys

pathlib.Path(sys.argv[1]).write_bytes(os.urandom(32))
PY

cleanup() {
  if [[ -n "${status_root:-}" && "${status_root}" == "${TMPDIR:-/tmp}"/nginx-credential-sync.* && -d "${status_root}" && ! -L "${status_root}" ]]; then
    find "${status_root}" -depth -mindepth 1 -delete
    rmdir -- "${status_root}"
  fi
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

python3 - "${inventory}" "${repository}" "${vault}" "${selected_scope}" \
  "${github_entries_file}" "${host_entries_file}" "${selected_sources_file}" <<'PY'
import json
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
expected_repository = sys.argv[2]
expected_vault = sys.argv[3]
selected_scope = sys.argv[4]
github_output = pathlib.Path(sys.argv[5])
host_output = pathlib.Path(sys.argv[6])
sources_output = pathlib.Path(sys.argv[7])

try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as error:
    raise SystemExit(f"credential inventory is not valid JSON: {error}") from error

expected_top = {
    "schemaVersion",
    "repository",
    "vault",
    "githubEnvironmentEntries",
    "repositoryVariables",
    "hostEntries",
}
if not isinstance(payload, dict) or set(payload) != expected_top:
    raise SystemExit("credential inventory has unexpected top-level keys")
if payload["schemaVersion"] != 1:
    raise SystemExit("unsupported credential inventory schema")
repository = payload["repository"]
if not isinstance(repository, dict) or repository != {
    "id": 1200300778,
    "fullName": expected_repository,
    "visibility": "public",
    "defaultBranch": "main",
    "fork": False,
}:
    raise SystemExit("credential inventory targets an unexpected repository policy")
if payload["vault"] != expected_vault:
    raise SystemExit("credential inventory targets an unexpected vault")

environments = payload["githubEnvironmentEntries"]
repository_variables = payload["repositoryVariables"]
host_entries = payload["hostEntries"]
if not isinstance(environments, list) or not environments:
    raise SystemExit("credential inventory must contain environment entries")
if not isinstance(repository_variables, list) or not repository_variables:
    raise SystemExit("credential inventory must contain repository variables")
if not isinstance(host_entries, list) or not host_entries:
    raise SystemExit("credential inventory must contain host entries")

allowed_environment_scopes = {"production", "release-nginx"}
allowed_kinds = {"secret", "variable"}
allowed_boundaries = {
    "ci-hypervisor-root-file",
    "ci-hypervisor-root-setting",
    "host-root-file",
    "operator-stdin",
    "operator-verification",
}
destination_pattern = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
field_pattern = re.compile(r"^[A-Za-z][A-Za-z0-9_ -]{0,127}$")


def valid_text(value: object, limit: int = 256) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and not any(character in value for character in "\t\r\n")
    )


def valid_source(entry: dict[str, object], offset: int, label: str) -> None:
    if not valid_text(entry["item"], 128):
        raise SystemExit(f"{label} {offset} has an invalid Proton item title")
    field = entry["field"]
    if not isinstance(field, str) or not field_pattern.fullmatch(field):
        raise SystemExit(f"{label} {offset} has an invalid Proton field name")


github_lines: list[str] = []
host_lines: list[str] = []
selected_sources: set[str] = set()
seen_github: set[tuple[str, str, str]] = set()
seen_host: set[tuple[str, str]] = set()
environment_counts = {scope: 0 for scope in allowed_environment_scopes}
observed_environment_entries: set[tuple[str, str, str, str, str]] = set()
expected_environment_entries = {
    ("production", "secret", "DEPLOY_SSH_PRIVATE_KEY", "Nginx · production deployment", "private_key"),
    ("production", "secret", "DEPLOY_SSH_KNOWN_HOSTS", "Nginx · production deployment", "known_hosts"),
    ("production", "secret", "MAKEPAD_PROXY_PROD_APP_NETWORK", "Nginx · production overlay names", "prod"),
    ("production", "secret", "MAKEPAD_PROXY_CANARY_APP_NETWORK", "Nginx · production overlay names", "canary"),
    ("production", "secret", "MAKEPAD_PROXY_ALERTECONSO_APP_NETWORK", "Nginx · production overlay names", "alerteconso"),
    ("production", "secret", "MAKEPAD_PROXY_LE_PETIT_COIN_APP_NETWORK", "Nginx · production overlay names", "le_petit_coin"),
    ("production", "secret", "MAKEPAD_PROXY_VIF_APP_NETWORK", "Nginx · production overlay names", "vif"),
    ("production", "secret", "MAKEPAD_PROXY_MAKEPAD_LANDING_APP_NETWORK", "Nginx · production overlay names", "makepad_landing"),
    ("production", "secret", "MAKEPAD_PROXY_EVIDELLA_APP_NETWORK", "Nginx · production overlay names", "evidella"),
    ("production", "secret", "MAKEPAD_PROXY_OPENPANEL_APP_NETWORK", "Nginx · production overlay names", "openpanel"),
    ("production", "secret", "MAKEPAD_PROXY_RUNTRACE_APP_NETWORK", "Nginx · production overlay names", "runtrace"),
    ("production", "secret", "MAKEPAD_PROXY_BRIO_STAGING_APP_NETWORK", "Nginx · production overlay names", "brio_staging"),
    ("production", "secret", "MAKEPAD_PROXY_MAILDEV_BRIO_STAGING_WEB_NETWORK", "Nginx · production overlay names", "maildev_brio_staging_web"),
    ("production", "variable", "NGINX_DEPLOY_HOST", "Nginx · production deployment", "host"),
    ("production", "variable", "NGINX_DEPLOY_PORT", "Nginx · production deployment", "port"),
    ("production", "variable", "NGINX_DEPLOY_USER", "Nginx · production deployment", "user"),
    ("production", "variable", "NGINX_DEPLOY_REMOTE_DIR", "Nginx · production deployment", "remote_dir"),
    ("production", "variable", "NGINX_DEPLOY_STACK_NAME", "Nginx · production deployment", "stack_name"),
    ("release-nginx", "secret", "NGINX_PR_CHECK_APP_PRIVATE_KEY", "Nginx · CI Checks App", "private_key"),
}

for offset, entry in enumerate(environments):
    if not isinstance(entry, dict) or set(entry) != {"scope", "kind", "destination", "item", "field"}:
        raise SystemExit(f"environment entry {offset} has unexpected keys")
    scope = entry["scope"]
    kind = entry["kind"]
    destination = entry["destination"]
    if scope not in allowed_environment_scopes or kind not in allowed_kinds:
        raise SystemExit(f"environment entry {offset} has an invalid classification")
    if not isinstance(destination, str) or not destination_pattern.fullmatch(destination):
        raise SystemExit(f"environment entry {offset} has an invalid destination")
    valid_source(entry, offset, "environment entry")
    identity = (scope, kind, destination)
    if identity in seen_github:
        raise SystemExit(f"duplicate GitHub destination: {scope}/{kind}/{destination}")
    seen_github.add(identity)
    observed_environment_entries.add((scope, kind, destination, entry["item"], entry["field"]))
    environment_counts[scope] += 1
    if not selected_scope or selected_scope == scope:
        github_lines.append("\t".join((scope, "environment", kind, destination, entry["item"], entry["field"])))
        selected_sources.add(entry["item"])

if any(count == 0 for count in environment_counts.values()):
    raise SystemExit("every approved environment must have at least one inventory entry")
if observed_environment_entries != expected_environment_entries:
    raise SystemExit("GitHub environment tuple mapping does not match the reviewed Nginx contract")

seen_repository: set[str] = set()
observed_repository_entries: set[tuple[str, str, str]] = set()
expected_repository_entries = {
    ("NGINX_PR_CHECK_APP_ID", "Nginx · CI Checks App", "app_id"),
    ("NGINX_CI_LAUNCHER_APP_SENDER_ID", "Nginx · CI Launcher App", "bot_user_id"),
    ("NGINX_CI_APPROVED_BASE_IMAGE_SHA256", "Nginx · CI base image approval", "qcow2_sha256"),
    ("NGINX_CI_ATTESTATION_PUBLIC_KEY", "Nginx · CI hypervisor attestation", "ed25519_public_key"),
}
for offset, entry in enumerate(repository_variables):
    if not isinstance(entry, dict) or set(entry) != {"destination", "item", "field"}:
        raise SystemExit(f"repository-variable entry {offset} has unexpected keys")
    destination = entry["destination"]
    if not isinstance(destination, str) or not destination_pattern.fullmatch(destination):
        raise SystemExit(f"repository-variable entry {offset} has an invalid destination")
    valid_source(entry, offset, "repository-variable entry")
    if destination in seen_repository:
        raise SystemExit(f"duplicate repository variable: {destination}")
    seen_repository.add(destination)
    observed_repository_entries.add((destination, entry["item"], entry["field"]))
    if not selected_scope or selected_scope == "repository-variables":
        github_lines.append("\t".join(("repository-variables", "repository", "variable", destination, entry["item"], entry["field"])))
        selected_sources.add(entry["item"])

if observed_repository_entries != expected_repository_entries:
    raise SystemExit("repository-variable tuple mapping does not match the reviewed Nginx contract")

observed_host_entries: set[tuple[str, str, str, str]] = set()
expected_host_entries = {
    ("operator-verification", "Checks App private-key fingerprint", "Nginx · CI Checks App", "private_key_fingerprint"),
    ("ci-hypervisor-root-setting", "/etc/makepad/nginx-ci/controller.env:NGINX_CI_LAUNCHER_APP_ID", "Nginx · CI Launcher App", "app_id"),
    ("ci-hypervisor-root-setting", "/etc/makepad/nginx-ci/controller.env:NGINX_CI_LAUNCHER_APP_INSTALLATION_ID", "Nginx · CI Launcher App", "installation_id"),
    ("ci-hypervisor-root-file", "/etc/makepad/nginx-ci/launcher-app-private-key.pem", "Nginx · CI Launcher App", "private_key"),
    ("operator-verification", "Launcher App private-key fingerprint", "Nginx · CI Launcher App", "private_key_fingerprint"),
    ("ci-hypervisor-root-file", "/etc/makepad/nginx-ci/attestation-private-key.pem", "Nginx · CI hypervisor attestation", "ed25519_private_key"),
    ("operator-verification", "Attestation public-key fingerprint", "Nginx · CI hypervisor attestation", "public_key_fingerprint"),
    ("ci-hypervisor-root-setting", "/etc/makepad/nginx-ci/controller.env:NGINX_CI_BASE_IMAGE_SHA256", "Nginx · CI base image approval", "qcow2_sha256"),
    ("ci-hypervisor-root-setting", "/etc/makepad/nginx-ci/controller.env:NGINX_CI_REPOSITORY_ID", "Nginx · CI base image approval", "repository_id"),
    ("host-root-file", "NGINX_HOST_ALERT_URL_FILE", "Nginx · host control alert webhook", "url"),
    ("operator-stdin", "configure-github-ci-policy.sh standard input", "Nginx · GitHub repository policy bootstrap", "repository_admin_token"),
    ("operator-stdin", "configure-runner-groups.sh standard input", "Nginx · GitHub runner policy bootstrap", "organization_runner_admin_token"),
}
for offset, entry in enumerate(host_entries):
    if not isinstance(entry, dict) or set(entry) != {"boundary", "destination", "item", "field"}:
        raise SystemExit(f"host entry {offset} has unexpected keys")
    boundary = entry["boundary"]
    destination = entry["destination"]
    if boundary not in allowed_boundaries:
        raise SystemExit(f"host entry {offset} has an invalid boundary")
    if not valid_text(destination):
        raise SystemExit(f"host entry {offset} has an invalid destination")
    valid_source(entry, offset, "host entry")
    identity = (boundary, destination)
    if identity in seen_host:
        raise SystemExit(f"duplicate host destination: {boundary}/{destination}")
    seen_host.add(identity)
    observed_host_entries.add((boundary, destination, entry["item"], entry["field"]))
    if not selected_scope or selected_scope == "host-boundaries":
        host_lines.append("\t".join((boundary, destination, entry["item"], entry["field"])))
        selected_sources.add(entry["item"])

if observed_host_entries != expected_host_entries:
    raise SystemExit("host/operator tuple mapping does not match the reviewed Nginx contract")

github_output.write_text("\n".join(github_lines) + ("\n" if github_lines else ""), encoding="utf-8")
host_output.write_text("\n".join(host_lines) + ("\n" if host_lines else ""), encoding="utf-8")
sources_output.write_text("\n".join(sorted(selected_sources)) + "\n", encoding="utf-8")
PY

[[ -s "${selected_sources_file}" ]] || die 'the selected inventory is empty'
if [[ "${mode}" == sync && ! -s "${github_entries_file}" ]]; then
  die 'the selected scope has no GitHub write destinations'
fi

declare -a entry_scope=()
declare -a entry_target=()
declare -a entry_kind=()
declare -a entry_destination=()
declare -a entry_item=()
declare -a entry_field=()
while IFS=$'\t' read -r scope target kind destination item field; do
  index=${#entry_scope[@]}
  entry_scope[index]=${scope}
  entry_target[index]=${target}
  entry_kind[index]=${kind}
  entry_destination[index]=${destination}
  entry_item[index]=${item}
  entry_field[index]=${field}
done <"${github_entries_file}"

pass-cli test >/dev/null || die 'Proton Pass is not authenticated'
GH_PROMPT_DISABLED=1 gh auth status >/dev/null 2>&1 || die 'GitHub CLI is not authenticated'

load_proton_item_names() {
  if ! pass-cli item list --vault-name "${vault}" --filter-state active --output json |
    python3 -c '
import json
import pathlib
import sys

expected = set(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines())
try:
    payload = json.load(sys.stdin)
except json.JSONDecodeError as error:
    raise SystemExit(f"invalid Proton item inventory: {error}") from error
if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
    raise SystemExit("invalid Proton item inventory")
for item in payload["items"]:
    if not isinstance(item, dict) or not isinstance(item.get("title"), str):
        raise SystemExit("invalid Proton item-name entry")
    if item["title"] in expected:
        print(item["title"])
' "${selected_sources_file}" >"${proton_items_file}"
  then
    die 'could not read the Proton Pass item-name inventory'
  fi
  sort -o "${proton_items_file}" "${proton_items_file}"
}

normalize_github_names() {
  local label=$1
  python3 -c '
import re
import sys

pattern = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
values = [line.rstrip("\r\n") for line in sys.stdin]
if any(not pattern.fullmatch(value) for value in values):
    raise SystemExit(f"{sys.argv[1]} contains an invalid destination name")
if len(values) != len(set(values)):
    raise SystemExit(f"{sys.argv[1]} contains duplicate destination names")
for value in sorted(values):
    print(value)
' "${label}"
}

repository_policy_valid() {
  local metadata_file=$1
  local protection_file=$2
  local workflow_file=$3
  local phase=$4
  python3 - "${metadata_file}" "${protection_file}" "${workflow_file}" \
    "${repository_id_file}" "${repository_policy_file}" \
    "${required_check_app_id_file}" "${phase}" "${repository_id}" \
    "${required_check_context}" <<'PY'
import json
import pathlib
import sys


def load(path: str, label: str) -> dict:
    try:
        value = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"{label} is unreadable or invalid: {error}") from error
    if not isinstance(value, dict):
        raise SystemExit(f"{label} must be an object")
    return value


repository = load(sys.argv[1], "repository metadata")
protection = load(sys.argv[2], "main protection")
workflow = load(sys.argv[3], "Actions workflow-token policy")
expected_repository_id = int(sys.argv[8])
expected_context = sys.argv[9]
observed_repository_id = repository.get("id")
owner = repository.get("owner")
if (
    isinstance(observed_repository_id, bool)
    or observed_repository_id != expected_repository_id
    or repository.get("full_name") != "Makepad-fr/nginx"
    or repository.get("private") is not False
    or repository.get("visibility") != "public"
    or repository.get("default_branch") != "main"
    or repository.get("fork") is not False
    or repository.get("archived") is not False
    or repository.get("disabled") is not False
    or not isinstance(owner, dict)
    or owner.get("login") != "Makepad-fr"
    or owner.get("type") != "Organization"
):
    raise SystemExit("repository identity or exact public, non-fork, default-main policy is invalid")

required_checks = protection.get("required_status_checks")
if not isinstance(required_checks, dict) or required_checks.get("strict") is not True:
    raise SystemExit("main must require strict status checks")
checks = required_checks.get("checks")
contexts = required_checks.get("contexts")
if not isinstance(checks, list) or len(checks) != 1 or contexts != [expected_context]:
    raise SystemExit("main must have one exact required-check context")
check = checks[0]
check_app_id = check.get("app_id") if isinstance(check, dict) else None
if (
    not isinstance(check, dict)
    or set(check) != {"context", "app_id"}
    or check.get("context") != expected_context
    or isinstance(check_app_id, bool)
    or not isinstance(check_app_id, int)
    or check_app_id <= 0
):
    raise SystemExit("main required check is not bound to an exact GitHub App")

reviews = protection.get("required_pull_request_reviews")
if not isinstance(reviews, dict):
    raise SystemExit("main must require pull-request reviews")
for setting in ("dismiss_stale_reviews", "require_code_owner_reviews", "require_last_push_approval"):
    if reviews.get(setting) is not True:
        raise SystemExit(f"main does not enforce {setting}")
review_count = reviews.get("required_approving_review_count")
if isinstance(review_count, bool) or review_count != 1:
    raise SystemExit("main must require exactly one approving review")
allowances = reviews.get("bypass_pull_request_allowances", {})
if not isinstance(allowances, dict) or any(allowances.get(kind, []) != [] for kind in ("users", "teams", "apps")):
    raise SystemExit("main unexpectedly permits pull-request review bypasses")

for setting in (
    "enforce_admins",
    "required_signatures",
    "required_linear_history",
    "required_conversation_resolution",
):
    node = protection.get(setting)
    if not isinstance(node, dict) or node.get("enabled") is not True:
        raise SystemExit(f"main does not enforce {setting}")
for setting in ("allow_force_pushes", "allow_deletions"):
    node = protection.get(setting)
    if not isinstance(node, dict) or node.get("enabled") is not False:
        raise SystemExit(f"main unexpectedly enables {setting}")

expected_workflow = {
    "default_workflow_permissions": "read",
    "can_approve_pull_request_reviews": False,
}
if workflow != expected_workflow:
    raise SystemExit("Actions workflow-token policy is not exact read-only/no-approval")

policy_snapshot = {
    "repository_id": observed_repository_id,
    "required_check": {"context": expected_context, "app_id": check_app_id},
    "required_approving_review_count": 1,
    "workflow": expected_workflow,
}
identity_path = pathlib.Path(sys.argv[4])
policy_path = pathlib.Path(sys.argv[5])
check_app_path = pathlib.Path(sys.argv[6])
phase = sys.argv[7]
serialized_policy = json.dumps(policy_snapshot, separators=(",", ":"), sort_keys=True)
if phase == "initial":
    identity_path.write_text(f"{observed_repository_id}\n", encoding="ascii")
    policy_path.write_text(f"{serialized_policy}\n", encoding="utf-8")
    check_app_path.write_text(f"{check_app_id}\n", encoding="ascii")
elif identity_path.read_text(encoding="ascii").strip() != str(observed_repository_id):
    raise SystemExit("repository numeric identity changed during sync")
elif policy_path.read_text(encoding="utf-8").strip() != serialized_policy:
    raise SystemExit("protected-main or Actions policy changed during sync")
elif check_app_path.read_text(encoding="ascii").strip() != str(check_app_id):
    raise SystemExit("required-check App identity changed during sync")
PY
}

environment_identity_valid() {
  local metadata_file=$1
  local branches_file=$2
  local environment_name=$3
  local phase=$4
  local identity_file=${status_root}/environment-${environment_name}-identity.json
  python3 - "${metadata_file}" "${branches_file}" "${identity_file}" \
    "${environment_name}" "${phase}" <<'PY'
import json
import pathlib
import sys

environment = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
branches = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
environment_id = environment.get("id")
if (
    isinstance(environment_id, bool)
    or not isinstance(environment_id, int)
    or environment_id <= 0
    or environment.get("name") != sys.argv[4]
):
    raise SystemExit("environment identity is invalid")
rules = environment.get("protection_rules")
if not isinstance(rules, list):
    raise SystemExit("environment protection-rule inventory is invalid")
rule_ids = []
for rule in rules:
    rule_id = rule.get("id") if isinstance(rule, dict) else None
    rule_type = rule.get("type") if isinstance(rule, dict) else None
    if (
        isinstance(rule_id, bool)
        or not isinstance(rule_id, int)
        or rule_id <= 0
        or not isinstance(rule_type, str)
        or not rule_type
    ):
        raise SystemExit("environment protection-rule identity is invalid")
    rule_ids.append((rule_type, rule_id))
if len(rule_ids) != len(set(rule_ids)) or len(rule_ids) != len({rule_id for _, rule_id in rule_ids}):
    raise SystemExit("environment protection-rule identity is ambiguous")

branch_policies = branches.get("branch_policies")
if branches.get("total_count") != 1 or not isinstance(branch_policies, list) or len(branch_policies) != 1:
    raise SystemExit("deployment branch-policy identity is ambiguous")
branch_policy = branch_policies[0]
branch_policy_id = branch_policy.get("id") if isinstance(branch_policy, dict) else None
if (
    isinstance(branch_policy_id, bool)
    or not isinstance(branch_policy_id, int)
    or branch_policy_id <= 0
    or branch_policy.get("name") != "main"
    or branch_policy.get("type") != "branch"
):
    raise SystemExit("deployment branch-policy identity is invalid")

identity = {
    "environment_id": environment_id,
    "protection_rule_ids": sorted(rule_ids),
    "main_branch_policy_id": branch_policy_id,
}
serialized = json.dumps(identity, separators=(",", ":"), sort_keys=True)
identity_path = pathlib.Path(sys.argv[3])
if sys.argv[5] == "initial":
    identity_path.write_text(f"{serialized}\n", encoding="utf-8")
elif identity_path.read_text(encoding="utf-8").strip() != serialized:
    raise SystemExit("environment or branch-policy numeric identity changed during sync")
PY
}

scope_selected() {
  local scope=$1
  [[ -z "${selected_scope}" || "${selected_scope}" == "${scope}" ]]
}

destination_expected() {
  local target=$1
  local scope=$2
  local kind=$3
  local destination=$4
  local index
  for index in "${!entry_scope[@]}"; do
    if [[ "${entry_target[index]}" == "${target}" && "${entry_scope[index]}" == "${scope}" && \
      "${entry_kind[index]}" == "${kind}" && "${entry_destination[index]}" == "${destination}" ]]; then
      return 0
    fi
  done
  return 1
}

protection_errors=0
load_github_state() {
  local phase=$1
  local repository_json=${status_root}/repository-${phase}.json
  local protection_json=${status_root}/main-protection-${phase}.json
  local workflow_json=${status_root}/actions-workflow-policy-${phase}.json
  local environment environment_json branches_json snapshot_json kind output_file

  protection_errors=0
  find "${status_root}" -maxdepth 1 -type f -name 'github-current-*.txt' -delete

  if ! GH_PROMPT_DISABLED=1 gh api --header "X-GitHub-Api-Version: ${api_version}" \
    "repos/${repository}" >"${repository_json}" 2>/dev/null ||
    ! GH_PROMPT_DISABLED=1 gh api --header "X-GitHub-Api-Version: ${api_version}" \
      "repos/${repository}/branches/main/protection" >"${protection_json}" 2>/dev/null ||
    ! GH_PROMPT_DISABLED=1 gh api --header "X-GitHub-Api-Version: ${api_version}" \
      "repos/${repository}/actions/permissions/workflow" >"${workflow_json}" 2>/dev/null ||
    ! repository_policy_valid "${repository_json}" "${protection_json}" \
      "${workflow_json}" "${phase}"; then
    printf 'REPOSITORY name=%s policy=invalid-or-changed\n' "${repository}"
    ((protection_errors += 1))
  else
    printf 'REPOSITORY name=%s policy=exact-public-nonfork-default-main-protected-actions-read identity=stable\n' \
      "${repository}"
  fi

  if ! GH_PROMPT_DISABLED=1 gh secret list --repo "${repository}" --app actions \
    --json name --jq '.[].name' | normalize_github_names 'repository secrets' \
    >"${repository_secrets_file}"; then
    die 'could not list repository-level Actions secret names'
  fi

  if scope_selected repository-variables; then
    if ! GH_PROMPT_DISABLED=1 gh variable list --repo "${repository}" \
      --json name --jq '.[].name' | normalize_github_names 'repository variables' \
      >"${repository_variables_file}"; then
      die 'could not list repository-level variable names'
    fi
  else
    : >"${repository_variables_file}"
  fi

  for environment in production release-nginx; do
    scope_selected "${environment}" || continue
    environment_json=${status_root}/environment-${environment}-${phase}.json
    branches_json=${status_root}/environment-${environment}-branches-${phase}.json
    snapshot_json=${status_root}/environment-${environment}-preserved.json
    if ! GH_PROMPT_DISABLED=1 gh api --header "X-GitHub-Api-Version: ${api_version}" \
      "repos/${repository}/environments/${environment}" >"${environment_json}" 2>/dev/null ||
      ! GH_PROMPT_DISABLED=1 gh api --header "X-GitHub-Api-Version: ${api_version}" \
      "repos/${repository}/environments/${environment}/deployment-branch-policies?per_page=100" \
      >"${branches_json}" 2>/dev/null; then
      printf 'ENVIRONMENT name=%s protection=missing-or-unreadable\n' "${environment}"
      ((protection_errors += 1))
    elif [[ "${phase}" == initial ]]; then
      if environment_identity_valid "${environment_json}" "${branches_json}" \
        "${environment}" "${phase}" &&
        python3 "${environment_policy}" snapshot "${environment_json}" "${environment}" >"${snapshot_json}" &&
        python3 "${environment_policy}" verify "${environment_json}" "${branches_json}" \
          "${environment}" "${snapshot_json}"; then
        printf 'ENVIRONMENT name=%s protection=exact-main-preserved identity=stable\n' "${environment}"
      else
        printf 'ENVIRONMENT name=%s protection=invalid-or-ambiguous\n' "${environment}"
        ((protection_errors += 1))
      fi
    elif [[ ! -s "${snapshot_json}" ]] ||
      ! environment_identity_valid "${environment_json}" "${branches_json}" \
        "${environment}" "${phase}" ||
      ! python3 "${environment_policy}" verify "${environment_json}" "${branches_json}" \
        "${environment}" "${snapshot_json}"; then
      printf 'ENVIRONMENT name=%s protection=changed-or-invalid\n' "${environment}"
      ((protection_errors += 1))
    else
      printf 'ENVIRONMENT name=%s protection=exact-main-preserved identity=stable\n' "${environment}"
    fi

    for kind in secret variable; do
      output_file=${status_root}/github-current-environment-${environment}-${kind}.txt
      if [[ "${kind}" == secret ]]; then
        if ! GH_PROMPT_DISABLED=1 gh secret list --repo "${repository}" --env "${environment}" \
          --app actions --json name --jq '.[].name' |
          normalize_github_names "${environment} secrets" >"${output_file}"; then
          die "could not list ${environment} secret names"
        fi
      elif ! GH_PROMPT_DISABLED=1 gh variable list --repo "${repository}" --env "${environment}" \
        --json name --jq '.[].name' |
        normalize_github_names "${environment} variables" >"${output_file}"; then
        die "could not list ${environment} variable names"
      fi
    done
  done
}

missing_required_sources=0
missing_required_destinations=0
unexpected_destinations=0
report_status() {
  local item item_count status index target scope kind destination destination_file actual_name
  local boundary host_destination field
  missing_required_sources=0
  missing_required_destinations=0
  unexpected_destinations=0

  while IFS= read -r item; do
    [[ -n "${item}" ]] || continue
    item_count=$(grep -Fxc -- "${item}" "${proton_items_file}" || true)
    case "${item_count}" in
      1) status=present ;;
      0) status=missing ;;
      *) status=ambiguous ;;
    esac
    printf 'SOURCE_ITEM title=%s status=%s\n' "${item}" "${status}"
    [[ "${status}" == present ]] || ((missing_required_sources += 1))
  done <"${selected_sources_file}"

  for index in "${!entry_scope[@]}"; do
    target=${entry_target[index]}
    scope=${entry_scope[index]}
    kind=${entry_kind[index]}
    destination=${entry_destination[index]}
    if [[ "${target}" == repository ]]; then
      destination_file=${repository_variables_file}
    else
      destination_file=${status_root}/github-current-environment-${scope}-${kind}.txt
    fi
    if [[ -f "${destination_file}" ]] && grep -Fqx -- "${destination}" "${destination_file}"; then
      status=present
    else
      status=missing
      ((missing_required_destinations += 1))
    fi
    printf 'DESTINATION scope=%s target=%s kind=%s name=%s status=%s\n' \
      "${scope}" "${target}" "${kind}" "${destination}" "${status}"
  done

  while IFS= read -r actual_name; do
    [[ -n "${actual_name}" ]] || continue
    printf 'UNEXPECTED_DESTINATION scope=repository kind=secret name=%s status=forbidden-broad-secret\n' \
      "${actual_name}"
    ((unexpected_destinations += 1))
  done <"${repository_secrets_file}"

  if scope_selected repository-variables; then
    while IFS= read -r actual_name; do
      [[ -n "${actual_name}" ]] || continue
      if ! destination_expected repository repository-variables variable "${actual_name}"; then
        printf 'UNEXPECTED_DESTINATION scope=repository-variables kind=variable name=%s status=legacy-or-unmanaged\n' \
          "${actual_name}"
        ((unexpected_destinations += 1))
      fi
    done <"${repository_variables_file}"
  fi

  for scope in production release-nginx; do
    scope_selected "${scope}" || continue
    for kind in secret variable; do
      destination_file=${status_root}/github-current-environment-${scope}-${kind}.txt
      [[ -f "${destination_file}" ]] || continue
      while IFS= read -r actual_name; do
        [[ -n "${actual_name}" ]] || continue
        if ! destination_expected environment "${scope}" "${kind}" "${actual_name}"; then
          printf 'UNEXPECTED_DESTINATION scope=%s kind=%s name=%s status=legacy-or-unmanaged\n' \
            "${scope}" "${kind}" "${actual_name}"
          ((unexpected_destinations += 1))
        fi
      done <"${destination_file}"
    done
  done

  while IFS=$'\t' read -r boundary host_destination item field; do
    [[ -n "${boundary}" ]] || continue
    printf 'HOST_DESTINATION boundary=%s name=%s source=%s/%s status=operator-managed\n' \
      "${boundary}" "${host_destination}" "${item}" "${field}"
  done <"${host_entries_file}"

  printf 'SUMMARY required_source_issues=%d required_destination_missing=%d unexpected_destinations=%d protection_errors=%d\n' \
    "${missing_required_sources}" "${missing_required_destinations}" \
    "${unexpected_destinations}" "${protection_errors}"
}

filter_value() {
  local operation=$1
  local destination=$2
  local snapshot_file=$3
  local target=${4:-}
  local scope=${5:-}
  local kind=${6:-}
  python3 -c '
import base64
import binascii
import hashlib
import hmac
import os
import pathlib
import re
import subprocess
import sys

operation = sys.argv[1]
destination = sys.argv[2]
maximum = int(sys.argv[3])
key_path = pathlib.Path(sys.argv[4])
snapshot_path = pathlib.Path(sys.argv[5])
value = sys.stdin.buffer.read(maximum + 2)
if len(value) > maximum + 1:
    raise SystemExit("value exceeds the GitHub size bound")
if value.endswith(b"\n"):
    value = value[:-1]
    if value.endswith(b"\r"):
        value = value[:-1]
if not value:
    raise SystemExit("value is empty")
if len(value) > maximum:
    raise SystemExit("value exceeds the GitHub size bound")
if b"\x00" in value:
    raise SystemExit("value contains NUL")

try:
    text = value.decode("utf-8")
except UnicodeDecodeError as error:
    raise SystemExit("value is not valid UTF-8") from error
if any(character in text for character in "\x01\x02\x03\x04\x05\x06\x07\x08\x0b\x0c\x0e\x0f"):
    raise SystemExit("value contains a forbidden control character")


def validate_pem(labels):
    lines = text.splitlines()
    matching = [label for label in labels if lines and lines[0] == f"-----BEGIN {label}-----"]
    if len(matching) != 1 or lines[-1] != f"-----END {matching[0]}-----":
        raise SystemExit("private key has an unexpected PEM envelope")
    try:
        decoded = base64.b64decode("".join(lines[1:-1]), validate=True)
    except (binascii.Error, ValueError) as error:
        raise SystemExit("private key has invalid base64") from error
    if len(decoded) < 16:
        raise SystemExit("private key payload is too short")


if destination == "DEPLOY_SSH_PRIVATE_KEY":
    validate_pem(("OPENSSH PRIVATE KEY", "PRIVATE KEY", "RSA PRIVATE KEY"))
elif destination == "NGINX_PR_CHECK_APP_PRIVATE_KEY":
    validate_pem(("PRIVATE KEY", "RSA PRIVATE KEY"))
elif destination == "DEPLOY_SSH_KNOWN_HOSTS":
    for line in text.splitlines():
        parts = line.split()
        algorithm_offset = 2 if parts and parts[0].startswith("@") else 1
        if len(parts) <= algorithm_offset + 1 or not re.fullmatch(
            r"(?:ssh-|ecdsa-|sk-)[A-Za-z0-9@._+-]+", parts[algorithm_offset]
        ):
            raise SystemExit("known_hosts contains an invalid entry")
        try:
            base64.b64decode(parts[algorithm_offset + 1], validate=True)
        except (binascii.Error, ValueError) as error:
            raise SystemExit("known_hosts contains invalid key data") from error
elif destination.startswith("MAKEPAD_PROXY_") and destination.endswith("_APP_NETWORK") or destination == "MAKEPAD_PROXY_MAILDEV_BRIO_STAGING_WEB_NETWORK":
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", text):
        raise SystemExit("Docker network name is invalid")
    exact_networks = {
        "MAKEPAD_PROXY_BRIO_STAGING_APP_NETWORK": "makepad_brio_staging_app",
        "MAKEPAD_PROXY_MAILDEV_BRIO_STAGING_WEB_NETWORK": "makepad_brio_staging_maildev_web",
    }
    if destination in exact_networks and text != exact_networks[destination]:
        raise SystemExit("Brio network name is not canonical")
elif destination in {
    "NGINX_DEPLOY_HOST": "135.181.141.31",
    "NGINX_DEPLOY_PORT": "22",
    "NGINX_DEPLOY_USER": "makepad",
    "NGINX_DEPLOY_REMOTE_DIR": "/srv/makepad/nginx",
    "NGINX_DEPLOY_STACK_NAME": "makepad-edge",
}:
    expected = {
        "NGINX_DEPLOY_HOST": "135.181.141.31",
        "NGINX_DEPLOY_PORT": "22",
        "NGINX_DEPLOY_USER": "makepad",
        "NGINX_DEPLOY_REMOTE_DIR": "/srv/makepad/nginx",
        "NGINX_DEPLOY_STACK_NAME": "makepad-edge",
    }[destination]
    if text != expected:
        raise SystemExit("deployment coordinate does not match protected policy")
elif destination in {"NGINX_PR_CHECK_APP_ID", "NGINX_CI_LAUNCHER_APP_SENDER_ID"}:
    if not re.fullmatch(r"[1-9][0-9]*", text):
        raise SystemExit("GitHub numeric identity is invalid")
    if destination == "NGINX_PR_CHECK_APP_ID":
        try:
            protected_app_id = pathlib.Path(sys.argv[10]).read_text(encoding="ascii").strip()
        except OSError as error:
            raise SystemExit(f"protected-main App identity is unreadable: {error}") from error
        if text != protected_app_id:
            raise SystemExit("Checks App ID does not match protected main")
elif destination == "NGINX_CI_APPROVED_BASE_IMAGE_SHA256":
    if not re.fullmatch(r"[a-f0-9]{64}", text):
        raise SystemExit("approved base-image digest is invalid")
elif destination == "NGINX_CI_ATTESTATION_PUBLIC_KEY":
    lines = text.splitlines()
    if not lines or lines[0] != "-----BEGIN PUBLIC KEY-----" or lines[-1] != "-----END PUBLIC KEY-----":
        raise SystemExit("attestation public key has an unexpected PEM envelope")
    try:
        der = base64.b64decode("".join(lines[1:-1]), validate=True)
    except (binascii.Error, ValueError) as error:
        raise SystemExit("attestation public key has invalid base64") from error
    if len(der) != 44 or not der.startswith(bytes.fromhex("302a300506032b6570032100")):
        raise SystemExit("attestation public key is not Ed25519 SubjectPublicKeyInfo")
else:
    raise SystemExit("destination has no approved value validator")

try:
    key = key_path.read_bytes()
except OSError as error:
    raise SystemExit(f"source comparison key is unreadable: {error}") from error
if len(key) != 32:
    raise SystemExit("source comparison key is invalid")
observed = hmac.new(key, value, hashlib.sha256).hexdigest()
if operation == "snapshot":
    snapshot_path.write_text(f"{observed}\n", encoding="ascii")
elif operation in {"verify", "set-verified"}:
    try:
        expected = snapshot_path.read_text(encoding="ascii").strip()
    except OSError as error:
        raise SystemExit(f"source snapshot is unreadable: {error}") from error
    if not re.fullmatch(r"[a-f0-9]{64}", expected) or not hmac.compare_digest(observed, expected):
        raise SystemExit("source value changed after preflight")
    if operation == "set-verified":
        target, scope, kind, repository = sys.argv[6:10]
        if target == "environment" and kind == "secret":
            command = [
                "gh", "secret", "set", destination, "--repo", repository,
                "--env", scope, "--app", "actions",
            ]
        elif target == "environment" and kind == "variable":
            command = ["gh", "variable", "set", destination, "--repo", repository, "--env", scope]
        elif target == "repository" and kind == "variable" and scope == "repository-variables":
            command = ["gh", "variable", "set", destination, "--repo", repository]
        else:
            raise SystemExit("unsupported GitHub write tuple")
        environment = os.environ.copy()
        environment["GH_PROMPT_DISABLED"] = "1"
        completed = subprocess.run(
            command,
            input=value,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            check=False,
        )
        if completed.returncode != 0:
            raise SystemExit("GitHub rejected the exact destination")
else:
    raise SystemExit("invalid filter operation")
' "${operation}" "${destination}" "${max_value_bytes}" \
    "${source_hmac_key_file}" "${snapshot_file}" "${target}" "${scope}" \
    "${kind}" "${repository}" "${required_check_app_id_file}"
}

extract_github_variable_value() {
  local destination=$1
  python3 -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except json.JSONDecodeError as error:
    raise SystemExit(f"GitHub variable read-back is invalid JSON: {error}") from error
if not isinstance(payload, dict) or payload.get("name") != sys.argv[1]:
    raise SystemExit("GitHub variable read-back has the wrong identity")
value = payload.get("value")
if not isinstance(value, str):
    raise SystemExit("GitHub variable read-back has no string value")
sys.stdout.buffer.write(value.encode("utf-8"))
' "${destination}"
}

load_proton_item_names
load_github_state initial
report_status

if [[ "${mode}" == check ]]; then
  if (( protection_errors > 0 || missing_required_sources > 0 || missing_required_destinations > 0 )); then
    exit 1
  fi
  if (( unexpected_destinations > 0 )); then
    exit 2
  fi
  exit 0
fi

(( protection_errors == 0 )) || die 'refusing to sync an invalid or changed repository/environment policy'
(( unexpected_destinations == 0 )) || die 'refusing to sync while broad, legacy, or unmanaged GitHub names remain'
(( missing_required_sources == 0 )) || die 'required Proton Pass source items are missing or ambiguous'
ulimit -c 0 || die 'could not disable process core dumps before handling credential values'

# Read and validate every selected value before the first GitHub mutation. A
# per-run keyed fingerprint, not the value, is retained for exact comparisons.
for index in "${!entry_scope[@]}"; do
  source_snapshot=${status_root}/source-${index}.hmac
  if ! pass-cli item view --vault-name "${vault}" \
    --item-title "${entry_item[index]}" --field "${entry_field[index]}" 2>/dev/null |
    filter_value snapshot "${entry_destination[index]}" "${source_snapshot}" >/dev/null; then
    die "required Proton field is missing, empty, invalid, or oversized: ${entry_item[index]}/${entry_field[index]}"
  fi
  printf 'SOURCE_FIELD item=%s field=%s status=ready\n' \
    "${entry_item[index]}" "${entry_field[index]}"
done

# Re-read the exact numeric resource identities, complete name sets, main-only
# branch policy, wait timer, reviewers, and prevent-self-review setting after
# the value preflight. Any uncertainty stops before the first write.
load_proton_item_names
load_github_state recheck
report_status
(( protection_errors == 0 )) || die 'repository identity or environment protection changed during preflight'
(( unexpected_destinations == 0 )) || die 'a broad, legacy, or unmanaged GitHub name appeared during preflight'
(( missing_required_sources == 0 )) || die 'a Proton source item changed during preflight'

for index in "${!entry_scope[@]}"; do
  scope=${entry_scope[index]}
  target=${entry_target[index]}
  kind=${entry_kind[index]}
  destination=${entry_destination[index]}
  source_snapshot=${status_root}/source-${index}.hmac
  if ! pass-cli item view --vault-name "${vault}" \
    --item-title "${entry_item[index]}" --field "${entry_field[index]}" 2>/dev/null |
    filter_value set-verified "${destination}" "${source_snapshot}" \
      "${target}" "${scope}" "${kind}" >/dev/null; then
    die "source changed or GitHub rejected ${scope}/${target}/${kind}/${destination}"
  fi
  printf 'SYNCED scope=%s target=%s kind=%s name=%s\n' \
    "${scope}" "${target}" "${kind}" "${destination}"
done

load_proton_item_names
load_github_state final
report_status
(( protection_errors == 0 )) || die 'repository identity or environment protection changed during sync'
(( unexpected_destinations == 0 )) || die 'GitHub read-back found a broad, legacy, or unmanaged destination'
(( missing_required_destinations == 0 )) || die 'GitHub destination read-back is incomplete'
(( missing_required_sources == 0 )) || die 'a Proton source item changed during sync'

# Re-read every selected Proton field after all writes, then compare every
# public variable value exactly. Secret values remain intentionally unreadable;
# their complete exact name sets were checked by load_github_state above.
for index in "${!entry_scope[@]}"; do
  source_snapshot=${status_root}/source-${index}.hmac
  if ! pass-cli item view --vault-name "${vault}" \
    --item-title "${entry_item[index]}" --field "${entry_field[index]}" 2>/dev/null |
    filter_value verify "${entry_destination[index]}" "${source_snapshot}" >/dev/null; then
    die "Proton source changed after write: ${entry_item[index]}/${entry_field[index]}"
  fi
  printf 'SOURCE_FIELD item=%s field=%s status=stable-after-write\n' \
    "${entry_item[index]}" "${entry_field[index]}"

  [[ "${entry_kind[index]}" == variable ]] || continue
  if [[ "${entry_target[index]}" == repository ]]; then
    variable_endpoint="repos/${repository}/actions/variables/${entry_destination[index]}"
  else
    variable_endpoint="repos/${repository}/environments/${entry_scope[index]}/variables/${entry_destination[index]}"
  fi
  if ! GH_PROMPT_DISABLED=1 gh api --header "X-GitHub-Api-Version: ${api_version}" \
    "${variable_endpoint}" 2>/dev/null |
    extract_github_variable_value "${entry_destination[index]}" |
    filter_value verify "${entry_destination[index]}" "${source_snapshot}" >/dev/null; then
    die "GitHub public variable failed exact read-back: ${entry_scope[index]}/${entry_destination[index]}"
  fi
  printf 'VARIABLE_READBACK scope=%s name=%s status=exact\n' \
    "${entry_scope[index]}" "${entry_destination[index]}"
done

# Close policy, identity, source-title, and name-set races introduced by the
# post-write field and public-variable comparisons themselves.
load_proton_item_names
load_github_state confirm
report_status
(( protection_errors == 0 )) || die 'repository identity or environment protection changed during final read-back'
(( unexpected_destinations == 0 )) || die 'final GitHub read-back found a broad, legacy, or unmanaged destination'
(( missing_required_destinations == 0 )) || die 'final GitHub destination name read-back is incomplete'
(( missing_required_sources == 0 )) || die 'a Proton source item changed during final read-back'
printf 'SYNC_COMPLETE repository=%s vault=%s scope=%s\n' \
  "${repository}" "${vault}" "${selected_scope}"
