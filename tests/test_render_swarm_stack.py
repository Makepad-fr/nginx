#!/usr/bin/env python3
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
RENDERER = REPO_ROOT / "scripts" / "render-swarm-stack.py"


class RenderSwarmStackTest(unittest.TestCase):
    def test_normalizes_ports_and_versions_configs_by_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "site.conf"
            config_path.write_text("server { listen 80; }\n")
            source_path = root / "compose.json"
            target_path = root / "stack.json"
            source_path.write_text(
                json.dumps(
                    {
                        "name": "nginx",
                        "services": {
                            "nginx": {
                                "command": None,
                                "entrypoint": None,
                                "networks": {"app": None},
                                "ports": [
                                    {
                                        "target": 80,
                                        "published": "80",
                                        "protocol": "tcp",
                                    }
                                ]
                            }
                        },
                        "configs": {
                            "site": {
                                "name": "nginx_site",
                                "file": str(config_path),
                            }
                        },
                    }
                )
            )

            subprocess.run(
                ["python3", str(RENDERER), str(source_path), str(target_path)],
                check=True,
            )
            rendered = json.loads(target_path.read_text())

            self.assertNotIn("name", rendered)
            self.assertNotIn("command", rendered["services"]["nginx"])
            self.assertNotIn("entrypoint", rendered["services"]["nginx"])
            self.assertEqual({"app": None}, rendered["services"]["nginx"]["networks"])
            self.assertEqual(80, rendered["services"]["nginx"]["ports"][0]["published"])
            digest = hashlib.sha256(config_path.read_bytes()).hexdigest()[:12]
            self.assertEqual(f"nginx_site_{digest}", rendered["configs"]["site"]["name"])


if __name__ == "__main__":
    unittest.main()
