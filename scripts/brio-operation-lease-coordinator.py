#!/usr/bin/env python3
"""Acquire the three host-local Brio leases in one immutable order."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn


CONFIG_DIRECTORY = Path("/etc/makepad/brio-operation-lease")
CONFIG_PATH = CONFIG_DIRECTORY / "coordinator.json"
PRIVATE_KEY_PATH = CONFIG_DIRECTORY / "id_ed25519"
KNOWN_HOSTS_PATH = CONFIG_DIRECTORY / "known_hosts"
NODE_ORDER = ("app", "identity", "database")
OWNER_PATTERN = re.compile(r"^[0-9a-f]{64}$")
HOST_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
RESULT_KEYS = {"expires_at", "kind", "node", "owner", "released_at", "state", "version"}


class CoordinatorError(RuntimeError):
    """A bounded coordination operation failed closed."""


def fail(message: str, status: int = 78) -> NoReturn:
    print(f"brio operation coordinator: {message}", file=sys.stderr)
    raise SystemExit(status)


def validate_regular(path: Path, mode: int, maximum_size: int, *, read_contents: bool = True) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise CoordinatorError(f"required root configuration is unavailable: {type(error).__name__}") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != mode
        or metadata.st_nlink != 1
        or metadata.st_size < 1
        or metadata.st_size > maximum_size
    ):
        raise CoordinatorError("required root configuration has unsafe metadata")
    if not read_contents:
        return b""
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise CoordinatorError("root configuration changed while opening")
        return os.read(descriptor, maximum_size + 1)
    finally:
        os.close(descriptor)


def load_config() -> list[dict[str, Any]]:
    directory = CONFIG_DIRECTORY.lstat()
    if (
        not stat.S_ISDIR(directory.st_mode)
        or stat.S_ISLNK(directory.st_mode)
        or directory.st_uid != 0
        or directory.st_gid != 0
        or stat.S_IMODE(directory.st_mode) != 0o700
    ):
        raise CoordinatorError("coordinator configuration directory is unsafe")
    try:
        value = json.loads(validate_regular(CONFIG_PATH, 0o600, 2048))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CoordinatorError("coordinator configuration is malformed") from error
    if not isinstance(value, dict) or set(value) != {"nodes", "user", "version"}:
        raise CoordinatorError("coordinator configuration schema is invalid")
    if value["version"] != 1 or value["user"] != "brio-operation-lease":
        raise CoordinatorError("coordinator identity is invalid")
    nodes = value["nodes"]
    if not isinstance(nodes, list) or len(nodes) != 3:
        raise CoordinatorError("coordinator node inventory is invalid")
    for offset, node in enumerate(nodes):
        if not isinstance(node, dict) or set(node) != {"host", "name", "port"}:
            raise CoordinatorError("coordinator node schema is invalid")
        if (
            node["name"] != NODE_ORDER[offset]
            or not isinstance(node["host"], str)
            or HOST_PATTERN.fullmatch(node["host"]) is None
            or not isinstance(node["port"], int)
            or isinstance(node["port"], bool)
            or not 1 <= node["port"] <= 65535
        ):
            raise CoordinatorError("coordinator node value is invalid")
    validate_regular(PRIVATE_KEY_PATH, 0o600, 16_384, read_contents=False)
    validate_regular(KNOWN_HOSTS_PATH, 0o600, 65_536, read_contents=False)
    return nodes


def validate_result(raw: str, expected_node: str) -> dict[str, Any]:
    if not raw or len(raw.encode("utf-8")) > 1024 or raw.count("\n") > 1:
        raise CoordinatorError("lease endpoint returned an invalid response size")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise CoordinatorError("lease endpoint returned malformed JSON") from error
    if not isinstance(value, dict) or set(value) != RESULT_KEYS:
        raise CoordinatorError("lease endpoint returned an unexpected schema")
    if (
        value["version"] != 1
        or value["node"] != expected_node
        or not isinstance(value["owner"], str)
        or OWNER_PATTERN.fullmatch(value["owner"]) is None
        or value["kind"] not in {"deployment", "evidence"}
        or value["state"] not in {"absent", "acquired", "busy", "expired", "held", "released"}
        or (value["expires_at"] is not None and (not isinstance(value["expires_at"], int) or isinstance(value["expires_at"], bool)))
        or (value["released_at"] is not None and (not isinstance(value["released_at"], int) or isinstance(value["released_at"], bool)))
    ):
        raise CoordinatorError("lease endpoint returned invalid values")
    return value


def invoke(node: dict[str, Any], action: str, owner: str, kind: str) -> tuple[int, dict[str, Any]]:
    command = [
        "/usr/bin/ssh",
        "-F", "/dev/null",
        "-T",
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS_PATH}",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        "-o", "ConnectionAttempts=1",
        "-i", str(PRIVATE_KEY_PATH),
        "-p", str(node["port"]),
        f"brio-operation-lease@{node['host']}",
        f"{action} {owner} {kind}",
    ]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=20,
            check=False,
            env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CoordinatorError(f"{node['name']} lease endpoint is unavailable") from error
    result = validate_result(completed.stdout, node["name"])
    return completed.returncode, result


def emit(result: dict[str, Any]) -> None:
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


def release_nodes(nodes: list[dict[str, Any]], owner: str, kind: str) -> bool:
    succeeded = True
    for node in reversed(nodes):
        try:
            status, result = invoke(node, "release", owner, kind)
            emit(result)
            if status != 0 or result["owner"] != owner or result["kind"] != kind or result["state"] not in {"released", "absent", "expired"}:
                succeeded = False
        except CoordinatorError:
            succeeded = False
    return succeeded


def main(argv: list[str]) -> int:
    if os.geteuid() != 0:
        fail("must run as root", 77)
    if len(argv) != 4 or argv[1] not in {"acquire", "status", "release"}:
        fail("usage: brio-operation-lease-coordinator acquire|status|release OWNER deployment|evidence", 64)
    action, owner, kind = argv[1:]
    if OWNER_PATTERN.fullmatch(owner) is None or kind not in {"deployment", "evidence"}:
        fail("arguments do not match the bounded coordinator grammar", 64)
    try:
        nodes = load_config()
        if action == "release":
            if not release_nodes(nodes, owner, kind):
                raise CoordinatorError("one or more lease releases failed")
            return 0
        if action == "status":
            for node in nodes:
                status, result = invoke(node, action, owner, kind)
                emit(result)
                if status != 0 or result["owner"] != owner or result["kind"] != kind or result["state"] != "held":
                    raise CoordinatorError(f"{node['name']} lease is not held by the requested owner")
            return 0

        for offset, node in enumerate(nodes):
            attempted_nodes = nodes[: offset + 1]
            try:
                status, result = invoke(node, action, owner, kind)
                emit(result)
                if status == 75 and result["state"] == "busy" and (
                    result["owner"] != owner or result["kind"] != kind
                ):
                    attempted_nodes = nodes[:offset]
                if status != 0 or result["owner"] != owner or result["kind"] != kind or result["state"] not in {"acquired", "held"}:
                    raise CoordinatorError(f"{node['name']} lease acquisition was rejected")
            except CoordinatorError as acquisition_error:
                if not release_nodes(attempted_nodes, owner, kind):
                    raise CoordinatorError(
                        f"{node['name']} lease acquisition failed and partial lease cleanup failed"
                    ) from acquisition_error
                raise
        return 0
    except (CoordinatorError, OSError) as error:
        fail(str(error))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
