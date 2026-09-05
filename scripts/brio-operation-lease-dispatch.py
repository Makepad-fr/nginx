#!/usr/bin/env python3
"""Forced-command adapter for the root-owned Brio operation lease."""

from __future__ import annotations

import os
import re
import sys


LEASE_EXECUTABLE = "/usr/local/libexec/makepad/brio-operation-lease"
COMMAND_PATTERN = re.compile(
    r"^(acquire|status|release) ([0-9a-f]{64}) (deployment|evidence)$"
)


def main() -> int:
    if len(sys.argv) != 1:
        print("brio lease dispatch: arguments are forbidden", file=sys.stderr)
        return 64
    original = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    if len(original) > 96 or "\n" in original or "\r" in original or "\x00" in original:
        print("brio lease dispatch: command is outside the bounded grammar", file=sys.stderr)
        return 64
    match = COMMAND_PATTERN.fullmatch(original)
    if match is None:
        print("brio lease dispatch: command is outside the bounded grammar", file=sys.stderr)
        return 64
    os.execv(
        "/usr/bin/sudo",
        ["sudo", "-n", "--", LEASE_EXECUTABLE, *match.groups()],
    )
    return 70


if __name__ == "__main__":
    raise SystemExit(main())
