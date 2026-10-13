#!/usr/bin/env python3
"""Exercise Posey route preservation with real Nginx and isolated stub backends."""
import json, os, pathlib, ssl, subprocess, tempfile, time, urllib.request, urllib.error
root = pathlib.Path(__file__).resolve().parents[1]
image = 'nginx:1.30-alpine3.24@sha256:97d490c12ba55b4946b01546d1c3ed324e8d41ab1c9fcb2a616aa470620e5b46'
name = f'posey-ingress-test-{os.getpid()}-{time.time_ns()}'
def run(*args):
 return subprocess.check_output(args, stderr=subprocess.STDOUT, text=True)
endpoint = json.loads(run('docker', 'context', 'inspect'))[0]['Endpoints']['docker']['Host']
assert endpoint.startswith('unix://') and not os.environ.get('DOCKER_HOST', '').startswith(('ssh:', 'tcp:')), 'Use a local fixture daemon'
with tempfile.TemporaryDirectory(prefix='posey-routing-') as raw:
 d = pathlib.Path(raw); conf = d/'conf'; conf.mkdir(); cert = d/'cert'; cert.mkdir()
 run('openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1','-keyout',str(cert/'key.pem'),'-out',str(cert/'cert.pem'))
 source = (root/'sites/posey-prod.conf.template').read_text()
 for old,new in [('http://posey-prod_web:8080','http://127.0.0.1:8081'),('http://posey-community_api:8080','http://127.0.0.1:8082'),('/etc/letsencrypt/live/posey.makepad.fr/fullchain.pem','/cert/cert.pem'),('/etc/letsencrypt/live/posey.makepad.fr/privkey.pem','/cert/key.pem')]: source = source.replace(old,new)
 (conf/'posey.conf').write_text(source)
 (conf/'fixtures.conf').write_text('''server { listen 8081; location = / { return 200 "landing fixture"; } location / { return 404; } }
server { listen 8082; client_max_body_size 20m; location / { if ($http_authorization != "Bearer synthetic-fixture") { return 401; } return 200 "community fixture"; } }
''')
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
  assert request('/')[2]==b'landing fixture'
  code,headers,body=request('/.well-known/apple-app-site-association')
  assert code==200 and headers['Content-Type']=='application/json'
  assert json.loads(body)=={'applinks':{'apps':[],'details':[{'appID':'533428ZQTT.fr.makepad.posey','paths':['/pose/*']}]}}
  assert request('/api/community/poses')[0]==401
  auth={'Authorization':'Bearer synthetic-fixture'}
  assert request('/api/community/poses',headers=auth)[2]==b'community fixture'
  assert request('/api/community/upload',b'x'*(2<<20),auth)[0]==200
  assert request('/api/community/upload',b'x'*(7<<20),auth)[0]==413
  assert request('/unrelated',b'x'*(2<<20),auth)[0]==413
  assert request('/api/community-extra',headers=auth)[0]==404
  assert request('/.well-known/apple-app-site-association/extra')[0]==404
  print('Posey real-Nginx routing passed: landing, exact AASA, community forwarding/auth rejection, scoped body limits and lookalike-path rejection')
 finally:
  subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
