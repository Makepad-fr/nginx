#!/usr/bin/env python3
"""Add Brio staging routes to the existing shared ingress without replacing other projects."""
from brio_release_guard import deployment_guard
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

def run(*args, **kwargs):
    return subprocess.check_output(args, **kwargs).decode()

def inspect():
    return json.loads(run('docker', 'service', 'inspect', SERVICE))[0]

def selection(vif_staging, settings, legacy_cutover=False):
    if legacy_cutover and not vif_staging:
        raise RuntimeError("Legacy cutover requires the explicit Vif staging target")
    if vif_staging:
        if settings.get('MAKEPAD_PROXY_VIF_PLATFORM_STAGING_APP_NETWORK') != 'makepad_vif_platform_staging_edge':
            raise RuntimeError('Vif staging requires its dedicated inventoried edge network')
        if settings.get('MAKEPAD_PROXY_VIF_LANDING_STAGING_APP_NETWORK') != 'makepad_vif_landing_staging_edge':
            raise RuntimeError('Vif landing requires its dedicated inventoried edge network')
        return (('vif-staging.conf.template','brio-staging.conf.template') if legacy_cutover else ('vif-staging.conf.template',)), ['makepad_brio_staging_app', 'makepad_vif_platform_staging_edge', 'makepad_vif_landing_staging_edge']
    return ('brio-staging.conf.template', 'maildev-brio-staging.conf.template'), ['makepad_brio_staging_app', 'makepad_brio_staging_maildev_web']


def rollback_owned(before, current):
    if current['Spec'] == before['Spec']:
        return False
    if current.get('PreviousSpec') != before['Spec']:
        raise RuntimeError('Ingress history changed; refusing to revert an unrelated deployment')
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--vif-staging", action="store_true")
    parser.add_argument("--legacy-cutover", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    with deployment_guard(), open('/tmp/makepad-brio-ingress.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = inspect()
        spec = before['Spec']['TaskTemplate']['ContainerSpec']
        containers = run('docker', 'ps', '-q', '--filter', 'label=com.docker.swarm.service.name='+SERVICE).split()
        if len(containers) != 1:
            raise RuntimeError('Expected one local shared ingress container')
        current = containers[0]
        settings = dict(line.split('=', 1) for line in (ROOT/'envs/production/.env.proxy').read_text().splitlines() if line and not line.startswith('#') and '=' in line)
        names, networks = selection(args.vif_staging, settings, args.legacy_cutover)
        targets = set(names) | {'02-brio-common.conf.template'}
        rendered = {}
        for name in names:
            source_name = 'brio-vif-redirects.conf.template' if args.legacy_cutover and name == 'brio-staging.conf.template' else name
            rendered[name] = re.sub(r'\$\{([A-Z][A-Z0-9_]*)\}', lambda m: settings[m[1]], (ROOT/'sites'/source_name).read_text())
        common = (ROOT/'sites/00-common.conf.template').read_text()
        rendered['02-brio-common.conf.template'] = common[common.index('# Brio logs'):]
        previous_configs = spec.get('Configs', [])
        for config in previous_configs:
            if Path(config['File']['Name']).name in targets:
                continue
            content = json.loads(run('docker', 'config', 'inspect', config['ConfigID']))[0]['Spec']['Data']
            if 'log_format brio_privacy' in base64.b64decode(content).decode():
                rendered.pop('02-brio-common.conf.template', None)
        previous_networks = {n['Target'] for n in before['Spec']['TaskTemplate']['Networks']}
        additions = []
        for network in networks:
            value = json.loads(run('docker', 'network', 'inspect', network))[0]
            if value['Driver'] != 'overlay' or not value['Attachable'] or value['Options'].get('encrypted') != 'true':
                raise RuntimeError('Brio requires its encrypted attachable overlays')
            if value['Id'] not in previous_networks:
                additions.extend(['--network-add', network])
        with tempfile.TemporaryDirectory(prefix='brio-ingress-') as directory:
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
            # Apply the health check already declared in compose.yml even when
            # the existing shared service predates that declaration.
            changes.extend(['--health-cmd', 'nginx -t', '--health-interval', '15s',
                            '--health-timeout', '5s', '--health-retries', '3',
                            '--health-start-period', '10s'])
            for config in previous_configs:
                if Path(config['File']['Name']).name in targets:
                    changes.extend(['--config-rm', config['ConfigName']])
            for name, content in rendered.items():
                digest = hashlib.sha256(content.encode()).hexdigest()[:16]
                config_name = ('vif_staging_' if args.vif_staging and (name.startswith('vif-') or args.legacy_cutover and name == 'brio-staging.conf.template') else 'brio_')+name.replace('.', '_')+'_'+digest
                exists = subprocess.run(['docker', 'config', 'inspect', config_name], capture_output=True)
                if exists.returncode:
                    run('docker', 'config', 'create', '--label', 'com.makepad.owner=Makepad-fr/nginx', config_name, '-', input=content.encode())
                changes.extend(['--config-add', 'source='+config_name+',target=/etc/nginx/templates/'+name+',mode=0444'])
            try:
                try:
                    run('docker', 'service', 'update', '--detach=false', *changes, SERVICE, stderr=subprocess.STDOUT)
                except subprocess.CalledProcessError as error:
                    output = (error.output or b'').decode(errors='replace')
                    raise RuntimeError('Shared ingress update failed: '+output[-4000:]) from error
                after = inspect()
                preserved = {c['ConfigID'] for c in previous_configs if Path(c['File']['Name']).name not in targets}
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
                print('Brio ingress deployed; existing routes, image, mounts, environment and networks preserved.')
            except BaseException:
                # CLI validation can fail before changing the service. Rolling
                # back in that case would revert an unrelated prior deployment.
                if rollback_owned(before, inspect()):
                    run('docker', 'service', 'rollback', '--detach=false', SERVICE, stderr=subprocess.STDOUT)
                    if inspect()['Spec'] != before['Spec']:
                        raise RuntimeError('Ingress rollback did not restore the exact previous configuration')
                raise

if __name__ == '__main__':
    main()
