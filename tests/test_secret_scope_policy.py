from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(os.environ.get("NGINX_CANDIDATE_ROOT", pathlib.Path(__file__).resolve().parents[1])).resolve()
SCRIPT = ROOT / "scripts" / "migrate-openpanel-secret-scope.sh"
TOKEN = "github_pat_" + "T" * 32
CONFIRMATION = "Nginx · production overlay names/openpanel"


class SecretScopePolicyTests(unittest.TestCase):
    def run_policy(
        self,
        *arguments: str,
        repository_secret: bool = True,
        environment_secret: bool = True,
        environment_newer: bool = True,
        exact_main: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], bool]:
        with tempfile.TemporaryDirectory(prefix="nginx-secret-scope-") as value:
            directory = pathlib.Path(value)
            tools = directory / "tools"
            tools.mkdir()
            marker = directory / "deleted"
            fixtures = directory / "fixtures"
            fixtures.mkdir()

            (fixtures / "repository.json").write_text(json.dumps({
                "full_name": "Makepad-fr/nginx", "private": False, "default_branch": "main"
            }))
            (fixtures / "environment.json").write_text(json.dumps({
                "name": "production",
                "deployment_branch_policy": {
                    "protected_branches": False,
                    "custom_branch_policies": True,
                },
            }))
            policies = ([{"name": "main", "type": "branch"}] if exact_main
                        else [{"name": "release/*", "type": "branch"}])
            (fixtures / "policies.json").write_text(json.dumps({
                "total_count": len(policies), "branch_policies": policies
            }))
            repository_updated = "2026-09-05T10:00:00Z"
            environment_updated = ("2026-09-05T10:01:00Z" if environment_newer
                                   else "2026-09-05T09:59:00Z")
            repository_secrets = ([{
                "name": "MAKEPAD_PROXY_OPENPANEL_APP_NETWORK", "updated_at": repository_updated
            }] if repository_secret else [])
            environment_secrets = ([{
                "name": "MAKEPAD_PROXY_OPENPANEL_APP_NETWORK", "updated_at": environment_updated
            }] if environment_secret else [])
            (fixtures / "repository-secrets.json").write_text(json.dumps({
                "total_count": len(repository_secrets), "secrets": repository_secrets
            }))
            (fixtures / "repository-secrets-empty.json").write_text(json.dumps({
                "total_count": 0, "secrets": []
            }))
            (fixtures / "environment-secrets.json").write_text(json.dumps({
                "total_count": len(environment_secrets), "secrets": environment_secrets
            }))

            gh = tools / "gh"
            gh.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "endpoint=${!#}\n"
                "if [[ \" $* \" == *' --method DELETE '* ]]; then\n"
                "  [[ \"${endpoint}\" == 'repos/Makepad-fr/nginx/actions/secrets/MAKEPAD_PROXY_OPENPANEL_APP_NETWORK' ]] || exit 91\n"
                "  : >\"${DELETE_MARKER}\"\n"
                "  exit 0\n"
                "fi\n"
                "case \"${endpoint}\" in\n"
                "  repos/Makepad-fr/nginx) exec /bin/cat \"${FIXTURES}/repository.json\" ;;\n"
                "  repos/Makepad-fr/nginx/environments/production) exec /bin/cat \"${FIXTURES}/environment.json\" ;;\n"
                "  *deployment-branch-policies*) exec /bin/cat \"${FIXTURES}/policies.json\" ;;\n"
                "  repos/Makepad-fr/nginx/actions/secrets?per_page=100)\n"
                "    if [[ -e \"${DELETE_MARKER}\" ]]; then exec /bin/cat \"${FIXTURES}/repository-secrets-empty.json\"; fi\n"
                "    exec /bin/cat \"${FIXTURES}/repository-secrets.json\" ;;\n"
                "  *environments/production/secrets*) exec /bin/cat \"${FIXTURES}/environment-secrets.json\" ;;\n"
                "  *) echo \"unexpected gh endpoint: ${endpoint}\" >&2; exit 92 ;;\n"
                "esac\n"
            )
            gh.chmod(0o755)
            result = subprocess.run(
                [str(SCRIPT), *arguments],
                input=f"{TOKEN}\n",
                env={
                    **os.environ,
                    "PATH": f"{tools}:/usr/bin:/bin",
                    "DELETE_MARKER": str(marker),
                    "FIXTURES": str(fixtures),
                },
                text=True,
                capture_output=True,
            )
            return result, marker.exists()

    def test_check_rejects_repository_level_duplicate(self) -> None:
        result, deleted = self.run_policy("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("forbidden repository-level deployment secret", result.stderr)
        self.assertFalse(deleted)

    def test_check_accepts_environment_only_inventory(self) -> None:
        result, deleted = self.run_policy("--check", repository_secret=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("present only in the protected production environment", result.stdout)
        self.assertFalse(deleted)

    def test_delete_requires_exact_proton_source_confirmation(self) -> None:
        result, deleted = self.run_policy("--delete-repository-duplicate", "wrong source")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exact canonical-source confirmation", result.stderr)
        self.assertFalse(deleted)

    def test_delete_rejects_missing_or_stale_environment_copy(self) -> None:
        missing, deleted = self.run_policy(
            "--delete-repository-duplicate", CONFIRMATION, environment_secret=False
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertFalse(deleted)
        stale, deleted = self.run_policy(
            "--delete-repository-duplicate", CONFIRMATION, environment_newer=False
        )
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("re-mirror the canonical Proton field", stale.stderr)
        self.assertFalse(deleted)

    def test_delete_rejects_non_main_environment_policy(self) -> None:
        result, deleted = self.run_policy(
            "--delete-repository-duplicate", CONFIRMATION, exact_main=False
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("only from the exact main branch", result.stderr)
        self.assertFalse(deleted)

    def test_delete_targets_only_duplicate_and_proves_postcondition(self) -> None:
        result, deleted = self.run_policy("--delete-repository-duplicate", CONFIRMATION)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(deleted)
        self.assertIn("present only in the protected production environment", result.stdout)


if __name__ == "__main__":
    unittest.main()
