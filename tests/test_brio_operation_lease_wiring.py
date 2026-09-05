#!/usr/bin/env python3
from __future__ import annotations

import os
import pathlib
import re
import unittest


ROOT = pathlib.Path(
    os.environ.get("NGINX_CANDIDATE_ROOT", pathlib.Path(__file__).resolve().parents[1])
).resolve()


class NginxOperationLeaseWiringTests(unittest.TestCase):
    def test_deployment_acquires_gates_and_releases_before_cleanup(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "manual-deploy.yml").read_text(encoding="utf-8")
        deploy = workflow.split("\n  deploy:\n", 1)[1]
        timeout = re.search(r"timeout-minutes:\s*([0-9]+)", deploy)
        self.assertIsNotNone(timeout)
        self.assertLessEqual(int(timeout.group(1)), 210)
        acquire = deploy.index("brio-operation-lease-remote.sh acquire")
        mutation = deploy.index("- name: Deploy via Docker Swarm")
        release = deploy.index("brio-operation-lease-remote.sh release")
        cleanup = deploy.index("- name: Remove job-scoped deployment material")
        self.assertLess(acquire, mutation)
        self.assertLess(mutation, release)
        self.assertLess(release, cleanup)
        self.assertIn("derive-brio-operation-owner.py deployment", deploy)
        self.assertIn("BRIO_OPERATION_LEASE_ACQUIRED=true", deploy)
        self.assertIn('[[ "${BRIO_OPERATION_LEASE_ACQUIRED:-}" == true ]] || exit 0', deploy)
        mutation_body = deploy[mutation:release]
        self.assertIn("brio-operation-lease status", mutation_body)
        self.assertIn('status "${lease_owner}" deployment', mutation_body)
        self.assertLess(
            mutation_body.rindex('status "${lease_owner}" deployment'),
            mutation_body.index('docker stack deploy --compose-file'),
        )

    def test_remote_client_is_bounded_to_deployment_coordination(self) -> None:
        client = (ROOT / "scripts" / "brio-operation-lease-remote.sh").read_text(encoding="utf-8")
        self.assertIn('[[ "${action}" =~ ^(acquire|status|release)$ ]]', client)
        self.assertIn('[[ "${owner}" =~ ^[0-9a-f]{64}$ ]]', client)
        self.assertIn(
            "/usr/local/libexec/makepad/brio-operation-lease-coordinator ${action} ${owner} deployment",
            client,
        )
        for forbidden in ("eval ", "bash -c", "sh -c", "docker "):
            self.assertNotIn(forbidden, client)

    def test_installer_creates_the_app_endpoint_without_persistent_lease_state(self) -> None:
        installer = (ROOT / "scripts" / "install-brio-operation-lease.sh").read_text(encoding="utf-8")
        self.assertIn("readonly expected_node=app", installer)
        self.assertIn("d /run/makepad/brio-operation-lease 0700 root root -", installer)
        self.assertIn("f /run/makepad/brio-operation-lease/guard 0600 root root -", installer)
        self.assertNotIn("f /run/makepad/brio-operation-lease/lease", installer)
        self.assertIn("install only when no Brio operation lease is active", installer)


if __name__ == "__main__":
    unittest.main()
