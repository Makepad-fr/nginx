#!/usr/bin/env python3
"""Exercise monitoring routes with real TLS/Nginx and isolated loopback upstreams."""
from pathlib import Path
import re
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
IMAGE = re.search(r'nginx:[^\s}]+@sha256:[a-f0-9]{64}', (ROOT / 'compose.yml').read_text())[0]

def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=120, **kwargs).stdout

with tempfile.TemporaryDirectory(prefix='monitoring-routing-') as directory:
    root = Path(directory)
    certs = root / 'letsencrypt/live'
    for host in ('sentry.makepad.fr', 'consent.makepad.fr'):
        target = certs / host
        target.mkdir(parents=True)
        run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=' + host,
            '-keyout', str(target / 'privkey.pem'), '-out', str(target / 'fullchain.pem'))
    common = (ROOT / 'sites/00-common.conf.template').read_text()
    stub = '''server { listen 8080; access_log off;
        location = /unavailable { return 503; }
        location / { return 200 "$http_host|$http_x_forwarded_proto|$http_x_forwarded_for|$http_forwarded"; }
    }
    server { listen 8081; access_log off; location / { return 200 "consent fixture"; } }'''
    sentry = (ROOT / 'sites/sentry-prod.conf.template').read_text().replace('http://makepad-sentry-ingress:80', 'http://127.0.0.1:8080')
    consent = (ROOT / 'sites/consent-lens-prod.conf.template').read_text().replace('http://consent-lens-web:3000', 'http://127.0.0.1:8081')
    config = root / 'nginx.conf'
    config.write_text('events {}\nhttp {\n' + common + '\n' + stub + '\n' + sentry + '\n' + consent + '\n}\n')
    container = run('docker', 'create', '--network', 'none', '--cpus', '0.5', '--memory', '128m',
        '--add-host', 'sentry.makepad.fr:127.0.0.1', '--add-host', 'consent.makepad.fr:127.0.0.1',
        '--entrypoint', 'nginx', IMAGE,
        '-c', '/tmp/nginx.conf', '-g', 'daemon off;').strip()
    try:
        run('docker', 'cp', str(root / 'letsencrypt'), container + ':/etc/letsencrypt')
        run('docker', 'cp', str(config), container + ':/tmp/nginx.conf')
        run('docker', 'start', container)
        def request(host, path='/', tls=True):
            return subprocess.run(['docker', 'exec', container, 'wget', '-T', '5', '-S', '-O', '-',
                '--no-check-certificate', '--header', 'Host: ' + host,
                '--header', 'X-Forwarded-For: 203.0.113.99', '--header', 'Forwarded: for=spoofed',
                '--header', 'Authorization: Bearer synthetic-private-probe', '--header', 'Cookie: probe=synthetic-private-probe',
                ('https' if tls else 'http') + '://' + host + path], capture_output=True, text=True, timeout=10)
        for _ in range(30):
            response = request('sentry.makepad.fr', '/?token=synthetic-private-probe')
            if '200 OK' in response.stderr: break
            time.sleep(.1)
        else: raise AssertionError('Sentry fixture did not become ready')
        assert response.stdout == 'sentry.makepad.fr|https|127.0.0.1|', response.stdout
        assert '503 Service' in request('sentry.makepad.fr', '/unavailable').stderr
        assert request('consent.makepad.fr').stdout == 'consent fixture'
        results = [request('consent.makepad.fr', '/api/scan').stderr for _ in range(6)]
        assert any('200 OK' in value for value in results)
        assert any('429 Too Many Requests' in value for value in results)
        logs = run('docker', 'logs', container)
        assert 'synthetic-private-probe' not in logs
        assert '"request_id"' in logs, 'Expected metadata access logging'
        print('PASS: TLS, Sentry host/forwarded-header boundaries, upstream failure, Consent Lens rate limiting, and credential/query log redaction')
    finally:
        subprocess.run(['docker', 'rm', '-f', container], capture_output=True, timeout=30)
