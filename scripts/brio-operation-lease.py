#!/usr/bin/env python3
"""Root-owned, host-local exclusion lease for Brio mutations and evidence."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, NoReturn


RUNTIME_DIRECTORY = Path("/run/makepad/brio-operation-lease")
GUARD_PATH = RUNTIME_DIRECTORY / "guard"
LEASE_PATH = RUNTIME_DIRECTORY / "lease"
NODE_PATH = Path("/etc/makepad/brio-operation-lease-node")
TTL_SECONDS = 14_400
OWNER_PATTERN = re.compile(r"^[0-9a-f]{64}$")
KINDS = {"deployment", "evidence"}
ACTIONS = {"acquire", "status", "release"}
NODES = {"app", "identity", "database"}
STATE_KEYS = {"acquired_at", "expires_at", "kind", "node", "owner", "version"}


class LeaseError(RuntimeError):
    """A fail-closed local lease validation or operation error."""


def fail(message: str, status: int = 78) -> NoReturn:
    print(f"brio operation lease: {message}", file=sys.stderr)
    raise SystemExit(status)


def canonical_result(
    *, node: str, owner: str, kind: str, state: str,
    expires_at: int | None, released_at: int | None,
) -> str:
    return json.dumps(
        {
            "expires_at": expires_at,
            "kind": kind,
            "node": node,
            "owner": owner,
            "released_at": released_at,
            "state": state,
            "version": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class LeaseStore:
    def __init__(
        self,
        runtime_directory: Path = RUNTIME_DIRECTORY,
        node_path: Path = NODE_PATH,
        *,
        expected_uid: int = 0,
        expected_gid: int = 0,
        now: Callable[[], int] | None = None,
    ) -> None:
        self.runtime_directory = runtime_directory
        self.guard_path = runtime_directory / "guard"
        self.lease_path = runtime_directory / "lease"
        self.node_path = node_path
        self.expected_uid = expected_uid
        self.expected_gid = expected_gid
        self.now = now or (lambda: int(time.time()))

    def _validate_metadata(self, path: Path, *, directory: bool, mode: int) -> os.stat_result:
        try:
            metadata = path.lstat()
        except OSError as error:
            raise LeaseError(f"required local object is unavailable: {type(error).__name__}") from error
        expected_type = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected_type(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise LeaseError("required local object has an unsafe type")
        if metadata.st_uid != self.expected_uid or metadata.st_gid != self.expected_gid:
            raise LeaseError("required local object has unsafe ownership")
        if stat.S_IMODE(metadata.st_mode) != mode:
            raise LeaseError("required local object has unsafe permissions")
        if not directory and metadata.st_nlink != 1:
            raise LeaseError("required local file has an unsafe link count")
        return metadata

    def _read_node(self) -> str:
        metadata = self._validate_metadata(self.node_path, directory=False, mode=0o600)
        if metadata.st_size > 16:
            raise LeaseError("node identity is oversized")
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.node_path, flags)
        try:
            observed = os.fstat(descriptor)
            if (observed.st_dev, observed.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise LeaseError("node identity changed while opening")
            raw = os.read(descriptor, 17)
        finally:
            os.close(descriptor)
        if raw not in {f"{node}\n".encode("ascii") for node in NODES}:
            raise LeaseError("node identity is invalid")
        return raw[:-1].decode("ascii")

    def _read_lease(self, node: str) -> dict[str, Any] | None:
        try:
            metadata = self.lease_path.lstat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise LeaseError(f"lease state is unavailable: {type(error).__name__}") from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != self.expected_uid
            or metadata.st_gid != self.expected_gid
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_size > 512
        ):
            raise LeaseError("lease state metadata is unsafe")
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.lease_path, flags)
        try:
            observed = os.fstat(descriptor)
            if (observed.st_dev, observed.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise LeaseError("lease state changed while opening")
            raw = os.read(descriptor, 513)
        finally:
            os.close(descriptor)
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LeaseError("lease state is malformed") from error
        if not isinstance(value, dict) or set(value) != STATE_KEYS:
            raise LeaseError("lease state has an unexpected schema")
        if (
            value["version"] != 1
            or value["node"] != node
            or not isinstance(value["owner"], str)
            or OWNER_PATTERN.fullmatch(value["owner"]) is None
            or value["kind"] not in KINDS
            or not isinstance(value["acquired_at"], int)
            or isinstance(value["acquired_at"], bool)
            or not isinstance(value["expires_at"], int)
            or isinstance(value["expires_at"], bool)
            or value["acquired_at"] < 0
            or value["expires_at"] - value["acquired_at"] != TTL_SECONDS
        ):
            raise LeaseError("lease state values are invalid")
        return value

    def _replace_lease(self, encoded: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".lease-", dir=self.runtime_directory)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            written = 0
            while written < len(encoded):
                written += os.write(descriptor, encoded[written:])
            os.fsync(descriptor)
            observed = os.fstat(descriptor)
            if (
                observed.st_uid != self.expected_uid
                or observed.st_gid != self.expected_gid
                or stat.S_IMODE(observed.st_mode) != 0o600
                or observed.st_nlink != 1
            ):
                raise LeaseError("temporary lease state metadata is unsafe")
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, self.lease_path)
            directory_descriptor = os.open(self.runtime_directory, os.O_RDONLY | os.O_CLOEXEC)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _write_lease(self, value: dict[str, Any]) -> None:
        encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
        self._replace_lease(encoded)

    def _remove_lease(self) -> None:
        try:
            self.lease_path.unlink()
        except FileNotFoundError as error:
            raise LeaseError("lease state disappeared while held") from error
        directory_descriptor = os.open(self.runtime_directory, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)

    def operate(self, action: str, owner: str, kind: str) -> tuple[int, str]:
        self._validate_metadata(self.runtime_directory, directory=True, mode=0o700)
        node = self._read_node()
        guard_metadata = self._validate_metadata(self.guard_path, directory=False, mode=0o600)
        flags = os.O_RDWR | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        guard_descriptor = os.open(self.guard_path, flags)
        try:
            opened_guard = os.fstat(guard_descriptor)
            if (opened_guard.st_dev, opened_guard.st_ino) != (guard_metadata.st_dev, guard_metadata.st_ino):
                raise LeaseError("lease guard changed while opening")
            fcntl.flock(guard_descriptor, fcntl.LOCK_EX)
            now = self.now()
            if not isinstance(now, int) or isinstance(now, bool) or now < 0:
                raise LeaseError("system clock is invalid")
            active = self._read_lease(node)
            if active is not None and (
                active["acquired_at"] > now or active["expires_at"] > now + TTL_SECONDS
            ):
                raise LeaseError("lease state is inconsistent with the system clock")
            expired: dict[str, Any] | None = None
            if active is not None and active["expires_at"] <= now:
                expired = active
                self._remove_lease()
                active = None

            if action == "acquire":
                if active is not None:
                    if active["owner"] == owner and active["kind"] == kind:
                        return 0, canonical_result(
                            node=node, owner=owner, kind=kind, state="held",
                            expires_at=active["expires_at"], released_at=None,
                        )
                    return 75, canonical_result(
                        node=node, owner=active["owner"], kind=active["kind"], state="busy",
                        expires_at=active["expires_at"], released_at=None,
                    )
                value = {
                    "acquired_at": now,
                    "expires_at": now + TTL_SECONDS,
                    "kind": kind,
                    "node": node,
                    "owner": owner,
                    "version": 1,
                }
                self._write_lease(value)
                return 0, canonical_result(
                    node=node, owner=owner, kind=kind, state="acquired",
                    expires_at=value["expires_at"], released_at=None,
                )

            if action == "status":
                if active is not None:
                    if active["owner"] == owner and active["kind"] == kind:
                        return 0, canonical_result(
                            node=node, owner=owner, kind=kind, state="held",
                            expires_at=active["expires_at"], released_at=None,
                        )
                    return 75, canonical_result(
                        node=node, owner=active["owner"], kind=active["kind"], state="busy",
                        expires_at=active["expires_at"], released_at=None,
                    )
                return 3, canonical_result(
                    node=node, owner=owner, kind=kind,
                    state="expired" if expired is not None else "absent",
                    expires_at=expired["expires_at"] if expired is not None else None,
                    released_at=now,
                )

            if active is not None:
                if active["owner"] != owner or active["kind"] != kind:
                    return 75, canonical_result(
                        node=node, owner=active["owner"], kind=active["kind"], state="busy",
                        expires_at=active["expires_at"], released_at=None,
                    )
                self._remove_lease()
                return 0, canonical_result(
                    node=node, owner=owner, kind=kind, state="released",
                    expires_at=active["expires_at"], released_at=now,
                )
            return 0, canonical_result(
                node=node, owner=owner, kind=kind,
                state="expired" if expired is not None else "absent",
                expires_at=expired["expires_at"] if expired is not None else None,
                released_at=now,
            )
        finally:
            os.close(guard_descriptor)


def main(argv: list[str]) -> int:
    if os.geteuid() != 0:
        fail("must run as root", 77)
    if len(argv) != 4 or argv[1] not in ACTIONS:
        fail("usage: brio-operation-lease acquire|status|release OWNER deployment|evidence", 64)
    action, owner, kind = argv[1:]
    if OWNER_PATTERN.fullmatch(owner) is None or kind not in KINDS:
        fail("arguments do not match the bounded lease grammar", 64)
    try:
        status_code, output = LeaseStore().operate(action, owner, kind)
    except (LeaseError, OSError) as error:
        fail(str(error))
    print(output)
    return status_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
