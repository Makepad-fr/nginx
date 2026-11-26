import copy
import importlib.util
import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('docker_context_api', Path(__file__).resolve().parents[1] / 'scripts/docker_context_api.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class VersionBoundTransport(unittest.TestCase):
    def setUp(self):
        self.transport = patch.object(module.subprocess, 'Popen', side_effect=AssertionError('external process blocked'))
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

    def context_read(self, *args, **kwargs):
        if args == ('docker', 'context', 'show'):
            return 'visitaki-ingress-123-1\n'
        if args[:3] == ('docker', 'context', 'inspect'):
            return '"ssh://makepad@135.181.141.31"\n'
        if args[:4] == ('docker', '--context', 'visitaki-ingress-123-1', 'version'):
            return '1.51\n'
        raise AssertionError(args)

    def response(self, body=b'{}', status=200):
        return b'HTTP/1.1 ' + str(status).encode() + b' Status\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body

    def test_atomic_request_binds_original_version_and_exact_spec(self):
        before = self.service()
        expected = self.candidate(before)
        with patch.object(module, '_read', side_effect=self.context_read), patch.object(module, '_command', return_value=subprocess.CompletedProcess([], 0, self.response(), b'')) as command:
            module.update_service(before, expected)
        self.assertEqual(command.call_count, 1)
        self.assertEqual(command.call_args.args[0], ['docker', '--context', 'visitaki-ingress-123-1', 'system', 'dial-stdio'])
        header, body = command.call_args.kwargs['data'].split(b'\r\n\r\n', 1)
        self.assertIn(b'/services/serviceid/update?version=100 HTTP/1.1', header)
        self.assertIn(b'Connection: close', header)
        self.assertIn(f'Content-Length: {len(body)}'.encode(), header)
        self.assertEqual(json.loads(body), expected)
        self.assertNotIn('timeout', command.call_args.kwargs)

    def test_failed_or_ambiguous_transport_never_retries(self):
        for output, code in [(self.response(status=400), 0), (b'', 1), (b'not HTTP', 0),
                             (b'HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n{}', 0),
                             (self.response(b'[]'), 0), (b'x' * (72 * 1024 + 1), 0)]:
            with self.subTest(output=output[:40], code=code), patch.object(module, '_read', side_effect=self.context_read), patch.object(module, '_command', return_value=subprocess.CompletedProcess([], code, output, b'')) as command:
                with self.assertRaises(RuntimeError):
                    module.update_service(self.service(), self.service()['Spec'])
                self.assertEqual(command.call_count, 1)
        with patch.object(module, '_read', side_effect=self.context_read), patch.object(module, '_command', side_effect=subprocess.TimeoutExpired('fixed transport', 30)) as command:
            with self.assertRaisesRegex(RuntimeError, 'Unacknowledged'):
                module.update_service(self.service(), self.service()['Spec'])
            self.assertEqual(command.call_count, 1)

    def test_foreign_context_or_invalid_version_refuses_before_request(self):
        for context, host, version in [('default', 'ssh://makepad@135.181.141.31', 100),
                                       ('visitaki-ingress-123-1', 'unix:///var/run/docker.sock', 100),
                                       ('visitaki-ingress-123-1', 'ssh://makepad@other', 100),
                                       ('visitaki-ingress-123-1', 'ssh://makepad@135.181.141.31', True)]:
            before = self.service(); before['Version']['Index'] = version
            def selected(*args, **kwargs):
                if args == ('docker', 'context', 'show'): return context
                if args[:3] == ('docker', 'context', 'inspect'): return json.dumps(host)
                return '1.51'
            with self.subTest(context=context, host=host, version=version), patch.object(module, '_read', side_effect=selected), patch.object(module, '_command') as command:
                with self.assertRaises(RuntimeError): module.update_service(before, before['Spec'])
                command.assert_not_called()

    def test_metadata_reads_are_bounded_and_errors_do_not_expose_output(self):
        with patch.object(module, '_command', return_value=subprocess.CompletedProcess([], 0, b'1.51\n', b'')) as command:
            self.assertEqual(module._read('docker', 'version'), '1.51\n')
            self.assertNotIn('timeout', command.call_args.kwargs)
        for output, code in [(b'secret-output', 1), (b'x' * (64 * 1024 + 1), 0), (b'\xff', 0)]:
            with self.subTest(code=code, length=len(output)), patch.object(module, '_command', return_value=subprocess.CompletedProcess([], code, output, b'secret-error')):
                with self.assertRaisesRegex(RuntimeError, '^Docker context metadata unavailable$'):
                    module._read('docker', 'version')
        with patch.object(module, '_command', side_effect=subprocess.TimeoutExpired('docker', 30)):
            with self.assertRaisesRegex(RuntimeError, '^Docker context metadata unavailable$'):
                module._read('docker', 'version')


class OwnedProcessTimeout(unittest.TestCase):
    def test_timeout_kills_owned_descendant_and_returns_boundedly(self):
        # A real disposable Python parent and descendant model Docker + SSH. Both
        # retain stdout; no daemon, network, SSH or unrelated process is involved.
        with tempfile.TemporaryDirectory() as temporary:
            pid_path = Path(temporary) / 'descendant.pid'
            code = "import os,time,sys; child=os.fork(); open(sys.argv[1],'w').write(str(os.getpid())) if child==0 else None; time.sleep(60)"
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                module._command([sys.executable, '-c', code, str(pid_path)], timeout=0.3)
            self.assertLess(time.monotonic() - started, 7)
            child = int(pid_path.read_text())
            # A zombie has exited; it cannot retain pipes or execute work.
            state = subprocess.run(['ps', '-p', str(child), '-o', 'stat='], capture_output=True, text=True, timeout=2).stdout.strip()
            self.assertTrue(not state or state.startswith('Z'), state)

    def test_cleanup_targets_only_new_group_and_preserves_primary(self):
        from unittest.mock import Mock
        process = Mock(pid=12345, returncode=-9)
        primary = subprocess.TimeoutExpired('owned', 30)
        process.communicate.side_effect = [primary, (b'', b'')]
        with patch.object(module.subprocess, 'Popen', return_value=process) as spawn, patch.object(module.os, 'killpg') as kill:
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                module._command(['docker', 'fixed'])
        self.assertIs(raised.exception, primary)
        self.assertTrue(spawn.call_args.kwargs['start_new_session'])
        kill.assert_called_once_with(12345, signal.SIGKILL)
        self.assertEqual(process.communicate.call_args.kwargs['timeout'], 5)

    def test_cleanup_failure_is_bounded_and_keeps_primary(self):
        from unittest.mock import Mock
        process = Mock(pid=12345)
        primary = subprocess.TimeoutExpired('owned', 30)
        process.communicate.side_effect = [primary, subprocess.TimeoutExpired('drain', 5)]
        process.wait.side_effect = subprocess.TimeoutExpired('reap', 1)
        with patch.object(module.subprocess, 'Popen', return_value=process), patch.object(module.os, 'killpg'):
            with self.assertRaisesRegex(RuntimeError, 'cleanup unverified') as raised:
                module._command(['docker', 'fixed'])
        self.assertIs(raised.exception.__cause__, primary)
        process.wait.assert_called_once_with(timeout=1)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
