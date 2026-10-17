#!/usr/bin/env python3
"""Exercise BetaCrew routing with real Nginx and isolated stub backends."""
import json, os, pathlib, ssl, subprocess, tempfile, time
from tls_loopback import request as tls_request
root = pathlib.Path(__file__).resolve().parents[1]
image = 'nginx:1.30-alpine3.24@sha256:97d490c12ba55b4946b01546d1c3ed324e8d41ab1c9fcb2a616aa470620e5b46'
name = f'betacrew-ingress-test-{os.getpid()}-{time.time_ns()}'
def run(*args):
 return subprocess.check_output(args, stderr=subprocess.STDOUT, text=True)
endpoint = json.loads(run('docker', 'context', 'inspect'))[0]['Endpoints']['docker']['Host']
assert endpoint.startswith('unix://') and not os.environ.get('DOCKER_HOST', '').startswith(('ssh:', 'tcp:')), 'Use a local fixture daemon'
with tempfile.TemporaryDirectory(prefix='betacrew-routing-') as raw:
 d = pathlib.Path(raw); conf = d/'conf'; conf.mkdir(); cert = d/'cert'; cert.mkdir()
 run('openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=betacrew.app','-addext','subjectAltName=DNS:betacrew.app,DNS:www.betacrew.app','-keyout',str(cert/'key.pem'),'-out',str(cert/'cert.pem'))
 source = (root/'sites/betacrew-prod.conf.template').read_text()
 env=dict(line.split('=',1) for line in (root/'envs/production/.env.betacrew').read_text().splitlines() if '=' in line)
 for key,value in env.items():source=source.replace('${'+key+'}',value)
 for old,new in [('http://betacrew-prod-app:8080','http://127.0.0.1:8081'),('/etc/letsencrypt-betacrew/live/betacrew.app/fullchain.pem','/cert/cert.pem'),('/etc/letsencrypt-betacrew/live/betacrew.app/privkey.pem','/cert/key.pem')]: source=source.replace(old,new)
 (conf/'betacrew.conf').write_text(source)
 (conf/'00-common.conf').write_text((root/'sites/00-common.conf.template').read_text())
 (conf/'fixtures.conf').write_text('server { listen 8081; client_max_body_size 10m; location = / { return 200 "landing fixture"; } location = /upload { return 200 "upload fixture"; } location / { return 404; } }')
 try:
  run('docker','run','-d','--name',name,'-p','127.0.0.1::443','-v',str(conf)+':/etc/nginx/conf.d:ro','-v',str(cert)+':/cert:ro',image)
  run('docker','exec',name,'nginx','-t')
  port=run('docker','port',name,'443/tcp').strip().split(':')[-1]
  context=ssl.create_default_context(cafile=str(cert/'cert.pem'))
  def request(path,data=None,headers=None,host='betacrew.app'):
   return tls_request(host,int(port),context,path,data,headers)
  for _ in range(50):
   try:
    if request('/')[0]==200:break
   except OSError:pass
   time.sleep(.1)
  code,headers,actual=request('/')
  assert code==200 and actual==b'landing fixture'
  assert headers['X-Content-Type-Options']=='nosniff' and headers['X-Frame-Options']=='DENY'
  assert headers['Referrer-Policy']=='strict-origin-when-cross-origin'
  assert 'max-age=31536000' in headers['Strict-Transport-Security']
  assert request('/unknown')[0]==404
  assert request('/upload',b'x'*(5<<20))[0]==200
  assert request('/upload',b'x'*(7<<20))[0]==413
  code,headers,_=request('/path?x=1',host='www.betacrew.app')
  assert code==301 and headers['Location']=='https://betacrew.app/path?x=1'
  print('BetaCrew real-Nginx routing passed: backend, security headers,404,accepted5MiB/rejected7MiB uploads,canonical redirect')
 finally:
  subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
