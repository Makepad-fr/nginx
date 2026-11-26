import importlib.util
import base64
import copy
import json
import os
from pathlib import Path
import subprocess
import stat
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

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



class VersionBoundUpdates(unittest.TestCase):
    def setUp(self):
        self.transport = patch.object(module.subprocess, 'run', side_effect=AssertionError('external process blocked'))
        self.transport.start()
        self.addCleanup(self.transport.stop)

    def service(self):
        return {'ID': 'serviceid', 'Version': {'Index': 100}, 'UpdateStatus': {'State': 'completed'},
                'Spec': {'Name': module.SERVICE, 'Labels': {'unrelated': 'preserved'},
                         'TaskTemplate': {'ContainerSpec': {'Image': 'nginx@sha256:' + 'a' * 64,
                                                           'Configs': []},
                                          'Networks': [{'Target': 'appnetwork', 'Aliases': ['old-alias']}]},
                         'EndpointSpec': {'Ports': [{'TargetPort': 443, 'PublishedPort': 443}]}}}

    def candidate(self, before):
        result = copy.deepcopy(before['Spec'])
        result['TaskTemplate']['ContainerSpec']['Configs'].append(
            {'ConfigID': 'newconfig', 'ConfigName': 'visitaki_new',
             'File': {'Name': '/etc/nginx/templates/visitaki-preview.conf.template', 'Mode': 0o444}})
        return result






    def test_candidate_preserves_all_other_fields_and_existing_network(self):
        before = self.service()
        original = copy.deepcopy(before)
        content = 'reviewed route'
        name = 'visitaki_visitaki-preview_' + module.hashlib.sha256(content.encode()).hexdigest()[:16]
        config = {'ID': 'newconfig', 'Spec': {'Name': name, 'Data': base64.b64encode(content.encode()).decode()}}
        def selected(*args, **kwargs):
            return json.dumps([config] if args[:3] == ('docker', 'config', 'inspect') else [{'Id': 'appnetwork', 'Driver': 'overlay', 'Attachable': True}])
        with patch.object(module, 'run', side_effect=selected):
            candidate = module.candidate_spec(before, {'visitaki-preview': content}, ['app'])
        self.assertEqual(before, original)
        reference = candidate['TaskTemplate']['ContainerSpec']['Configs'].pop()
        self.assertEqual(reference['ConfigID'], 'newconfig')
        self.assertEqual(reference['File']['Mode'], 0o444)
        self.assertEqual(candidate, before['Spec'])

    def test_owned_rollback_restores_full_spec_with_fresh_version(self):
        before = self.service(); candidate = self.candidate(before)
        current = copy.deepcopy(before); current.update(Spec=candidate, PreviousSpec=before['Spec'], Version={'Index': 104})
        with patch.object(module, 'inspect', return_value=current), patch.object(module, 'update_service') as update, patch.object(module, 'wait_service', return_value=before) as wait:
            module.restore_owned(before, candidate, True)
        update.assert_called_once_with(current, before['Spec'])
        self.assertEqual(update.call_args.args[0]['Version']['Index'], 104)
        self.assertEqual(wait.call_args.args[:2], (before['ID'], before['Spec']))
        self.assertTrue(wait.call_args.kwargs['restored'])

    def test_rollback_requires_acknowledgement_candidate_and_predecessor(self):
        before = self.service(); candidate = self.candidate(before)
        current = copy.deepcopy(before); current.update(Spec=candidate, PreviousSpec=before['Spec'], Version={'Index': 104})
        variants = [(False, current)]
        for field, value in [('ID', 'foreign'), ('Spec', {'foreign': True}), ('PreviousSpec', {'foreign': True})]:
            changed = copy.deepcopy(current); changed[field] = value; variants.append((True, changed))
        for acknowledged, actual in variants:
            with self.subTest(acknowledged=acknowledged, actual=actual), patch.object(module, 'inspect', return_value=actual), patch.object(module, 'update_service') as update:
                with self.assertRaises(RuntimeError): module.restore_owned(before, candidate, acknowledged)
                update.assert_not_called()

    def test_already_restored_does_not_send_second_update(self):
        before = self.service()
        with patch.object(module, 'inspect', return_value=before), patch.object(module, 'update_service') as update, patch.object(module, 'wait_service', return_value=before):
            module.restore_owned(before, self.candidate(before), True)
        update.assert_not_called()

    def test_rollback_conflict_is_not_retried(self):
        before = self.service(); candidate = self.candidate(before)
        current = copy.deepcopy(before); current.update(Spec=candidate, PreviousSpec=before['Spec'], Version={'Index': 104})
        with patch.object(module, 'inspect', return_value=current), patch.object(module, 'update_service', side_effect=RuntimeError('version conflict')) as update:
            with self.assertRaisesRegex(RuntimeError, 'version conflict'): module.restore_owned(before, candidate, True)
        self.assertEqual(update.call_count, 1)

    def test_convergence_preserves_full_spec_and_deadline(self):
        before = self.service(); current = copy.deepcopy(before)
        with patch.object(module, 'inspect', return_value=current), patch.object(module.time, 'monotonic', return_value=1):
            self.assertEqual(module.wait_service(before['ID'], before['Spec'], 10), current)
        current['Spec']['Labels']['unrelated'] = 'foreign'
        with patch.object(module, 'inspect', return_value=current), patch.object(module.time, 'monotonic', return_value=1):
            with self.assertRaisesRegex(RuntimeError, 'concurrent'): module.wait_service(before['ID'], before['Spec'], 10)
        with patch.object(module, 'inspect') as read, patch.object(module.time, 'monotonic', return_value=10):
            with self.assertRaisesRegex(RuntimeError, 'timed out'): module.wait_service(before['ID'], before['Spec'], 10)
            read.assert_not_called()

    def test_absent_update_status_waits_for_owned_update_to_start(self):
        before = self.service()
        for pending_status in ['missing', None]:
            pending = copy.deepcopy(before)
            if pending_status == 'missing':
                del pending['UpdateStatus']
            else:
                pending['UpdateStatus'] = pending_status
            updating = copy.deepcopy(before); updating['UpdateStatus'] = {'State': 'updating'}
            with self.subTest(status=pending_status), patch.object(module, 'inspect', side_effect=[pending, updating, before]) as read, patch.object(module.time, 'monotonic', side_effect=[1, 3, 5]), patch.object(module.time, 'sleep') as sleep:
                self.assertEqual(module.wait_service(before['ID'], before['Spec'], 10), before)
                self.assertEqual(read.call_count, 3)
                self.assertEqual(sleep.call_count, 2)

    def test_absent_update_status_still_times_out(self):
        before = self.service(); pending = copy.deepcopy(before)
        del pending['UpdateStatus']
        with patch.object(module, 'inspect', return_value=pending) as read, patch.object(module.time, 'monotonic', side_effect=[1, 3, 10]), patch.object(module.time, 'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                module.wait_service(before['ID'], before['Spec'], 10)
            self.assertEqual(read.call_count, 2)
            self.assertEqual(sleep.call_count, 2)

    def test_pending_status_never_accepts_foreign_spec_or_malformed_status(self):
        before = self.service()
        for status in [None, [], 'updating']:
            current = copy.deepcopy(before); current['UpdateStatus'] = status
            if status is None:
                current['Spec']['Labels']['unrelated'] = 'foreign'
            with self.subTest(status=status), patch.object(module, 'inspect', return_value=current), patch.object(module.time, 'monotonic', return_value=1), patch.object(module.time, 'sleep') as sleep:
                with self.assertRaises(RuntimeError):
                    module.wait_service(before['ID'], before['Spec'], 10)
                sleep.assert_not_called()

    def exercise_failed_main(self, acknowledged, foreign=False, preexisting=False):
        before = self.service(); candidate = self.candidate(before); calls = []
        def selected(*args, data=None):
            calls.append(args)
            if args[:2] == ('docker', 'ps'): return 'running-container\n'
            if args[:2] == ('docker', 'create'): return 'candidate-container\n'
            if args[:3] == ('docker', 'inspect', '-f'): return '0\n'
            return ''
        current = copy.deepcopy(before); current.update(Spec=candidate, PreviousSpec=before['Spec'], Version={'Index': 104})
        if foreign: current['Spec'] = {'foreign': True}
        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp) / 'result.json'
            if preexisting:
                receipt.with_suffix('.before.json').write_text('original evidence')
            with patch.object(module, 'run', side_effect=selected), patch.object(module, 'status', return_value='200'), patch.object(module, 'inspect', side_effect=[before, before, current]), patch.object(module, 'changes_for', return_value=(['planned'], [], [])), patch.object(module, 'candidate_spec', return_value=candidate), patch.object(module.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'', b'')), patch.object(module, 'update_service', side_effect=None if acknowledged else RuntimeError('own update rejected before write')) as update, patch.object(module, 'wait_service', side_effect=RuntimeError('original verification failure')), patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'GITHUB_REF': 'refs/heads/main', 'DEPLOY_SSH_HOST': '135.181.141.31', 'GITHUB_SHA': '0' * 40}), patch.object(sys, 'argv', ['helper', 'app', '--receipt', str(receipt)]):
                with self.assertRaises(Exception) as failure: module.main()
                recovery = json.loads(receipt.with_suffix('.recovery.json').read_text()) if receipt.with_suffix('.recovery.json').exists() else None
            modes = {p.suffixes[-2]: stat.S_IMODE(p.stat().st_mode) for p in Path(tmp).glob('result.*.json')}
            preserved = receipt.with_suffix('.before.json').read_text() == 'original evidence' if preexisting else None
            return calls, update.call_count, failure.exception, recovery, modes, preserved

    def test_main_receipts_are_private_and_never_overwrite_evidence(self):
        calls, count, failure, recovery, modes, preserved = self.exercise_failed_main(True, foreign=True)
        self.assertEqual(modes, {'.before': 0o600, '.candidate': 0o600, '.recovery': 0o600})
        calls, count, failure, recovery, modes, preserved = self.exercise_failed_main(False, preexisting=True)
        self.assertIsInstance(failure, FileExistsError)
        self.assertEqual(count, 0)
        self.assertTrue(preserved)
        self.assertEqual(set(modes), {'.before'})

    def test_failed_own_write_cannot_rollback_foreign_update(self):
        calls, count, failure, recovery, modes, preserved = self.exercise_failed_main(False, foreign=True)
        self.assertEqual(count, 1)
        self.assertIn('own update rejected', str(failure))
        self.assertFalse(any(c[:3] == ('docker', 'service', 'rollback') for c in calls))
        self.assertIsNone(recovery)

    def test_foreign_change_after_ack_preserves_primary_without_rollback(self):
        calls, count, failure, recovery, modes, preserved = self.exercise_failed_main(True, foreign=True)
        self.assertEqual(count, 1)
        self.assertIn('original verification failure', str(failure.__cause__))
        self.assertFalse(recovery['automatic_recovery_verified'])
        self.assertFalse(any(c[:3] == ('docker', 'service', 'rollback') for c in calls))


if __name__ == '__main__':
    unittest.main()
