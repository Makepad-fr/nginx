"""Actual Engine/CLI compatibility in one isolated DinD daemon, only in CI.

This does not exercise SSH end to end. Production's SSH/context allowlist remains
in update_service; the fixture calls its internal request primitive directly.
"""
import copy
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import uuid

from docker_context_api import SERVICE, _command, _request_update

IMAGE = 'docker:29.10.1-dind@sha256:fbbc96e0af32cdc190aff92ba97a1281a115351a723afe21af78879cc55126b0'
HOST = ['docker', '--host', 'unix:///var/run/docker.sock']


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or sys.platform != 'linux':
        raise RuntimeError('Isolated daemon compatibility fixture is CI-only')
    if any(os.environ.get(key) for key in ['DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH']):
        raise RuntimeError('Fixture requires the local default Docker socket')
    socket_info = Path('/var/run/docker.sock').lstat()
    if not stat.S_ISSOCK(socket_info.st_mode):
        raise RuntimeError('Local Docker socket is not a socket')
    deadline = time.monotonic() + 240

    def call(args, timeout=30):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('Isolated daemon fixture deadline expired')
        result = _command(args, timeout=min(timeout, remaining))
        if result.returncode:
            raise RuntimeError('Isolated daemon fixture command failed')
        return result.stdout.decode()

    prior_swarm = call(HOST + ['info', '--format', '{{.Swarm.LocalNodeState}}']).strip()
    name = 'nginx-context-api-' + uuid.uuid4().hex
    created = False
    primary = None
    with tempfile.TemporaryDirectory(prefix='nginx-context-api-') as temporary:
        directory = Path(temporary)
        sockets = directory / 'socket'; sockets.mkdir(mode=0o700)
        config = directory / 'client'; config.mkdir(mode=0o700)
        prior_config = os.environ.get('DOCKER_CONFIG')
        os.environ['DOCKER_CONFIG'] = str(config)
        try:
            call(HOST + ['pull', '--platform', 'linux/amd64', IMAGE], timeout=120)
            # No host socket, ports, source, credentials or daemon data is mounted.
            # Only our private empty socket directory crosses this boundary.
            created = True  # uncertain create is reconciled by this unique name
            call(HOST + ['run', '--detach', '--name', name, '--label', 'com.makepad.fixture=' + name,
                         '--privileged', '--network', 'none', '--platform', 'linux/amd64',
                         '--env', 'DOCKER_TLS_CERTDIR=', '--mount', f'type=bind,src={sockets},dst=/socket',
                         IMAGE, 'dockerd', '--host=unix:///socket/docker.sock',
                         '--storage-driver=vfs', '--iptables=false', '--bridge=none'])
            for _ in range(60):
                if (sockets / 'docker.sock').exists():
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('Isolated daemon socket timed out')
                time.sleep(1)
            else:
                raise RuntimeError('Isolated daemon socket timed out')
            call(HOST + ['exec', name, 'chown', f'{os.getuid()}:{os.getgid()}', '/socket/docker.sock'])
            call(HOST + ['exec', name, 'chmod', '0600', '/socket/docker.sock'])
            owned_socket = sockets / 'docker.sock'
            if not stat.S_ISSOCK(owned_socket.lstat().st_mode) or owned_socket.lstat().st_uid != os.getuid():
                raise RuntimeError('Isolated socket ownership changed')
            context = name
            call(['docker', 'context', 'create', context, '--docker', 'host=unix://' + str(owned_socket)])
            isolated = ['docker', '--context', context]
            for _ in range(60):
                result = _command(isolated + ['info', '--format', '{{.ID}}'], timeout=min(5, max(0.1, deadline-time.monotonic())))
                if result.returncode == 0:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('Isolated daemon readiness timed out')
                time.sleep(1)
            else:
                raise RuntimeError('Isolated daemon readiness timed out')
            call(isolated + ['swarm', 'init', '--advertise-addr', '127.0.0.1'])
            # Zero replicas: no nested workload container or network image pull.
            call(isolated + ['service', 'create', '--name', SERVICE, '--replicas', '0', '--no-resolve-image', IMAGE])
            def inspect():
                return json.loads(call(isolated + ['service', 'inspect', SERVICE]))[0]
            before = inspect()
            expected = copy.deepcopy(before['Spec'])
            expected.setdefault('Labels', {})['com.makepad.fixture-revision'] = 'candidate'
            api = call(isolated + ['version', '--format', '{{.Server.APIVersion}}']).strip()
            _request_update(context, api, before, expected)
            after = inspect()
            assert after['ID'] == before['ID'] and after['Spec'] == expected
            assert after['Version']['Index'] > before['Version']['Index']
            foreign = copy.deepcopy(expected)
            foreign['Labels']['com.makepad.fixture-revision'] = 'stale-write'
            try:
                _request_update(context, api, before, foreign)
            except RuntimeError:
                pass
            else:
                raise AssertionError('Actual Engine accepted a stale original version')
            assert inspect()['Spec'] == expected
            print(json.dumps({'actual_engine_http_200': True, 'actual_stale_version_rejected': True,
                              'exact_candidate_readback': True, 'api_version': api,
                              'isolated_daemon_image': IMAGE, 'ssh_end_to_end_exercised': False}))
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                if created:
                    # Only this invocation's unique container and its anonymous
                    # daemon-data volume. Never remove shared host services/data.
                    proof = _command(HOST + ['inspect', '--type', 'container', name], timeout=10)
                    if proof.returncode:
                        raise RuntimeError('Isolated daemon cleanup ownership unverified')
                    owned = json.loads(proof.stdout)[0]
                    if owned['Name'] != '/' + name or owned['Config']['Labels'].get('com.makepad.fixture') != name:
                        raise RuntimeError('Isolated daemon cleanup ownership changed')
                    result = _command(HOST + ['rm', '--force', '--volumes', owned['Id']], timeout=30)
                    if result.returncode:
                        raise RuntimeError('Isolated daemon cleanup unverified')
                result = _command(HOST + ['info', '--format', '{{.Swarm.LocalNodeState}}'], timeout=15)
                if result.returncode or result.stdout.decode().strip() != prior_swarm:
                    raise RuntimeError('Shared daemon Swarm state changed')
            except BaseException as cleanup:
                if primary is not None:
                    raise RuntimeError('Isolated fixture failed and cleanup is unverified') from primary
                raise
            finally:
                if prior_config is None:
                    os.environ.pop('DOCKER_CONFIG', None)
                else:
                    os.environ['DOCKER_CONFIG'] = prior_config


if __name__ == '__main__':
    main()
