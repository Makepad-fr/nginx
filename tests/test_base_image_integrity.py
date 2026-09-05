from __future__ import annotations

import hashlib
import importlib.util
import os
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(os.environ.get("NGINX_CANDIDATE_ROOT", pathlib.Path(__file__).resolve().parents[1])).resolve()
SPEC = importlib.util.spec_from_file_location("ci_base_image", ROOT / "scripts" / "ci_base_image.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class BaseImageIntegrityTests(unittest.TestCase):
    def test_mutation_after_approval_is_detected(self):
        with tempfile.TemporaryDirectory() as value:
            image = pathlib.Path(value) / "base.qcow2"
            image.write_bytes(b"approved immutable image")
            approved = hashlib.sha256(image.read_bytes()).hexdigest()
            MODULE.assert_digest(image, approved)
            image.write_bytes(b"mutated image")
            with self.assertRaisesRegex(ValueError, "digest changed"):
                MODULE.assert_digest(image, approved)

    def test_launcher_forbids_backing_and_data_chains_and_rechecks_after_copy(self):
        script = (ROOT / "scripts" / "run-nginx-ci-jit-vm.sh").read_text()
        self.assertIn("qemu-img convert", script)
        self.assertNotIn("qemu-img create -q -f qcow2 -F qcow2 -b", script)
        self.assertGreaterEqual(script.count("ci_base_image.py"), 2)
        self.assertIn('payload.get("backing-filename")', script)
        self.assertIn('payload.get("data-file")', script)


if __name__ == "__main__":
    unittest.main()
