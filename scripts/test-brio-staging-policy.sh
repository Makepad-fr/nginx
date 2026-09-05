#!/usr/bin/env bash
set -euo pipefail

repo_root=${NGINX_POLICY_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
repo_root=$(cd "${repo_root}" && pwd -P)
work_dir=$(mktemp -d)
suffix="${RANDOM}-$$"
network="brio-nginx-test-${suffix}"
app_upstream="brio-app-${suffix}"
mail_upstream="brio-mail-${suffix}"
auth_upstream="brio-auth-${suffix}"
proxy="brio-proxy-${suffix}"
nginx_image="nginx:1.30-alpine3.24@sha256:97d490c12ba55b4946b01546d1c3ed324e8d41ab1c9fcb2a616aa470620e5b46"

for expected in \
  'log_format brio_privacy escape=json' \
  'zone=brio_staging_apply:10m rate=5r/m' \
  'zone=brio_staging_general:10m rate=15r/s' \
  'zone=brio_staging_connections:10m'; do
  grep -Fq -- "${expected}" "${repo_root}/sites/00-common.conf.template" || {
    echo "Brio shared proxy policy is missing: ${expected}" >&2
    exit 1
  }
done

for expected in \
  'openssl x509 -in "${certificate}" -noout -checkhost "${host}"' \
  'openssl x509 -in "${certificate}" -noout -checkend 604800' \
  'openssl verify -purpose sslserver -verify_hostname "${host}"' \
  'MAKEPAD_PROXY_BRIO_STAGING_APP_NETWORK must be makepad_brio_staging_app' \
  'MAKEPAD_PROXY_MAILDEV_BRIO_STAGING_WEB_NETWORK must be makepad_brio_staging_maildev_web' \
  'Brio deployment bundle contains non-canonical network names' \
  'create_args=(--driver overlay --attachable --opt encrypted=true)' \
  'wait_for_nginx_convergence' \
  'rollback_failed_release' \
  'docker service rollback "${nginx_service}"' \
  'prior_spec_sha=$(sha256sum "${prior_spec}"' \
  'docker service ps --no-trunc --filter desired-state=running' \
  '--resolve "${host}:443:${ingress_ip}"' \
  'wait_for_status "https://${brio_host}/livez" 204' \
  'wait_for_status "https://${maildev_host}/" 302' \
  '"${remote_dir}/sites/catwlk-prod.conf.template"' \
  '"${remote_dir}/sites/openpanel-prod.conf.template"' \
  '"${remote_dir}/sites/runtrace-prod.conf.template"' \
  '"${nginx_image}" nginx -t'; do
  grep -Fq -- "${expected}" "${repo_root}/.github/workflows/manual-deploy.yml" || {
    echo "Brio deployment TLS preflight is missing: ${expected}" >&2
    exit 1
  }
  done

for expected in \
  'nginx-deploy-ssh-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}' \
  'nginx-deploy-bundle-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}' \
  'UserKnownHostsFile=${known_hosts_file}' \
  '-F /dev/null' \
  'GlobalKnownHostsFile=/dev/null' \
  'IdentitiesOnly=yes' \
  'Remove job-scoped deployment material' \
  'if: always()' \
  "if: github.repository == 'Makepad-fr/nginx' && github.ref == 'refs/heads/main'" \
  'REMOTE_HOST: ${{ vars.NGINX_DEPLOY_HOST }}' \
  'REMOTE_PORT: ${{ vars.NGINX_DEPLOY_PORT }}' \
  'REMOTE_USER: ${{ vars.NGINX_DEPLOY_USER }}' \
  'REMOTE_DIR: ${{ vars.NGINX_DEPLOY_REMOTE_DIR }}' \
  'STACK_NAME: ${{ vars.NGINX_DEPLOY_STACK_NAME }}' \
  '"${REMOTE_HOST}" != "135.181.141.31"' \
  '"${REMOTE_DIR}" != "/srv/makepad/nginx"' \
  '"${STACK_NAME}" != "makepad-edge"' \
  'ref: ${{ github.sha }}' \
  '[[ "$(git rev-parse HEAD)" == "${GITHUB_SHA}" ]]'; do
  grep -Fq -- "${expected}" "${repo_root}/.github/workflows/manual-deploy.yml" || {
    echo "Self-hosted Nginx deploy cleanup control is missing: ${expected}" >&2
    exit 1
  }
done

for expected in \
  'needs: verify-ci' \
  'group: org/Nginx Deploy' \
  'makepad-nginx-deploy' \
  'actions: read' \
  'checks: read' \
  'NGINX_PR_CHECK_APP_ID: ${{ vars.NGINX_PR_CHECK_APP_ID }}' \
  './scripts/require-successful-ci.sh "${GITHUB_REPOSITORY}" "${GITHUB_SHA}" "${NGINX_PR_CHECK_APP_ID}"'; do
  grep -Fq -- "${expected}" "${repo_root}/.github/workflows/manual-deploy.yml" || {
    echo "Nginx deployment exact-CI gate is missing: ${expected}" >&2
    exit 1
  }
done

for forbidden_target_secret in \
  'secrets.DEPLOY_SSH_HOST' \
  'secrets.DEPLOY_SSH_PORT' \
  'secrets.DEPLOY_SSH_USER' \
  'secrets.DEPLOY_REMOTE_DIR' \
  'secrets.DEPLOY_STACK_NAME'; do
  if grep -Fq -- "${forbidden_target_secret}" "${repo_root}/.github/workflows/manual-deploy.yml"; then
    echo "Non-secret Nginx deployment coordinates must use protected environment variables: ${forbidden_target_secret}" >&2
    exit 1
  fi
done

if grep -Eq -- '--opt[[:space:]]+encrypted([[:space:]]|\))' "${repo_root}/.github/workflows/manual-deploy.yml"; then
  echo "Brio network creation must not use Docker's valueless encrypted option." >&2
  exit 1
fi

# These are literal workflow patterns, not shell paths.
# shellcheck disable=SC2088
for forbidden in '${HOME}/.ssh' '$HOME/.ssh' '~/.ssh' 'add-ssh-host-key-action'; do
  if grep -Fq -- "${forbidden}" "${repo_root}/.github/workflows/manual-deploy.yml"; then
    echo "Self-hosted Nginx deploy workflow persists SSH state via ${forbidden}." >&2
    exit 1
  fi
done

for workflow in \
  "${repo_root}/.github/workflows/ci.yml" \
  "${repo_root}/.github/workflows/pr-ci-result.yml" \
  "${repo_root}/.github/workflows/manual-deploy.yml"; do
  checkout_count=$(grep -Ec -- 'uses: actions/checkout@[0-9a-f]{40}[[:space:]]+# v[0-9]+' "${workflow}")
  credential_count=$(grep -Fc -- 'persist-credentials: false' "${workflow}")
  if [[ "${checkout_count}" -eq 0 || "${credential_count}" -ne "${checkout_count}" ]]; then
    echo "Every self-hosted Nginx checkout must disable persisted Git credentials: ${workflow}" >&2
    exit 1
  fi
done
python3 - "${repo_root}/.github/workflows" <<'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
actual = sorted(path.name for path in root.iterdir() if path.suffix in {".yml", ".yaml"})
expected = ["ci.yml", "manual-deploy.yml", "pr-ci-result.yml"]
if actual != expected:
    raise SystemExit(f"unexpected Nginx workflow inventory: {actual!r}")
for path in root.iterdir():
    if path.suffix in {".yml", ".yaml"} and "ubuntu-" in path.read_text():
        raise SystemExit(f"GitHub-hosted runner fallback is forbidden: {path.name}")
PY

ci_workflow="${repo_root}/.github/workflows/ci.yml"
finalizer_workflow="${repo_root}/.github/workflows/pr-ci-result.yml"
candidate_harness="${repo_root}/scripts/run-protected-candidate-harness.sh"
credential_inventory="${repo_root}/deploy/credential-inventory.json"
credential_sync="${repo_root}/scripts/sync-github-credentials.sh"
check_publisher="${repo_root}/scripts/publish-pr-ci-check.mjs"
queue_controller="${repo_root}/scripts/nginx-ci-queue-controller.mjs"
jit_launcher="${repo_root}/scripts/run-nginx-ci-jit-vm.sh"
jit_reconciler="${repo_root}/scripts/reconcile-nginx-ci-jit.sh"
attestation_dispatch="${repo_root}/scripts/dispatch-ci-attestation.mjs"
runner_policy="${repo_root}/scripts/configure-runner-groups.sh"
github_policy="${repo_root}/scripts/configure-github-ci-policy.sh"
environment_policy="${repo_root}/scripts/github_environment_policy.py"
secret_scope_policy="${repo_root}/scripts/migrate-openpanel-secret-scope.sh"
controller_wrapper="${repo_root}/scripts/run-nginx-ci-queue-controller.sh"
controller_unit="${repo_root}/host/systemd/nginx-ci-queue-controller.service"
controller_alert_unit="${repo_root}/host/systemd/nginx-ci-queue-alert.service"
host_alert="${repo_root}/host/libexec/send-nginx-host-alert"
codeowners="${repo_root}/.github/CODEOWNERS"
for expected in \
  'pull_request_target:' \
  "github.event.pull_request.head.repo.full_name == github.repository" \
  "github.event.pull_request.base.ref == 'main'" \
  'group: org/Nginx CI' \
  'makepad-nginx-ci-ephemeral' \
  'makepad-nginx-job-${{ github.run_id }}-${{ github.run_attempt }}' \
  "github.event.pull_request.base.sha || github.sha" \
  "github.event.pull_request.head.sha || github.sha" \
  './_policy/scripts/run-protected-candidate-harness.sh _source'; do
  grep -Fq -- "${expected}" "${ci_workflow}" || {
    echo "Nginx protected-base CI is missing: ${expected}" >&2
    exit 1
  }
done
if grep -Fq -- 'secrets.' "${ci_workflow}"; then
  echo "Disposable Nginx CI must not receive secrets." >&2
  exit 1
fi
if grep -Eq '^[[:space:]]+pull_request:[[:space:]]*$' "${ci_workflow}"; then
  echo "Nginx CI must not load runner policy from a pull-request branch." >&2
  exit 1
fi
for expected in \
  'repository_dispatch:' \
  'types: [nginx-pr-ci-attestation]' \
  "github.event.action == 'nginx-pr-ci-attestation'" \
  "github.event.sender.type == 'Bot'" \
  'github.event.sender.id == fromJSON(vars.NGINX_CI_LAUNCHER_APP_SENDER_ID)' \
  'group: org/Nginx CI' \
  'makepad-nginx-ci-attestor' \
  'environment: release-nginx' \
  'NGINX_CI_APPROVED_BASE_IMAGE_SHA256: ${{ vars.NGINX_CI_APPROVED_BASE_IMAGE_SHA256 }}' \
  'NGINX_CI_ATTESTATION_PUBLIC_KEY: ${{ vars.NGINX_CI_ATTESTATION_PUBLIC_KEY }}' \
  'NGINX_PR_CHECK_APP_ID: ${{ vars.NGINX_PR_CHECK_APP_ID }}' \
  'NGINX_PR_CHECK_APP_PRIVATE_KEY: ${{ secrets.NGINX_PR_CHECK_APP_PRIVATE_KEY }}' \
  'node scripts/publish-pr-ci-check.mjs'; do
  grep -Fq -- "${expected}" "${finalizer_workflow}" || {
    echo "Nginx PR result finalizer is missing: ${expected}" >&2
    exit 1
  }
done
for forbidden in '_source' 'download-artifact' 'workflow_run.head_repository'; do
  if grep -Fq -- "${forbidden}" "${finalizer_workflow}"; then
    echo "Nginx trusted finalizer consumes an untrusted source: ${forbidden}" >&2
    exit 1
  fi
done
for required_file in \
  "${candidate_harness}" \
  "${credential_inventory}" \
  "${credential_sync}" \
  "${check_publisher}" \
  "${queue_controller}" \
  "${jit_launcher}" \
  "${jit_reconciler}" \
  "${attestation_dispatch}" \
  "${runner_policy}" \
  "${github_policy}" \
  "${environment_policy}" \
  "${secret_scope_policy}" \
  "${controller_wrapper}" \
  "${controller_unit}" \
  "${controller_alert_unit}" \
  "${host_alert}"; do
  [[ -f "${required_file}" && ! -L "${required_file}" ]] || {
    echo "Nginx CI trust helper is missing or symlinked: ${required_file}" >&2
    exit 1
  }
done
for expected in \
  'readonly secret_name="MAKEPAD_PROXY_OPENPANEL_APP_NETWORK"' \
  'readonly canonical_source="Nginx · production overlay names/openpanel"' \
  'repos/${repository}/environments/${environment_name}/secrets?per_page=100' \
  'repos/${repository}/actions/secrets/${secret_name}' \
  'production must use exact custom deployment-branch protection' \
  'production secret was not explicitly refreshed after the repository duplicate' \
  'validate_inventory check'; do
  grep -Fq -- "${expected}" "${secret_scope_policy}" || {
    echo "Nginx deployment-secret scope migration is missing: ${expected}" >&2
    exit 1
  }
done
for expected in \
  'flock -n 9' \
  'exec node "${script_directory}/nginx-ci-queue-controller.mjs"'; do
  grep -Fq -- "${expected}" "${controller_wrapper}" || {
    echo "Nginx controller singleton wrapper is missing: ${expected}" >&2
    exit 1
  }
done
for expected in \
  'OnFailure=nginx-ci-queue-alert.service' \
  'Requires=libvirtd.service' \
  'EnvironmentFile=/etc/makepad/nginx-ci/controller.env' \
  'ReadWritePaths=/run/lock /run /var/lib/makepad/nginx-ci /var/lib/libvirt'; do
  grep -Fq -- "${expected}" "${controller_unit}" || {
    echo "Nginx queue-controller service is missing: ${expected}" >&2
    exit 1
  }
done
grep -Fq -- 'send-nginx-host-alert nginx-ci-queue-controller' "${controller_alert_unit}" || {
  echo "Nginx controller OnFailure alert service is missing." >&2
  exit 1
}
for expected in \
  'NGINX_HOST_ALERT_URL_FILE' \
  '0:400' \
  "printf 'url = \"%s\"\\n'" \
  'curl --config -' \
  "--proto '=https'"; do
  grep -Fq -- "${expected}" "${host_alert}" || {
    echo "Nginx independent host alert is missing: ${expected}" >&2
    exit 1
  }
done
[[ "$(tr -d '\r' < "${codeowners}")" == '* @kaanyagci @idilsaglam' ]] || {
  echo "Nginx CODEOWNERS must require one of the two repository administrators." >&2
  exit 1
}
for expected in \
  'git -C "${candidate_root}" ls-files -z --stage' \
  "'host/libexec/*'" \
  'shellcheck --severity=warning' \
  'export NGINX_POLICY_REPO_ROOT="${candidate_root}"' \
  '"${policy_root}/scripts/test-runtrace-upload-policy.sh"' \
  '"${policy_root}/scripts/test-brio-staging-policy.sh"' \
  'tests/nginx-ci-queue-controller.test.mjs' \
  'tests/pr-ci-check.test.mjs' \
  'tests/test_base_image_integrity.py' \
  'tests/test_ci_release_gate.py' \
  'tests/test_environment_policy.py' \
  'tests/test_secret_scope_policy.py' \
  'tests/test_credential_sync.py'; do
  grep -Fq -- "${expected}" "${candidate_harness}" || {
    echo "Nginx protected candidate harness is missing: ${expected}" >&2
    exit 1
  }
done

for expected in \
  'deploy/credential-inventory.json' \
  'scripts/sync-github-credentials.sh'; do
  grep -Fq -- "${expected}" "${candidate_harness}" || {
    echo "Nginx protected harness does not require credential policy: ${expected}" >&2
    exit 1
  }
done

if grep -Fq -- "secrets.MAKEPAD_PROXY_RUNTRACE_APP_NETWORK ||" \
  "${repo_root}/.github/workflows/manual-deploy.yml"; then
  echo "Runtrace overlay must not fall back around the protected production inventory." >&2
  exit 1
fi
for expected in \
  'const EXPECTED_REPOSITORY = "Makepad-fr/nginx"' \
  'const EXPECTED_WORKFLOW_PATH = ".github/workflows/ci.yml"' \
  'const EXPECTED_RUNNER_GROUP = "Nginx CI"' \
  'const EXPECTED_SCHEMA = "makepad.nginx.ci-attestation.v1"' \
  'makepad-nginx-job-${attestation.run.id}-${attestation.run.attempt}' \
  'attestation.run.job_name !== "candidate-policy-and-render"' \
  'association.base?.sha !== attestation.run.workflow_sha' \
  '/attempts/${attestation.run.attempt}/jobs?per_page=100' \
  'organization_self_hosted_runners: "read"' \
  'assertNoAttestationReplay' \
  'published.app?.id'; do
  grep -Fq -- "${expected}" "${check_publisher}" || {
    echo "Nginx Checks-only result publisher is missing: ${expected}" >&2
    exit 1
  }
done

for expected in \
  'const REPOSITORY = "Makepad-fr/nginx"' \
  'const WORKFLOW_PATH = ".github/workflows/ci.yml"' \
  'makepad-nginx-ci-ephemeral' \
  'makepad-nginx-job-${run.id}-${run.run_attempt}' \
  'association.head?.repo?.id !== repositoryID' \
  'association.base?.sha !== run.head_sha' \
  'resourceIdentity' \
  'recordedResourceIDs.has(resources.id)' \
  'NGINX_CI_RECONCILER' \
  'phase: "local"' \
  'phase: "registration"' \
  'state.jobs[String(job.jobID)] = {...job, nonce, resources, status: "launching"' \
  'await atomicState(stateFile, state);' \
  'all remain permanently no-retry' \
  'organization_self_hosted_runners: "write"'; do
  grep -Fq -- "${expected}" "${queue_controller}" || {
    echo "Nginx JIT queue controller is missing: ${expected}" >&2
    exit 1
  }
done

for expected in \
  'NGINX_CI_RESOURCE_ID' \
  'virsh list --all' \
  'virsh destroy "${vm_name}"' \
  'virsh undefine "${vm_name}"' \
  'virsh net-list --all' \
  'virsh net-destroy "${network_name}"' \
  'virsh net-undefine "${network_name}"' \
  'ip -j link show' \
  'ip link delete dev "${bridge_name}"' \
  'nft list tables' \
  'nft delete table inet "${nft_table}"' \
  'find "${job_directory}" -depth -mindepth 1 -delete' \
  'actions/runners?per_page=100' \
  'actions/runners/${observed_runner_id}'; do
  grep -Fq -- "${expected}" "${jit_reconciler}" || {
    echo "Nginx crash/reboot reconciler is missing: ${expected}" >&2
    exit 1
  }
done

for expected in \
  'generate-jitconfig' \
  'readonly job_label="makepad-nginx-job-${run_id}-${run_attempt}"' \
  'NGINX_CI_RESOURCE_ID' \
  'virsh net-autostart --disable "${network_name}"' \
  'virsh autostart --disable "${vm_name}"' \
  'associations[0].get("base", {}).get("sha") != sys.argv[8]' \
  '--jitconfig "${jit_config}"' \
  'qemu-img convert' \
  'ci_base_image.py' \
  'ip daddr @denied4 drop' \
  'meta nfproto ipv6 drop' \
  'tcp dport 443 accept' \
  'registration_absent' \
  'makepad.nginx.ci-attestation.v1' \
  'openssl pkey -in "${attestation_private_key}"' \
  'dispatch-ci-attestation.mjs'; do
  grep -Fq -- "${expected}" "${jit_launcher}" || {
    echo "Nginx JIT hypervisor launcher is missing: ${expected}" >&2
    exit 1
  }
done
if grep -Fq -- 'qemu-img create -q -f qcow2 -F qcow2 -b' "${jit_launcher}"; then
  echo "Nginx JIT disk must not retain a mutable backing chain." >&2
  exit 1
fi
for expected in \
  'sign(null, Buffer.from(canonical), privateKey)' \
  'await unlink(evidencePath)' \
  'event_type: "nginx-pr-ci-attestation"'; do
  grep -Fq -- "${expected}" "${attestation_dispatch}" || {
    echo "Nginx signed attestation dispatch is missing: ${expected}" >&2
    exit 1
  }
done
for expected in \
  'allows_public_repositories": True' \
  'Makepad-fr/nginx/.github/workflows/ci.yml@refs/heads/main' \
  'Makepad-fr/nginx/.github/workflows/pr-ci-result.yml@refs/heads/main' \
  'Makepad-fr/nginx/.github/workflows/manual-deploy.yml@refs/heads/main' \
  'makepad-nginx-ci-attestor' \
  'makepad-nginx-ci-ephemeral' \
  'makepad-nginx-deploy' \
  'outside its restricted groups'; do
  grep -Fq -- "${expected}" "${runner_policy}" || {
    echo "Nginx selected-workflow runner policy is missing: ${expected}" >&2
    exit 1
  }
done
for expected in \
  'configure_environment release-nginx' \
  'configure_environment production' \
  'environment-presence' \
  '"${environment_policy_helper}" request' \
  '--input "${environment_request_json}"' \
  '"${environment_policy_helper}" verify' \
  'NGINX_CI_LAUNCHER_APP_SENDER_ID' \
  'NGINX_CI_APPROVED_BASE_IMAGE_SHA256' \
  'NGINX_CI_ATTESTATION_PUBLIC_KEY' \
  'required_signatures' \
  '"context": "policy-and-render"' \
  '"app_id": int(sys.argv[1])'; do
  grep -Fq -- "${expected}" "${github_policy}" || {
    echo "Nginx GitHub trust policy is missing: ${expected}" >&2
    exit 1
  }
done

for expected in \
  'repository_token=${GH_TOKEN}' \
  'unset GH_TOKEN' \
  "curl --config - --proto '=https'" \
  'check.get("app", {}).get("id") == expected_app_id'; do
  grep -Fq -- "${expected}" "${repo_root}/scripts/require-successful-ci.sh" || {
    echo "Nginx deployment CI gate is missing: ${expected}" >&2
    exit 1
  }
done

grep -Fq -- 'test: ["CMD", "nginx", "-t"]' "${repo_root}/compose.yml" || {
  echo "Nginx service is missing its configuration healthcheck." >&2
  exit 1
}

python3 - "${repo_root}/sites/brio-staging.conf.template" "${repo_root}/sites/maildev-brio-staging.conf.template" <<'PY'
import pathlib
import sys

for raw_path in sys.argv[1:]:
    path = pathlib.Path(raw_path)
    text = path.read_text()
    start = text.index("server {")
    depth = 0
    end = None
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end is None:
        raise SystemExit(f"unterminated HTTP server block in {path}")
    redirect_server = text[start:end]
    for required in ("access_log /dev/stdout brio_privacy;", "error_log /dev/stderr crit;"):
        if required not in redirect_server:
            raise SystemExit(f"HTTP redirect server in {path} is missing privacy logging control: {required}")
PY

for forbidden in '$request_uri' '$remote_addr' '$http_referer' '$http_user_agent'; do
  if sed -n '/log_format brio_privacy/,/};/p' "${repo_root}/sites/00-common.conf.template" | grep -Fq -- "${forbidden}"; then
    echo "Brio privacy log exposes forbidden field: ${forbidden}" >&2
    exit 1
  fi
done

for expected in \
  'server_name ${BRIO_STAGING_SERVER_NAME}' \
  'location = /applications' \
  'client_max_body_size 1m' \
  'access_log /dev/stdout brio_privacy' \
  'error_log /dev/stderr crit' \
  'add_header Referrer-Policy "strict-origin-when-cross-origin" always' \
  'proxy_set_header X-Forwarded-For ""' \
  'proxy_set_header X-Real-IP ""' \
  'proxy_set_header Forwarded ""' \
  'add_header X-Robots-Tag "noindex, nofollow, noarchive" always'; do
  grep -Fq -- "${expected}" "${repo_root}/sites/brio-staging.conf.template" || {
    echo "Brio application virtual host policy is missing: ${expected}" >&2
    exit 1
  }
done

for expected in \
  'server_name ${MAILDEV_BRIO_STAGING_SERVER_NAME}' \
  'auth_request /oauth2/auth' \
  'location ~ ^/(?:api/)?email/[^/]+/relay(?:/|$)' \
  'return 403' \
  'error_log /dev/stderr crit' \
  'add_header Cache-Control "private, no-store" always' \
  'proxy_set_header X-Forwarded-For ""'; do
  grep -Fq -- "${expected}" "${repo_root}/sites/maildev-brio-staging.conf.template" || {
    echo "Brio MailDev virtual host policy is missing: ${expected}" >&2
    exit 1
  }
done

cleanup() {
  docker rm -f "${proxy}" "${app_upstream}" "${mail_upstream}" "${auth_upstream}" >/dev/null 2>&1 || true
  docker network rm "${network}" >/dev/null 2>&1 || true
  rm -rf "${work_dir}"
}
trap cleanup EXIT

mkdir -p "${work_dir}/proxy-conf" "${work_dir}/upstream-conf" "${work_dir}/auth-conf" "${work_dir}/certs" "${work_dir}/acme"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -subj "/CN=brio-staging.localhost" \
  -addext "subjectAltName=DNS:brio-staging.localhost,DNS:maildev-brio-staging.localhost" \
  -keyout "${work_dir}/certs/privkey.pem" \
  -out "${work_dir}/certs/fullchain.pem" >/dev/null 2>&1

export BRIO_STAGING_SERVER_NAME=brio-staging.localhost
export BRIO_STAGING_UPSTREAM="http://${app_upstream}:80"
export BRIO_STAGING_TLS_CERT_FILE=/etc/nginx/test-certs/fullchain.pem
export BRIO_STAGING_TLS_KEY_FILE=/etc/nginx/test-certs/privkey.pem
export MAILDEV_BRIO_STAGING_SERVER_NAME=maildev-brio-staging.localhost
export MAILDEV_BRIO_STAGING_UPSTREAM="http://${mail_upstream}:80"
export MAILDEV_BRIO_STAGING_AUTH_UPSTREAM="http://${auth_upstream}:80"
export MAILDEV_BRIO_STAGING_TLS_CERT_FILE=/etc/nginx/test-certs/fullchain.pem
export MAILDEV_BRIO_STAGING_TLS_KEY_FILE=/etc/nginx/test-certs/privkey.pem
export CATWLK_ACME_WEBROOT=/var/lib/letsencrypt

envsubst '${BRIO_STAGING_SERVER_NAME} ${BRIO_STAGING_UPSTREAM} ${BRIO_STAGING_TLS_CERT_FILE} ${BRIO_STAGING_TLS_KEY_FILE} ${CATWLK_ACME_WEBROOT}' \
  < "${repo_root}/sites/brio-staging.conf.template" \
  > "${work_dir}/proxy-conf/brio.conf"
envsubst '${MAILDEV_BRIO_STAGING_SERVER_NAME} ${MAILDEV_BRIO_STAGING_UPSTREAM} ${MAILDEV_BRIO_STAGING_AUTH_UPSTREAM} ${MAILDEV_BRIO_STAGING_TLS_CERT_FILE} ${MAILDEV_BRIO_STAGING_TLS_KEY_FILE} ${CATWLK_ACME_WEBROOT}' \
  < "${repo_root}/sites/maildev-brio-staging.conf.template" \
  > "${work_dir}/proxy-conf/maildev.conf"
cp "${repo_root}/sites/00-common.conf.template" "${work_dir}/proxy-conf/00-common.conf"

printf '%s\n' \
  'server {' \
  '    listen 80;' \
  '    add_header X-Upstream-Forwarded-For "$http_x_forwarded_for" always;' \
  '    add_header X-Upstream-Real-IP "$http_x_real_ip" always;' \
  '    add_header X-Upstream-Forwarded "$http_forwarded" always;' \
  '    location / { return 204; }' \
  '}' > "${work_dir}/upstream-conf/default.conf"
printf '%s\n' \
  'server {' \
  '    listen 80;' \
  '    location = /oauth2/auth {' \
  '        if ($http_cookie != "maildev_auth=allowed") { return 401; }' \
  '        return 202;' \
  '    }' \
  '    location / { return 204; }' \
  '}' > "${work_dir}/auth-conf/default.conf"

docker network create "${network}" >/dev/null
docker run -d --name "${app_upstream}" --network "${network}" -v "${work_dir}/upstream-conf:/etc/nginx/conf.d:ro" "${nginx_image}" >/dev/null
docker run -d --name "${mail_upstream}" --network "${network}" -v "${work_dir}/upstream-conf:/etc/nginx/conf.d:ro" "${nginx_image}" >/dev/null
docker run -d --name "${auth_upstream}" --network "${network}" -v "${work_dir}/auth-conf:/etc/nginx/conf.d:ro" "${nginx_image}" >/dev/null
docker run -d --name "${proxy}" --network "${network}" -p 127.0.0.1::443 \
  -v "${work_dir}/proxy-conf:/etc/nginx/conf.d:ro" \
  -v "${work_dir}/certs:/etc/nginx/test-certs:ro" \
  -v "${work_dir}/acme:/var/lib/letsencrypt:ro" \
  "${nginx_image}" >/dev/null

docker exec "${proxy}" nginx -t >/dev/null
proxy_port=$(docker port "${proxy}" 443/tcp | awk -F: 'NR == 1 {print $NF}')
test -n "${proxy_port}"

request() {
  local host=$1
  local path=$2
  local cookie=${3:-}
  local args=(-ksS --http1.1 --resolve "${host}:${proxy_port}:127.0.0.1" -D - -o /dev/null)
  if [[ -n "${cookie}" ]]; then
    args+=(-H "Cookie: ${cookie}")
  fi
  curl "${args[@]}" "https://${host}:${proxy_port}${path}"
}

attempts=0
until request brio-staging.localhost /livez >/dev/null 2>&1; do
  attempts=$((attempts + 1))
  if (( attempts >= 30 )); then
    echo "Disposable Brio proxy did not become ready." >&2
    exit 1
  fi
  sleep 0.2
done

app_headers=$(request brio-staging.localhost '/livez?token=must-not-be-logged')
mail_headers=$(request maildev-brio-staging.localhost '/?email=must-not-be-logged' 'maildev_auth=allowed')
for header in 'strict-transport-security:' 'x-content-type-options: nosniff' 'x-frame-options: DENY' 'x-robots-tag: noindex, nofollow, noarchive'; do
  printf '%s\n' "${app_headers}" | tr '[:upper:]' '[:lower:]' | grep -Fq -- "$(printf '%s' "${header}" | tr '[:upper:]' '[:lower:]')" || {
    echo "Brio response is missing security header: ${header}" >&2
    exit 1
  }
done
printf '%s\n' "${mail_headers}" | tr '[:upper:]' '[:lower:]' | grep -Fq 'cache-control: private, no-store' || {
  echo "MailDev response is missing no-store cache policy." >&2
  exit 1
}

for protected_path in / /api/email '/socket.io/?EIO=4&transport=polling'; do
  unauthenticated_status=$(request maildev-brio-staging.localhost "${protected_path}" | awk 'NR == 1 {print $2}')
  authenticated_status=$(request maildev-brio-staging.localhost "${protected_path}" 'maildev_auth=allowed' | awk 'NR == 1 {print $2}')
  if [[ "${unauthenticated_status}" != "302" || "${authenticated_status}" != "204" ]]; then
    echo "MailDev route ${protected_path} did not enforce the OAuth gate (unauthenticated=${unauthenticated_status}, authenticated=${authenticated_status})." >&2
    exit 1
  fi
done

for relay_path in /email/example/relay /api/email/example/relay; do
  relay_status=$(curl -ksS --http1.1 --resolve "maildev-brio-staging.localhost:${proxy_port}:127.0.0.1" \
    -o /dev/null -w '%{http_code}' "https://maildev-brio-staging.localhost:${proxy_port}${relay_path}")
  if [[ "${relay_status}" != "403" ]]; then
    echo "MailDev relay endpoint ${relay_path} was not denied (HTTP ${relay_status})." >&2
    exit 1
  fi
done

for leaked_header in x-upstream-forwarded-for x-upstream-real-ip x-upstream-forwarded; do
  if printf '%s\n' "${app_headers}" | tr '[:upper:]' '[:lower:]' | grep -Eq "^${leaked_header}: .+"; then
    echo "Brio upstream received forbidden forwarding identity header: ${leaked_header}" >&2
    exit 1
  fi
done

sleep 0.2
access_logs=$(docker logs "${proxy}" 2>&1 | awk '/^\{/')
printf '%s\n' "${access_logs}" | python3 -c '
import json
import sys

records = [json.loads(line) for line in sys.stdin if line.startswith("{")]
if not records:
    raise SystemExit("no Brio privacy access records found")
required = {
    "timestamp", "request_id", "method", "status", "response_bytes",
    "request_time_seconds", "upstream_status", "upstream_response_time_seconds",
}
if any(set(record) != required for record in records):
    raise SystemExit("Brio privacy access record has an unexpected field set")
'
for forbidden in 'must-not-be-logged' '/livez' '/api/email' '127.0.0.1'; do
  if printf '%s\n' "${access_logs}" | grep -Fq -- "${forbidden}"; then
    echo "Brio privacy access log exposed forbidden data: ${forbidden}" >&2
    exit 1
  fi
done

echo "Brio nginx staging policy passed."
