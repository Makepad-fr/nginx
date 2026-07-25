#!/usr/bin/env python3
import argparse
import hashlib
import json
from pathlib import Path


MAX_CONFIG_NAME_LENGTH = 64
DIGEST_LENGTH = 12


def content_addressed_name(base_name: str, config_path: Path) -> str:
    digest = hashlib.sha256(config_path.read_bytes()).hexdigest()[:DIGEST_LENGTH]
    max_base_length = MAX_CONFIG_NAME_LENGTH - DIGEST_LENGTH - 1
    return f"{base_name[:max_base_length]}_{digest}"


def render(source_path: Path, target_path: Path) -> None:
    document = json.loads(source_path.read_text())
    document.pop("name", None)

    for service in document.get("services", {}).values():
        for unsupported_null in ("command", "entrypoint"):
            if service.get(unsupported_null) is None:
                service.pop(unsupported_null, None)
        for port in service.get("ports", []):
            published = port.get("published")
            if isinstance(published, str) and published.isdecimal():
                port["published"] = int(published)

    for config_key, config in document.get("configs", {}).items():
        config_file = config.get("file")
        if not config_file:
            continue
        config_path = Path(config_file)
        if not config_path.is_file():
            raise FileNotFoundError(f"Missing config source for {config_key}: {config_path}")
        base_name = config.get("name", config_key)
        config["name"] = content_addressed_name(base_name, config_path)

    target_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Normalize Docker Compose JSON for an immutable Swarm deployment."
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    render(args.source, args.target)


if __name__ == "__main__":
    main()
