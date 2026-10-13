#!/usr/bin/env python3
import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
spec = importlib.util.spec_from_file_location('ingress', Path(__file__).with_name('deploy-posey-ingress.py'))
ingress = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ingress)

class ConfigContentTests(unittest.TestCase):
    def config(self, content):
        return json.dumps([{'ID': 'verified-id', 'Spec': {'Data': base64.b64encode(content.encode()).decode()}}])
    def test_matching_existing_config_is_reused_without_mutation(self):
        with patch.object(ingress.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, self.config('route').encode())), patch.object(ingress, 'run') as run:
            self.assertEqual(ingress.verified_config('name', 'route'), 'verified-id')
            run.assert_not_called()
    def test_name_collision_refuses_different_content_without_mutation(self):
        with patch.object(ingress.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, self.config('wrong').encode())), patch.object(ingress, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'differs'):
                ingress.verified_config('name', 'route')
            run.assert_not_called()
    def test_new_config_is_checked_after_creation(self):
        with patch.object(ingress.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)), patch.object(ingress, 'run', side_effect=['verified-id', self.config('route')]) as run:
            self.assertEqual(ingress.verified_config('name', 'route'), 'verified-id')
            self.assertEqual(run.call_args_list[0].kwargs['input'], b'route')
            self.assertEqual(run.call_args_list[1].args, ('docker', 'config', 'inspect', 'name'))
    def test_creation_race_cannot_install_other_content(self):
        with patch.object(ingress.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)), patch.object(ingress, 'run', side_effect=['verified-id', self.config('other')]):
            with self.assertRaisesRegex(RuntimeError, 'differs'):
                ingress.verified_config('name', 'route')

if __name__ == '__main__': unittest.main()
