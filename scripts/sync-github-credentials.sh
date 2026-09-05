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
readonly vault=Makepad
readonly max_value_bytes=49152
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
readonly repo_root
readonly inventory=${repo_root}/deploy/credential-inventory.json
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
[[ -f "${environment_policy}" && ! -L "${environment_policy}" ]] || die 'environment policy helper is missing or is a symbolic link'

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
    "fullName": expected_repository,
    "visibility": "public",
    "defaultBranch": "main",
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
    environment_counts[scope] += 1
    if not selected_scope or selected_scope == scope:
        github_lines.append("\t".join((scope, "environment", kind, destination, entry["item"], entry["field"])))
        selected_sources.add(entry["item"])

if any(count == 0 for count in environment_counts.values()):
    raise SystemExit("every approved environment must have at least one inventory entry")

seen_repository: set[str] = set()
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
    if not selected_scope or selected_scope == "repository-variables":
        github_lines.append("\t".join(("repository-variables", "repository", "variable", destination, entry["item"], entry["field"])))
        selected_sources.add(entry["item"])

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
    if not selected_scope or selected_scope == "host-boundaries":
        host_lines.append("\t".join((boundary, destination, entry["item"], entry["field"])))
        selected_sources.add(entry["item"])

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

repository_metadata_valid() {
  local metadata_file=$1
  local phase=$2
  python3 - "${metadata_file}" "${repository_id_file}" "${phase}" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
repository_id = payload.get("id")
owner = payload.get("owner")
if (
    isinstance(repository_id, bool)
    or not isinstance(repository_id, int)
    or repository_id <= 0
    or payload.get("full_name") != "Makepad-fr/nginx"
    or payload.get("private") is not False
    or payload.get("visibility") != "public"
    or payload.get("default_branch") != "main"
    or payload.get("fork") is not False
    or payload.get("archived") is not False
    or payload.get("disabled") is not False
    or not isinstance(owner, dict)
    or owner.get("login") != "Makepad-fr"
    or owner.get("type") != "Organization"
):
    raise SystemExit("repository identity or public-main policy is invalid")
identity_path = pathlib.Path(sys.argv[2])
if sys.argv[3] == "initial":
    identity_path.write_text(f"{repository_id}\n", encoding="ascii")
elif identity_path.read_text(encoding="ascii").strip() != str(repository_id):
    raise SystemExit("repository numeric identity changed during sync")
PY
}

environment_identity_valid() {
  local metadata_file=$1
  local environment_name=$2
  local phase=$3
  local identity_file=${status_root}/environment-${environment_name}-id.txt
  python3 - "${metadata_file}" "${identity_file}" "${environment_name}" "${phase}" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
environment_id = payload.get("id")
if (
    isinstance(environment_id, bool)
    or not isinstance(environment_id, int)
    or environment_id <= 0
    or payload.get("name") != sys.argv[3]
):
    raise SystemExit("environment identity is invalid")
identity_path = pathlib.Path(sys.argv[2])
if sys.argv[4] == "initial":
    identity_path.write_text(f"{environment_id}\n", encoding="ascii")
elif identity_path.read_text(encoding="ascii").strip() != str(environment_id):
    raise SystemExit("environment numeric identity changed during sync")
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
  local environment environment_json branches_json snapshot_json kind output_file

  protection_errors=0
  find "${status_root}" -maxdepth 1 -type f -name 'github-current-*.txt' -delete

  if ! GH_PROMPT_DISABLED=1 gh api "repos/${repository}" >"${repository_json}" 2>/dev/null ||
    ! repository_metadata_valid "${repository_json}" "${phase}"; then
    printf 'REPOSITORY name=%s policy=invalid-or-changed\n' "${repository}"
    ((protection_errors += 1))
  else
    printf 'REPOSITORY name=%s policy=exact-public-main identity=stable\n' "${repository}"
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
    if ! GH_PROMPT_DISABLED=1 gh api \
      "repos/${repository}/environments/${environment}" >"${environment_json}" 2>/dev/null ||
      ! GH_PROMPT_DISABLED=1 gh api \
      "repos/${repository}/environments/${environment}/deployment-branch-policies?per_page=100" \
      >"${branches_json}" 2>/dev/null; then
      printf 'ENVIRONMENT name=%s protection=missing-or-unreadable\n' "${environment}"
      ((protection_errors += 1))
    elif [[ "${phase}" == initial ]]; then
      if environment_identity_valid "${environment_json}" "${environment}" "${phase}" &&
        python3 "${environment_policy}" snapshot "${environment_json}" "${environment}" >"${snapshot_json}" &&
        python3 "${environment_policy}" verify "${environment_json}" "${branches_json}" \
          "${environment}" "${snapshot_json}"; then
        printf 'ENVIRONMENT name=%s protection=exact-main-preserved identity=stable\n' "${environment}"
      else
        printf 'ENVIRONMENT name=%s protection=invalid-or-ambiguous\n' "${environment}"
        ((protection_errors += 1))
      fi
    elif [[ ! -s "${snapshot_json}" ]] ||
      ! environment_identity_valid "${environment_json}" "${environment}" "${phase}" ||
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
  python3 -c '
import sys

operation = sys.argv[1]
maximum = int(sys.argv[2])
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
if operation == "emit":
    sys.stdout.buffer.write(value)
elif operation != "check":
    raise SystemExit("invalid filter operation")
' "${operation}" "${max_value_bytes}"
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

# Read and validate every selected value before the first GitHub mutation. The
# validator discards its input, so this completes the preflight without ever
# retaining a credential in a shell variable or temporary file.
for index in "${!entry_scope[@]}"; do
  if ! pass-cli item view --vault-name "${vault}" \
    --item-title "${entry_item[index]}" --field "${entry_field[index]}" 2>/dev/null |
    filter_value check >/dev/null; then
    die "required Proton field is missing, empty, invalid, or oversized: ${entry_item[index]}/${entry_field[index]}"
  fi
  printf 'SOURCE_FIELD item=%s field=%s status=ready\n' \
    "${entry_item[index]}" "${entry_field[index]}"
done

# Re-read the exact numeric resource identities, complete name sets, main-only
# branch policy, wait timer, reviewers, and prevent-self-review setting after
# the value preflight. Any uncertainty stops before the first write.
load_github_state recheck
report_status
(( protection_errors == 0 )) || die 'repository identity or environment protection changed during preflight'
(( unexpected_destinations == 0 )) || die 'a broad, legacy, or unmanaged GitHub name appeared during preflight'

for index in "${!entry_scope[@]}"; do
  scope=${entry_scope[index]}
  target=${entry_target[index]}
  kind=${entry_kind[index]}
  destination=${entry_destination[index]}
  if [[ "${target}" == environment && "${kind}" == secret ]]; then
    if ! pass-cli item view --vault-name "${vault}" \
      --item-title "${entry_item[index]}" --field "${entry_field[index]}" 2>/dev/null |
      filter_value emit |
      GH_PROMPT_DISABLED=1 gh secret set "${destination}" --repo "${repository}" \
        --env "${scope}" --app actions >/dev/null 2>&1; then
      die "GitHub rejected ${scope}/${kind}/${destination}"
    fi
  elif [[ "${target}" == environment && "${kind}" == variable ]]; then
    if ! pass-cli item view --vault-name "${vault}" \
      --item-title "${entry_item[index]}" --field "${entry_field[index]}" 2>/dev/null |
      filter_value emit |
      GH_PROMPT_DISABLED=1 gh variable set "${destination}" --repo "${repository}" \
        --env "${scope}" >/dev/null 2>&1; then
      die "GitHub rejected ${scope}/${kind}/${destination}"
    fi
  elif [[ "${target}" == repository && "${kind}" == variable ]]; then
    if ! pass-cli item view --vault-name "${vault}" \
      --item-title "${entry_item[index]}" --field "${entry_field[index]}" 2>/dev/null |
      filter_value emit |
      GH_PROMPT_DISABLED=1 gh variable set "${destination}" --repo "${repository}" >/dev/null 2>&1; then
      die "GitHub rejected repository/variable/${destination}"
    fi
  else
    die "unsupported write destination: ${scope}/${target}/${kind}/${destination}"
  fi
  printf 'SYNCED scope=%s target=%s kind=%s name=%s\n' \
    "${scope}" "${target}" "${kind}" "${destination}"
done

load_github_state final
report_status
(( protection_errors == 0 )) || die 'repository identity or environment protection changed during sync'
(( unexpected_destinations == 0 )) || die 'GitHub read-back found a broad, legacy, or unmanaged destination'
(( missing_required_destinations == 0 )) || die 'GitHub destination read-back is incomplete'
printf 'SYNC_COMPLETE repository=%s vault=%s scope=%s\n' \
  "${repository}" "${vault}" "${selected_scope}"
