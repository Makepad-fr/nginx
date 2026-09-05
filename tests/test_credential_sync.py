from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sync-github-credentials.sh"
VALIDATOR = ROOT / "scripts" / "validate-credential-inventory.py"
INVENTORY = ROOT / "deploy" / "credential-inventory.json"


class CredentialSyncTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_command(self, *command: str, cwd: pathlib.Path | None = None, env=None):
        return subprocess.run(
            command,
            cwd=cwd or ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_inventory_is_exact_and_excludes_discarded_runner_authority(self) -> None:
        result = self.run_command("python3", str(VALIDATOR), str(INVENTORY))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(INVENTORY.read_text())
        self.assertEqual(payload["environment"]["name"], "production")
        self.assertEqual(len(payload["entries"]), 18)
        serialized = json.dumps(payload)
        for forbidden in (
            "release-nginx",
            "runner",
            "GitHub App",
            "oauth",
            "NGINX_PR_CHECK_APP",
            "NGINX_CI_",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_validator_rejects_redirection_duplicate_keys_and_symlinks(self) -> None:
        payload = json.loads(INVENTORY.read_text())
        payload["entries"][0]["field"] = "wrong"
        redirected = self.root / "redirected.json"
        redirected.write_text(json.dumps(payload))
        result = self.run_command("python3", str(VALIDATOR), str(redirected))
        self.assertNotEqual(result.returncode, 0)

        duplicate = self.root / "duplicate.json"
        duplicate.write_text(INVENTORY.read_text().replace('"schemaVersion": 1,', '"schemaVersion": 1, "schemaVersion": 1,'))
        result = self.run_command("python3", str(VALIDATOR), str(duplicate))
        self.assertNotEqual(result.returncode, 0)

        linked = self.root / "linked.json"
        linked.symlink_to(INVENTORY)
        result = self.run_command("python3", str(VALIDATOR), str(linked))
        self.assertNotEqual(result.returncode, 0)

    def make_candidate(self) -> tuple[pathlib.Path, str]:
        candidate = self.root / "candidate"
        (candidate / "scripts").mkdir(parents=True)
        (candidate / "deploy").mkdir()
        shutil.copy2(SCRIPT, candidate / "scripts" / SCRIPT.name)
        shutil.copy2(VALIDATOR, candidate / "scripts" / VALIDATOR.name)
        shutil.copy2(INVENTORY, candidate / "deploy" / INVENTORY.name)
        subprocess.run(["git", "init", "-b", "main"], cwd=candidate, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=candidate, check=True)
        subprocess.run(["git", "config", "user.name", "Credential Test"], cwd=candidate, check=True)
        subprocess.run(["git", "add", "."], cwd=candidate, check=True)
        subprocess.run(["git", "commit", "-m", "test fixture"], cwd=candidate, check=True, capture_output=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=candidate, check=True, text=True, capture_output=True
        ).stdout.strip()
        return candidate, head

    @staticmethod
    def values() -> dict[str, str]:
        values = {
            "DEPLOY_SSH_PRIVATE_KEY": "-----BEGIN OPENSSH PRIVATE KEY-----\nQUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=\n-----END OPENSSH PRIVATE KEY-----",
            "DEPLOY_SSH_KNOWN_HOSTS": "135.181.141.31 ssh-ed25519 QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=",
            "NGINX_DEPLOY_HOST": "135.181.141.31",
            "NGINX_DEPLOY_PORT": "22",
            "NGINX_DEPLOY_USER": "makepad",
            "NGINX_DEPLOY_REMOTE_DIR": "/srv/makepad/nginx",
            "NGINX_DEPLOY_STACK_NAME": "makepad-edge",
        }
        payload = json.loads(INVENTORY.read_text())
        for entry in payload["entries"]:
            if entry["destination"].startswith("MAKEPAD_PROXY_"):
                values[entry["destination"]] = "network_" + entry["field"]
        values["MAKEPAD_PROXY_BRIO_STAGING_APP_NETWORK"] = "makepad_brio_staging_app"
        values["MAKEPAD_PROXY_MAILDEV_BRIO_STAGING_WEB_NETWORK"] = "makepad_brio_staging_maildev_web"
        return values

    def mock_environment(self, head: str, missing: str = "") -> dict[str, str]:
        mock_bin = self.root / "bin"
        mock_bin.mkdir(exist_ok=True)
        log = self.root / "mock.log"
        values = self.root / "values.json"
        values.write_text(json.dumps(self.values()))
        inventory = json.loads(INVENTORY.read_text())
        secret_names = sorted(
            entry["destination"] for entry in inventory["entries"] if entry["kind"] == "secret" and entry["destination"] != missing
        )
        variable_names = sorted(
            entry["destination"] for entry in inventory["entries"] if entry["kind"] == "variable" and entry["destination"] != missing
        )

        pass_cli = mock_bin / "pass-cli"
        pass_cli.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json, os, pathlib, sys
                log = pathlib.Path(os.environ["MOCK_LOG"])
                with log.open("a") as handle:
                    handle.write("pass-cli " + " ".join(sys.argv[1:]) + "\\n")
                args = sys.argv[1:]
                if args == ["test"]:
                    raise SystemExit(0)
                if args[:2] == ["item", "list"]:
                    print(json.dumps({"items": [{"title": "Nginx · production deployment"}, {"title": "Nginx · production overlay names"}]}))
                    raise SystemExit(0)
                if args[:2] == ["item", "view"]:
                    if os.environ.get("MOCK_ALLOW_ITEM_VIEW") != "1":
                        raise SystemExit(97)
                    title = args[args.index("--item-title") + 1]
                    field = args[args.index("--field") + 1]
                    inventory = json.loads(pathlib.Path(os.environ["MOCK_INVENTORY"]).read_text())
                    values = json.loads(pathlib.Path(os.environ["MOCK_VALUES"]).read_text())
                    for entry in inventory["entries"]:
                        if entry["item"] == title and entry["field"] == field:
                            print(values[entry["destination"]])
                            raise SystemExit(0)
                raise SystemExit(98)
                """
            )
        )
        pass_cli.chmod(0o755)

        gh = mock_bin / "gh"
        gh.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json, os, pathlib, sys
                args = sys.argv[1:]
                log = pathlib.Path(os.environ["MOCK_LOG"])
                with log.open("a") as handle:
                    handle.write("gh " + " ".join(args) + "\\n")
                if args[:2] == ["auth", "status"]:
                    raise SystemExit(0)
                if args and args[0] == "api":
                    endpoint = next((arg for arg in args if arg.startswith("repos/")), "")
                    if endpoint == "repos/Makepad-fr/nginx":
                        print(json.dumps({"id": 1200300778, "full_name": "Makepad-fr/nginx", "visibility": "public", "default_branch": "main", "fork": False}))
                    elif endpoint.endswith("/environments/production"):
                        print(json.dumps({"id": 14167387315, "name": "production", "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}}))
                    elif "deployment-branch-policies" in endpoint:
                        print(json.dumps({"total_count": 1, "branch_policies": [{"id": 59152649, "node_id": "fixture", "name": "main", "type": "branch"}]}))
                    elif endpoint.endswith("/branches/main/protection"):
                        print(json.dumps({
                            "required_status_checks": {"strict": True, "checks": [{"context": "policy-and-render", "app_id": 15368}]},
                            "required_pull_request_reviews": {"dismiss_stale_reviews": True, "require_code_owner_reviews": True, "require_last_push_approval": True, "required_approving_review_count": 1},
                            "required_signatures": {"enabled": True}, "enforce_admins": {"enabled": True},
                            "required_linear_history": {"enabled": True}, "required_conversation_resolution": {"enabled": True},
                            "allow_force_pushes": {"enabled": False}, "allow_deletions": {"enabled": False}
                        }))
                    elif endpoint.endswith("/commits/main"):
                        print(os.environ["MOCK_HEAD"])
                    elif "/environments/production/variables/" in endpoint:
                        destination = endpoint.rsplit("/", 1)[-1]
                        print(json.loads(pathlib.Path(os.environ["MOCK_VALUES"]).read_text())[destination])
                    else:
                        raise SystemExit(89)
                    raise SystemExit(0)
                if len(args) >= 2 and args[0] in {"secret", "variable"} and args[1] == "list":
                    if "--env" not in args:
                        raise SystemExit(0)
                    names = os.environ["MOCK_SECRET_NAMES"] if args[0] == "secret" else os.environ["MOCK_VARIABLE_NAMES"]
                    print(names)
                    raise SystemExit(0)
                if len(args) >= 3 and args[0] in {"secret", "variable"} and args[1] == "set":
                    destination = args[2]
                    value = sys.stdin.read().rstrip("\\r\\n")
                    expected = json.loads(pathlib.Path(os.environ["MOCK_VALUES"]).read_text())[destination]
                    if value != expected:
                        raise SystemExit(88)
                    raise SystemExit(0)
                raise SystemExit(99)
                """
            )
        )
        gh.chmod(0o755)

        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{mock_bin}:{environment['PATH']}",
                "MOCK_LOG": str(log),
                "MOCK_VALUES": str(values),
                "MOCK_INVENTORY": str(INVENTORY),
                "MOCK_HEAD": head,
                "MOCK_SECRET_NAMES": "\n".join(secret_names),
                "MOCK_VARIABLE_NAMES": "\n".join(variable_names),
            }
        )
        return environment

    def test_check_is_names_only_and_reports_complete_state(self) -> None:
        candidate, head = self.make_candidate()
        environment = self.mock_environment(head)
        result = self.run_command(
            str(candidate / "scripts" / SCRIPT.name), "--check", "--scope", "production", cwd=candidate, env=environment
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        log = pathlib.Path(environment["MOCK_LOG"]).read_text()
        self.assertNotIn("item view", log)
        self.assertNotIn(" secret set ", log)
        self.assertNotIn(" variable set ", log)
        self.assertIn("CHECK_COMPLETE", result.stdout)

    def test_check_fails_when_a_managed_destination_is_missing(self) -> None:
        candidate, head = self.make_candidate()
        environment = self.mock_environment(head, "MAKEPAD_PROXY_BRIO_STAGING_APP_NETWORK")
        result = self.run_command(str(candidate / "scripts" / SCRIPT.name), "--check", cwd=candidate, env=environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("status=missing", result.stdout)
        self.assertNotIn("item view", pathlib.Path(environment["MOCK_LOG"]).read_text())

    def test_sync_requires_confirmation_and_streams_exact_values(self) -> None:
        candidate, head = self.make_candidate()
        environment = self.mock_environment(head)
        denied = self.run_command(
            str(candidate / "scripts" / SCRIPT.name), "--sync", "--scope", "production", cwd=candidate, env=environment
        )
        self.assertNotEqual(denied.returncode, 0)
        log_path = pathlib.Path(environment["MOCK_LOG"])
        self.assertNotIn("item view", log_path.read_text() if log_path.exists() else "")

        log_path.write_text("")
        environment["MOCK_ALLOW_ITEM_VIEW"] = "1"
        result = self.run_command(
            str(candidate / "scripts" / SCRIPT.name),
            "--sync",
            "--scope",
            "production",
            "--confirm",
            "Makepad-fr/nginx:production",
            cwd=candidate,
            env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        log = pathlib.Path(environment["MOCK_LOG"]).read_text()
        self.assertEqual(log.count("gh secret set "), 13)
        self.assertEqual(log.count("gh variable set "), 5)
        self.assertIn("SYNC_COMPLETE", result.stdout)
        non_distinct_public_values = {"22", "makepad"}
        for value in (value for value in self.values().values() if value not in non_distinct_public_values):
            self.assertNotIn(value, result.stdout + result.stderr + log)


if __name__ == "__main__":
    unittest.main()
