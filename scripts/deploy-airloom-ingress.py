#!/usr/bin/env python3
"""Add Airloom routes to the existing shared ingress without replacing other projects."""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SERVICE = 'makepad-edge_nginx'
TARGETS = {'airloom-prod.conf.template'}

def run(*args, **kwargs):
    return subprocess.check_output(args, **kwargs).decode()

def inspect():
    return json.loads(run('docker', 'service', 'inspect', SERVICE))[0]

def verify_applied(after, expected_configs, required_networks):
    if after.get('UpdateStatus', {}).get('State') != 'completed':
        raise RuntimeError('Ingress update did not complete; inspect Swarm rollback/update state')
    actual = {c['File']['Name']: c['ConfigID'] for c in after['Spec']['TaskTemplate']['ContainerSpec'].get('Configs', [])}
    if any(actual.get(target) != config_id for target, config_id in expected_configs.items()):
        raise RuntimeError('Expected Airloom route was not installed')
    attached = {n['Target'] for n in after['Spec']['TaskTemplate'].get('Networks', [])}
    if not required_networks <= attached:
        raise RuntimeError('Expected Airloom network was not attached')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--bootstrap", action="store_true", help="Serve only HTTP ACME while provisioning TLS")
    args = parser.parse_args()
    os.umask(0o077)
    with open('/tmp/makepad-airloom-ingress.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = inspect()
        spec = before['Spec']['TaskTemplate']['ContainerSpec']
        if before['Spec'].get('UpdateConfig', {}).get('FailureAction') != 'rollback':
            raise RuntimeError('Shared ingress must have Swarm automatic rollback configured')
        containers = run('docker', 'ps', '-q', '--filter', 'label=com.docker.swarm.service.name='+SERVICE).split()
        if len(containers) != 1:
            raise RuntimeError('Expected one local shared ingress container')
        current = containers[0]
        source = 'airloom-acme.conf' if args.bootstrap else 'airloom-prod.conf.template'
        rendered = {'airloom-prod.conf.template': (ROOT/'sites'/source).read_text()}
        previous_configs = spec.get('Configs', [])
        networks = ['makepad_airloom_prod_app']
        previous_networks = {n['Target'] for n in before['Spec']['TaskTemplate']['Networks']}
        additions = []
        required_networks = set()
        for network in networks:
            value = json.loads(run('docker', 'network', 'inspect', network))[0]
            if value['Driver'] != 'overlay' or not value['Attachable'] or value['Options'].get('encrypted') != 'true':
                raise RuntimeError('Airloom requires its encrypted attachable overlays')
            required_networks.add(value['Id'])
            if value['Id'] not in previous_networks:
                additions.extend(['--network-add', network])
        with tempfile.TemporaryDirectory(prefix='airloom-ingress-') as directory:
            candidate = Path(directory)/'conf'
            candidate.mkdir()
            run('docker', 'cp', current+':/etc/nginx/conf.d/.', str(candidate))
            for name, content in rendered.items():
                (candidate/name.removesuffix('.template')).write_text(content)
            run('docker', 'run', '--rm', '--network', 'container:'+current,
                '--volumes-from', current+':ro', '--mount', 'type=bind,src='+str(candidate)+',dst=/etc/nginx/conf.d,readonly',
                '--entrypoint', 'nginx', spec['Image'], '-t', stderr=subprocess.STDOUT)
            if inspect()['Version']['Index'] != before['Version']['Index']:
                raise RuntimeError('Shared ingress changed during validation; retry after review')
            if args.check:
                print('Candidate Nginx configuration passed syntax validation with all existing routes.')
                return
            changes = list(additions)
            for config in previous_configs:
                if Path(config['File']['Name']).name in TARGETS:
                    changes.extend(['--config-rm', config['ConfigName']])
            expected_configs = {}
            for name, content in rendered.items():
                digest = hashlib.sha256(content.encode()).hexdigest()[:16]
                config_name = 'airloom_'+name.replace('.', '_')+'_'+digest
                exists = subprocess.run(['docker', 'config', 'inspect', config_name], capture_output=True)
                if exists.returncode:
                    run('docker', 'config', 'create', '--label', 'com.makepad.owner=Makepad-fr/nginx', config_name, '-', input=content.encode())
                expected_configs['/etc/nginx/templates/'+name] = json.loads(run('docker', 'config', 'inspect', config_name))[0]['ID']
                changes.extend(['--config-add', 'source='+config_name+',target=/etc/nginx/templates/'+name+',mode=0444'])
            try:
                try:
                    run('docker', 'service', 'update', '--detach=false', *changes, SERVICE, stderr=subprocess.STDOUT)
                except subprocess.CalledProcessError as error:
                    output = (error.output or b'').decode(errors='replace')
                    raise RuntimeError('Shared ingress update failed: '+output[-4000:]) from error
                after = inspect()
                verify_applied(after, expected_configs, required_networks)
                preserved = {c['ConfigID'] for c in previous_configs if Path(c['File']['Name']).name not in TARGETS}
                actual = {c['ConfigID'] for c in after['Spec']['TaskTemplate']['ContainerSpec']['Configs']}
                if not preserved <= actual or not previous_networks <= {n['Target'] for n in after['Spec']['TaskTemplate']['Networks']}:
                    raise RuntimeError('Shared ingress resources were not preserved')
                after_container = after['Spec']['TaskTemplate']['ContainerSpec']
                normalize_mounts = lambda values: sorted(values, key=lambda mount: mount['Target'])
                if normalize_mounts(after_container.get('Mounts', [])) != normalize_mounts(spec.get('Mounts', [])):
                    raise RuntimeError('Unexpected change to shared mounts')
                if sorted(after_container.get('Env', [])) != sorted(spec.get('Env', [])):
                    raise RuntimeError('Unexpected change to shared environment')
                for key in ('Image', 'Command', 'Args'):
                    if after['Spec']['TaskTemplate']['ContainerSpec'].get(key) != spec.get(key):
                        raise RuntimeError('Unexpected change to '+key)
                active = run('docker', 'ps', '-q', '--filter', 'label=com.docker.swarm.service.name='+SERVICE).split()
                if len(active) != 1:
                    raise RuntimeError('Shared ingress did not converge to one container')
                health = json.loads(run('docker', 'inspect', active[0]))[0].get('State', {}).get('Health', {}).get('Status')
                if health != 'healthy':
                    raise RuntimeError('Shared ingress health check did not converge')
                run('docker', 'exec', active[0], 'nginx', '-t', stderr=subprocess.STDOUT)
                print('Airloom ingress deployed; existing routes, image, mounts, environment and networks preserved.')
            except BaseException:
                # Swarm owns health-failure rollback. Do not issue a second rollback:
                # this could undo automatic recovery or an unrelated concurrent update.
                # Any unexpected state is a failed deployment requiring inspection.
                raise

if __name__ == '__main__':
    main()
