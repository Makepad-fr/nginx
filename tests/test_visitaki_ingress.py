import importlib.util
import json
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('ingress', Path(__file__).resolve().parents[1] / 'scripts/deploy-visitaki-ingress.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ScopedUpdates(unittest.TestCase):
    def baseline(self, configs=()):
        return {'Spec': {'TaskTemplate': {'ContainerSpec': {'Configs': list(configs)}, 'Networks': [{'Target': 'neighbor'}]}}}

    def test_replacement_removes_only_owned_route(self):
        unrelated = {'ConfigID': 'neighbor-config', 'ConfigName': 'neighbor', 'File': {'Name': '/etc/nginx/templates/other.conf.template'}}
        previous = {'ConfigID': 'bootstrap', 'ConfigName': 'visitaki_bootstrap', 'File': {'Name': '/etc/nginx/templates/visitaki-identity.conf.template'}}
        changes, removed, added = module.changes_for(self.baseline([unrelated, previous]), {'visitaki-identity': 'new'}, [])
        self.assertEqual(removed, ['bootstrap'])
        self.assertNotIn('neighbor-config', changes)
        self.assertEqual(len(added), 1)

    def test_unowned_target_cannot_be_replaced(self):
        previous = {'ConfigID': 'foreign', 'ConfigName': 'other-owner', 'File': {'Name': '/etc/nginx/templates/visitaki-identity.conf.template'}}
        with self.assertRaises(AssertionError):
            module.changes_for(self.baseline([previous]), {'visitaki-identity': 'new'}, [])

    def test_existing_network_is_preserved_and_not_added_twice(self):
        with patch.object(module, 'run', return_value='[{"Driver":"overlay","Attachable":true,"Id":"neighbor"}]'):
            changes, _, _ = module.changes_for(self.baseline(), {}, ['proxy'])
        self.assertEqual(changes, [])

    def test_bootstrap_has_no_upstream(self):
        route = module.bootstrap('auth.visitaki.com')
        self.assertIn('return 503', route)
        self.assertIn('/.well-known/acme-challenge/', route)
        self.assertNotIn('proxy_pass', route)

    def test_shared_release_retains_deployed_visitaki_phase(self):
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/manual-deploy.yml').read_text()
        block = workflow.split('python3 - "${prior_spec}" "${remote_dir}/stack.json" "${remote_dir}/stack.yml" <<\'PY\'\n', 1)[1].split('\n          PY', 1)[0]
        code = textwrap.dedent(block)
        before = {'TaskTemplate': {'ContainerSpec': {'Configs': [{
            'ConfigName': 'visitaki_identity_revision',
            'File': {'Name': '/etc/nginx/templates/visitaki-identity.conf.template', 'Mode': 292},
        }]}, 'Networks': [{'Target': 'identity-network', 'Aliases': ['edge']}]}}
        base = {'services': {'nginx': {'image': 'existing', 'configs': [{'source': 'neighbor', 'target': '/neighbor'}], 'networks': {'neighbor': {}}}}, 'configs': {'neighbor': {'external': True}}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'before').write_text(json.dumps(before))
            (root / 'base').write_text(json.dumps(base))
            with patch('sys.argv', ['inline', str(root / 'before'), str(root / 'base'), str(root / 'result')]), patch('subprocess.check_output', return_value=b'[{"Name":"makepad_keycloak_visitaki_proxy"}]'):
                exec(compile(code, 'shared-release-retention', 'exec'), {})
            result = json.loads((root / 'result').read_text())
        self.assertEqual(result['configs']['visitaki_identity_revision'], {'external': True, 'name': 'visitaki_identity_revision'})
        self.assertEqual(result['services']['nginx']['configs'][0], base['services']['nginx']['configs'][0])
        self.assertEqual(result['services']['nginx']['image'], 'existing')
        self.assertIn('neighbor', result['services']['nginx']['networks'])
        self.assertIn('makepad_keycloak_visitaki_proxy', result['services']['nginx']['networks'])


if __name__ == '__main__':
    unittest.main()
