#!/usr/bin/env python3
"""Exercise scoped upload sizes and storage routing against real Nginx."""
import pathlib,subprocess,tempfile,time,os,json,urllib.request,urllib.error,ssl
root=pathlib.Path(__file__).resolve().parent.parent
image='nginx:1.30-alpine3.24@sha256:97d490c12ba55b4946b01546d1c3ed324e8d41ab1c9fcb2a616aa470620e5b46'
name=f'brio-photo-ingress-test-{os.getpid()}-{time.time_ns()}'
def run(*args):
 try: return subprocess.check_output(args,stderr=subprocess.STDOUT,text=True)
 except subprocess.CalledProcessError as e:
  print(e.output,flush=True)
  raise
with tempfile.TemporaryDirectory(prefix='brio-photo-ingress-') as raw:
 d=pathlib.Path(raw);conf=d/'conf';conf.mkdir();cert=d/'cert';cert.mkdir()
 run('openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=127.0.0.1','-addext','subjectAltName=IP:127.0.0.1','-keyout',str(cert/'key.pem'),'-out',str(cert/'cert.pem'))
 source=(root/'sites/brio-staging.conf.template').read_text()
 assert source.count('proxy_pass http://10.80.0.2:9000;')==1
 replacements={'BRIO_STAGING_SERVER_NAME':'127.0.0.1','BRIO_STAGING_UPSTREAM':'http://127.0.0.1:8081','BRIO_STAGING_TLS_CERT_FILE':'/cert/cert.pem','BRIO_STAGING_TLS_KEY_FILE':'/cert/key.pem','CATWLK_ACME_WEBROOT':'/tmp'}
 for k,v in replacements.items():source=source.replace('${'+k+'}',v)
 source=source.replace('proxy_pass http://10.80.0.2:9000;','proxy_pass http://127.0.0.1:8082;')
 (conf/'brio.conf').write_text(source)
 (conf/'00-common.conf').write_text((root/'sites/00-common.conf.template').read_text())
 (conf/'fixtures.conf').write_text('''server { listen 8081; client_max_body_size 20m; location / { return 204; } }
server { listen 8082; client_max_body_size 20m; add_header X-Fixture-Host $host always; add_header X-Fixture-Cookie $http_cookie always; location / { if ($http_authorization != "fixture-signature") { return 403; } return 200 "private fixture"; } }
''')
 try:
  run('docker','run','-d','--name',name,'-p','127.0.0.1::443','-v',str(conf)+':/etc/nginx/conf.d:ro','-v',str(cert)+':/cert:ro',image)
  run('docker','exec',name,'nginx','-t')
  port=run('docker','port',name,'443/tcp').strip().split(':')[-1]
  context=ssl.create_default_context(cafile=str(cert/'cert.pem'))
  def request(path,method='GET',data=None,headers=None):
   r=urllib.request.Request(f'https://127.0.0.1:{port}'+path,data=data,method=method,headers=headers or {})
   try: res=urllib.request.urlopen(r,context=context,timeout=15)
   except urllib.error.HTTPError as e: res=e
   with res: return res.status,res.headers,res.read()
  for _ in range(50):
   try:
    if request('/readyz')[0]==204:break
   except (OSError,urllib.error.URLError):pass
   time.sleep(.1)
  medium=b'x'*(2<<20)
  for path in ['/admin/events','/admin/events/11111111-1111-4111-8111-111111111111','/admin/photo-library']:
   assert request(path,'POST',medium)[0]==204, 'allowed upload blocked'
  assert request('/unrelated','POST',medium)[0]==413,'global limit widened'
  for path in ['/admin/events', '/admin/photo-library']:
   assert request(path,'POST',b'x'*(12<<20))[0]==413,'oversized upload accepted'
  for path in ['/admin/photo-library/anything', '/admin/photo-library-extra']:
   assert request(path,'POST',medium)[0]==413,'library limit escaped its exact route'
  key='/brio-staging-event-photos/brio/'+'a'*64+'.jpg'
  assert request(key)[0]==403,'unsigned object request bypassed storage authentication'
  status,headers,body=request(key,headers={'Authorization':'fixture-signature','Cookie':'must-not-reach-storage=1'})
  assert status==200 and body==b'private fixture'
  assert headers.get('X-Fixture-Host')=='127.0.0.1' and not headers.get('X-Fixture-Cookie'),'signature host or cookie isolation broken'
  assert request(key,'PUT',medium,{'Authorization':'fixture-signature'})[0]==200
  assert request(key,'DELETE',headers={'Authorization':'fixture-signature'})[0]==200
  assert request(key,'POST',headers={'Authorization':'fixture-signature'})[0]==403
  for path in ['/brio-staging-event-photos/','/brio-staging-event-photos/other/'+'a'*64+'.jpg',key.replace('.jpg','Xjpg')]:
   assert request(path,headers={'Authorization':'fixture-signature'})[0]==404,'unintended storage path exposed'
  print('Brio HTTPS photo ingress: scoped upload limits, methods, exact prefix, Host and cookie isolation passed.')
 except Exception:
  print(run('docker','logs',name),flush=True)
  raise
 finally:
  subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)
