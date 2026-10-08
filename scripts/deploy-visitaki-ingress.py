"""Add only Visitaki routes to the existing proxy through a remote Docker context."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

SERVICE = 'makepad-edge_nginx'
ROOT = Path(__file__).resolve().parents[1]
TARGETS = {
    'identity': ('visitaki-identity', 'makepad_keycloak_visitaki_proxy'),
    'app': ('visitaki-preview', 'makepad_visitaki_preview_app'),
}


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
            assert previous[0]['ConfigName'].startswith('visitaki_'), 'Unowned config at Visitaki target'
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
        receipt.with_suffix('.before.json').write_text(json.dumps(before, indent=2))
        try:
            run('docker', 'service', 'update', '--detach=false', *changes, SERVICE)
            after = inspect()
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
            receipt.write_text(json.dumps({'phase': args.phase, 'revision': os.environ['GITHUB_SHA'],
                'service_version': after['Version']['Index'], 'existing_configuration_preserved': True,
                'configs': [name for name, _ in added], 'neighbor_status': neighbors,
                'external_route_smoke': True, 'passed': True}, indent=2))
            print(receipt.read_text())
        except BaseException:
            if inspect()['Version']['Index'] != before['Version']['Index']:
                run('docker', 'service', 'rollback', '--detach=false', SERVICE)
                restored = inspect()['Spec']
                assert restored['TaskTemplate']['ContainerSpec'] == spec
            raise
    finally:
        if candidate:
            subprocess.run(['docker', 'rm', '-f', candidate], capture_output=True)


if __name__ == '__main__':
    main()
