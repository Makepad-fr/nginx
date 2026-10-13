#!/usr/bin/env python3
"""Upgrade only the shared proxy image, preserving its complete live service spec."""
import argparse
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
SERVICE = 'makepad-edge_nginx'


def run(*args, **kwargs):
    return subprocess.check_output(args, **kwargs).decode()


def inspect():
    return json.loads(run('docker', 'service', 'inspect', SERVICE))[0]


def image_from_compose(text):
    images = re.findall(r'^    image: (nginx:[^\s]+@sha256:[a-f0-9]{64})$', text, re.M)
    if len(images) != 1:
        raise RuntimeError('Require exactly one digest-pinned official Nginx image')
    return images[0]


def candidate_spec(before, expected, image):
    spec = copy.deepcopy(before['Spec'])
    if spec['TaskTemplate']['ContainerSpec']['Image'] != expected:
        raise RuntimeError('Previous image differs from reviewed release input')
    if before.get('UpdateStatus', {}).get('State', 'completed') != 'completed':
        raise RuntimeError('An ingress update or rollback is unresolved')
    if spec.get('UpdateConfig', {}).get('FailureAction') != 'rollback':
        raise RuntimeError('Require Swarm automatic health-failure rollback')
    if spec.get('Mode', {}).get('Replicated', {}).get('Replicas') != 1:
        raise RuntimeError('Require the reviewed one-replica ingress topology')
    spec['TaskTemplate']['ContainerSpec']['Image'] = image
    return spec


class LocalDocker(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/var/run/docker.sock')


def update(before, spec):
    # Docker's optimistic version check rejects concurrent changes atomically.
    api = run('docker', 'version', '--format', '{{.Server.APIVersion}}').strip()
    if not re.fullmatch(r'1\.\d+', api) or not re.fullmatch(r'[a-z0-9]+', before['ID']):
        raise RuntimeError('Unexpected Docker service identity or API version')
    connection = LocalDocker('localhost', timeout=30)
    try:
        connection.request('POST', f"/v{api}/services/{before['ID']}/update?version={before['Version']['Index']}",
                           body=json.dumps(spec), headers={'Content-Type': 'application/json'})
        response = connection.getresponse()
        response.read()  # Never emit daemon response bodies containing runtime data.
        if response.status != 200:
            raise RuntimeError(f'Atomic image update rejected (HTTP {response.status}); inspect before retrying')
    finally:
        connection.close()


def verify_spec(after, expected):
    if after['Spec'] != expected:
        raise RuntimeError('Live service differs from the exact image-only candidate; do not overwrite concurrent changes')
    state = after.get('UpdateStatus', {}).get('State')
    if state in ('paused', 'rollback_started', 'rollback_paused', 'rollback_completed'):
        raise RuntimeError('Image rollout failed; inspect Swarm automatic rollback before any further action')
    return state == 'completed'


def local_container():
    ids = run('docker', 'ps', '-q', '--filter', 'label=com.docker.swarm.service.name='+SERVICE).split()
    if len(ids) != 1:
        raise RuntimeError('Require exactly one running local ingress container')
    return ids[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-image', required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    # This helper runs on the existing application host against its local daemon.
    context = json.loads(run('docker', 'context', 'inspect'))[0]
    if context['Endpoints']['docker']['Host'] != 'unix:///var/run/docker.sock' or os.environ.get('DOCKER_HOST', 'unix:///var/run/docker.sock') != 'unix:///var/run/docker.sock':
        raise RuntimeError('Run on the app host using its local Docker socket')
    image = image_from_compose((ROOT/'compose.yml').read_text())
    run('docker', 'pull', image, stderr=subprocess.STDOUT)
    before = inspect()
    spec = candidate_spec(before, args.expected_image, image)
    current = local_container()
    with tempfile.TemporaryDirectory(prefix='nginx-image-preflight-') as directory:
        conf = Path(directory)/'conf'
        conf.mkdir()
        run('docker', 'cp', current+':/etc/nginx/conf.d/.', str(conf))
        run('docker', 'run', '--rm', '--network', 'container:'+current,
            '--volumes-from', current+':ro', '--mount', 'type=bind,src='+str(conf)+',dst=/etc/nginx/conf.d,readonly',
            '--entrypoint', 'nginx', image, '-t', stderr=subprocess.STDOUT)
    if inspect()['Version']['Index'] != before['Version']['Index']:
        raise RuntimeError('Ingress changed during preflight; re-review before retrying')
    if args.check:
        print('Candidate image validates all live routes, certificates, mounts and upstreams; no service mutation.')
        return
    if image == args.expected_image:
        raise RuntimeError('Candidate is already deployed; use --check and live smoke instead')
    update(before, spec)
    deadline = time.monotonic()+180
    while time.monotonic() < deadline:
        after = inspect()
        if verify_spec(after, spec):
            current = local_container()
            state = json.loads(run('docker', 'inspect', current))[0]
            if state['State'].get('Health', {}).get('Status') == 'healthy' and state['Config']['Image'] == image:
                run('docker', 'exec', current, 'nginx', '-t', stderr=subprocess.STDOUT)
                print(json.dumps({'service': SERVICE, 'image': image, 'previous_image': args.expected_image,
                                  'version': after['Version']['Index'], 'health': 'healthy',
                                  'preserved_spec_sha256': hashlib.sha256(json.dumps(before['Spec'], sort_keys=True).encode()).hexdigest()}))
                return
        time.sleep(2)
    # Swarm alone owns health-failure rollback. A second blind rollback can undo
    # its recovery or a concurrent release; leave a failed gate for inspection.
    raise RuntimeError('Image rollout timed out; inspect service and automatic rollback before continuing')


if __name__ == '__main__':
    main()
