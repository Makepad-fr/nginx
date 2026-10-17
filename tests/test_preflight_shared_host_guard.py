"""Fail-closed synthetic coverage for the 27-config edge rollout plan."""

import copy
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
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
        self.assertIn(b'map "$ssl_server_name:$host:$http_host" $edge_sni_host_match',
                      candidate[preflight.COMMON_TARGET])
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

    def test_raw_includes_must_be_location_scoped(self):
        configs, networks = live_fixture()
        target = '/etc/nginx/templates/tenant-00.conf.template'
        for extra in (
            b'include /etc/letsencrypt/extra.conf;\n',
            b'include\n /etc/letsencrypt/extra.conf;\n',
        ):
            changed = dict(configs)
            changed[target] = extra + configs[target]
            with self.subTest(scope='http', extra=extra), self.assertRaises(preflight.PreflightError):
                preflight.build_candidate(changed, networks)

        changed = dict(configs)
        changed[target] = configs[target].replace(
            b'    listen 80;\n',
            b'    listen 80;\n    include /etc/letsencrypt/extra.conf;\n', 1,
        )
        with self.assertRaises(preflight.PreflightError):
            preflight.build_candidate(changed, networks)

        changed = dict(configs)
        changed[preflight.COMMON_TARGET] += b'include /etc/letsencrypt/extra.conf;\n'
        with self.assertRaises(preflight.PreflightError):
            preflight.build_candidate(changed, networks)

        changed = dict(configs)
        changed[target] = configs[target].replace(
            b'    location / { proxy_pass ${UPSTREAM}; }',
            b'    location / { include /etc/nginx/snippets/amiary-common-proxy.conf; '
            b'proxy_pass ${UPSTREAM}; }',
        )
        candidate, files, vhosts = preflight.build_candidate(changed, networks)
        self.assertEqual((files, vhosts), (25, 29))
        self.assertIn(b'amiary-common-proxy.conf;', candidate[target])
        for name in ('amiary-prod.conf.template', 'amiary-canary.conf.template'):
            with self.subTest(template=name):
                source = (SCRIPT.parents[1] / 'sites' / name).read_text()
                preflight.reject_vhost_includes(source)

    def test_include_words_in_comments_quotes_or_variables_are_not_directives(self):
        source = (
            '# include /etc/letsencrypt/ignored.conf;\n'
            'server {\n'
            '    listen 443 ssl;\n'
            '    server_name tenant.test;\n'
            '    add_header Test "include /etc/letsencrypt/ignored.conf; { }";\n'
            '    location ~ "^/path{2}$" {\n'
            '        proxy_pass ${UPSTREAM};\n'
            '        include /etc/nginx/snippets/amiary-common-proxy.conf;\n'
            '    }\n'
            '}\n'
        )
        preflight.reject_vhost_includes(source)

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
        mounts = [{'Type': 'bind', 'Source': '/srv/certs/' + str(index),
                   'Target': target, 'ReadOnly': True}
                  for index, target in enumerate(sorted(preflight.EXPECTED_MOUNT_TARGETS))]
        container = {'Mounts': [{'Type': 'bind', 'Source': mount['Source'],
                                 'Destination': mount['Target'], 'RW': False}
                                for mount in mounts]}
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

    def test_mount_mismatch_or_duplicate_fails_closed(self):
        mounts = [{'Type': 'bind', 'Source': '/srv/certs/' + str(index),
                   'Target': target, 'ReadOnly': True}
                  for index, target in enumerate(sorted(preflight.EXPECTED_MOUNT_TARGETS))]
        active = [{'Type': 'bind', 'Source': mount['Source'],
                   'Destination': mount['Target'], 'RW': False} for mount in mounts]
        for field, value in [('Source', '/wrong'), ('Type', 'volume'), ('RW', True)]:
            changed = copy.deepcopy(active)
            changed[0][field] = value
            with self.subTest(field=field), self.assertRaises(preflight.PreflightError):
                preflight.readonly_mount_args(mounts, {'Mounts': changed})
        with self.assertRaises(preflight.PreflightError):
            preflight.readonly_mount_args(mounts, {'Mounts': active + [active[0]]})
        with self.assertRaises(preflight.PreflightError):
            preflight.readonly_mount_args(mounts + [mounts[0]], {'Mounts': active})
        changed = copy.deepcopy(mounts)
        changed[0]['ReadOnly'] = False
        with self.assertRaises(preflight.PreflightError):
            preflight.readonly_mount_args(changed, {'Mounts': active})

    def test_candidate_cleanup_only_removes_its_labeled_container(self):
        with patch.object(preflight, 'docker', side_effect=[b'abc123 edge-host-guard-preflight-marker\n', b'']) as command:
            preflight.cleanup_candidate_container('edge-host-guard-preflight-marker', 'marker')
        self.assertEqual(command.call_args_list[1].args, ('rm', '--force', 'abc123'))
        with patch.object(preflight, 'docker', return_value=b'abc123 unrelated-container\n') as command:
            with self.assertRaises(preflight.PreflightError):
                preflight.cleanup_candidate_container('edge-host-guard-preflight-marker', 'marker')
        self.assertEqual(command.call_count, 1)

    def test_tmpfs_mount_is_verified_not_inferred_from_directory(self):
        valid = ('1 0 0:1 / / rw - ext4 /dev/root rw\n'
                 '2 1 0:2 / /dev/shm rw,nosuid,nodev - tmpfs shm rw\n')
        with (patch.object(preflight.Path, 'is_dir', return_value=True),
              patch.object(preflight.Path, 'is_symlink', return_value=False),
              patch.object(preflight.Path, 'read_text', return_value=valid)):
            self.assertEqual(preflight.candidate_tmpfs_root(), Path('/dev/shm'))
        for invalid in (valid.replace('tmpfs shm', 'ext4 disk'), valid + valid.splitlines()[1] + '\n',
                        'not valid mountinfo'):
            with (self.subTest(mountinfo=invalid),
                  patch.object(preflight.Path, 'is_dir', return_value=True),
                  patch.object(preflight.Path, 'is_symlink', return_value=False),
                  patch.object(preflight.Path, 'read_text', return_value=invalid),
                  self.assertRaises(preflight.PreflightError)):
                preflight.candidate_tmpfs_root()

    def test_invalid_utf8_is_redacted(self):
        with self.assertRaises(preflight.PreflightError) as failure:
            preflight.guard_tls_vhosts(b'server {\nlisten 443 ssl;\n# secret=\xff\n}\n')
        self.assertIn('bytes withheld', str(failure.exception))
        self.assertNotIn('secret', str(failure.exception))
        self.assertNotIn('\\xff', str(failure.exception))
        configs, networks = live_fixture()
        configs[preflight.COMMON_TARGET] = b'\xff secret'
        with self.assertRaises(preflight.PreflightError) as failure:
            preflight.build_candidate(configs, networks)
        self.assertNotIn('secret', str(failure.exception))

    def test_mocked_docker_preflight_success_and_timeout_cleanup(self):
        configs, networks = live_fixture()
        candidate, _, _ = preflight.build_candidate(configs, networks)
        mounts = [{'Type': 'bind', 'Source': '/srv/certs/' + str(index),
                   'Target': target, 'ReadOnly': True}
                  for index, target in enumerate(sorted(preflight.EXPECTED_MOUNT_TARGETS))]
        container = {
            'Config': {'Env': ['SAFE=fixture']},
            'Mounts': [{'Type': 'bind', 'Source': mount['Source'],
                        'Destination': mount['Target'], 'RW': False} for mount in mounts],
        }
        calls = []
        run_name = None

        def fake_success(*args, **kwargs):
            calls.append((args, kwargs))
            if args[0] == 'run':
                conf_mount = next(args[index + 1] for index, value in enumerate(args)
                                  if value == '--mount' and 'dst=/etc/nginx/conf.d' in args[index + 1])
                conf = Path(conf_mount.split('src=', 1)[1].split(',dst=', 1)[0])
                for target, raw in candidate.items():
                    (conf / Path(target).name.removesuffix('.template')).write_bytes(raw)
                return b''
            if args[0] == 'ps':
                return b''  # --rm removed the candidate.
            raise AssertionError(args)

        with tempfile.TemporaryFile() as env_file:
            with (patch.object(preflight, 'candidate_tmpfs_root', return_value=Path(tempfile.gettempdir())),
                  patch.object(preflight.os, 'memfd_create', create=True,
                               side_effect=lambda *args, **kwargs: os.dup(env_file.fileno())),
                  patch.object(preflight, 'docker', side_effect=fake_success)):
                rendered = preflight.syntax_preflight(candidate, 'active-fixture', container,
                                                      'nginx:fixture', mounts)
        self.assertEqual(rendered, 30)
        run = next(args for args, _ in calls if args[0] == 'run')
        self.assertIn('--pull=never', run)
        self.assertIn('container:active-fixture', run)
        self.assertNotIn('--volumes-from', run)
        self.assertFalse(any(args[0] in ('service', 'config') for args, _ in calls))

        def fake_timeout(*args, **kwargs):
            nonlocal run_name
            calls.append((args, kwargs))
            if args[0] == 'run':
                run_name = args[args.index('--name') + 1]
                raise preflight.PreflightError('Local Docker read/preflight failed; diagnostics withheld')
            if args[0] == 'ps':
                return f'abc123 {run_name}\n'.encode()
            if args[0] == 'rm':
                return b''
            raise AssertionError(args)

        calls.clear()
        with tempfile.TemporaryFile() as env_file:
            with (patch.object(preflight, 'candidate_tmpfs_root', return_value=Path(tempfile.gettempdir())),
                  patch.object(preflight.os, 'memfd_create', create=True,
                               side_effect=lambda *args, **kwargs: os.dup(env_file.fileno())),
                  patch.object(preflight, 'docker', side_effect=fake_timeout),
                  self.assertRaises(preflight.PreflightError)):
                preflight.syntax_preflight(candidate, 'active-fixture', container,
                                          'nginx:fixture', mounts)
        self.assertEqual(next(args for args, _ in calls if args[0] == 'rm')[1:], ('--force', 'abc123'))

        injected = dict(candidate)
        target = '/etc/nginx/templates/tenant-00.conf.template'
        injected[target] = b'${EXTRA}\n' + injected[target]
        preflight.reject_vhost_includes(preflight.decode_config(injected[target]))

        def fake_rendered_injection(*args, **kwargs):
            if args[0] == 'run':
                conf_mount = next(args[index + 1] for index, value in enumerate(args)
                                  if value == '--mount' and 'dst=/etc/nginx/conf.d' in args[index + 1])
                conf = Path(conf_mount.split('src=', 1)[1].split(',dst=', 1)[0])
                for name, raw in injected.items():
                    rendered = raw.replace(b'${EXTRA}', b'include /etc/letsencrypt/extra.conf;')
                    (conf / Path(name).name.removesuffix('.template')).write_bytes(rendered)
                return b''
            if args[0] == 'ps':
                return b''
            raise AssertionError(args)

        with tempfile.TemporaryFile() as env_file:
            with (patch.object(preflight, 'candidate_tmpfs_root', return_value=Path(tempfile.gettempdir())),
                  patch.object(preflight.os, 'memfd_create', create=True,
                               side_effect=lambda *args, **kwargs: os.dup(env_file.fileno())),
                  patch.object(preflight, 'docker', side_effect=fake_rendered_injection),
                  self.assertRaisesRegex(preflight.PreflightError, 'include outside a location')):
                preflight.syntax_preflight(injected, 'active-fixture', container,
                                          'nginx:fixture', mounts)

    def test_docker_timeout_is_redacted(self):
        with (patch.object(preflight.subprocess, 'run',
                           side_effect=subprocess.TimeoutExpired('docker', 1, output=b'secret')),
              self.assertRaises(preflight.PreflightError) as failure):
            preflight.docker('run', timeout=1)
        self.assertNotIn('secret', str(failure.exception))


if __name__ == '__main__':
    unittest.main()
