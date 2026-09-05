from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(os.environ.get("NGINX_CANDIDATE_ROOT", pathlib.Path(__file__).resolve().parents[1])).resolve()
SOURCE_SHA = "a" * 40
APP_ID = 4242
RUN_ID = 1001
ATTEMPT = 2


class ReleaseCIGateTests(unittest.TestCase):
    def fixtures(self, directory: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
        run = {
            "id": RUN_ID,
            "run_attempt": ATTEMPT,
            "head_sha": SOURCE_SHA,
            "head_branch": "main",
            "event": "push",
            "status": "completed",
            "conclusion": "success",
            "name": "CI",
            "path": ".github/workflows/ci.yml",
            "html_url": f"https://github.example/runs/{RUN_ID}",
            "repository": {"full_name": "Makepad-fr/nginx"},
        }
        check = {
            "name": "policy-and-render",
            "head_sha": SOURCE_SHA,
            "status": "completed",
            "conclusion": "success",
            "external_id": f"nginx-ci:push:{RUN_ID}:{ATTEMPT}:{'N' * 43}",
            "details_url": run["html_url"],
            "app": {"id": APP_ID},
        }
        runs = directory / "runs.json"
        checks = directory / "checks.json"
        runs.write_text(json.dumps({"total_count": 1, "workflow_runs": [run]}))
        checks.write_text(json.dumps({"total_count": 1, "check_runs": [check]}))
        return runs, checks

    def run_gate(self, mutate=None) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="nginx-ci-gate-") as value:
            directory = pathlib.Path(value)
            runs, checks = self.fixtures(directory)
            if mutate:
                mutate(runs, checks)
            tools = directory / "tools"
            tools.mkdir()
            curl = tools / "curl"
            curl.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "last=${!#}\n"
                "if [[ \"${last}\" == *'/actions/workflows/ci.yml/runs?'* ]]; then\n"
                f"  exec /bin/cat '{runs}'\n"
                "fi\n"
                f"exec /bin/cat '{checks}'\n"
            )
            curl.chmod(0o755)
            return subprocess.run(
                [
                    str(ROOT / "scripts" / "require-successful-ci.sh"),
                    "Makepad-fr/nginx",
                    SOURCE_SHA,
                    str(APP_ID),
                ],
                env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "GH_TOKEN": "T" * 32},
                text=True,
                capture_output=True,
            )

    def test_accepts_only_matching_app_bound_push_attestation(self) -> None:
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("App-bound Nginx CI and teardown", result.stdout)

    def test_native_success_without_matching_attestation_cannot_release(self) -> None:
        def mutate(_runs: pathlib.Path, checks: pathlib.Path) -> None:
            payload = json.loads(checks.read_text())
            payload["check_runs"] = []
            payload["total_count"] = 0
            checks.write_text(json.dumps(payload))

        result = self.run_gate(mutate)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no successful App-bound", result.stderr)

    def test_wrong_app_or_run_attempt_cannot_release(self) -> None:
        for field, replacement in (("app", {"id": APP_ID + 1}), ("external_id", f"nginx-ci:push:{RUN_ID}:3:{'N' * 43}")):
            def mutate(_runs: pathlib.Path, checks: pathlib.Path, field=field, replacement=replacement) -> None:
                payload = json.loads(checks.read_text())
                payload["check_runs"][0][field] = replacement
                checks.write_text(json.dumps(payload))

            with self.subTest(field=field):
                self.assertNotEqual(self.run_gate(mutate).returncode, 0)


if __name__ == "__main__":
    unittest.main()
