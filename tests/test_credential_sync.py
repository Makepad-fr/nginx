#!/usr/bin/env python3
"""Adversarial tests for the names-only Proton-to-GitHub sync boundary."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(
    os.environ.get("NGINX_CANDIDATE_ROOT", pathlib.Path(__file__).parents[1])
).resolve()
HELPER = ROOT / "scripts" / "sync-github-credentials.sh"
INVENTORY = ROOT / "deploy" / "credential-inventory.json"


FAKE_PASS = r"""#!/usr/bin/env bash
set -euo pipefail
printf 'pass-cli' >>"${FAKE_AUDIT_LOG}"
printf ' %q' "$@" >>"${FAKE_AUDIT_LOG}"
printf '\n' >>"${FAKE_AUDIT_LOG}"

if [[ "${1:-}" == test ]]; then
  exit 0
fi
if [[ "${1:-} ${2:-}" == 'item list' ]]; then
  python3 -c '
import json
import os
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
titles = {
    entry["item"]
    for group in ("githubEnvironmentEntries", "repositoryVariables", "hostEntries")
    for entry in payload[group]
}
missing = os.environ.get("FAKE_MISSING_ITEM", "")
items = [{"title": title} for title in sorted(titles) if title != missing]
duplicate = os.environ.get("FAKE_DUPLICATE_ITEM", "")
if duplicate:
    items.append({"title": duplicate})
print(json.dumps({"items": items}))
' "${FAKE_INVENTORY}"
  exit 0
fi
if [[ "${1:-} ${2:-}" == 'item view' ]]; then
  item=
  field=
  while (( $# > 0 )); do
    case "$1" in
      --item-title) item=$2; shift 2 ;;
      --field) field=$2; shift 2 ;;
      *) shift ;;
    esac
  done
  [[ -n "${item}" && -n "${field}" ]]
  printf 'pass-view item=%s field=%s\n' "${item}" "${field}" >>"${FAKE_AUDIT_LOG}"
  [[ "${field}" != "${FAKE_MISSING_FIELD:-}" ]] || exit 1
  if [[ "${field}" == "${FAKE_EMPTY_FIELD:-}" ]]; then
    exit 0
  fi
  if [[ "${field}" == "${FAKE_OVERSIZE_FIELD:-}" ]]; then
    python3 -c 'import sys; sys.stdout.write("S" * 50000)'
    exit 0
  fi
  if [[ "${field}" == "${FAKE_INVALID_SEMANTIC_FIELD:-}" ]]; then
    printf '%s\n' 'invalid-value'
    exit 0
  fi
  case "${item}/${field}" in
    'Nginx · production deployment/private_key')
      printf '%s\n' '-----BEGIN OPENSSH PRIVATE KEY-----' \
        'TkVWRVJfUFJJTlRfVkFMVUVfU1NIX0tFWQ==' \
        '-----END OPENSSH PRIVATE KEY-----'
      ;;
    'Nginx · CI Checks App/private_key')
      printf '%s\n' '-----BEGIN PRIVATE KEY-----' \
        'TkVWRVJfUFJJTlRfVkFMVUVfQ0hFQ0tTX0tFWQ==' \
        '-----END PRIVATE KEY-----'
      ;;
    'Nginx · production deployment/known_hosts')
      printf '%s\n' 'proxy.example ssh-ed25519 TkVWRVJfUFJJTlRfVkFMVUVfSE9TVF9LRVk='
      ;;
    'Nginx · production deployment/host') printf '%s\n' '135.181.141.31' ;;
    'Nginx · production deployment/port') printf '%s\n' '22' ;;
    'Nginx · production deployment/user') printf '%s\n' 'makepad' ;;
    'Nginx · production deployment/remote_dir') printf '%s\n' '/srv/makepad/nginx' ;;
    'Nginx · production deployment/stack_name') printf '%s\n' 'makepad-edge' ;;
    'Nginx · production overlay names/brio_staging') printf '%s\n' 'makepad_brio_staging_app' ;;
    'Nginx · production overlay names/maildev_brio_staging_web') printf '%s\n' 'makepad_brio_staging_maildev_web' ;;
    'Nginx · production overlay names/'*) printf 'NEVER_PRINT_VALUE_%s\n' "${field}" ;;
    'Nginx · CI Checks App/app_id') printf '%s\n' '10001' ;;
    'Nginx · CI Launcher App/bot_user_id') printf '%s\n' '10002' ;;
    'Nginx · CI base image approval/qcow2_sha256') printf '%064d\n' 0 | tr '0' 'a' ;;
    'Nginx · CI hypervisor attestation/ed25519_public_key')
      printf '%s\n' '-----BEGIN PUBLIC KEY-----' \
        'MCowBQYDK2VwAyEAAAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=' \
        '-----END PUBLIC KEY-----'
      ;;
    *) exit 92 ;;
  esac
  exit 0
fi
exit 97
"""


FAKE_GH = r"""#!/usr/bin/env bash
set -euo pipefail

printf 'gh' >>"${FAKE_AUDIT_LOG}"
printf ' %q' "$@" >>"${FAKE_AUDIT_LOG}"
printf '\n' >>"${FAKE_AUDIT_LOG}"

increment_count() {
  local name=$1
  local path="${FAKE_COUNT_DIR}/${name}"
  local count=0
  [[ ! -f "${path}" ]] || count=$(<"${path}")
  count=$((count + 1))
  printf '%s\n' "${count}" >"${path}"
  printf '%s' "${count}"
}

if [[ "${1:-} ${2:-}" == 'auth status' ]]; then
  exit 0
fi
if [[ "${1:-}" == api ]]; then
  path=${*: -1}
  if [[ "${path}" == repos/Makepad-fr/nginx ]]; then
    count=$(increment_count repository)
    repository_id=424242
    [[ "${FAKE_REPOSITORY_ID_CHANGE:-0}" != 1 || "${count}" == 1 ]] || repository_id=424243
    default_branch=main
    [[ "${FAKE_INVALID_REPOSITORY:-0}" != 1 ]] || default_branch=develop
    printf '{"id":%s,"full_name":"Makepad-fr/nginx","private":false,"visibility":"public","default_branch":"%s","fork":false,"archived":false,"disabled":false,"owner":{"login":"Makepad-fr","type":"Organization"}}\n' \
      "${repository_id}" "${default_branch}"
    exit 0
  fi
  if [[ "${path}" == */deployment-branch-policies\?per_page=100 ]]; then
    if [[ "${FAKE_INVALID_PROTECTION:-0}" == 1 ]]; then
      printf '%s\n' '{"total_count":1,"branch_policies":[{"id":91,"name":"release/*","type":"branch"}]}'
    else
      printf '%s\n' '{"total_count":1,"branch_policies":[{"id":90,"name":"main","type":"branch"}]}'
    fi
    exit 0
  fi
  if [[ "${path}" == repos/Makepad-fr/nginx/environments/* ]]; then
    environment=${path##*/}
    count=$(increment_count "environment-${environment}")
    environment_id=7001
    [[ "${environment}" != release-nginx ]] || environment_id=7002
    if [[ "${FAKE_ENVIRONMENT_ID_CHANGE:-}" == "${environment}" && "${count}" != 1 ]]; then
      environment_id=$((environment_id + 20))
    fi
    wait_timer=30
    if [[ "${FAKE_CHANGED_POLICY:-}" == "${environment}" && "${count}" != 1 ]]; then
      wait_timer=31
    fi
    printf '{"id":%s,"name":"%s","protection_rules":[{"id":10,"type":"branch_policy"},{"id":11,"type":"wait_timer","wait_timer":%s},{"id":12,"type":"required_reviewers","prevent_self_review":true,"reviewers":[{"type":"Team","reviewer":{"id":81}},{"type":"User","reviewer":{"id":82}}]}],"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}\n' \
      "${environment_id}" "${environment}" "${wait_timer}"
    exit 0
  fi
  exit 96
fi

kind=${1:-}
operation=${2:-}
[[ "${kind}" == secret || "${kind}" == variable ]] || exit 95
shift 2
destination=
if [[ "${operation}" == set ]]; then
  destination=${1:-}
  shift
fi
environment=
while (( $# > 0 )); do
  case "$1" in
    --env) environment=$2; shift 2 ;;
    --body|-b|-f|--env-file)
      printf 'value-bearing gh argument is forbidden\n' >&2
      exit 94
      ;;
    --repo|--app|--json|--jq) shift 2 ;;
    *) shift ;;
  esac
done
scope=${environment:-repository-variables}

if [[ "${operation}" == list ]]; then
  python3 -c '
import json
import os
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
scope, kind = sys.argv[2:4]
if scope == "repository-variables":
    names = [entry["destination"] for entry in payload["repositoryVariables"]] if kind == "variable" else []
else:
    names = [
        entry["destination"]
        for entry in payload["githubEnvironmentEntries"]
        if entry["scope"] == scope and entry["kind"] == kind
    ]
missing = os.environ.get("FAKE_MISSING_DESTINATION", "")
state_path = pathlib.Path(os.environ["FAKE_DESTINATION_STATE"])
written = set(state_path.read_text(encoding="utf-8").splitlines()) if state_path.exists() else set()
identity = lambda name: f"{scope}|{kind}|{name}"
names = [name for name in names if name != missing or identity(name) in written]
if kind == "secret" and scope == "repository-variables" and os.environ.get("FAKE_REPOSITORY_SECRET"):
    names.append(os.environ["FAKE_REPOSITORY_SECRET"])
unexpected = os.environ.get("FAKE_UNEXPECTED_DESTINATION", "")
if (
    unexpected
    and os.environ.get("FAKE_UNEXPECTED_SCOPE", "production") == scope
    and os.environ.get("FAKE_UNEXPECTED_KIND", "secret") == kind
):
    names.append(unexpected)
for name in names:
    print(name)
' "${FAKE_INVENTORY}" "${scope}" "${kind}"
  exit 0
fi

[[ "${operation}" == set && -n "${destination}" ]]
[[ "${destination}" != "${FAKE_FAIL_SET:-}" ]] || exit 93
bytes=$(python3 -c '
import sys
value = sys.stdin.buffer.read()
if not value:
    raise SystemExit("missing streamed value")
print(len(value))
')
printf '%s|%s|%s\n' "${scope}" "${kind}" "${destination}" >>"${FAKE_DESTINATION_STATE}"
printf 'gh-set scope=%s kind=%s name=%s bytes=%s\n' \
  "${scope}" "${kind}" "${destination}" "${bytes}" >>"${FAKE_AUDIT_LOG}"
"""


class CredentialSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="nginx-credential-sync-test-")
        self.temp = pathlib.Path(self.temporary.name)
        self.fake_bin = self.temp / "bin"
        self.fake_bin.mkdir(mode=0o700)
        (self.fake_bin / "pass-cli").write_text(FAKE_PASS, encoding="utf-8")
        (self.fake_bin / "gh").write_text(FAKE_GH, encoding="utf-8")
        (self.fake_bin / "pass-cli").chmod(0o755)
        (self.fake_bin / "gh").chmod(0o755)
        self.audit = self.temp / "audit.log"
        self.state = self.temp / "destinations.state"
        self.counts = self.temp / "counts"
        self.counts.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_helper(
        self,
        *arguments: str,
        expected: int,
        xtrace: bool = False,
        helper: pathlib.Path = HELPER,
        inventory: pathlib.Path = INVENTORY,
        **overrides: str,
    ) -> subprocess.CompletedProcess[str]:
        self.audit.write_text("", encoding="utf-8")
        self.state.write_text("", encoding="utf-8")
        shutil.rmtree(self.counts)
        self.counts.mkdir(mode=0o700)
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{self.fake_bin}{os.pathsep}{environment['PATH']}",
                "TMPDIR": str(self.temp),
                "FAKE_AUDIT_LOG": str(self.audit),
                "FAKE_COUNT_DIR": str(self.counts),
                "FAKE_DESTINATION_STATE": str(self.state),
                "FAKE_INVENTORY": str(inventory),
            }
        )
        environment.update(overrides)
        command = [str(helper), *arguments]
        if xtrace:
            command = ["bash", "-x", str(helper), *arguments]
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        self.assertEqual(result.returncode, expected, result.stdout)
        self.assertNotIn("NEVER_PRINT_VALUE_", result.stdout)
        self.assertNotIn("NEVER_PRINT_VALUE_", self.audit.read_text(encoding="utf-8"))
        return result

    def audit_lines(self) -> list[str]:
        return self.audit.read_text(encoding="utf-8").splitlines()

    def test_inventory_is_exact_and_keeps_private_host_material_out_of_github(self) -> None:
        payload = json.loads(INVENTORY.read_text(encoding="utf-8"))
        production = {
            (entry["kind"], entry["destination"], entry["item"], entry["field"])
            for entry in payload["githubEnvironmentEntries"]
            if entry["scope"] == "production"
        }
        expected_production = {
            ("secret", "DEPLOY_SSH_PRIVATE_KEY", "Nginx · production deployment", "private_key"),
            ("secret", "DEPLOY_SSH_KNOWN_HOSTS", "Nginx · production deployment", "known_hosts"),
            *{
                ("secret", destination, "Nginx · production overlay names", field)
                for field, destination in {
                    "prod": "MAKEPAD_PROXY_PROD_APP_NETWORK",
                    "canary": "MAKEPAD_PROXY_CANARY_APP_NETWORK",
                    "alerteconso": "MAKEPAD_PROXY_ALERTECONSO_APP_NETWORK",
                    "le_petit_coin": "MAKEPAD_PROXY_LE_PETIT_COIN_APP_NETWORK",
                    "vif": "MAKEPAD_PROXY_VIF_APP_NETWORK",
                    "makepad_landing": "MAKEPAD_PROXY_MAKEPAD_LANDING_APP_NETWORK",
                    "evidella": "MAKEPAD_PROXY_EVIDELLA_APP_NETWORK",
                    "openpanel": "MAKEPAD_PROXY_OPENPANEL_APP_NETWORK",
                    "runtrace": "MAKEPAD_PROXY_RUNTRACE_APP_NETWORK",
                    "brio_staging": "MAKEPAD_PROXY_BRIO_STAGING_APP_NETWORK",
                    "maildev_brio_staging_web": "MAKEPAD_PROXY_MAILDEV_BRIO_STAGING_WEB_NETWORK",
                }.items()
            },
            *{
                ("variable", destination, "Nginx · production deployment", field)
                for field, destination in {
                    "host": "NGINX_DEPLOY_HOST",
                    "port": "NGINX_DEPLOY_PORT",
                    "user": "NGINX_DEPLOY_USER",
                    "remote_dir": "NGINX_DEPLOY_REMOTE_DIR",
                    "stack_name": "NGINX_DEPLOY_STACK_NAME",
                }.items()
            },
        }
        self.assertEqual(production, expected_production)
        self.assertEqual(
            payload["repositoryVariables"],
            [
                {"destination": "NGINX_PR_CHECK_APP_ID", "item": "Nginx · CI Checks App", "field": "app_id"},
                {"destination": "NGINX_CI_LAUNCHER_APP_SENDER_ID", "item": "Nginx · CI Launcher App", "field": "bot_user_id"},
                {"destination": "NGINX_CI_APPROVED_BASE_IMAGE_SHA256", "item": "Nginx · CI base image approval", "field": "qcow2_sha256"},
                {"destination": "NGINX_CI_ATTESTATION_PUBLIC_KEY", "item": "Nginx · CI hypervisor attestation", "field": "ed25519_public_key"},
            ],
        )
        github_sources = {
            (entry["item"], entry["field"])
            for group in ("githubEnvironmentEntries", "repositoryVariables")
            for entry in payload[group]
        }
        self.assertNotIn(("Nginx · CI Launcher App", "private_key"), github_sources)
        self.assertNotIn(("Nginx · CI hypervisor attestation", "ed25519_private_key"), github_sources)
        host_sources = {(entry["item"], entry["field"]) for entry in payload["hostEntries"]}
        self.assertIn(("Nginx · CI Launcher App", "private_key"), host_sources)
        self.assertIn(("Nginx · CI hypervisor attestation", "ed25519_private_key"), host_sources)
        self.assertIn(("Nginx · CI base image approval", "repository_id"), host_sources)

    def test_check_is_names_only_and_reports_host_boundaries(self) -> None:
        result = self.run_helper("--check", expected=0)
        self.assertIn("HOST_DESTINATION boundary=ci-hypervisor-root-file", result.stdout)
        self.assertIn("protection=exact-main-preserved identity=stable", result.stdout)
        self.assertIn("SUMMARY required_source_issues=0 required_destination_missing=0", result.stdout)
        self.assertFalse(any("item view" in line for line in self.audit_lines()))
        self.assertFalse(any("gh-set" in line for line in self.audit_lines()))

    def test_modes_and_write_scopes_are_bounded(self) -> None:
        result = self.run_helper("--sync", expected=1)
        self.assertIn("requires one explicit --scope", result.stdout)
        result = self.run_helper("--check", "--sync", expected=1)
        self.assertIn("select exactly one mode", result.stdout)
        result = self.run_helper("--sync", "--scope", "host-boundaries", expected=1)
        self.assertIn("audit-only", result.stdout)
        self.assertFalse(any("item view" in line for line in self.audit_lines()))
        result = self.run_helper("--check", "--scope", "other", expected=1)
        self.assertIn("not in the immutable Nginx inventory", result.stdout)

    def test_check_fails_closed_on_source_destination_and_policy_drift(self) -> None:
        result = self.run_helper(
            "--check", "--scope", "production", expected=1,
            FAKE_MISSING_ITEM="Nginx · production deployment",
        )
        self.assertIn("title=Nginx · production deployment status=missing", result.stdout)
        result = self.run_helper(
            "--check", "--scope", "production", expected=1,
            FAKE_MISSING_DESTINATION="DEPLOY_SSH_PRIVATE_KEY",
        )
        self.assertIn("name=DEPLOY_SSH_PRIVATE_KEY status=missing", result.stdout)
        result = self.run_helper(
            "--check", "--scope", "production", expected=2,
            FAKE_UNEXPECTED_DESTINATION="LEGACY_DEPLOY_TOKEN",
        )
        self.assertIn("name=LEGACY_DEPLOY_TOKEN status=legacy-or-unmanaged", result.stdout)
        result = self.run_helper(
            "--check", "--scope", "release-nginx", expected=2,
            FAKE_REPOSITORY_SECRET="BROAD_CHECKS_KEY",
        )
        self.assertIn("status=forbidden-broad-secret", result.stdout)
        result = self.run_helper(
            "--check", "--scope", "production", expected=1,
            FAKE_INVALID_REPOSITORY="1",
        )
        self.assertIn("policy=invalid-or-changed", result.stdout)
        result = self.run_helper(
            "--check", "--scope", "production", expected=1,
            FAKE_INVALID_PROTECTION="1",
        )
        self.assertIn("protection=invalid-or-ambiguous", result.stdout)

    def test_sync_completes_all_value_preflights_before_any_write(self) -> None:
        result = self.run_helper(
            "--sync", "--scope", "production", expected=1,
            FAKE_MISSING_FIELD="stack_name",
        )
        self.assertIn("required Proton field is missing", result.stdout)
        self.assertFalse(any(line.startswith("gh-set ") for line in self.audit_lines()))
        result = self.run_helper(
            "--sync", "--scope", "production", expected=1,
            FAKE_EMPTY_FIELD="known_hosts",
        )
        self.assertFalse(any(line.startswith("gh-set ") for line in self.audit_lines()))
        result = self.run_helper(
            "--sync", "--scope", "production", expected=1,
            FAKE_OVERSIZE_FIELD="private_key",
        )
        self.assertFalse(any(line.startswith("gh-set ") for line in self.audit_lines()))
        result = self.run_helper(
            "--sync", "--scope", "repository-variables", expected=1,
            FAKE_INVALID_SEMANTIC_FIELD="qcow2_sha256",
        )
        self.assertFalse(any(line.startswith("gh-set ") for line in self.audit_lines()))

    def test_policy_or_identity_change_after_value_preflight_blocks_all_writes(self) -> None:
        result = self.run_helper(
            "--sync", "--scope", "production", expected=1,
            FAKE_CHANGED_POLICY="production",
        )
        self.assertIn("environment protection changed during preflight", result.stdout)
        self.assertTrue(any(line.startswith("pass-view ") for line in self.audit_lines()))
        self.assertFalse(any(line.startswith("gh-set ") for line in self.audit_lines()))
        result = self.run_helper(
            "--sync", "--scope", "release-nginx", expected=1,
            FAKE_ENVIRONMENT_ID_CHANGE="release-nginx",
        )
        self.assertIn("environment protection changed during preflight", result.stdout)
        self.assertFalse(any(line.startswith("gh-set ") for line in self.audit_lines()))
        result = self.run_helper(
            "--sync", "--scope", "repository-variables", expected=1,
            FAKE_REPOSITORY_ID_CHANGE="1",
        )
        self.assertIn("repository identity or environment protection changed", result.stdout)
        self.assertFalse(any(line.startswith("gh-set ") for line in self.audit_lines()))

    def test_production_sync_streams_only_the_selected_scope_and_reads_back(self) -> None:
        payload = json.loads(INVENTORY.read_text(encoding="utf-8"))
        expected_entries = [
            entry for entry in payload["githubEnvironmentEntries"] if entry["scope"] == "production"
        ]
        result = self.run_helper(
            "--sync", "--scope", "production", expected=0, xtrace=True,
            FAKE_MISSING_DESTINATION="MAKEPAD_PROXY_RUNTRACE_APP_NETWORK",
        )
        self.assertIn("SYNC_COMPLETE repository=Makepad-fr/nginx vault=Makepad scope=production", result.stdout)
        lines = self.audit_lines()
        writes = [line for line in lines if line.startswith("gh-set ")]
        self.assertEqual(len(writes), len(expected_entries))
        self.assertTrue(all("scope=production" in line for line in writes))
        first_write = min(index for index, line in enumerate(lines) if line.startswith("gh-set "))
        preflight_fields = {
            line.rsplit(" field=", 1)[1]
            for line in lines[:first_write]
            if line.startswith("pass-view ")
        }
        self.assertEqual(preflight_fields, {entry["field"] for entry in expected_entries})
        self.assertFalse(any(" --body" in line or " -b" in line or " -f" in line for line in lines))
        helper_source = HELPER.read_text(encoding="utf-8")
        self.assertNotIn("gh secret delete", helper_source)
        self.assertNotIn("gh variable delete", helper_source)

    def test_repository_variable_and_release_scopes_do_not_cross_boundaries(self) -> None:
        payload = json.loads(INVENTORY.read_text(encoding="utf-8"))
        result = self.run_helper("--sync", "--scope", "repository-variables", expected=0)
        writes = [line for line in self.audit_lines() if line.startswith("gh-set ")]
        self.assertEqual(len(writes), len(payload["repositoryVariables"]))
        self.assertTrue(all("scope=repository-variables kind=variable" in line for line in writes))
        self.assertNotIn("ENVIRONMENT name=", result.stdout)

        result = self.run_helper("--sync", "--scope", "release-nginx", expected=0)
        writes = [line for line in self.audit_lines() if line.startswith("gh-set ")]
        self.assertEqual(len(writes), 1)
        self.assertIn(
            "scope=release-nginx kind=secret name=NGINX_PR_CHECK_APP_PRIVATE_KEY",
            writes[0],
        )
        self.assertIn("protection=exact-main-preserved identity=stable", result.stdout)

    def test_unexpected_name_blocks_before_any_value_read_or_write(self) -> None:
        result = self.run_helper(
            "--sync", "--scope", "production", expected=1,
            FAKE_UNEXPECTED_DESTINATION="LEGACY_DEPLOY_TOKEN",
        )
        self.assertIn("refusing to sync while broad, legacy, or unmanaged", result.stdout)
        self.assertFalse(any("item view" in line for line in self.audit_lines()))
        self.assertFalse(any(line.startswith("gh-set ") for line in self.audit_lines()))

    def test_schema_ambiguity_stops_before_authentication(self) -> None:
        copied_root = self.temp / "candidate"
        (copied_root / "deploy").mkdir(parents=True)
        (copied_root / "scripts").mkdir()
        copied_helper = copied_root / "scripts" / HELPER.name
        copied_policy = copied_root / "scripts" / "github_environment_policy.py"
        shutil.copy2(HELPER, copied_helper)
        shutil.copy2(ROOT / "scripts" / "github_environment_policy.py", copied_policy)
        payload = json.loads(INVENTORY.read_text(encoding="utf-8"))
        payload["repositoryVariables"].append(dict(payload["repositoryVariables"][0]))
        copied_inventory = copied_root / "deploy" / INVENTORY.name
        copied_inventory.write_text(json.dumps(payload), encoding="utf-8")
        result = self.run_helper(
            "--check", expected=1, helper=copied_helper, inventory=copied_inventory
        )
        self.assertIn("duplicate repository variable", result.stdout)
        self.assertEqual(self.audit.read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
