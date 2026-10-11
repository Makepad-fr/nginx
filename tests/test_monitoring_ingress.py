import copy
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

class MonitoringIngress(unittest.TestCase):
    def modules(self):
        for name in ('consent', 'sentry'):
            path = ROOT / 'scripts' / ('deploy-consent-ingress.py' if name == 'consent' else 'deploy-sentry-ingress.py')
            spec = importlib.util.spec_from_file_location('monitor_' + name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            yield name, module

    def test_sentry_logs_exclude_urls_and_credentials(self):
        route = (ROOT / 'sites/sentry-prod.conf.template').read_text()
        self.assertEqual(route.count('access_log /var/log/nginx/access.log runtrace_json;'), 2)
        common = (ROOT / 'sites/00-common.conf.template').read_text().split('log_format runtrace_json', 1)[1].split(';', 1)[0]
        for field in ('$request_uri', '$args', '$http_authorization', '$http_cookie'):
            self.assertNotIn(field, common)
        self.assertIn('proxy_set_header Forwarded "";', route)

    def test_release_guard_failure_precedes_docker(self):
        for name, module in self.modules():
            with self.subTest(name=name), patch.object(module, 'deployment_guard', create=True, side_effect=RuntimeError('active evidence')), patch.object(module, 'inspect') as inspect, patch.object(module, 'run', return_value=''), patch('sys.argv', [name, '--check']):
                with self.assertRaisesRegex(RuntimeError, 'active evidence'):
                    module.main()
                inspect.assert_not_called()

    def test_unchanged_spec_never_rolls_back_previous_deployment(self):
        before = {'Spec': {'Labels': {'route': 'existing'}}, 'Version': {'Index': 1}}
        for name, module in self.modules():
            with self.subTest(name=name):
                current = copy.deepcopy(before)
                current['Version']['Index'] = 2
                self.assertFalse(module.rollback_owned(before, current))

    def test_owned_update_can_restore_exact_predecessor(self):
        before = {'Spec': {'Labels': {'route': 'existing'}}}
        for name, module in self.modules():
            with self.subTest(name=name):
                self.assertTrue(module.rollback_owned(before, {'Spec': {'Labels': {'route': name}}, 'PreviousSpec': before['Spec']}))

    def test_unrelated_history_refuses_rollback(self):
        before = {'Spec': {'Labels': {'route': 'existing'}}}
        for name, module in self.modules():
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, 'unrelated'):
                module.rollback_owned(before, {'Spec': {'Labels': {'route': 'newer'}}, 'PreviousSpec': {'Labels': {'route': 'other'}}})

if __name__ == '__main__': unittest.main()
