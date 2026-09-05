#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

policy_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)

die() {
  printf '%s\n' "$*" >&2
  exit 1
}

[[ $# -eq 1 ]] || die "usage: run-protected-candidate-harness.sh CANDIDATE_ROOT"
candidate_input=$1
[[ -d "${candidate_input}" && ! -L "${candidate_input}" ]] || die "candidate root must be a real directory"
candidate_root=$(cd "${candidate_input}" && pwd -P)
[[ "${candidate_root}" != / ]] || die "candidate root cannot be /"
[[ -d "${candidate_root}/.git" && ! -L "${candidate_root}/.git" ]] || die "candidate must be a Git checkout"
[[ "${candidate_root}" != "${policy_root}" ]] || die "candidate and protected policy must be separate checkouts"

while IFS=$' \t' read -r -d '' mode _object _stage tracked_path; do
  [[ "${mode}" == 100644 || "${mode}" == 100755 ]] || \
    die "candidate contains a non-regular tracked entry: ${tracked_path} (${mode})"
done < <(git -C "${candidate_root}" ls-files -z --stage)

for required in \
  .github/workflows/ci.yml \
  .github/workflows/manual-deploy.yml \
  .github/workflows/pr-ci-result.yml \
  compose.yml \
  deploy/credential-inventory.json \
  scripts/ci_base_image.py \
  scripts/dispatch-ci-attestation.mjs \
  scripts/github_environment_policy.py \
  scripts/migrate-openpanel-secret-scope.sh \
  scripts/nginx-ci-queue-controller.mjs \
  scripts/publish-pr-ci-check.mjs \
  scripts/reconcile-nginx-ci-jit.sh \
  scripts/require-successful-ci.sh \
  scripts/run-nginx-ci-jit-vm.sh \
  scripts/run-nginx-ci-queue-controller.sh \
  scripts/sync-github-credentials.sh \
  scripts/test-brio-staging-policy.sh \
  scripts/test-runtrace-upload-policy.sh \
  sites/00-common.conf.template \
  sites/brio-staging.conf.template \
  sites/maildev-brio-staging.conf.template \
  sites/runtrace-prod.conf.template; do
  [[ -f "${candidate_root}/${required}" && ! -L "${candidate_root}/${required}" ]] || \
    die "candidate is missing regular file ${required}"
done

command -v shellcheck >/dev/null || die "shellcheck is required"
command -v actionlint >/dev/null || die "actionlint is required"
command -v docker >/dev/null || die "docker is required"
command -v node >/dev/null || die "node is required"
command -v python3 >/dev/null || die "python3 is required"

shell_files=()
while IFS= read -r -d '' shell_file; do
  shell_files+=("${candidate_root}/${shell_file}")
done < <(git -C "${candidate_root}" ls-files -z '*.sh' 'host/libexec/*')
(( ${#shell_files[@]} > 0 )) || die "candidate has no shell scripts"
shellcheck --severity=warning "${shell_files[@]}"
workflow_files=()
while IFS= read -r -d '' workflow_file; do
  workflow_files+=("${candidate_root}/${workflow_file}")
done < <(git -C "${candidate_root}" ls-files -z '.github/workflows/*.yml' '.github/workflows/*.yaml')
(( ${#workflow_files[@]} > 0 )) || die "candidate has no workflows"
actionlint "${workflow_files[@]}"
node_files=()
while IFS= read -r -d '' node_file; do
  node_files+=("${candidate_root}/${node_file}")
done < <(git -C "${candidate_root}" ls-files -z 'scripts/*.mjs')
for node_file in "${node_files[@]}"; do
  node --check "${node_file}"
done

# Execute only test logic loaded from protected main, pointing it at candidate
# data. Candidate-authored scripts are diagnostics, never the merge authority.
export NGINX_POLICY_REPO_ROOT="${candidate_root}"
"${policy_root}/scripts/test-runtrace-upload-policy.sh"
"${policy_root}/scripts/test-brio-staging-policy.sh"
NGINX_CANDIDATE_ROOT="${candidate_root}" node --test \
  "${policy_root}/tests/nginx-ci-queue-controller.test.mjs" \
  "${policy_root}/tests/pr-ci-check.test.mjs"
NGINX_CANDIDATE_ROOT="${candidate_root}" python3 \
  "${policy_root}/tests/test_base_image_integrity.py"
NGINX_CANDIDATE_ROOT="${candidate_root}" python3 \
  "${policy_root}/tests/test_ci_release_gate.py"
NGINX_CANDIDATE_ROOT="${candidate_root}" python3 \
  "${policy_root}/tests/test_environment_policy.py"
NGINX_CANDIDATE_ROOT="${candidate_root}" python3 \
  "${policy_root}/tests/test_secret_scope_policy.py"
NGINX_CANDIDATE_ROOT="${candidate_root}" python3 \
  "${policy_root}/tests/test_credential_sync.py"

printf 'Protected Nginx candidate policy passed for %s.\n' "$(git -C "${candidate_root}" rev-parse HEAD)"
