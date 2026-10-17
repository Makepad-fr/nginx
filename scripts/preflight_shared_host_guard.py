#!/usr/bin/env python3
"""Dry-run the shared-edge host guard against every exact live Docker Config.

Run on the current Swarm manager with the local Docker socket. This command
reads service/config/container state and starts one disposable nginx -t
container; it never creates Docker Configs or updates the shared service.
No configuration body, environment value, or nginx diagnostic is printed.
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid


SERVICE = 'makepad-edge_nginx'
COMMON_TARGET = '/etc/nginx/templates/00-common.conf.template'
DEFAULT_TARGET = '/etc/nginx/conf.d/default.conf'
EXPECTED_CONFIGS = 27
EXPECTED_NETWORKS = 27
EXPECTED_TLS_FILES = 25
EXPECTED_TLS_VHOSTS = 29
EXPECTED_MOUNT_TARGETS = {
    '/etc/letsencrypt',
    '/etc/letsencrypt-betacrew',
    '/var/lib/letsencrypt',
    '/var/lib/letsencrypt-betacrew',
}

# This is only prepended to the existing live common config. In particular,
# its Brio zones and log formats remain byte-for-byte intact.
COMMON_PREFIX = b'''# Unknown hosts must not inherit a tenant's redirect or content.
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    access_log off;
    return 421;
}
server {
    listen 443 ssl default_server;
    listen [::]:443 ssl default_server;
    server_name _;
    ssl_reject_handshake on;
    access_log off;
    return 421;
}

map "$ssl_server_name:$host" $edge_sni_host_match {
    default 0;
    ~*^([^:]+):\\1$ 1;
}

'''
GUARD = 'if ($edge_sni_host_match = 0) { return 421; }'
TLS_LISTEN = re.compile(r'(?m)^([ \t]*)listen[ \t]+[^;\n]*\bssl\b[^;\n]*;[^\n]*$')
SERVER_NAME = re.compile(r'(?m)^[ \t]*server_name[ \t]+[^;\n]+;')
TARGET = re.compile(r'^/etc/nginx/(templates|conf\.d)/([A-Za-z0-9][A-Za-z0-9_.-]*\.conf(?:\.template)?)$')


class PreflightError(RuntimeError):
    pass


def sha(value):
    return hashlib.sha256(value).hexdigest()


def canonical_sha(value):
    return sha(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())


def server_blocks(source):
    """Find top-level server blocks without interpreting quoted/comment braces."""
    blocks = []
    depth = 0
    current = None
    quote = None
    comment = False
    escaped = False
    line_start = 0
    index = 0
    while index < len(source):
        char = source[index]
        if comment:
            if char == '\n':
                comment = False
                line_start = index + 1
            index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == quote:
                quote = None
            if char == '\n':
                line_start = index + 1
            index += 1
            continue
        if char == '#':
            comment = True
        elif char in ('"', "'"):
            quote = char
        elif char == '$' and source[index:index + 2] == '${':
            end = source.find('}', index + 2)
            if end < 0 or '\n' in source[index:end]:
                raise PreflightError('Unparseable nginx variable expansion')
            index = end + 1
            continue
        elif char == '{':
            if depth == 0 and source[line_start:index].strip() == 'server':
                current = index
            depth += 1
        elif char == '}':
            depth -= 1
            if depth < 0:
                raise PreflightError('Unbalanced nginx configuration')
            if depth == 0 and current is not None:
                blocks.append((current, index))
                current = None
        if char == '\n':
            line_start = index + 1
        index += 1
    if depth or quote or current is not None:
        raise PreflightError('Unbalanced nginx configuration')
    return blocks


def guard_tls_vhosts(raw):
    try:
        source = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise PreflightError('Docker Config is not UTF-8') from exc
    edits = []
    for opening, closing in server_blocks(source):
        body = source[opening + 1:closing]
        listens = list(TLS_LISTEN.finditer(body))
        if not listens:
            if 'ssl_certificate' in body or GUARD in body:
                raise PreflightError('Ambiguous non-TLS server block')
            continue
        if ('default_server' in body or 'ssl_reject_handshake' in body
                or GUARD in body or not SERVER_NAME.search(body)):
            raise PreflightError('Unreviewed TLS server block shape')
        first = listens[0]
        line_end = source.find('\n', opening + 1 + first.end())
        if line_end < 0 or line_end > closing:
            raise PreflightError('TLS listen directive needs a following line')
        edits.append((line_end + 1, first.group(1) + GUARD + '\n'))
    for position, addition in reversed(edits):
        source = source[:position] + addition + source[position:]
    return source.encode(), len(edits)


def target_output(target):
    match = TARGET.fullmatch(target)
    if not match:
        raise PreflightError('Unsupported Docker Config target')
    directory, filename = match.groups()
    if directory == 'templates' and not filename.endswith('.conf.template'):
        raise PreflightError('Unexpected template target')
    if directory == 'conf.d' and not filename.endswith('.conf'):
        raise PreflightError('Unexpected direct config target')
    return directory, filename


def build_candidate(configs, networks):
    """Produce an in-memory plan; preserve every original target and byte."""
    if len(configs) != EXPECTED_CONFIGS or len(networks) != EXPECTED_NETWORKS:
        raise PreflightError('Live config/network inventory changed; review it first')
    if len(set(configs)) != len(configs) or COMMON_TARGET not in configs or DEFAULT_TARGET in configs:
        raise PreflightError('Common/default Docker Config inventory is unexpected')
    if len({canonical_sha(item) for item in networks}) != len(networks):
        raise PreflightError('Duplicate live network attachment')
    generated_names = set()
    for target in configs:
        directory, filename = target_output(target)
        if directory != 'templates':
            raise PreflightError('A live direct conf.d attachment needs manual review')
        rendered = filename.removesuffix('.template') if directory == 'templates' else filename
        if rendered in generated_names:
            raise PreflightError('Two live configs would render to the same file')
        generated_names.add(rendered)
    if 'default.conf' in generated_names:
        raise PreflightError('A live config already owns default.conf')
    common = configs[COMMON_TARGET]
    if (b'$edge_sni_host_match' in common or b'default_server' in common
            or server_blocks(common.decode('utf-8'))):
        raise PreflightError('Live common config needs manual review')
    candidate = dict(configs)
    candidate[COMMON_TARGET] = COMMON_PREFIX + common
    tls_files = 0
    tls_vhosts = 0
    for target, raw in configs.items():
        if target == COMMON_TARGET:
            continue
        transformed, count = guard_tls_vhosts(raw)
        candidate[target] = transformed
        tls_files += bool(count)
        tls_vhosts += count
    if tls_files != EXPECTED_TLS_FILES or tls_vhosts != EXPECTED_TLS_VHOSTS:
        raise PreflightError('Live TLS vhost inventory changed; review it first')
    # Refuse even a comment/directive in this mask unless it is from the
    # reviewed repository file. It must not create another listening server.
    mask = (Path(__file__).resolve().parents[1] / 'sites/default.conf').read_bytes()
    if not mask or any(line.strip() and not line.lstrip().startswith(b'#') for line in mask.splitlines()):
        raise PreflightError('The stock-default mask is no longer comment-only')
    candidate[DEFAULT_TARGET] = mask
    return candidate, tls_files, tls_vhosts


def docker(*args, timeout=60, pass_fds=()):
    try:
        result = subprocess.run(['docker', *args], capture_output=True, timeout=timeout,
                                pass_fds=pass_fds)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreflightError('Local Docker read/preflight failed; diagnostics withheld') from exc
    if result.returncode:
        raise PreflightError(f'Local Docker {args[0]} failed (exit {result.returncode}); diagnostics withheld')
    return result.stdout


def inspect_service():
    result = json.loads(docker('service', 'inspect', SERVICE))
    if len(result) != 1:
        raise PreflightError('Expected one shared-edge service')
    return result[0]


def read_live_configs(before):
    entries = before['Spec']['TaskTemplate']['ContainerSpec'].get('Configs', [])
    if len(entries) != EXPECTED_CONFIGS:
        raise PreflightError('Live Config count changed')
    contents = {}
    for entry in entries:
        target = entry['File']['Name']
        target_output(target)
        if target in contents:
            raise PreflightError('Duplicate Docker Config target')
        result = json.loads(docker('config', 'inspect', entry['ConfigID']))
        if len(result) != 1 or result[0]['ID'] != entry['ConfigID'] or result[0]['Spec']['Name'] != entry['ConfigName']:
            raise PreflightError('Docker Config identity changed during read')
        contents[target] = base64.b64decode(result[0]['Spec']['Data'], validate=True)
    return contents


def active_container(before):
    ids = docker('ps', '-q', '--filter', 'label=com.docker.swarm.service.name=' + SERVICE).decode().split()
    if len(ids) != 1:
        raise PreflightError('Expected one local running edge container')
    detail = json.loads(docker('inspect', ids[0]))
    if len(detail) != 1 or not detail[0]['State']['Running']:
        raise PreflightError('Active edge container changed')
    image = before['Spec']['TaskTemplate']['ContainerSpec']['Image']
    if detail[0]['Config']['Image'] != image:
        raise PreflightError('Active edge image differs from service Spec')
    if detail[0]['Config'].get('Labels', {}).get('com.docker.swarm.service.name') != SERVICE:
        raise PreflightError('Active container is not the reviewed edge service')
    return ids[0], detail[0]


def write_private(path, data):
    path.write_bytes(data)
    path.chmod(0o600)


def readonly_mount_args(mounts, container):
    if {mount.get('Target') for mount in mounts} != EXPECTED_MOUNT_TARGETS:
        raise PreflightError('Live certificate/ACME mount inventory changed')
    active_targets = {mount.get('Destination') for mount in container.get('Mounts', [])}
    if not EXPECTED_MOUNT_TARGETS.issubset(active_targets):
        raise PreflightError('Active container lacks a reviewed certificate/ACME mount')
    args = []
    for mount in mounts:
        source = mount.get('Source', '')
        if (mount.get('Type') != 'bind' or not Path(source).is_absolute()
                or any(char in source for char in ',\n\r')):
            raise PreflightError('Cannot replay an unreviewed edge mount')
        # Do not inherit Docker Config file mounts from the active container:
        # they would override the temporary candidate templates. nginx -t
        # only needs the four existing certificate/ACME binds, all read-only.
        args.extend(['--mount', f'type=bind,src={source},dst={mount["Target"]},readonly'])
    return args


def cleanup_candidate_container(name, marker):
    rows = docker('ps', '--all', '--filter', 'label=com.makepad.edge-preflight=' + marker,
                  '--format', '{{.ID}} {{.Names}}', timeout=15).decode().splitlines()
    if not rows:
        return  # --rm already removed it, or docker run never created it.
    if len(rows) != 1 or len(rows[0].split()) != 2 or rows[0].split()[1] != name:
        raise PreflightError('Candidate container identity mismatch; manual inspection required')
    docker('rm', '--force', rows[0].split()[0], timeout=15)


def syntax_preflight(candidate, active_id, container, image, mounts):
    if not Path('/dev/shm').is_dir():
        raise PreflightError('Candidate tmpfs /dev/shm is unavailable')
    with tempfile.TemporaryDirectory(prefix='edge-host-guard-preflight-', dir='/dev/shm') as temporary:
        root = Path(temporary)
        templates = root / 'templates'
        conf = root / 'conf.d'
        templates.mkdir(mode=0o700)
        conf.mkdir(mode=0o700)
        outputs = set()
        for target, raw in candidate.items():
            directory, filename = target_output(target)
            write_private((templates if directory == 'templates' else conf) / filename, raw)
            outputs.add(filename.removesuffix('.template'))
        env_lines = []
        for item in container['Config'].get('Env', []):
            if '\n' in item or '\r' in item or '=' not in item:
                raise PreflightError('Active edge environment cannot be safely replayed')
            env_lines.append(item)
        if not hasattr(os, 'memfd_create'):
            raise PreflightError('Memory-only environment replay requires Linux memfd')
        mount_args = readonly_mount_args(mounts, container)
        marker = uuid.uuid4().hex
        candidate_name = 'edge-host-guard-preflight-' + marker
        env_fd = os.memfd_create('edge-preflight-environment', flags=0)
        try:
            os.write(env_fd, ('\n'.join(env_lines) + '\n').encode())
            os.lseek(env_fd, 0, os.SEEK_SET)
            try:
                docker('run', '--rm', '--pull=never', '--name', candidate_name,
                       '--label', 'com.makepad.edge-preflight=' + marker,
                       '--memory', '256m', '--cpus', '0.5', '--pids-limit', '128',
                       '--network', 'container:' + active_id, *mount_args,
                       '--mount', f'type=bind,src={templates},dst=/etc/nginx/templates,readonly',
                       '--mount', f'type=bind,src={conf},dst=/etc/nginx/conf.d',
                       '--env-file', f'/proc/self/fd/{env_fd}', image, 'nginx', '-t',
                       timeout=120, pass_fds=(env_fd,))
            finally:
                cleanup_candidate_container(candidate_name, marker)
        finally:
            os.close(env_fd)
        if {path.name for path in conf.iterdir()} != outputs:
            raise PreflightError('Not every live candidate config was rendered')
        common_rendered = (conf / '00-common.conf').read_text()
        if ('map "$ssl_server_name:$host" $edge_sni_host_match {' not in common_rendered
                or common_rendered.count('ssl_reject_handshake on;') != 1):
            raise PreflightError('Rendered common host guard differs from reviewed plan')
        rendered_tls = 0
        default_rejects = 0
        for path in conf.iterdir():
            rendered = path.read_text()
            for opening, closing in server_blocks(rendered):
                body = rendered[opening + 1:closing]
                if not TLS_LISTEN.search(body):
                    continue
                rendered_tls += 1
                if 'ssl_reject_handshake on;' in body and 'default_server' in body:
                    default_rejects += 1
                elif body.count(GUARD) != 1:
                    raise PreflightError('Rendered TLS vhost lost its host guard')
        if rendered_tls != EXPECTED_TLS_VHOSTS + 1 or default_rejects != 1:
            raise PreflightError('Rendered TLS vhost/default inventory differs from plan')
        return rendered_tls


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-service-version', required=True, type=int)
    parser.add_argument('--expected-image', required=True)
    args = parser.parse_args()
    context = json.loads(docker('context', 'inspect'))[0]
    if (context['Endpoints']['docker']['Host'] != 'unix:///var/run/docker.sock'
            or os.environ.get('DOCKER_HOST', 'unix:///var/run/docker.sock') != 'unix:///var/run/docker.sock'):
        raise PreflightError('Run only on the app host with its local Docker socket')
    before = inspect_service()
    spec = before['Spec']
    if (before['Version']['Index'] != args.expected_service_version
            or spec['TaskTemplate']['ContainerSpec']['Image'] != args.expected_image
            or spec.get('Mode', {}).get('Replicated', {}).get('Replicas') != 1
            or spec.get('UpdateConfig', {}).get('FailureAction') != 'rollback'
            or before.get('UpdateStatus', {}).get('State', 'completed') != 'completed'):
        raise PreflightError('Reviewed edge version/image/topology is no longer current')
    networks = spec['TaskTemplate'].get('Networks', [])
    configs = read_live_configs(before)
    candidate, tls_files, tls_vhosts = build_candidate(configs, networks)
    active_id, container = active_container(before)
    mounts = spec['TaskTemplate']['ContainerSpec'].get('Mounts', [])
    rendered_tls = syntax_preflight(candidate, active_id, container, args.expected_image, mounts)
    after = inspect_service()
    if after['ID'] != before['ID'] or after['Version']['Index'] != before['Version']['Index'] or after['Spec'] != spec:
        raise PreflightError('Edge changed during preflight; discard this result')
    if active_container(after)[0] != active_id:
        raise PreflightError('Edge task changed during preflight; discard this result')
    plan = [(target, sha(configs[target]), sha(candidate[target])) for target in sorted(configs)]
    plan.append((DEFAULT_TARGET, None, sha(candidate[DEFAULT_TARGET])))
    print(json.dumps({
        'service': SERVICE,
        'service_version': before['Version']['Index'],
        'baseline_spec_sha256': canonical_sha(spec),
        'candidate_plan_sha256': canonical_sha(plan),
        'attached_configs_before': len(configs),
        'attached_configs_candidate': len(candidate),
        'networks_preserved': len(networks),
        'tls_config_files_guarded': tls_files,
        'tls_vhosts_guarded': tls_vhosts,
        'rendered_tls_vhosts_including_rejecting_default': rendered_tls,
        'nginx_syntax_preflight': 'passed',
        'service_mutated': False,
    }, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (PreflightError, KeyError, ValueError, UnicodeDecodeError) as exc:
        raise SystemExit(f'Preflight refused: {exc}') from None
