#!/usr/bin/env python3
"""Validate and preserve GitHub deployment-environment protection rules."""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any


EXACT_MAIN_POLICY = {
    "protected_branches": False,
    "custom_branch_policies": True,
}
SUPPORTED_RULE_TYPES = {"branch_policy", "required_reviewers", "wait_timer"}


def fail(message: str) -> None:
    raise ValueError(message)


def load_object(path: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(pathlib.Path(path).read_text())
    except (OSError, json.JSONDecodeError) as error:
        fail(f"{label} is not valid JSON: {error}")
    if not isinstance(value, dict):
        fail(f"{label} must be a JSON object")
    return value


def positive_id(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        fail(f"{label} must be a positive integer")
    return value


def exact_count(payload: dict[str, Any], key: str, label: str) -> list[Any]:
    values = payload.get(key)
    total_count = payload.get("total_count")
    if not isinstance(values, list):
        fail(f"{label} must contain a {key} array")
    if isinstance(total_count, bool) or not isinstance(total_count, int) or total_count < 0:
        fail(f"{label} has no valid total_count")
    if total_count != len(values):
        fail(f"{label} response is truncated")
    return values


def environment_presence(payload: dict[str, Any], expected_name: str) -> str:
    environments = exact_count(payload, "environments", "environment inventory")
    names: list[str] = []
    for environment in environments:
        if not isinstance(environment, dict):
            fail("environment inventory contains a non-object entry")
        name = environment.get("name")
        if not isinstance(name, str) or not name:
            fail("environment inventory contains an invalid name")
        names.append(name)
    folded = [name.casefold() for name in names]
    if len(folded) != len(set(folded)):
        fail("environment inventory contains duplicate names")
    matches = [name for name in names if name.casefold() == expected_name.casefold()]
    if not matches:
        return "missing"
    if matches != [expected_name]:
        fail(f"environment name must use exact canonical spelling {expected_name}")
    return "present"


def deployment_policy(value: Any, label: str) -> dict[str, bool] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        fail(f"{label} deployment_branch_policy must be an object or null")
    protected = value.get("protected_branches")
    custom = value.get("custom_branch_policies")
    if set(value) != {"protected_branches", "custom_branch_policies"}:
        fail(f"{label} deployment_branch_policy has unknown fields")
    if not isinstance(protected, bool) or not isinstance(custom, bool) or protected == custom:
        fail(f"{label} deployment_branch_policy is invalid")
    return {"protected_branches": protected, "custom_branch_policies": custom}


def protection_snapshot(environment: dict[str, Any], expected_name: str) -> dict[str, Any]:
    if environment.get("name") != expected_name:
        fail(f"environment response is not exact {expected_name}")
    rules = environment.get("protection_rules")
    if not isinstance(rules, list):
        fail(f"{expected_name} protection_rules must be an array")
    if len(rules) > 6:
        fail(f"{expected_name} protection_rules exceeds GitHub's supported maximum")

    by_type: dict[str, dict[str, Any]] = {}
    for rule in rules:
        if not isinstance(rule, dict):
            fail(f"{expected_name} contains a non-object protection rule")
        rule_type = rule.get("type")
        if rule_type not in SUPPORTED_RULE_TYPES:
            fail(f"{expected_name} contains unknown protection rule {rule_type!r}")
        if rule_type in by_type:
            fail(f"{expected_name} contains duplicate {rule_type} protection rules")
        positive_id(rule.get("id"), f"{expected_name} {rule_type} rule ID")
        by_type[rule_type] = rule

    current_deployment_policy = deployment_policy(
        environment.get("deployment_branch_policy"), expected_name
    )
    has_branch_rule = "branch_policy" in by_type
    if has_branch_rule != (current_deployment_policy is not None):
        fail(f"{expected_name} branch policy rule does not match deployment_branch_policy")

    wait_timer: int | None = None
    if wait_rule := by_type.get("wait_timer"):
        wait_timer = wait_rule.get("wait_timer")
        if (
            isinstance(wait_timer, bool)
            or not isinstance(wait_timer, int)
            or not 0 <= wait_timer <= 43_200
        ):
            fail(f"{expected_name} wait timer is outside GitHub's supported range")

    reviewers: list[dict[str, Any]] | None = None
    prevent_self_review: bool | None = None
    if reviewer_rule := by_type.get("required_reviewers"):
        raw_reviewers = reviewer_rule.get("reviewers")
        prevent_self_review = reviewer_rule.get("prevent_self_review")
        if not isinstance(raw_reviewers, list) or not 1 <= len(raw_reviewers) <= 6:
            fail(f"{expected_name} required reviewers must contain one to six entries")
        if not isinstance(prevent_self_review, bool):
            fail(f"{expected_name} prevent_self_review must be boolean")
        reviewers = []
        identities: set[tuple[str, int]] = set()
        for raw_reviewer in raw_reviewers:
            if not isinstance(raw_reviewer, dict):
                fail(f"{expected_name} contains a non-object reviewer")
            reviewer_type = raw_reviewer.get("type")
            if reviewer_type not in {"User", "Team"}:
                fail(f"{expected_name} contains unknown reviewer type {reviewer_type!r}")
            reviewer = raw_reviewer.get("reviewer")
            if not isinstance(reviewer, dict):
                fail(f"{expected_name} reviewer is missing its identity")
            reviewer_id = positive_id(
                reviewer.get("id"), f"{expected_name} {reviewer_type} reviewer ID"
            )
            identity = (reviewer_type, reviewer_id)
            if identity in identities:
                fail(f"{expected_name} contains duplicate required reviewers")
            identities.add(identity)
            reviewers.append({"type": reviewer_type, "id": reviewer_id})
        reviewers.sort(key=lambda item: (item["type"], item["id"]))

    return {
        "wait_timer": wait_timer,
        "reviewers": reviewers,
        "prevent_self_review": prevent_self_review,
    }


def request_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {"deployment_branch_policy": EXACT_MAIN_POLICY}
    if snapshot["wait_timer"] is not None:
        payload["wait_timer"] = snapshot["wait_timer"]
    if snapshot["reviewers"] is not None:
        payload["reviewers"] = snapshot["reviewers"]
        payload["prevent_self_review"] = snapshot["prevent_self_review"]
    return payload


def branch_plan(payload: dict[str, Any]) -> tuple[int, list[int]]:
    policies = exact_count(payload, "branch_policies", "deployment branch policy inventory")
    identities: set[tuple[str, str]] = set()
    ids: set[int] = set()
    main_count = 0
    delete_ids: list[int] = []
    for policy in policies:
        if not isinstance(policy, dict):
            fail("deployment branch policy inventory contains a non-object entry")
        name = policy.get("name")
        policy_type = policy.get("type")
        policy_id = positive_id(policy.get("id"), "deployment branch policy ID")
        if not isinstance(name, str) or not name:
            fail("deployment branch policy has an invalid name")
        if policy_type not in {"branch", "tag"}:
            fail(f"deployment branch policy has unknown type {policy_type!r}")
        identity = (name, policy_type)
        if identity in identities or policy_id in ids:
            fail("deployment branch policy inventory contains duplicates")
        identities.add(identity)
        ids.add(policy_id)
        if identity == ("main", "branch"):
            main_count += 1
        else:
            delete_ids.append(policy_id)
    return main_count, delete_ids


def verify_environment(
    environment: dict[str, Any],
    branches: dict[str, Any],
    expected_name: str,
    expected_snapshot: dict[str, Any],
) -> None:
    observed_snapshot = protection_snapshot(environment, expected_name)
    if deployment_policy(environment.get("deployment_branch_policy"), expected_name) != EXACT_MAIN_POLICY:
        fail(f"{expected_name} must use exact custom deployment branches")
    if observed_snapshot != expected_snapshot:
        fail(f"{expected_name} protection rules changed during branch-policy reconciliation")
    main_count, delete_ids = branch_plan(branches)
    if main_count != 1 or delete_ids:
        fail(f"{expected_name} must permit only the exact main branch")


def dump(value: Any) -> None:
    print(json.dumps(value, separators=(",", ":"), sort_keys=True))


def main(arguments: list[str]) -> None:
    if not arguments:
        fail("a command is required")
    command, *values = arguments
    if command == "environment-presence" and len(values) == 2:
        print(environment_presence(load_object(values[0], "environment inventory"), values[1]))
    elif command in {"request", "snapshot"} and len(values) == 2:
        snapshot = protection_snapshot(load_object(values[0], values[1]), values[1])
        dump(request_payload(snapshot) if command == "request" else snapshot)
    elif command == "branch-plan" and len(values) == 1:
        main_count, delete_ids = branch_plan(load_object(values[0], "deployment branches"))
        print(main_count)
        for policy_id in delete_ids:
            print(policy_id)
    elif command == "verify" and len(values) == 4:
        verify_environment(
            load_object(values[0], values[2]),
            load_object(values[1], "deployment branches"),
            values[2],
            load_object(values[3], "preserved protection snapshot"),
        )
    else:
        fail(f"invalid {command!r} command arguments")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except ValueError as error:
        raise SystemExit(str(error)) from error
