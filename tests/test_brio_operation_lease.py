#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import re
import stat
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(os.environ.get("BRIO_LEASE_CANDIDATE_ROOT", pathlib.Path(__file__).parents[1])).resolve()


def load_module(name: str, relative: str):
    specification = importlib.util.spec_from_file_location(name, ROOT / relative)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"cannot load {relative}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


LEASE = load_module("brio_operation_lease", "scripts/brio-operation-lease.py")
DISPATCH = load_module("brio_operation_lease_dispatch", "scripts/brio-operation-lease-dispatch.py")
COORDINATOR = load_module("brio_operation_lease_coordinator", "scripts/brio-operation-lease-coordinator.py")

OWNER_A = "a" * 64
OWNER_B = "b" * 64


class LeaseFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.runtime = self.root / "run" / "makepad" / "brio-operation-lease"
        self.runtime.mkdir(parents=True, mode=0o700)
        self.runtime.chmod(0o700)
        self.guard = self.runtime / "guard"
        self.guard.write_bytes(b"")
        self.guard.chmod(0o600)
        self.lease = self.runtime / "lease"
        self.node = self.root / "node"
        self.node.write_text("app\n", encoding="ascii")
        self.node.chmod(0o600)
        self.clock = [1_000_000]
        self.store = LEASE.LeaseStore(
            self.runtime,
            self.node,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
            now=lambda: self.clock[0],
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def result(self, action: str, owner: str = OWNER_A, kind: str = "deployment") -> tuple[int, dict]:
        status_code, raw = self.store.operate(action, owner, kind)
        self.assertEqual(raw, json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":")))
        return status_code, json.loads(raw)

    def test_same_owner_acquire_and_status_are_idempotent_without_renewal(self) -> None:
        status, acquired = self.result("acquire")
        self.assertEqual((status, acquired["state"]), (0, "acquired"))
        self.assertEqual(acquired["expires_at"], self.clock[0] + 14_400)
        original_expiry = acquired["expires_at"]

        self.clock[0] += 300
        status, held = self.result("acquire")
        self.assertEqual((status, held["state"], held["expires_at"]), (0, "held", original_expiry))
        status, held = self.result("status")
        self.assertEqual((status, held["state"], held["expires_at"]), (0, "held", original_expiry))

    def test_different_owner_or_kind_contends_until_exact_stale_takeover(self) -> None:
        _, acquired = self.result("acquire")
        expiry = acquired["expires_at"]
        status, busy = self.result("acquire", OWNER_B)
        self.assertEqual((status, busy["state"], busy["owner"]), (75, "busy", OWNER_A))
        status, busy = self.result("acquire", OWNER_A, "evidence")
        self.assertEqual((status, busy["state"], busy["kind"]), (75, "busy", "deployment"))

        self.clock[0] = expiry
        status, replacement = self.result("acquire", OWNER_B, "evidence")
        self.assertEqual((status, replacement["state"], replacement["owner"]), (0, "acquired", OWNER_B))
        self.assertEqual(replacement["expires_at"], expiry + 14_400)

    def test_release_is_owner_bound_reverse_safe_and_idempotent(self) -> None:
        self.result("acquire")
        status, busy = self.result("release", OWNER_B)
        self.assertEqual((status, busy["state"]), (75, "busy"))
        status, released = self.result("release")
        self.assertEqual((status, released["state"], released["released_at"]), (0, "released", self.clock[0]))
        status, absent = self.result("release")
        self.assertEqual((status, absent["state"]), (0, "absent"))

    def test_malformed_symlink_and_permission_drift_fail_closed(self) -> None:
        lease_path = self.lease
        lease_path.write_bytes(b"")
        lease_path.chmod(0o600)
        with self.assertRaisesRegex(LEASE.LeaseError, "malformed"):
            self.store.operate("acquire", OWNER_A, "deployment")

        lease_path.write_text("not-json\n", encoding="ascii")
        lease_path.chmod(0o600)
        with self.assertRaisesRegex(LEASE.LeaseError, "malformed"):
            self.store.operate("acquire", OWNER_A, "deployment")

        lease_path.unlink()
        target = self.root / "outside"
        target.write_text("{}", encoding="ascii")
        lease_path.symlink_to(target)
        with self.assertRaisesRegex(LEASE.LeaseError, "metadata is unsafe"):
            self.store.operate("acquire", OWNER_A, "deployment")

        lease_path.unlink()
        self.guard.chmod(0o644)
        with self.assertRaisesRegex(LEASE.LeaseError, "unsafe permissions"):
            self.store.operate("acquire", OWNER_A, "deployment")
        self.guard.chmod(0o600)
        self.runtime.chmod(0o755)
        with self.assertRaisesRegex(LEASE.LeaseError, "unsafe permissions"):
            self.store.operate("acquire", OWNER_A, "deployment")

    def test_pristine_absent_state_acquires_and_release_removes_state(self) -> None:
        self.assertFalse(self.lease.exists())
        status, acquired = self.result("acquire")
        self.assertEqual((status, acquired["state"]), (0, "acquired"))
        self.assertTrue(self.lease.is_file())
        status, released = self.result("release")
        self.assertEqual((status, released["state"]), (0, "released"))
        self.assertFalse(self.lease.exists())

    def test_recreated_volatile_runtime_does_not_restore_stale_lease(self) -> None:
        self.result("acquire")
        self.lease.unlink()
        status, acquired = self.result("acquire", OWNER_B, "evidence")
        self.assertEqual((status, acquired["state"], acquired["owner"]), (0, "acquired", OWNER_B))

    def test_tmpfiles_recreates_only_volatile_guard_not_stale_lease(self) -> None:
        installer = (ROOT / "scripts/install-brio-operation-lease.sh").read_text(encoding="utf-8")
        self.assertIn("d /run/makepad/brio-operation-lease 0700 root root -", installer)
        self.assertIn("f /run/makepad/brio-operation-lease/guard 0600 root root -", installer)
        self.assertNotIn("f /run/makepad/brio-operation-lease/lease", installer)
        self.assertIn("install only when no Brio operation lease is active", installer)

    def test_acquired_state_mode_drift_is_rejected(self) -> None:
        self.result("acquire")
        (self.runtime / "lease").chmod(0o644)
        with self.assertRaisesRegex(LEASE.LeaseError, "metadata is unsafe"):
            self.store.operate("status", OWNER_A, "deployment")

    def test_noncanonical_node_and_clock_inconsistent_state_fail_closed(self) -> None:
        self.node.write_text("app", encoding="ascii")
        with self.assertRaisesRegex(LEASE.LeaseError, "node identity is invalid"):
            self.store.operate("acquire", OWNER_A, "deployment")
        self.node.write_text("app\n", encoding="ascii")
        self.result("acquire")
        self.clock[0] -= 1
        with self.assertRaisesRegex(LEASE.LeaseError, "inconsistent with the system clock"):
            self.store.operate("status", OWNER_A, "deployment")


class DispatchTests(unittest.TestCase):
    def test_dispatch_executes_only_the_fixed_argv(self) -> None:
        command = f"acquire {OWNER_A} evidence"
        with mock.patch.object(DISPATCH.sys, "argv", ["dispatch"]), mock.patch.dict(os.environ, {"SSH_ORIGINAL_COMMAND": command}, clear=True), mock.patch.object(
            DISPATCH.os, "execv", side_effect=RuntimeError("captured")
        ) as execute:
            with self.assertRaisesRegex(RuntimeError, "captured"):
                DISPATCH.main()
        execute.assert_called_once_with(
            "/usr/bin/sudo",
            [
                "sudo", "-n", "--",
                "/usr/local/libexec/makepad/brio-operation-lease",
                "acquire", OWNER_A, "evidence",
            ],
        )

    def test_dispatch_rejects_shell_syntax_extra_arguments_and_uppercase_owner(self) -> None:
        invalid = (
            f"acquire {OWNER_A} evidence; id",
            f"acquire {OWNER_A} evidence extra",
            f"acquire {OWNER_A.upper()} evidence",
            f"acquire {OWNER_A} deployment\nstatus {OWNER_A} deployment",
            "",
        )
        for command in invalid:
            with self.subTest(command=command), mock.patch.dict(
                os.environ, {"SSH_ORIGINAL_COMMAND": command}, clear=True
            ), mock.patch.object(DISPATCH.sys, "argv", ["dispatch"]), mock.patch.object(DISPATCH.os, "execv") as execute, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(DISPATCH.main(), 64)
                execute.assert_not_called()


class OwnerDerivationTests(unittest.TestCase):
    def test_deployment_and_evidence_owners_are_deterministic_and_domain_separated(self) -> None:
        owner_module = load_module("derive_brio_operation_owner", "scripts/derive-brio-operation-owner.py")
        environment = {
            "GITHUB_REPOSITORY": "Makepad-fr/postgres",
            "GITHUB_RUN_ID": "12345",
            "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_SHA": "1" * 40,
        }
        outputs: dict[str, str] = {}
        for kind in ("deployment", "evidence"):
            stream = io.StringIO()
            with mock.patch.dict(os.environ, environment, clear=True), contextlib.redirect_stdout(stream):
                self.assertEqual(owner_module.main(["owner", kind]), 0)
            outputs[kind] = stream.getvalue().strip()
            self.assertRegex(outputs[kind], r"^[0-9a-f]{64}$")
        self.assertNotEqual(outputs["deployment"], outputs["evidence"])

        repeated = io.StringIO()
        with mock.patch.dict(os.environ, environment, clear=True), contextlib.redirect_stdout(repeated):
            owner_module.main(["owner", "deployment"])
        self.assertEqual(repeated.getvalue().strip(), outputs["deployment"])

    def test_owner_derivation_rejects_incomplete_or_malformed_identity(self) -> None:
        owner_module = load_module("derive_brio_operation_owner_invalid", "scripts/derive-brio-operation-owner.py")
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "0", "GITHUB_SHA": "A" * 40}, clear=True):
            with self.assertRaises(SystemExit):
                owner_module.main(["owner", "evidence"])


def remote_result(node: str, owner: str, kind: str, state: str) -> dict:
    return {
        "expires_at": 1_014_400 if state not in {"absent", "expired"} else None,
        "kind": kind,
        "node": node,
        "owner": owner,
        "released_at": 1_000_100 if state in {"absent", "expired", "released"} else None,
        "state": state,
        "version": 1,
    }


class CoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.nodes = [
            {"name": "app", "host": "app.invalid", "port": 22},
            {"name": "identity", "host": "identity.invalid", "port": 22},
            {"name": "database", "host": "database.invalid", "port": 22},
        ]

    def run_main(self, action: str, invoke):
        output = io.StringIO()
        errors = io.StringIO()
        with mock.patch.object(COORDINATOR.os, "geteuid", return_value=0), mock.patch.object(
            COORDINATOR, "load_config", return_value=self.nodes
        ), mock.patch.object(COORDINATOR, "invoke", side_effect=invoke), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                status = COORDINATOR.main(["coordinator", action, OWNER_A, "deployment"])
            except SystemExit as error:
                status = int(error.code)
        return status, output.getvalue(), errors.getvalue()

    def test_acquisition_and_release_use_fixed_opposite_orders(self) -> None:
        calls: list[tuple[str, str]] = []

        def invoke(node, action, owner, kind):
            calls.append((action, node["name"]))
            state = "acquired" if action == "acquire" else "released"
            return 0, remote_result(node["name"], owner, kind, state)

        status, _, _ = self.run_main("acquire", invoke)
        self.assertEqual(status, 0)
        self.assertEqual(calls, [("acquire", "app"), ("acquire", "identity"), ("acquire", "database")])
        calls.clear()
        status, _, _ = self.run_main("release", invoke)
        self.assertEqual(status, 0)
        self.assertEqual(calls, [("release", "database"), ("release", "identity"), ("release", "app")])

    def test_explicit_foreign_contention_cleans_only_acquired_prefix(self) -> None:
        calls: list[tuple[str, str]] = []

        def invoke(node, action, owner, kind):
            calls.append((action, node["name"]))
            if action == "acquire" and node["name"] == "identity":
                return 75, remote_result(node["name"], OWNER_B, "evidence", "busy")
            state = "acquired" if action == "acquire" else "released"
            return 0, remote_result(node["name"], owner, kind, state)

        status, _, errors = self.run_main("acquire", invoke)
        self.assertEqual(status, 78)
        self.assertEqual(
            calls,
            [
                ("acquire", "app"),
                ("acquire", "identity"),
                ("release", "app"),
            ],
        )
        self.assertIn("identity lease acquisition was rejected", errors)
        self.assertNotIn("partial lease cleanup failed", errors)

    def test_partial_acquisition_cleanup_failure_is_surfaced(self) -> None:
        calls: list[tuple[str, str]] = []

        def invoke(node, action, owner, kind):
            calls.append((action, node["name"]))
            if action == "acquire" and node["name"] == "identity":
                raise COORDINATOR.CoordinatorError("identity lease endpoint is unavailable")
            if action == "release" and node["name"] == "app":
                return 75, remote_result(node["name"], OWNER_B, "evidence", "busy")
            state = "acquired" if action == "acquire" else "released"
            return 0, remote_result(node["name"], owner, kind, state)

        status, _, errors = self.run_main("acquire", invoke)
        self.assertEqual(status, 78)
        self.assertEqual(calls[-2:], [("release", "identity"), ("release", "app")])
        self.assertIn("identity lease acquisition failed and partial lease cleanup failed", errors)

    def test_same_owner_other_kind_contention_does_not_release_current_node(self) -> None:
        calls: list[tuple[str, str]] = []

        def invoke(node, action, owner, kind):
            calls.append((action, node["name"]))
            if action == "acquire" and node["name"] == "identity":
                return 75, remote_result(node["name"], owner, "evidence", "busy")
            state = "acquired" if action == "acquire" else "released"
            return 0, remote_result(node["name"], owner, kind, state)

        status, _, errors = self.run_main("acquire", invoke)
        self.assertEqual(status, 78)
        self.assertEqual(calls[-1], ("release", "app"))
        self.assertNotIn(("release", "identity"), calls)
        self.assertIn("identity lease acquisition was rejected", errors)



if __name__ == "__main__":
    unittest.main()
