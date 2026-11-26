"""Add only Visitaki routes to the existing proxy through a remote Docker context."""
import argparse
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from docker_context_api import update_service

SERVICE = 'makepad-edge_nginx'
ROOT = Path(__file__).resolve().parents[1]
TARGETS = {
    'identity': ('visitaki-identity', 'makepad_keycloak_visitaki_proxy'),
    'app': ('visitaki-preview', 'makepad_visitaki_preview_app'),
}
# The reviewed host-guard rollout predates this helper's config-name prefix.
# Permit migration only from its exact app-route bytes, never another route.
LEGACY_APP_CONFIG_SHA256 = '025120c125755976cf6956db45f581e9271ac84f43762df4e99746403786c818'


def run(*args, data=None):
    result = subprocess.run(list(args), input=data, capture_output=True, timeout=300)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors='replace')[-3000:])
    return result.stdout.decode()


def inspect():
    return json.loads(run('docker', 'service', 'inspect', SERVICE))[0]


def status(host, path='/', tls=True):
    port = '443' if tls else '80'
    scheme = 'https' if tls else 'http'
    return run('curl', '--silent', '--show-error', '--max-time', '15',
        '--resolve', host + ':' + port + ':135.181.141.31',
        '--output', '/dev/null', '--write-out', '%{http_code}',
        scheme + '://' + host + path).strip()


def verify_phase(phase):
    if phase == 'bootstrap':
        return all(status(host, tls=False) == '503' for host in ['visitaki.com', 'www.visitaki.com', 'auth.visitaki.com'])
    if phase == 'identity':
        return (status('auth.visitaki.com', '/realms/visitaki/.well-known/openid-configuration') == '200'
                and all(status('auth.visitaki.com', path) == '404' for path in ['/admin/', '/realms/master/', '/health/ready']))
    return status('visitaki.com') == '200' and status('www.visitaki.com') == '308'


def bootstrap(hosts):
    return ('server { listen 80; server_name ' + hosts + '; access_log off; '
            'error_log /dev/stderr crit; '
            'location /.well-known/acme-challenge/ { root /var/lib/letsencrypt; } '
            'location / { return 503; } }\n')


def owned_config(config, route):
    if config['ConfigName'].startswith('visitaki_'):
        return True
    match = re.fullmatch(r'visitaki-host-guard-[a-f0-9]{8}-([a-f0-9]{16})', config['ConfigName'])
    if route != 'visitaki-preview' or not match:
        return False
    if config['File']['Name'] != '/etc/nginx/templates/visitaki-preview.conf.template':
        return False
    inspected = json.loads(run('docker', 'config', 'inspect', config['ConfigID']))
    if len(inspected) != 1:
        return False
    current = inspected[0]
    if current['ID'] != config['ConfigID'] or current['Spec']['Name'] != config['ConfigName']:
        return False
    digest = hashlib.sha256(base64.b64decode(current['Spec']['Data'], validate=True)).hexdigest()
    return digest == LEGACY_APP_CONFIG_SHA256 and match[1] == digest[:16]


def changes_for(before, routes, networks):
    configs = before['Spec']['TaskTemplate']['ContainerSpec'].get('Configs', [])
    changes = []
    removed = []
    added = []
    for route, content in routes.items():
        target = '/etc/nginx/templates/' + route + '.conf.template'
        previous = [c for c in configs if c['File']['Name'] == target]
        assert len(previous) <= 1
        name = 'visitaki_' + route + '_' + hashlib.sha256(content.encode()).hexdigest()[:16]
        if previous and previous[0]['ConfigName'] == name:
            continue
        if previous:
            assert owned_config(previous[0], route), 'Unowned config at Visitaki target'
            changes.extend(['--config-rm', previous[0]['ConfigName']])
            removed.append(previous[0]['ConfigID'])
        changes.extend(['--config-add', f'source={name},target={target},mode=0444'])
        added.append((name, content))
    current = {n['Target'] for n in before['Spec']['TaskTemplate'].get('Networks', [])}
    for network in networks:
        item = json.loads(run('docker', 'network', 'inspect', network))[0]
        assert item['Driver'] == 'overlay' and item['Attachable'], network
        if item['Id'] not in current:
            changes.extend(['--network-add', network])
    return changes, removed, added


def candidate_spec(before, routes, networks):
    """Change only the selected config references and missing network attachments."""
    result = copy.deepcopy(before['Spec'])
    spec = result['TaskTemplate']['ContainerSpec']
    configs = spec.setdefault('Configs', [])
    for route, content in routes.items():
        target = '/etc/nginx/templates/' + route + '.conf.template'
        name = 'visitaki_' + route + '_' + hashlib.sha256(content.encode()).hexdigest()[:16]
        matches = [c for c in configs if c['File']['Name'] == target]
        assert len(matches) <= 1
        if matches and matches[0]['ConfigName'] == name:
            continue
        values = json.loads(run('docker', 'config', 'inspect', name))
        assert len(values) == 1 and values[0]['Spec']['Name'] == name
        value = values[0]
        assert base64.b64decode(value['Spec']['Data'], validate=True) == content.encode()
        replacement = {'ConfigID': value['ID'], 'ConfigName': name,
                       'File': {'Name': target, 'UID': '0', 'GID': '0', 'Mode': 0o444}}
        if matches:
            assert owned_config(matches[0], route), 'Unowned config at Visitaki target'
            configs[configs.index(matches[0])] = replacement
        else:
            configs.append(replacement)
    attached = result['TaskTemplate'].get('Networks', [])
    for network in networks:
        value = json.loads(run('docker', 'network', 'inspect', network))[0]
        assert value['Driver'] == 'overlay' and value['Attachable']
        if not any(n['Target'] == value['Id'] for n in attached):
            attached.append({'Target': value['Id']})
            result['TaskTemplate']['Networks'] = attached
    return result


def wait_service(service_id, expected, deadline, restored=False):
    while time.monotonic() < deadline:
        current = inspect()
        if current['ID'] != service_id or current['Spec'] != expected:
            raise RuntimeError('Shared service changed; refusing to overwrite concurrent work')
        status = current.get('UpdateStatus')
        if status is not None and not isinstance(status, dict):
            raise RuntimeError('Invalid service update status')
        # Swarm resets UpdateStatus when committing the spec; the updater sets it
        # asynchronously. Missing status is pending only under the exact spec guard.
        state = status.get('State') if status is not None else None
        if state == 'completed' or (restored and state == 'rollback_completed'):
            return current
        if state not in (None, 'updating') and not (restored and state == 'rollback_started'):
            raise RuntimeError('Service update did not converge; inspect retained state')
        time.sleep(2)
    raise RuntimeError('Service convergence timed out')


def restore_owned(before, candidate, acknowledged):
    if acknowledged is not True:
        raise RuntimeError('Unacknowledged update cannot authorize rollback')
    current = inspect()
    if current['ID'] != before['ID']:
        raise RuntimeError('Service identity changed; rollback refused')
    deadline = time.monotonic() + 300
    if current['Spec'] != before['Spec']:
        if current['Spec'] != candidate or current.get('PreviousSpec') != before['Spec']:
            raise RuntimeError('Service update ownership lost; rollback refused')
        # Restore the saved full spec with the just-verified version. Do not ask
        # Docker CLI to inspect again or use the mutable "previous" rollback slot.
        update_service(current, before['Spec'])
    return wait_service(before['ID'], before['Spec'], deadline, restored=True)


def write_receipt(path, value):
    # Full service specs can contain private runtime fields. Never overwrite
    # earlier evidence or rely on the runner's umask for confidentiality.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['bootstrap', 'identity', 'app'])
    parser.add_argument('--receipt', required=True)
    args = parser.parse_args()
    assert os.environ.get('GITHUB_ACTIONS') == 'true'
    assert os.environ.get('GITHUB_REF') == 'refs/heads/main'
    assert os.environ.get('DEPLOY_SSH_HOST') == '135.181.141.31'
    receipt = Path(args.receipt)
    assert not receipt.exists()
    before = inspect()
    neighbors = {host: status(host) for host in ['catwlk.com', 'pluck.makepad.fr', 'brio-staging.makepad.fr']}
    assert all(int(code) < 500 for code in neighbors.values()), 'Neighbor unhealthy before deployment'
    assert before.get('UpdateStatus', {}).get('State') not in ['updating', 'rollback_started']
    spec = before['Spec']['TaskTemplate']['ContainerSpec']
    active = run('docker', 'ps', '-q', '--filter', 'label=com.docker.swarm.service.name=' + SERVICE).split()
    assert len(active) == 1
    if args.phase == 'bootstrap':
        routes = {'visitaki-identity': bootstrap('auth.visitaki.com'),
                  'visitaki-preview': bootstrap('visitaki.com www.visitaki.com')}
        assert not any(c['File']['Name'].split('/')[-1].startswith('visitaki-') for c in spec.get('Configs', [])), 'Bootstrap already installed'
        networks = []
    else:
        route, network = TARGETS[args.phase]
        routes = {route: (ROOT / 'sites' / (route + '.conf.template')).read_text()}
        networks = [network]
    changes, removed, added = changes_for(before, routes, networks)
    assert changes, 'Requested route revision is already installed'
    candidate = None
    try:
        with tempfile.TemporaryDirectory(prefix='visitaki-ingress-') as temporary:
            directory = Path(temporary) / 'conf'
            directory.mkdir()
            run('docker', 'cp', active[0] + ':/etc/nginx/conf.d/.', str(directory))
            for route, content in routes.items():
                (directory / (route + '.conf')).write_text(content)
            candidate = run('docker', 'create', '--network', 'container:' + active[0],
                '--volumes-from', active[0] + ':ro', '--entrypoint', 'nginx', spec['Image'], '-t').strip()
            run('docker', 'cp', str(directory) + '/.', candidate + ':/etc/nginx/conf.d/')
            run('docker', 'start', '-a', candidate)
            assert run('docker', 'inspect', '-f', '{{.State.ExitCode}}', candidate).strip() == '0'
        assert inspect()['Version']['Index'] == before['Version']['Index'], 'Shared edge changed during preflight'
        for name, content in added:
            exists = subprocess.run(['docker', 'config', 'inspect', name], capture_output=True)
            if exists.returncode:
                run('docker', 'config', 'create', '--label', 'com.makepad.owner=Makepad-fr/nginx', name, '-', data=content.encode())
            else:
                import base64
                current = json.loads(exists.stdout)[0]
                assert base64.b64decode(current['Spec']['Data']).decode() == content
        expected = candidate_spec(before, routes, networks)
        write_receipt(receipt.with_suffix('.before.json'), before)
        write_receipt(receipt.with_suffix('.candidate.json'), expected)
        acknowledged = False
        try:
            deadline = time.monotonic() + 300
            update_service(before, expected)
            acknowledged = True
            after = wait_service(before['ID'], expected, deadline)
            updated = after['Spec']['TaskTemplate']['ContainerSpec']
            for key in set(spec) | set(updated):
                if key != 'Configs':
                    assert spec.get(key) == updated.get(key), key
            retained = {c['ConfigID']: c for c in spec.get('Configs', []) if c['ConfigID'] not in removed}
            actual = {c['ConfigID']: c for c in updated.get('Configs', [])}
            assert all(actual.get(k) == v for k, v in retained.items())
            prior_networks = before['Spec']['TaskTemplate'].get('Networks', [])
            assert all(n in after['Spec']['TaskTemplate'].get('Networks', []) for n in prior_networks)
            active = run('docker', 'ps', '-q', '--filter', 'label=com.docker.swarm.service.name=' + SERVICE).split()
            assert len(active) == 1
            run('docker', 'exec', active[0], 'nginx', '-t')
            for attempt in range(15):
                try:
                    if verify_phase(args.phase):
                        break
                except RuntimeError:
                    pass
                time.sleep(2)
            else:
                raise RuntimeError('Visitaki external route smoke failed')
            assert {host: status(host) for host in neighbors} == neighbors, 'Neighbor route status changed'
            write_receipt(receipt, {'phase': args.phase, 'revision': os.environ['GITHUB_SHA'],
                'service_version': after['Version']['Index'], 'existing_configuration_preserved': True,
                'configs': [name for name, _ in added], 'neighbor_status': neighbors,
                'external_route_smoke': True, 'passed': True})
            print(receipt.read_text())
        except BaseException as primary:
            if acknowledged:
                try:
                    restore_owned(before, expected, acknowledged)
                except BaseException as recovery:
                    write_receipt(receipt.with_suffix('.recovery.json'), {
                        'primary_error_type': type(primary).__name__,
                        'recovery_error_type': type(recovery).__name__,
                        'automatic_recovery_verified': False,
                    })
                    raise RuntimeError('Ingress failed and recovery is unverified; inspect retained receipts') from primary
            raise
    finally:
        if candidate:
            subprocess.run(['docker', 'rm', '-f', candidate], capture_output=True)


if __name__ == '__main__':
    main()
