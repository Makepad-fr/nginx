#!/usr/bin/env python3
"""Exercise Pocket Gremlin routing with real Nginx and isolated stub backends."""
import json, os, pathlib, ssl, subprocess, tempfile, time, urllib.request, urllib.error
root = pathlib.Path(__file__).resolve().parents[1]
image = 'nginx:1.30-alpine3.24@sha256:97d490c12ba55b4946b01546d1c3ed324e8d41ab1c9fcb2a616aa470620e5b46'
name = f'pocket-gremlin-ingress-test-{os.getpid()}-{time.time_ns()}'
def run(*args):
 return subprocess.check_output(args, stderr=subprocess.STDOUT, text=True)
endpoint = json.loads(run('docker', 'context', 'inspect'))[0]['Endpoints']['docker']['Host']
assert endpoint.startswith('unix://') and not os.environ.get('DOCKER_HOST', '').startswith(('ssh:', 'tcp:')), 'Use a local fixture daemon'
with tempfile.TemporaryDirectory(prefix='pocket-gremlin-routing-') as raw:
 d = pathlib.Path(raw); conf = d/'conf'; conf.mkdir(); cert = d/'cert'; cert.mkdir()
 run('openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1','-keyout',str(cert/'key.pem'),'-out',str(cert/'cert.pem'))
 source = (root/'sites/pocket-gremlin.conf.template').read_text()
 for old,new in [('http://makepad-landing-prod-app:8080','http://127.0.0.1:8081'),('/etc/letsencrypt/live/pocketgremlin.makepad.fr/fullchain.pem','/cert/cert.pem'),('/etc/letsencrypt/live/pocketgremlin.makepad.fr/privkey.pem','/cert/key.pem')]: source = source.replace(old,new)
 (conf/'pocket-gremlin.conf').write_text(source)
 (conf/'fixtures.conf').write_text('server { listen 8081; location = /pocket-gremlin/ { return 200 "landing fixture"; } location = /pocket-gremlin/privacy/ { return 200 "privacy fixture"; } location = /pocket-gremlin/support/ { return 200 "support fixture"; } location = /pocket-gremlin/assets/site.css { default_type text/css; return 200 "body{}"; } location / { return 404; } }')
 try:
  run('docker','run','-d','--name',name,'-p','127.0.0.1::443','-v',str(conf)+':/etc/nginx/conf.d:ro','-v',str(cert)+':/cert:ro',image)
  run('docker','exec',name,'nginx','-t')
  port=run('docker','port',name,'443/tcp').strip().split(':')[-1]
  context=ssl.create_default_context(cafile=str(cert/'cert.pem'))
  def request(path,data=None,headers=None):
   req=urllib.request.Request(f'https://127.0.0.1:{port}'+path,data=data,headers=headers or {})
   try: res=urllib.request.urlopen(req,context=context,timeout=15)
   except urllib.error.HTTPError as e: res=e
   with res:return res.status,res.headers,res.read()
  for _ in range(50):
   try:
    if request('/')[0]==200:break
   except (OSError,urllib.error.URLError):pass
   time.sleep(.1)
  for path,body in [('/',b'landing fixture'),('/privacy/',b'privacy fixture'),('/support/',b'support fixture'),('/assets/site.css',b'body{}')]:
   code,headers,actual=request(path)
   assert code==200 and actual==body,(path,code)
   assert headers['X-Content-Type-Options']=='nosniff' and headers['Referrer-Policy']=='no-referrer'
  assert request('/unknown')[0]==404
  assert request('/unrelated',b'x'*(2<<20))[0]==413
  print('Pocket Gremlin real-Nginx routing passed: landing, privacy, support, relative CSS, security headers, unknown-route and oversized-body rejection')
 finally:
  subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
