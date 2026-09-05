#!/usr/bin/env python3
"""Derive the public lease owner from immutable GitHub run identity."""

from __future__ import annotations

import hashlib
import os
import re
import sys


REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RUN_NUMBER = re.compile(r"^[1-9][0-9]*$")
SHA = re.compile(r"^[0-9a-f]{40}$")


def required(name: str, pattern: re.Pattern[str]) -> str:
    value = os.environ.get(name, "")
    if pattern.fullmatch(value) is None:
        raise SystemExit(f"Brio operation owner: {name} is invalid")
    return value


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in {"deployment", "evidence"}:
        raise SystemExit("usage: derive-brio-operation-owner.py deployment|evidence")
    kind = argv[1]
    run_id = required("GITHUB_RUN_ID", RUN_NUMBER)
    sha = required("GITHUB_SHA", SHA)
    if kind == "deployment":
        repository = required("GITHUB_REPOSITORY", REPOSITORY)
        run_attempt = required("GITHUB_RUN_ATTEMPT", RUN_NUMBER)
        identity = f"deployment\0{repository}\0{run_id}\0{run_attempt}\0{sha}"
    else:
        identity = f"evidence\0{run_id}\0{sha}"
    print(hashlib.sha256(identity.encode("ascii")).hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
