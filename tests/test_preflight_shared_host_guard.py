"""Fail-closed synthetic coverage for the 27-config edge rollout plan."""

import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/preflight_shared_host_guard.py'
SPEC = importlib.util.spec_from_file_location('preflight_shared_host_guard', SCRIPT)
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


def live_fixture():
    configs = {
        preflight.COMMON_TARGET: (
            b'map $http_upgrade $connection_upgrade {\n    default upgrade;\n}\n'
            b'limit_req_zone $binary_remote_addr zone=brio:10m rate=5r/s;\n'
        ),
        '/etc/nginx/templates/02-brio-common.conf.template': (
            b'limit_req_zone $binary_remote_addr zone=brio_extra:10m rate=2r/s;\n'
        ),
    }
    for index in range(25):
        name = f'tenant-{index:02d}.conf.template'
        source = (
            f'# preserve exactly: tenant {index}\n'
            'server {\n    listen 80;\n    server_name tenant.test;\n'
            '    location / { return 308 https://tenant.test$request_uri; }\n}\n'
            'server {\n    listen 443 ssl;\n    listen [::]:443 ssl;\n'
            '    server_name tenant.test;\n    location / { proxy_pass ${UPSTREAM}; }\n}\n'
        )
        if index < 4:
            source += (
                'server {\n    listen 443 ssl;\n    server_name www.tenant.test;\n'
                '    location / { return 308 https://tenant.test$request_uri; }\n}\n'
            )
        configs['/etc/nginx/templates/' + name] = source.encode()
    networks = [{'Target': f'network-{index}'} for index in range(27)]
    return configs, networks


class CandidateTests(unittest.TestCase):
    def test_all_27_source_bytes_and_29_tls_vhosts_are_preserved(self):
        configs, networks = live_fixture()
        original = copy.deepcopy(configs)
        original_networks = copy.deepcopy(networks)
        candidate, files, vhosts = preflight.build_candidate(configs, networks)
        self.assertEqual((files, vhosts, len(candidate)), (25, 29, 28))
        self.assertEqual(configs, original)
        self.assertEqual(networks, original_networks)
        self.assertEqual(candidate[preflight.COMMON_TARGET], preflight.COMMON_PREFIX + original[preflight.COMMON_TARGET])
        brio = '/etc/nginx/templates/02-brio-common.conf.template'
        self.assertEqual(candidate[brio], original[brio])
        self.assertEqual(sum(value.count(preflight.GUARD.encode()) for value in candidate.values()), 29)
        for target, old in original.items():
            if target in (preflight.COMMON_TARGET, brio):
                continue
            new = candidate[target]
            self.assertIn(old.splitlines()[0], new)
            self.assertEqual(new.replace(('    ' + preflight.GUARD + '\n').encode(), b''), old)

    def test_missing_live_config_or_network_refuses(self):
        configs, networks = live_fixture()
        configs.pop('/etc/nginx/templates/tenant-00.conf.template')
        with self.assertRaises(preflight.PreflightError):
            preflight.build_candidate(configs, networks)
        configs, networks = live_fixture()
        networks.pop()
        with self.assertRaises(preflight.PreflightError):
            preflight.build_candidate(configs, networks)

    def test_new_or_previously_guarded_tls_shape_refuses(self):
        configs, networks = live_fixture()
        target = '/etc/nginx/templates/tenant-00.conf.template'
        configs[target] = configs[target].replace(b'listen 443 ssl;', b'listen 443;')
        with self.assertRaises(preflight.PreflightError):
            preflight.build_candidate(configs, networks)
        configs, networks = live_fixture()
        configs[target] = configs[target].replace(b'listen 443 ssl;',
            b'listen 443 ssl;\n    if ($edge_sni_host_match = 0) { return 421; }')
        with self.assertRaises(preflight.PreflightError):
            preflight.build_candidate(configs, networks)

    def test_default_or_common_drift_refuses(self):
        configs, networks = live_fixture()
        configs[preflight.COMMON_TARGET] += b'server { listen 443 ssl default_server; }\n'
        with self.assertRaises(preflight.PreflightError):
            preflight.build_candidate(configs, networks)
        configs, networks = live_fixture()
        configs[preflight.DEFAULT_TARGET] = configs.pop('/etc/nginx/templates/tenant-00.conf.template')
        with self.assertRaises(preflight.PreflightError):
            preflight.build_candidate(configs, networks)

    def test_quoted_and_commented_braces_do_not_hide_tls_server(self):
        raw = (
            b'# server { fake }\n'
            b'log_format quoted "{still data}";\n'
            b'server {\n'
            b'    listen 443 ssl;\n'
            b'    server_name example.test;\n'
            b'    location ~ "^/path{2}$" { return 404; }\n'
            b'    location / { proxy_pass ${UPSTREAM}; }\n'
            b'}\n'
        )
        transformed, count = preflight.guard_tls_vhosts(raw)
        self.assertEqual(count, 1)
        self.assertEqual(len(preflight.server_blocks(transformed.decode())), 1)
        self.assertEqual(transformed.count(preflight.GUARD.encode()), 1)

    def test_unreviewed_target_and_duplicate_rendered_name_refuse(self):
        with self.assertRaises(preflight.PreflightError):
            preflight.target_output('/etc/nginx/templates/../../secrets')
        configs, networks = live_fixture()
        configs['/etc/nginx/conf.d/tenant-00.conf'] = configs.pop('/etc/nginx/templates/tenant-01.conf.template')
        with self.assertRaises(preflight.PreflightError):
            preflight.build_candidate(configs, networks)
        configs, networks = live_fixture()
        configs['/etc/nginx/conf.d/one-off.conf'] = configs.pop('/etc/nginx/templates/tenant-01.conf.template')
        with self.assertRaises(preflight.PreflightError):
            preflight.build_candidate(configs, networks)

    def test_only_four_live_certificate_mounts_are_replayed(self):
        mounts = [{'Type': 'bind', 'Source': '/srv/certs/' + str(index), 'Target': target}
                  for index, target in enumerate(sorted(preflight.EXPECTED_MOUNT_TARGETS))]
        container = {'Mounts': [{'Destination': target} for target in preflight.EXPECTED_MOUNT_TARGETS]}
        args = preflight.readonly_mount_args(mounts, container)
        self.assertEqual(args.count('--mount'), 4)
        self.assertTrue(all(value.endswith(',readonly') for value in args[1::2]))
        mounts[0]['Target'] = '/etc/nginx/templates'
        with self.assertRaises(preflight.PreflightError):
            preflight.readonly_mount_args(mounts, container)
        mounts[0]['Target'] = sorted(preflight.EXPECTED_MOUNT_TARGETS)[0]
        mounts[0]['Type'] = 'volume'
        with self.assertRaises(preflight.PreflightError):
            preflight.readonly_mount_args(mounts, container)

    def test_candidate_cleanup_only_removes_its_labeled_container(self):
        with patch.object(preflight, 'docker', side_effect=[b'abc123 edge-host-guard-preflight-marker\n', b'']) as command:
            preflight.cleanup_candidate_container('edge-host-guard-preflight-marker', 'marker')
        self.assertEqual(command.call_args_list[1].args, ('rm', '--force', 'abc123'))
        with patch.object(preflight, 'docker', return_value=b'abc123 unrelated-container\n') as command:
            with self.assertRaises(preflight.PreflightError):
                preflight.cleanup_candidate_container('edge-host-guard-preflight-marker', 'marker')
        self.assertEqual(command.call_count, 1)


if __name__ == '__main__':
    unittest.main()
