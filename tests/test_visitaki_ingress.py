import importlib.util
import base64
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
        self.assertEqual(changes[:2], ['--config-rm', 'visitaki_bootstrap'])
        self.assertNotIn('neighbor-config', changes)
        self.assertEqual(len(added), 1)

    def test_unowned_target_cannot_be_replaced(self):
        previous = {'ConfigID': 'foreign', 'ConfigName': 'other-owner', 'File': {'Name': '/etc/nginx/templates/visitaki-identity.conf.template'}}
        with self.assertRaises(AssertionError):
            module.changes_for(self.baseline([previous]), {'visitaki-identity': 'new'}, [])

    def legacy_config(self):
        route = (Path(__file__).resolve().parent / 'fixtures/visitaki-host-guard.conf').read_bytes()
        previous = {'ConfigID': 'legacy', 'ConfigName': 'visitaki-host-guard-38c397aa-025120c125755976',
                    'File': {'Name': '/etc/nginx/templates/visitaki-preview.conf.template'}}
        inspected = {'ID': 'legacy', 'Spec': {'Name': previous['ConfigName'],
                     'Data': base64.b64encode(route).decode()}}
        return previous, inspected

    def test_reviewed_legacy_app_route_can_be_migrated(self):
        previous, inspected = self.legacy_config()
        with patch.object(module, 'run', return_value=json.dumps([inspected])) as run:
            changes, removed, added = module.changes_for(self.baseline([previous]), {'visitaki-preview': 'new'}, [])
        run.assert_called_once_with('docker', 'config', 'inspect', 'legacy')
        self.assertEqual(changes[:2], ['--config-rm', previous['ConfigName']])
        self.assertEqual(removed, ['legacy'])
        self.assertEqual(len(added), 1)

    def test_legacy_name_cannot_authorize_another_route_or_name(self):
        previous, inspected = self.legacy_config()
        for route, name in [('visitaki-identity', previous['ConfigName']),
                            ('visitaki-preview', 'neighbor-host-guard-38c397aa-025120c125755976'),
                            ('visitaki-preview', 'visitaki-host-guard-arbitrary-025120c125755976')]:
            with self.subTest(route=route, name=name):
                config = {**previous, 'ConfigName': name,
                          'File': {'Name': '/etc/nginx/templates/' + route + '.conf.template'}}
                with patch.object(module, 'run', return_value=json.dumps([inspected])) as run:
                    with self.assertRaisesRegex(AssertionError, 'Unowned config'):
                        module.changes_for(self.baseline([config]), {route: 'new'}, [])
                run.assert_not_called()

    def test_legacy_name_requires_exact_reviewed_content_and_identity(self):
        previous, inspected = self.legacy_config()
        for mutation in ['content', 'id', 'name', 'digest']:
            with self.subTest(mutation=mutation):
                current = json.loads(json.dumps(inspected))
                config = json.loads(json.dumps(previous))
                if mutation == 'content':
                    current['Spec']['Data'] = base64.b64encode(b'unrelated configuration').decode()
                elif mutation == 'id':
                    current['ID'] = 'unrelated'
                elif mutation == 'name':
                    current['Spec']['Name'] = 'unrelated'
                else:
                    config['ConfigName'] = current['Spec']['Name'] = 'visitaki-host-guard-38c397aa-0000000000000000'
                with patch.object(module, 'run', return_value=json.dumps([current])):
                    with self.assertRaisesRegex(AssertionError, 'Unowned config'):
                        module.changes_for(self.baseline([config]), {'visitaki-preview': 'new'}, [])

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
        before['TaskTemplate']['ContainerSpec']['Configs'][0] = {
            'ConfigName': 'vif_staging_verified_revision',
            'File': {'Name': '/etc/nginx/templates/vif-staging.conf.template', 'Mode': 292},
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'before').write_text(json.dumps(before))
            (root / 'base').write_text(json.dumps(base))
            with patch('sys.argv', ['inline', str(root / 'before'), str(root / 'base'), str(root / 'result')]), patch('subprocess.check_output', return_value=b'[{"Name":"makepad_vif_platform_staging_edge"}]'):
                exec(compile(code, 'vif-release-retention', 'exec'), {})
            result = json.loads((root / 'result').read_text())
        self.assertIn('vif_staging_verified_revision', result['configs'])
        self.assertIn('makepad_vif_platform_staging_edge', result['services']['nginx']['networks'])
        self.assertEqual(result['services']['nginx']['image'], 'existing')


    def test_shared_release_keeps_reviewed_legacy_cutover_exactly_once(self):
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/manual-deploy.yml').read_text()
        block = workflow.split("python3 - \"${prior_spec}\" \"${remote_dir}/stack.json\" \"${remote_dir}/stack.yml\" <<'PY'\n", 1)[1].split('\n          PY', 1)[0]
        code = textwrap.dedent(block)
        target = '/etc/nginx/templates/brio-staging.conf.template'
        for old_name, expected in [('vif_staging_brio_cutover', 'vif_staging_brio_cutover'), ('brio_original', 'generated_brio')]:
            before = {'TaskTemplate': {'ContainerSpec': {'Configs': [{'ConfigName': old_name, 'File': {'Name': target, 'Mode': 292}}]}}}
            base = {'services': {'nginx': {'configs': [{'source': 'generated_brio', 'target': target}, {'source': 'neighbor', 'target': '/neighbor'}]}}}
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / 'before').write_text(json.dumps(before))
                (root / 'base').write_text(json.dumps(base))
                with patch('sys.argv', ['inline', str(root / 'before'), str(root / 'base'), str(root / 'result')]):
                    exec(compile(code, 'legacy-release-retention', 'exec'), {})
                result = json.loads((root / 'result').read_text())
            configs = result['services']['nginx']['configs']
            self.assertEqual([c['source'] for c in configs if c['target'] == target], [expected])
            self.assertIn({'source': 'neighbor', 'target': '/neighbor'}, configs)



if __name__ == '__main__':
    unittest.main()
