#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def assert_digest(path: Path, expected: str) -> None:
    if file_digest(path) != expected:
        raise ValueError("reviewed base-image digest changed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("expected")
    args = parser.parse_args()
    assert_digest(args.path, args.expected)
    print(args.expected)


if __name__ == "__main__":
    main()
