from __future__ import annotations

import importlib.util
import os
import pathlib
import unittest


ROOT = pathlib.Path(
    os.environ.get("NGINX_CANDIDATE_ROOT", pathlib.Path(__file__).resolve().parents[1])
).resolve()
MODULE_PATH = ROOT / "scripts" / "github_environment_policy.py"
SPEC = importlib.util.spec_from_file_location("github_environment_policy", MODULE_PATH)
assert SPEC and SPEC.loader
POLICY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY)


def branch_rule(rule_id: int = 101) -> dict[str, object]:
    return {"id": rule_id, "type": "branch_policy"}


def wait_rule(minutes: int = 30, rule_id: int = 102) -> dict[str, object]:
    return {"id": rule_id, "type": "wait_timer", "wait_timer": minutes}


def reviewer_rule(
    *, prevent_self_review: bool = True, rule_id: int = 103
) -> dict[str, object]:
    return {
        "id": rule_id,
        "type": "required_reviewers",
        "prevent_self_review": prevent_self_review,
        "reviewers": [
            {"type": "User", "reviewer": {"id": 7002, "login": "maintainer"}},
            {"type": "Team", "reviewer": {"id": 7001, "slug": "operators"}},
        ],
    }


def environment(
    name: str = "production", rules: list[dict[str, object]] | None = None
) -> dict[str, object]:
    return {
        "name": name,
        "protection_rules": rules if rules is not None else [branch_rule()],
        "deployment_branch_policy": {
            "protected_branches": True,
            "custom_branch_policies": False,
        },
    }


class EnvironmentPolicyTests(unittest.TestCase):
    def test_preserves_wait_timer_reviewer_ids_and_self_review_setting(self) -> None:
        current = environment(rules=[branch_rule(), wait_rule(45), reviewer_rule()])
        snapshot = POLICY.protection_snapshot(current, "production")
        self.assertEqual(snapshot, {
            "wait_timer": 45,
            "reviewers": [
                {"type": "Team", "id": 7001},
                {"type": "User", "id": 7002},
            ],
            "prevent_self_review": True,
        })
        self.assertEqual(POLICY.request_payload(snapshot), {
            "deployment_branch_policy": POLICY.EXACT_MAIN_POLICY,
            "wait_timer": 45,
            "reviewers": [
                {"type": "Team", "id": 7001},
                {"type": "User", "id": 7002},
            ],
            "prevent_self_review": True,
        })

    def test_missing_environment_does_not_force_reviewers_or_wait_timer(self) -> None:
        missing = {
            "name": "release-nginx",
            "protection_rules": [],
            "deployment_branch_policy": None,
        }
        snapshot = POLICY.protection_snapshot(missing, "release-nginx")
        self.assertEqual(POLICY.request_payload(snapshot), {
            "deployment_branch_policy": POLICY.EXACT_MAIN_POLICY,
        })

    def test_rejects_unknown_and_duplicate_protection_rules(self) -> None:
        cases = {
            "unknown": [branch_rule(), {"id": 999, "type": "custom"}],
            "duplicate": [branch_rule(), wait_rule(), wait_rule(60, 104)],
        }
        for message, rules in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    POLICY.protection_snapshot(environment(rules=rules), "production")

    def test_rejects_invalid_or_duplicate_reviewer_identity(self) -> None:
        duplicate = reviewer_rule()
        duplicate["reviewers"] = [
            {"type": "User", "reviewer": {"id": 7002}},
            {"type": "User", "reviewer": {"id": 7002}},
        ]
        with self.assertRaisesRegex(ValueError, "duplicate required reviewers"):
            POLICY.protection_snapshot(
                environment(rules=[branch_rule(), duplicate]), "production"
            )
        invalid_type = reviewer_rule()
        invalid_type["reviewers"] = [{"type": "App", "reviewer": {"id": 7002}}]
        with self.assertRaisesRegex(ValueError, "unknown reviewer type"):
            POLICY.protection_snapshot(
                environment(rules=[branch_rule(), invalid_type]), "production"
            )

    def test_rejects_truncated_or_ambiguous_environment_inventory(self) -> None:
        with self.assertRaisesRegex(ValueError, "truncated"):
            POLICY.environment_presence({
                "total_count": 2,
                "environments": [{"name": "production"}],
            }, "production")
        with self.assertRaisesRegex(ValueError, "duplicate names"):
            POLICY.environment_presence({
                "total_count": 2,
                "environments": [{"name": "production"}, {"name": "Production"}],
            }, "production")

    def test_rejects_truncated_duplicate_or_unknown_branch_policies(self) -> None:
        cases = {
            "truncated": {
                "total_count": 2,
                "branch_policies": [{"id": 1, "name": "main", "type": "branch"}],
            },
            "duplicates": {
                "total_count": 2,
                "branch_policies": [
                    {"id": 1, "name": "main", "type": "branch"},
                    {"id": 2, "name": "main", "type": "branch"},
                ],
            },
            "unknown type": {
                "total_count": 1,
                "branch_policies": [{"id": 1, "name": "main", "type": "commit"}],
            },
        }
        for message, payload in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    POLICY.branch_plan(payload)

    def test_plans_removal_of_every_known_non_main_policy(self) -> None:
        self.assertEqual(POLICY.branch_plan({
            "total_count": 3,
            "branch_policies": [
                {"id": 1, "name": "main", "type": "branch"},
                {"id": 2, "name": "release/*", "type": "branch"},
                {"id": 3, "name": "v*", "type": "tag"},
            ],
        }), (1, [2, 3]))

    def test_readback_rejects_dropped_preserved_protection(self) -> None:
        before = environment(rules=[branch_rule(), wait_rule(), reviewer_rule()])
        snapshot = POLICY.protection_snapshot(before, "production")
        after = environment(rules=[branch_rule(), wait_rule()])
        after["deployment_branch_policy"] = POLICY.EXACT_MAIN_POLICY
        branches = {
            "total_count": 1,
            "branch_policies": [{"id": 1, "name": "main", "type": "branch"}],
        }
        with self.assertRaisesRegex(ValueError, "protection rules changed"):
            POLICY.verify_environment(after, branches, "production", snapshot)

    def test_bootstrap_uses_preserved_payload_and_strict_readback(self) -> None:
        source = (ROOT / "scripts" / "configure-github-ci-policy.sh").read_text()
        request = '"${environment_policy_helper}" request'
        mutation = '--input "${environment_request_json}"'
        verification = '"${environment_policy_helper}" verify'
        self.assertIn(request, source)
        self.assertIn(mutation, source)
        self.assertIn(verification, source)
        self.assertLess(source.index(request), source.index(mutation))
        self.assertLess(source.index(mutation), source.index(verification))


if __name__ == "__main__":
    unittest.main()
