#!/usr/bin/env python3
"""Validate the immutable Proton-to-GitHub production credential map."""

from __future__ import annotations

import json
import pathlib
import re
import stat
import sys
from typing import Any, NoReturn


EXPECTED_ENTRIES = {
    ("secret", "DEPLOY_SSH_PRIVATE_KEY", "Nginx · production deployment", "private_key"),
    ("secret", "DEPLOY_SSH_KNOWN_HOSTS", "Nginx · production deployment", "known_hosts"),
    ("secret", "MAKEPAD_PROXY_PROD_APP_NETWORK", "Nginx · production overlay names", "prod"),
    ("secret", "MAKEPAD_PROXY_CANARY_APP_NETWORK", "Nginx · production overlay names", "canary"),
    ("secret", "MAKEPAD_PROXY_ALERTECONSO_APP_NETWORK", "Nginx · production overlay names", "alerteconso"),
    ("secret", "MAKEPAD_PROXY_LE_PETIT_COIN_APP_NETWORK", "Nginx · production overlay names", "le_petit_coin"),
    ("secret", "MAKEPAD_PROXY_VIF_APP_NETWORK", "Nginx · production overlay names", "vif"),
    ("secret", "MAKEPAD_PROXY_MAKEPAD_LANDING_APP_NETWORK", "Nginx · production overlay names", "makepad_landing"),
    ("secret", "MAKEPAD_PROXY_EVIDELLA_APP_NETWORK", "Nginx · production overlay names", "evidella"),
    ("secret", "MAKEPAD_PROXY_OPENPANEL_APP_NETWORK", "Nginx · production overlay names", "openpanel"),
    ("secret", "MAKEPAD_PROXY_RUNTRACE_APP_NETWORK", "Nginx · production overlay names", "runtrace"),
    ("secret", "MAKEPAD_PROXY_BRIO_STAGING_APP_NETWORK", "Nginx · production overlay names", "brio_staging"),
    ("secret", "MAKEPAD_PROXY_MAILDEV_BRIO_STAGING_WEB_NETWORK", "Nginx · production overlay names", "maildev_brio_staging_web"),
    ("variable", "NGINX_DEPLOY_HOST", "Nginx · production deployment", "host"),
    ("variable", "NGINX_DEPLOY_PORT", "Nginx · production deployment", "port"),
    ("variable", "NGINX_DEPLOY_USER", "Nginx · production deployment", "user"),
    ("variable", "NGINX_DEPLOY_REMOTE_DIR", "Nginx · production deployment", "remote_dir"),
    ("variable", "NGINX_DEPLOY_STACK_NAME", "Nginx · production deployment", "stack_name"),
}


def fail(message: str) -> NoReturn:
    raise SystemExit(f"credential inventory violation: {message}")


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            fail(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def load_inventory(path: pathlib.Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as error:
        fail(f"cannot inspect inventory: {type(error).__name__}")
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink() or metadata.st_size > 65536:
        fail("inventory must be a bounded regular non-symlink file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        fail(f"cannot parse inventory: {type(error).__name__}")
    if not isinstance(value, dict):
        fail("inventory root must be an object")
    return value


def validate(payload: dict[str, Any]) -> None:
    if set(payload) != {"schemaVersion", "repository", "vault", "environment", "entries"}:
        fail("unexpected top-level fields")
    if payload["schemaVersion"] != 1 or payload["vault"] != "Makepad":
        fail("schema version or vault changed")
    if payload["repository"] != {
        "id": 1200300778,
        "fullName": "Makepad-fr/nginx",
        "visibility": "public",
        "defaultBranch": "main",
        "fork": False,
    }:
        fail("repository identity changed")
    if payload["environment"] != {
        "id": 14167387315,
        "name": "production",
        "branch": "main",
        "branchPolicyId": 59152649,
    }:
        fail("environment identity or branch policy changed")

    entries = payload["entries"]
    if not isinstance(entries, list):
        fail("entries must be a list")
    observed: set[tuple[str, str, str, str]] = set()
    destinations: set[tuple[str, str]] = set()
    destination_pattern = re.compile(r"[A-Z][A-Z0-9_]{1,127}")
    for offset, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {"kind", "destination", "item", "field"}:
            fail(f"entry {offset} has unexpected fields")
        kind = entry["kind"]
        destination = entry["destination"]
        item = entry["item"]
        field = entry["field"]
        if kind not in {"secret", "variable"}:
            fail(f"entry {offset} has an invalid kind")
        if not isinstance(destination, str) or destination_pattern.fullmatch(destination) is None:
            fail(f"entry {offset} has an invalid destination")
        if not isinstance(item, str) or not item or any(character in item for character in "\t\r\n"):
            fail(f"entry {offset} has an invalid item title")
        if not isinstance(field, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", field) is None:
            fail(f"entry {offset} has an invalid field")
        destination_key = (kind, destination)
        if destination_key in destinations:
            fail(f"duplicate destination {kind}/{destination}")
        destinations.add(destination_key)
        observed.add((kind, destination, item, field))
    if observed != EXPECTED_ENTRIES or len(entries) != len(EXPECTED_ENTRIES):
        fail("source/destination tuple set changed")


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        fail("expected exactly one inventory path")
    validate(load_inventory(pathlib.Path(arguments[0])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
