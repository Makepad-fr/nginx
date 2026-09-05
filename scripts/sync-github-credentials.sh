#!/usr/bin/env bash

# Field values move only through anonymous pipes. Disable caller-provided
# tracing before Proton Pass can materialize a value.
set +x
set -Eeuo pipefail
umask 077
IFS=$' \t\n'
export LANG=C
export LC_ALL=C
unset DEBUG GH_DEBUG PASS_CLI_DEBUG

readonly repository=Makepad-fr/nginx
readonly vault=Makepad
readonly environment_name=production
readonly confirmation=Makepad-fr/nginx:production
readonly api_version=2022-11-28
readonly max_value_bytes=49152
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
readonly repo_root
readonly inventory=${repo_root}/deploy/credential-inventory.json
readonly inventory_validator=${repo_root}/scripts/validate-credential-inventory.py

usage() {
  printf '%s\n' \
    'usage: sync-github-credentials.sh [--check] [--scope production]' \
    '       sync-github-credentials.sh --sync --scope production --confirm Makepad-fr/nginx:production' \
    '' \
    '  --check  Audit identities, policy, Proton item titles, and destination names.' \
    '  --sync   Stream the reviewed production fields from Proton Pass to GitHub.'
}

die() {
  printf 'credential sync: %s\n' "$*" >&2
  exit 1
}

mode=check
mode_selected=0
selected_scope=
provided_confirmation=
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
    --confirm)
      (( $# >= 2 )) || die '--confirm requires a value'
      [[ -z "${provided_confirmation}" ]] || die '--confirm may be supplied only once'
      provided_confirmation=$2
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

[[ -z "${selected_scope}" || "${selected_scope}" == production ]] || \
  die 'the only managed scope is production'
if [[ "${mode}" == sync ]]; then
  [[ "${selected_scope}" == production ]] || die '--sync requires --scope production'
  [[ "${provided_confirmation}" == "${confirmation}" ]] || \
    die '--sync requires the exact production confirmation'
elif [[ -n "${provided_confirmation}" ]]; then
  die '--confirm is accepted only with --sync'
fi

for command_name in pass-cli gh git python3 sort cmp mktemp find; do
  command -v "${command_name}" >/dev/null || die "${command_name} is required"
done
[[ -f "${inventory}" && ! -L "${inventory}" ]] || die 'credential inventory is missing or symlinked'
[[ -f "${inventory_validator}" && ! -L "${inventory_validator}" ]] || die 'credential inventory validator is missing or symlinked'
PYTHONDONTWRITEBYTECODE=1 python3 "${inventory_validator}" "${inventory}" || die 'credential inventory is invalid'

status_root=$(mktemp -d "${TMPDIR:-/tmp}/nginx-credential-sync.XXXXXXXX")
[[ -d "${status_root}" && ! -L "${status_root}" ]] || die 'could not create a private status directory'
chmod 0700 "${status_root}"
readonly status_root
readonly entries_file=${status_root}/entries.tsv
readonly required_items_file=${status_root}/required-items.txt
readonly hmac_key_file=${status_root}/source-hmac.key

cleanup() {
  if [[ -n "${status_root:-}" && "${status_root}" == "${TMPDIR:-/tmp}"/nginx-credential-sync.* && -d "${status_root}" && ! -L "${status_root}" ]]; then
    find "${status_root}" -depth -mindepth 1 -delete
    rmdir -- "${status_root}"
  fi
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

python3 - "${inventory}" "${entries_file}" "${required_items_file}" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
entries = payload["entries"]
pathlib.Path(sys.argv[2]).write_text(
    "".join(
        f'{entry["kind"]}\t{entry["destination"]}\t{entry["item"]}\t{entry["field"]}\n'
        for entry in entries
    ),
    encoding="utf-8",
)
pathlib.Path(sys.argv[3]).write_text(
    "".join(f"{title}\n" for title in sorted({entry["item"] for entry in entries})),
    encoding="utf-8",
)
PY

declare -a entry_kind=()
declare -a entry_destination=()
declare -a entry_item=()
declare -a entry_field=()
while IFS=$'\t' read -r kind destination item field; do
  index=${#entry_kind[@]}
  entry_kind[index]=${kind}
  entry_destination[index]=${destination}
  entry_item[index]=${item}
  entry_field[index]=${field}
done <"${entries_file}"
(( ${#entry_kind[@]} > 0 )) || die 'credential inventory is empty'

pass-cli test >/dev/null || die 'Proton Pass is not authenticated'
GH_PROMPT_DISABLED=1 gh auth status >/dev/null 2>&1 || die 'GitHub CLI is not authenticated'

audit_proton_items() {
  local phase=$1
  local output=${status_root}/proton-items-${phase}.txt
  pass-cli item list --vault-name "${vault}" --filter-state active --output json |
    python3 -c '
import collections
import json
import pathlib
import sys

required = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
try:
    payload = json.load(sys.stdin)
except json.JSONDecodeError as error:
    raise SystemExit(f"invalid Proton item inventory: {error}") from error
if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
    raise SystemExit("invalid Proton item inventory")
counts = collections.Counter()
for item in payload["items"]:
    if not isinstance(item, dict) or not isinstance(item.get("title"), str):
        raise SystemExit("invalid Proton item-name entry")
    if item["title"] in required:
        counts[item["title"]] += 1
problems = [title for title in required if counts[title] != 1]
pathlib.Path(sys.argv[2]).write_text(
    "".join(f"{title}\t{counts[title]}\n" for title in required), encoding="utf-8"
)
if problems:
    raise SystemExit("required Proton item title is missing or ambiguous")
' "${required_items_file}" "${output}"
  while IFS=$'\t' read -r title count; do
    printf 'PROTON_ITEM title=%s count=%s status=unique\n' "${title}" "${count}"
  done <"${output}"
}

capture_github_state() {
  local phase=$1
  local require_complete=$2
  local prefix=${status_root}/github-${phase}
  GH_PROMPT_DISABLED=1 gh api --header "X-GitHub-Api-Version: ${api_version}" \
    "repos/${repository}" >"${prefix}-repository.json"
  GH_PROMPT_DISABLED=1 gh api --header "X-GitHub-Api-Version: ${api_version}" \
    "repos/${repository}/environments/${environment_name}" >"${prefix}-environment.json"
  GH_PROMPT_DISABLED=1 gh api --header "X-GitHub-Api-Version: ${api_version}" \
    "repos/${repository}/environments/${environment_name}/deployment-branch-policies?per_page=100" \
    >"${prefix}-branches.json"
  GH_PROMPT_DISABLED=1 gh api --header "X-GitHub-Api-Version: ${api_version}" \
    "repos/${repository}/branches/main/protection" >"${prefix}-protection.json"
  GH_PROMPT_DISABLED=1 gh secret list --repo "${repository}" --env "${environment_name}" \
    --json name --jq '.[].name' | sort -u >"${prefix}-environment-secrets.txt"
  GH_PROMPT_DISABLED=1 gh variable list --repo "${repository}" --env "${environment_name}" \
    --json name --jq '.[].name' | sort -u >"${prefix}-environment-variables.txt"
  GH_PROMPT_DISABLED=1 gh secret list --repo "${repository}" --app actions \
    --json name --jq '.[].name' | sort -u >"${prefix}-repository-secrets.txt"
  GH_PROMPT_DISABLED=1 gh variable list --repo "${repository}" \
    --json name --jq '.[].name' | sort -u >"${prefix}-repository-variables.txt"

  python3 - "${inventory}" "${prefix}" "${require_complete}" "${prefix}-policy.sha256" <<'PY'
import hashlib
import json
import pathlib
import sys

inventory = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
prefix = pathlib.Path(sys.argv[2])
require_complete = sys.argv[3] == "true"

def load_json(suffix):
    return json.loads(pathlib.Path(f"{prefix}-{suffix}.json").read_text(encoding="utf-8"))

repository = load_json("repository")
expected_repository = inventory["repository"]
observed_repository = {
    "id": repository.get("id"),
    "fullName": repository.get("full_name"),
    "visibility": repository.get("visibility"),
    "defaultBranch": repository.get("default_branch"),
    "fork": repository.get("fork"),
}
if observed_repository != expected_repository:
    raise SystemExit("GitHub repository identity or visibility changed")

environment = load_json("environment")
expected_environment = inventory["environment"]
if environment.get("id") != expected_environment["id"] or environment.get("name") != expected_environment["name"]:
    raise SystemExit("GitHub environment identity changed")
if environment.get("deployment_branch_policy") != {"protected_branches": False, "custom_branch_policies": True}:
    raise SystemExit("production environment is not restricted by a custom branch policy")

branches = load_json("branches")
expected_branch = {
    "id": expected_environment["branchPolicyId"],
    "name": expected_environment["branch"],
    "type": "branch",
}
branch_policies = branches.get("branch_policies")
observed_branches = [
    {key: branch.get(key) for key in expected_branch}
    for branch in branch_policies
] if isinstance(branch_policies, list) else []
if branches.get("total_count") != 1 or observed_branches != [expected_branch]:
    raise SystemExit("production environment is not restricted to exact main")

protection = load_json("protection")
checks = protection.get("required_status_checks") or {}
reviews = protection.get("required_pull_request_reviews") or {}
if checks.get("strict") is not True or checks.get("checks") != [{"context": "policy-and-render", "app_id": 15368}]:
    raise SystemExit("main does not require the exact GitHub Actions policy check")
for key in ("dismiss_stale_reviews", "require_code_owner_reviews", "require_last_push_approval"):
    if reviews.get(key) is not True:
        raise SystemExit(f"main review protection is missing {key}")
if reviews.get("required_approving_review_count", 0) < 1:
    raise SystemExit("main does not require an approving review")
for key in ("required_signatures", "enforce_admins", "required_linear_history", "required_conversation_resolution"):
    if (protection.get(key) or {}).get("enabled") is not True:
        raise SystemExit(f"main protection is missing {key}")
for key in ("allow_force_pushes", "allow_deletions"):
    if (protection.get(key) or {}).get("enabled") is not False:
        raise SystemExit(f"main protection permits {key}")

def names(suffix):
    return set(pathlib.Path(f"{prefix}-{suffix}.txt").read_text(encoding="utf-8").splitlines())

expected = {kind: {entry["destination"] for entry in inventory["entries"] if entry["kind"] == kind} for kind in ("secret", "variable")}
environment_names = {
    "secret": names("environment-secrets"),
    "variable": names("environment-variables"),
}
repository_names = {
    "secret": names("repository-secrets"),
    "variable": names("repository-variables"),
}
duplicates = sorted((kind, name) for kind in expected for name in expected[kind] & repository_names[kind])
if duplicates:
    raise SystemExit("a managed production name also exists at repository scope")
missing = sorted((kind, name) for kind in expected for name in expected[kind] - environment_names[kind])
for kind, name in missing:
    print(f"GITHUB_DESTINATION kind={kind} name={name} status=missing")
for kind in expected:
    for name in sorted(expected[kind] & environment_names[kind]):
        print(f"GITHUB_DESTINATION kind={kind} name={name} status=present")
if require_complete and missing:
    raise SystemExit("managed production destinations are incomplete")

policy = {
    "repository": observed_repository,
    "environment": {
        "id": environment.get("id"),
        "name": environment.get("name"),
        "deployment_branch_policy": environment.get("deployment_branch_policy"),
    },
    "branches": branches,
    "protection": protection,
}
canonical = json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
pathlib.Path(sys.argv[4]).write_text(hashlib.sha256(canonical).hexdigest() + "\n", encoding="ascii")
PY
}

filter_value() {
  local operation=$1
  local destination=$2
  local kind=$3
  local snapshot_file=$4
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

operation, destination, kind = sys.argv[1:4]
maximum = int(sys.argv[4])
key_path = pathlib.Path(sys.argv[5])
snapshot_path = pathlib.Path(sys.argv[6])
value = sys.stdin.buffer.read(maximum + 2)
if value.endswith(b"\n"):
    value = value[:-1]
    if value.endswith(b"\r"):
        value = value[:-1]
if not value or len(value) > maximum or b"\x00" in value:
    raise SystemExit("value is empty, oversized, or contains NUL")
try:
    text = value.decode("utf-8")
except UnicodeDecodeError as error:
    raise SystemExit("value is not UTF-8") from error

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
elif destination == "DEPLOY_SSH_KNOWN_HOSTS":
    for line in text.splitlines():
        parts = line.split()
        offset = 2 if parts and parts[0].startswith("@") else 1
        if len(parts) <= offset + 1 or re.fullmatch(r"(?:ssh-|ecdsa-|sk-)[A-Za-z0-9@._+-]+", parts[offset]) is None:
            raise SystemExit("known_hosts contains an invalid entry")
        try:
            base64.b64decode(parts[offset + 1], validate=True)
        except (binascii.Error, ValueError) as error:
            raise SystemExit("known_hosts contains invalid key data") from error
elif destination.startswith("MAKEPAD_PROXY_"):
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", text) is None:
        raise SystemExit("Docker network name is invalid")
    exact = {
        "MAKEPAD_PROXY_BRIO_STAGING_APP_NETWORK": "makepad_brio_staging_app",
        "MAKEPAD_PROXY_MAILDEV_BRIO_STAGING_WEB_NETWORK": "makepad_brio_staging_maildev_web",
    }
    if destination in exact and text != exact[destination]:
        raise SystemExit("Brio network name is not canonical")
else:
    exact = {
        "NGINX_DEPLOY_HOST": "135.181.141.31",
        "NGINX_DEPLOY_PORT": "22",
        "NGINX_DEPLOY_USER": "makepad",
        "NGINX_DEPLOY_REMOTE_DIR": "/srv/makepad/nginx",
        "NGINX_DEPLOY_STACK_NAME": "makepad-edge",
    }
    if destination not in exact or text != exact[destination]:
        raise SystemExit("deployment coordinate does not match protected policy")

key = key_path.read_bytes()
if len(key) != 32:
    raise SystemExit("source comparison key is invalid")
observed = hmac.new(key, value, hashlib.sha256).hexdigest()
if operation == "snapshot":
    snapshot_path.write_text(observed + "\n", encoding="ascii")
elif operation in {"verify", "set"}:
    expected = snapshot_path.read_text(encoding="ascii").strip()
    if re.fullmatch(r"[a-f0-9]{64}", expected) is None or not hmac.compare_digest(observed, expected):
        raise SystemExit("Proton source changed after preflight")
    if operation == "set":
        command = ["gh", kind, "set", destination, "--repo", "Makepad-fr/nginx", "--env", "production"]
        if kind == "secret":
            command.extend(("--app", "actions"))
        environment = os.environ.copy()
        environment["GH_PROMPT_DISABLED"] = "1"
        completed = subprocess.run(command, input=value, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment, check=False)
        if completed.returncode != 0:
            raise SystemExit("GitHub rejected the destination")
else:
    raise SystemExit("invalid value-filter operation")
' "${operation}" "${destination}" "${kind}" "${max_value_bytes}" "${hmac_key_file}" "${snapshot_file}"
}

audit_proton_items initial
capture_github_state initial false

if [[ "${mode}" == check ]]; then
  capture_github_state check true
  cmp -s "${status_root}/github-initial-policy.sha256" "${status_root}/github-check-policy.sha256" || \
    die 'GitHub identity or protection changed during the audit'
  printf 'CHECK_COMPLETE repository=%s vault=%s scope=%s\n' "${repository}" "${vault}" "${environment_name}"
  exit 0
fi

[[ "$(git -C "${repo_root}" branch --show-current)" == main ]] || die 'sync must run from the local main branch'
[[ -z "$(git -C "${repo_root}" status --porcelain=v1)" ]] || die 'sync requires a clean checkout'
remote_main=$(GH_PROMPT_DISABLED=1 gh api --header "X-GitHub-Api-Version: ${api_version}" \
  "repos/${repository}/commits/main" --jq .sha)
[[ "${remote_main}" =~ ^[a-f0-9]{40}$ && "$(git -C "${repo_root}" rev-parse HEAD)" == "${remote_main}" ]] || \
  die 'sync must run from the exact current protected main commit'
ulimit -c 0 || die 'could not disable process core dumps'
python3 - "${hmac_key_file}" <<'PY'
import os
import pathlib
import sys

pathlib.Path(sys.argv[1]).write_bytes(os.urandom(32))
PY

# Validate every source before the first GitHub write. Only keyed HMACs are
# retained; raw values never enter argv, shell variables, environment, or files.
for index in "${!entry_kind[@]}"; do
  snapshot=${status_root}/source-${index}.hmac
  if ! pass-cli item view --vault-name "${vault}" --item-title "${entry_item[index]}" \
    --field "${entry_field[index]}" 2>/dev/null |
    filter_value snapshot "${entry_destination[index]}" "${entry_kind[index]}" "${snapshot}" >/dev/null; then
    die "Proton field is missing or invalid: ${entry_item[index]}/${entry_field[index]}"
  fi
  printf 'SOURCE_FIELD item=%s field=%s status=ready\n' "${entry_item[index]}" "${entry_field[index]}"
done

audit_proton_items prewrite
capture_github_state prewrite false
cmp -s "${status_root}/github-initial-policy.sha256" "${status_root}/github-prewrite-policy.sha256" || \
  die 'GitHub identity or protection changed before write'

for index in "${!entry_kind[@]}"; do
  snapshot=${status_root}/source-${index}.hmac
  if ! pass-cli item view --vault-name "${vault}" --item-title "${entry_item[index]}" \
    --field "${entry_field[index]}" 2>/dev/null |
    filter_value set "${entry_destination[index]}" "${entry_kind[index]}" "${snapshot}" >/dev/null; then
    die "source changed or GitHub rejected ${entry_kind[index]}/${entry_destination[index]}"
  fi
  printf 'SYNCED scope=production kind=%s name=%s\n' "${entry_kind[index]}" "${entry_destination[index]}"
done

audit_proton_items postwrite
capture_github_state postwrite true
cmp -s "${status_root}/github-initial-policy.sha256" "${status_root}/github-postwrite-policy.sha256" || \
  die 'GitHub identity or protection changed during write'

for index in "${!entry_kind[@]}"; do
  snapshot=${status_root}/source-${index}.hmac
  if ! pass-cli item view --vault-name "${vault}" --item-title "${entry_item[index]}" \
    --field "${entry_field[index]}" 2>/dev/null |
    filter_value verify "${entry_destination[index]}" "${entry_kind[index]}" "${snapshot}" >/dev/null; then
    die "Proton source changed after write: ${entry_item[index]}/${entry_field[index]}"
  fi
  [[ "${entry_kind[index]}" == variable ]] || continue
  if ! GH_PROMPT_DISABLED=1 gh api --header "X-GitHub-Api-Version: ${api_version}" \
    "repos/${repository}/environments/${environment_name}/variables/${entry_destination[index]}" \
    --jq .value |
    filter_value verify "${entry_destination[index]}" variable "${snapshot}" >/dev/null; then
    die "GitHub variable failed exact read-back: ${entry_destination[index]}"
  fi
done

audit_proton_items final
capture_github_state final true
cmp -s "${status_root}/github-initial-policy.sha256" "${status_root}/github-final-policy.sha256" || \
  die 'GitHub identity or protection changed during final read-back'
printf 'SYNC_COMPLETE repository=%s vault=%s scope=%s\n' "${repository}" "${vault}" "${environment_name}"
