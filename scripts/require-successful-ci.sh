#!/usr/bin/env bash
set -euo pipefail

readonly expected_repository="Makepad-fr/nginx"
readonly api_version="2022-11-28"

die() {
  printf '%s\n' "$*" >&2
  exit 1
}

[[ $# -eq 3 ]] || die "usage: require-successful-ci.sh <repository> <commit-sha> <checks-app-id>"
readonly repository=$1
readonly source_sha=$2
readonly checks_app_id=$3

[[ "${repository}" == "${expected_repository}" ]] || die "unexpected deployment repository"
[[ "${source_sha}" =~ ^[a-f0-9]{40}$ ]] || die "source commit must be a full lowercase SHA"
[[ "${checks_app_id}" =~ ^[1-9][0-9]*$ ]] || die "Checks App ID must be a positive integer"
[[ "${GH_TOKEN:-}" =~ ^[A-Za-z0-9_]{20,}$ ]] || die "GH_TOKEN is required and must have a safe token format"
repository_token=${GH_TOKEN}
unset GH_TOKEN
for command_name in curl python3; do
  command -v "${command_name}" >/dev/null || die "${command_name} is required"
done

temporary_directory=$(mktemp -d)
chmod 0700 "${temporary_directory}"
cleanup() {
  find "${temporary_directory}" -depth -mindepth 1 -delete
  rmdir -- "${temporary_directory}"
}
trap cleanup EXIT
runs_file="${temporary_directory}/runs.json"
checks_file="${temporary_directory}/checks.json"

# Keep the job token out of curl's process arguments. The config is streamed
# over stdin and never materialized in the runner workspace.
printf 'header = "Authorization: Bearer %s"\n' "${repository_token}" | \
  curl --config - --proto '=https' --tlsv1.2 --fail --silent --show-error --max-time 30 \
    --header 'Accept: application/vnd.github+json' \
    --header "X-GitHub-Api-Version: ${api_version}" \
    "https://api.github.com/repos/${repository}/actions/workflows/ci.yml/runs?branch=main&event=push&status=completed&head_sha=${source_sha}&per_page=100" \
    >"${runs_file}"
printf 'header = "Authorization: Bearer %s"\n' "${repository_token}" | \
  curl --config - --proto '=https' --tlsv1.2 --fail --silent --show-error --max-time 30 \
    --header 'Accept: application/vnd.github+json' \
    --header "X-GitHub-Api-Version: ${api_version}" \
    "https://api.github.com/repos/${repository}/commits/${source_sha}/check-runs?check_name=policy-and-render&filter=all&per_page=100" \
    >"${checks_file}"
unset repository_token

python3 - "${runs_file}" "${checks_file}" "${source_sha}" "${repository}" "${checks_app_id}" <<'PY'
import json
import pathlib
import re
import sys

runs_payload = json.loads(pathlib.Path(sys.argv[1]).read_text())
checks_payload = json.loads(pathlib.Path(sys.argv[2]).read_text())
expected_sha = sys.argv[3]
expected_repository = sys.argv[4]
expected_app_id = int(sys.argv[5])
runs = runs_payload.get("workflow_runs", [])
checks = checks_payload.get("check_runs", [])
if not isinstance(runs, list) or runs_payload.get("total_count") != len(runs):
    raise SystemExit("The exact protected-main workflow-run response is truncated.")
if not isinstance(checks, list) or checks_payload.get("total_count") != len(checks):
    raise SystemExit("The App-bound CI check response is truncated.")
successful_runs = {
    (run.get("id"), run.get("run_attempt")): run
    for run in runs
    if run.get("head_sha") == expected_sha
    and run.get("head_branch") == "main"
    and run.get("event") == "push"
    and run.get("status") == "completed"
    and run.get("conclusion") == "success"
    and run.get("name") == "CI"
    and run.get("path") == ".github/workflows/ci.yml"
    and run.get("repository", {}).get("full_name") == expected_repository
    and isinstance(run.get("id"), int)
    and isinstance(run.get("run_attempt"), int)
}
for check in checks:
    match = re.fullmatch(
        r"nginx-ci:push:([1-9][0-9]*):([1-9][0-9]*):[A-Za-z0-9_-]{43}",
        str(check.get("external_id", "")),
    )
    if not match:
        continue
    identity = (int(match.group(1)), int(match.group(2)))
    run = successful_runs.get(identity)
    if (
        run
        and check.get("name") == "policy-and-render"
        and check.get("head_sha") == expected_sha
        and check.get("status") == "completed"
        and check.get("conclusion") == "success"
        and check.get("app", {}).get("id") == expected_app_id
        and check.get("details_url") == run.get("html_url")
    ):
        break
else:
    raise SystemExit("The exact protected-main commit has no successful App-bound CI and teardown attestation.")
PY

printf 'Verified successful App-bound Nginx CI and teardown for %s.\n' "${source_sha}"
