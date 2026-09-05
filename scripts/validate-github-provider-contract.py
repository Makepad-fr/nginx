#!/usr/bin/env python3
"""Fail closed unless the reviewed Nginx GitHub provider contract is exact."""

from __future__ import annotations

import json
import pathlib
import stat
import sys
from typing import Any, NoReturn


EXPECTED: dict[str, Any] = {
    "schemaVersion": 1,
    "owner": {"login": "Makepad-fr", "type": "Organization"},
    "repository": {"fullName": "Makepad-fr/nginx", "visibility": "public"},
    "installation": {
        "repositorySelection": "selected",
        "repositories": ["Makepad-fr/nginx"],
    },
    "apps": [
        {
            "name": "Makepad Nginx CI Checks",
            "role": "checks",
            "webhookActive": False,
            "webhookUrl": None,
            "events": [],
            "repositoryPermissions": {"metadata": "read", "checks": "write"},
            "organizationPermissions": {"self_hosted_runners": "read"},
        },
        {
            "name": "Makepad Nginx CI Launcher",
            "role": "launcher",
            "webhookActive": False,
            "webhookUrl": None,
            "events": [],
            "repositoryPermissions": {
                "metadata": "read",
                "actions": "read",
                "contents": "write",
                "issues": "write",
                "pull_requests": "read",
            },
            "organizationPermissions": {"self_hosted_runners": "write"},
        },
    ],
}


def fail(message: str) -> NoReturn:
    raise SystemExit(f"GitHub provider contract violation: {message}")


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        fail("expected exactly one contract path")
    path = pathlib.Path(arguments[0])
    try:
        metadata = path.lstat()
    except OSError as error:
        fail(f"contract cannot be inspected: {type(error).__name__}")
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink() or metadata.st_size > 65536:
        fail("contract must be a bounded regular non-symlink file")
    try:
        actual = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        fail(f"contract cannot be parsed: {type(error).__name__}")
    if actual != EXPECTED:
        fail("owner, App, webhook, event, installation, or permission scope drifted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
