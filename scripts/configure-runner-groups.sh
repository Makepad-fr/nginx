#!/usr/bin/env bash
set -euo pipefail

# Runner labels select a host; these selected-workflow organization groups are
# the policy boundary that prevents a branch-authored workflow from selecting
# either the CI attestor or deployment runner.

readonly organization="Makepad-fr"
readonly repository="nginx"
readonly api_version="2022-11-28"

die() {
  printf '%s\n' "$*" >&2
  exit 1
}

[[ $# -eq 0 ]] || die "usage: configure-runner-groups.sh < GITHUB_ORG_RUNNER_CONTROLLER_TOKEN"
for command_name in gh python3 sort; do
  command -v "${command_name}" >/dev/null || die "${command_name} is required"
done

IFS= read -r controller_token || die "An organization runner-controller token is required on standard input"
[[ "${controller_token}" =~ ^(github_pat_|ghp_|ghs_|ghu_)[A-Za-z0-9_]+$ ]] || die "The controller token has an invalid format"
export GH_TOKEN="${controller_token}"
unset controller_token

repository_id=$(gh api --header "X-GitHub-Api-Version: ${api_version}" \
  "repos/${organization}/${repository}" --jq .id)
[[ "${repository_id}" =~ ^[1-9][0-9]*$ ]] || die "Could not resolve the Nginx repository ID"

groups=(
  'Nginx CI|Makepad-fr/nginx/.github/workflows/ci.yml@refs/heads/main,Makepad-fr/nginx/.github/workflows/pr-ci-result.yml@refs/heads/main|makepad-nginx-ci-attestor|makepad-nginx-ci-ephemeral'
  'Nginx Deploy|Makepad-fr/nginx/.github/workflows/manual-deploy.yml@refs/heads/main|makepad-nginx-deploy|'
)

temporary_directory=$(mktemp -d)
chmod 0700 "${temporary_directory}"
cleanup() {
  find "${temporary_directory}" -depth -mindepth 1 -delete
  rmdir -- "${temporary_directory}"
  unset GH_TOKEN
}
trap cleanup EXIT
all_configured_runner_ids="${temporary_directory}/configured-runner-ids"
: >"${all_configured_runner_ids}"
reconciled_groups="${temporary_directory}/reconciled-groups"
: >"${reconciled_groups}"

# Reconcile every group before checking runner placement. This deliberately
# creates both fail-closed trust domains even when no runners have been
# registered yet; validation happens only after the complete desired state has
# been applied.
for entry in "${groups[@]}"; do
  IFS='|' read -r group_name selected_workflows required_labels forbidden_persistent_labels <<<"${entry}"
  group_list="${temporary_directory}/groups.json"
  gh api --header "X-GitHub-Api-Version: ${api_version}" \
    "orgs/${organization}/actions/runner-groups?per_page=100" >"${group_list}"
  group_id=$(python3 - "${group_list}" "${group_name}" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text())
groups = payload.get("runner_groups", [])
if payload.get("total_count", len(groups)) > len(groups):
    raise SystemExit("more than 100 organization runner groups require explicit pagination")
matches = [group.get("id") for group in groups if group.get("name") == sys.argv[2]]
if len(matches) > 1:
    raise SystemExit(f"duplicate runner groups named {sys.argv[2]}")
if matches:
    print(matches[0])
PY
  )

  payload=$(python3 - "${group_name}" "${selected_workflows}" "${repository_id}" <<'PY'
import json
import sys

print(json.dumps({
    "name": sys.argv[1],
    "visibility": "selected",
    "allows_public_repositories": True,
    "restricted_to_workflows": True,
    "selected_workflows": sys.argv[2].split(","),
    "selected_repository_ids": [int(sys.argv[3])],
}, separators=(",", ":")))
PY
  )

  if [[ -z "${group_id}" ]]; then
    created=$(printf '%s' "${payload}" | gh api --method POST \
      --header "X-GitHub-Api-Version: ${api_version}" \
      "orgs/${organization}/actions/runner-groups" --input -)
    group_id=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<<"${created}")
  else
    update_payload=$(python3 - "${group_name}" "${selected_workflows}" <<'PY'
import json
import sys

print(json.dumps({
    "name": sys.argv[1],
    "visibility": "selected",
    "allows_public_repositories": True,
    "restricted_to_workflows": True,
    "selected_workflows": sys.argv[2].split(","),
}, separators=(",", ":")))
PY
    )
    printf '%s' "${update_payload}" | gh api --method PATCH \
      --header "X-GitHub-Api-Version: ${api_version}" \
      "orgs/${organization}/actions/runner-groups/${group_id}" --input - >/dev/null
  fi
  [[ "${group_id}" =~ ^[1-9][0-9]*$ ]] || die "Invalid runner group ID for ${group_name}"

  gh api --method PUT --header "X-GitHub-Api-Version: ${api_version}" \
    "orgs/${organization}/actions/runner-groups/${group_id}/repositories/${repository_id}" >/dev/null

  repositories="${temporary_directory}/repositories-${group_id}.json"
  gh api --header "X-GitHub-Api-Version: ${api_version}" \
    "orgs/${organization}/actions/runner-groups/${group_id}/repositories?per_page=100" >"${repositories}"
  while IFS= read -r unrelated_repository_id; do
    [[ -z "${unrelated_repository_id}" ]] || gh api --method DELETE \
      --header "X-GitHub-Api-Version: ${api_version}" \
      "orgs/${organization}/actions/runner-groups/${group_id}/repositories/${unrelated_repository_id}" >/dev/null
  done < <(python3 - "${repositories}" "${repository_id}" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text())
repositories = payload.get("repositories", [])
if payload.get("total_count", len(repositories)) > len(repositories):
    raise SystemExit("more than 100 selected repositories require explicit pagination")
expected = int(sys.argv[2])
for repository in repositories:
    repository_id = repository.get("id")
    if isinstance(repository_id, int) and repository_id != expected:
        print(repository_id)
PY
  )

  printf '%s|%s|%s|%s|%s\n' "${group_name}" "${group_id}" "${selected_workflows}" "${required_labels}" \
    "${forbidden_persistent_labels}" \
    >>"${reconciled_groups}"
  printf 'Reconciled runner-group configuration %s (%s).\n' "${group_name}" "${group_id}"
done

# Read back every group only after the complete reconciliation pass. A missing
# host can therefore fail bootstrap without preventing creation or repair of a
# later group.
while IFS='|' read -r group_name group_id selected_workflows required_labels forbidden_persistent_labels; do
  observed_group="${temporary_directory}/group-${group_id}.json"
  observed_repositories="${temporary_directory}/observed-repositories-${group_id}.json"
  observed_runners="${temporary_directory}/runners-${group_id}.json"
  gh api --header "X-GitHub-Api-Version: ${api_version}" \
    "orgs/${organization}/actions/runner-groups/${group_id}" >"${observed_group}"
  gh api --header "X-GitHub-Api-Version: ${api_version}" \
    "orgs/${organization}/actions/runner-groups/${group_id}/repositories?per_page=100" >"${observed_repositories}"
  gh api --header "X-GitHub-Api-Version: ${api_version}" \
    "orgs/${organization}/actions/runner-groups/${group_id}/runners?per_page=100" >"${observed_runners}"
  python3 - "${observed_group}" "${observed_repositories}" "${observed_runners}" \
    "${group_name}" "${selected_workflows}" "${required_labels}" "${repository_id}" \
    "${all_configured_runner_ids}" "${forbidden_persistent_labels}" <<'PY'
import json
import pathlib
import sys

group = json.loads(pathlib.Path(sys.argv[1]).read_text())
repository_payload = json.loads(pathlib.Path(sys.argv[2]).read_text())
runner_payload = json.loads(pathlib.Path(sys.argv[3]).read_text())
repositories = repository_payload.get("repositories", [])
runners = runner_payload.get("runners", [])
expected = {
    "name": sys.argv[4],
    "visibility": "selected",
    "allows_public_repositories": True,
    "restricted_to_workflows": True,
    "workflow_restrictions_read_only": False,
}
for key, value in expected.items():
    if group.get(key) != value:
        raise SystemExit(f"runner group {sys.argv[4]} has unexpected {key}: {group.get(key)!r}")
if sorted(group.get("selected_workflows", [])) != sorted(sys.argv[5].split(",")):
    raise SystemExit(f"runner group {sys.argv[4]} has unexpected selected_workflows")
if repository_payload.get("total_count", len(repositories)) > len(repositories):
    raise SystemExit(f"runner group {sys.argv[4]} has more than 100 selected repositories")
if [repository.get("id") for repository in repositories] != [int(sys.argv[7])]:
    raise SystemExit(f"runner group {sys.argv[4]} is not restricted to Nginx")
if runner_payload.get("total_count", len(runners)) > len(runners):
    raise SystemExit(f"runner group {sys.argv[4]} has more than 100 runners")
available_labels = {
    label.get("name").lower()
    for runner in runners
    for label in runner.get("labels", [])
    if isinstance(label.get("name"), str)
}
missing = sorted(set(sys.argv[6].split(",")) - available_labels)
if missing:
    raise SystemExit(f"runner group {sys.argv[4]} has no host for labels: {', '.join(missing)}")
required = sys.argv[6].split(",")
forbidden_persistent = {label for label in sys.argv[9].split(",") if label}
present_forbidden = sorted(forbidden_persistent & available_labels)
if present_forbidden:
    raise SystemExit(
        f"runner group {sys.argv[4]} has a persistent runner carrying JIT-only labels: "
        f"{', '.join(present_forbidden)}"
    )
label_owners = {
    label: {
        runner.get("id")
        for runner in runners
        if label in {
            str(item.get("name", "")).lower()
            for item in runner.get("labels", [])
            if isinstance(item.get("name"), str)
        }
    }
    for label in required
}
default_labels = {"self-hosted", "linux", "x64"}
for runner in runners:
    labels = {
        str(item.get("name", "")).lower()
        for item in runner.get("labels", [])
        if isinstance(item.get("name"), str)
    }
    owned = set(required) & labels
    unexpected = labels - default_labels - set(required) - forbidden_persistent
    if len(owned) != 1 or unexpected:
        raise SystemExit(
            f"runner group {sys.argv[4]} contains an unapproved runner/label set on "
            f"{runner.get('name', runner.get('id'))}: {sorted(labels)}"
        )
for label, owners in label_owners.items():
    if len(owners) != 1:
        raise SystemExit(
            f"runner group {sys.argv[4]} must have exactly one persistent host for {label}"
        )
for index, label in enumerate(required):
    for other in required[index + 1:]:
        if label_owners[label] & label_owners[other]:
            raise SystemExit(
                f"runner group {sys.argv[4]} places mutually trusted labels "
                f"{label} and {other} on the same host"
            )
with pathlib.Path(sys.argv[8]).open("a", encoding="utf-8") as destination:
    for runner in runners:
        runner_id = runner.get("id")
        if isinstance(runner_id, int):
            destination.write(f"{runner_id}\n")
PY
  printf 'Validated runner group %s (%s).\n' "${group_name}" "${group_id}"
done <"${reconciled_groups}"

# Repository-level or unrelated organization runners bypass these two groups.
# Refuse to declare bootstrap complete while any such runner is available.
accessible_runners="${temporary_directory}/repository-runners.json"
gh api --header "X-GitHub-Api-Version: ${api_version}" \
  "repos/${organization}/${repository}/actions/runners?per_page=100" >"${accessible_runners}"
python3 - "${accessible_runners}" "${all_configured_runner_ids}" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text())
runners = payload.get("runners", [])
if payload.get("total_count", len(runners)) > len(runners):
    raise SystemExit("more than 100 Nginx-accessible runners require explicit pagination")
configured = {
    int(value)
    for value in pathlib.Path(sys.argv[2]).read_text().splitlines()
    if value.strip()
}
unexpected = [runner for runner in runners if runner.get("id") not in configured]
if unexpected:
    names = ", ".join(str(runner.get("name", runner.get("id"))) for runner in unexpected)
    raise SystemExit(f"Nginx still exposes runners outside its restricted groups: {names}")
PY

printf 'Nginx runner access is restricted to the two selected-workflow groups.\n'
