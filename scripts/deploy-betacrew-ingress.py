#!/usr/bin/env python3
"""Add BetaCrew routes to the existing shared ingress without replacing other projects."""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SERVICE = 'makepad-edge_nginx'
TARGETS = {'betacrew-prod.conf.template'}

def run(*args, **kwargs):
    return subprocess.check_output(args, **kwargs).decode()

def inspect():
    return json.loads(run('docker', 'service', 'inspect', SERVICE))[0]

def verify_applied(after, expected_configs, required_networks):
    if after.get('UpdateStatus', {}).get('State') != 'completed':
        raise RuntimeError('Ingress update did not complete; inspect Swarm rollback/update state')
    actual = {c['File']['Name']: c['ConfigID'] for c in after['Spec']['TaskTemplate']['ContainerSpec'].get('Configs', [])}
    if any(actual.get(target) != config_id for target, config_id in expected_configs.items()):
        raise RuntimeError('Expected BetaCrew route was not installed')
    attached = {n['Target'] for n in after['Spec']['TaskTemplate'].get('Networks', [])}
    if not required_networks <= attached:
        raise RuntimeError('Expected BetaCrew network was not attached')

def verified_config(config_name, content):
    existing = subprocess.run(['docker', 'config', 'inspect', config_name], capture_output=True)
    if existing.returncode:
        run('docker', 'config', 'create', '--label', 'com.makepad.owner=Makepad-fr/nginx', config_name, '-', input=content.encode())
        raw = run('docker', 'config', 'inspect', config_name)
    else:
        raw = existing.stdout
    config = json.loads(raw)[0]
    if base64.b64decode(config['Spec']['Data'], validate=True) != content.encode():
        raise RuntimeError('Existing BetaCrew config content differs from the validated route')
    return config['ID']

def render_template(template, environment):
    variables = dict(item.split('=', 1) for item in environment if '=' in item)
    names = set(re.findall(r'\$\{(BETACREW_[A-Z_]+)\}', template))
    expected = dict(line.split('=', 1) for line in (ROOT/'envs/production/.env.betacrew').read_text().splitlines() if '=' in line)
    if not names or any(variables.get(name) != expected.get(name) or not expected.get(name) for name in names):
        raise RuntimeError('Existing BetaCrew runtime differs from the reviewed environment')
    return re.sub(r'\$\{(BETACREW_[A-Z_]+)\}', lambda match: variables[match[1]], template)

def reload_certificate(current):
    # Certificate renewal changes mounted files, not service configuration.
    run('docker', 'exec', current, 'nginx', '-t', stderr=subprocess.STDOUT)
    run('docker', 'exec', current, 'nginx', '-s', 'reload', stderr=subprocess.STDOUT)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--reload-certificate", action="store_true")
    args = parser.parse_args()
    if any(socket.gethostbyname(host) != '135.181.141.31' for host in ('betacrew.app','www.betacrew.app')):
        raise RuntimeError('BetaCrew DNS does not point to the app proxy')
    os.umask(0o077)
    with open('/tmp/makepad-betacrew-ingress.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = inspect()
        spec = before['Spec']['TaskTemplate']['ContainerSpec']
        if before['Spec'].get('UpdateConfig', {}).get('FailureAction') != 'rollback':
            raise RuntimeError('Shared ingress must have Swarm automatic rollback configured')
        containers = run('docker', 'ps', '-q', '--filter', 'label=com.docker.swarm.service.name='+SERVICE).split()
        if len(containers) != 1:
            raise RuntimeError('Expected one local shared ingress container')
        current = containers[0]
        source = 'betacrew-prod.conf.template'
        rendered = {'betacrew-prod.conf.template': render_template((ROOT/'sites'/source).read_text(), spec.get('Env', []))}
        previous_configs = spec.get('Configs', [])
        networks = ['makepad_betacrew_prod_app']
        previous_networks = {n['Target'] for n in before['Spec']['TaskTemplate']['Networks']}
        additions = []
        required_networks = set()
        for network in networks:
            value = json.loads(run('docker', 'network', 'inspect', network))[0]
            if value['Driver'] != 'overlay' or not value['Attachable']:
                raise RuntimeError('BetaCrew requires the existing attachable BetaCrew overlay')
            required_networks.add(value['Id'])
            if value['Id'] not in previous_networks:
                raise RuntimeError('Proxy must already be attached to the BetaCrew overlay')
        if args.reload_certificate:
            reload_certificate(current)
            print('Existing ingress configuration validated; certificate reload requested.')
            return
        with tempfile.TemporaryDirectory(prefix='betacrew-ingress-') as directory:
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
                config_name = 'betacrew_'+name.replace('.', '_')+'_'+digest
                expected_configs['/etc/nginx/templates/'+name] = verified_config(config_name, content)
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
                print('BetaCrew ingress deployed; existing routes, image, mounts, environment and networks preserved.')
            except BaseException:
                # Swarm owns health-failure rollback. Do not issue a second rollback:
                # this could undo automatic recovery or an unrelated concurrent update.
                # Any unexpected state is a failed deployment requiring inspection.
                raise

if __name__ == '__main__':
    main()
