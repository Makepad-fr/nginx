import base64,fcntl,hashlib,json,os,pathlib,subprocess,tempfile
os.umask(0o077)
service='makepad-edge_nginx'
pathlib.Path('/var/lib/makepad/pluck-deploy').mkdir(parents=True, exist_ok=True)
def run(*args,**kw): return subprocess.check_output(args,**kw).decode()
def inspect(): return json.loads(run('docker','service','inspect',service))[0]
with open('/var/lock/makepad-pluck-ingress.lock','a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX)
 before=inspect(); spec=before['Spec']['TaskTemplate']['ContainerSpec']
 pathlib.Path('/var/lib/makepad/pluck-deploy/edge-before.json').write_text(json.dumps(before))
 current=run('docker','ps','-q','--filter','label=com.docker.swarm.service.name='+service).split(); assert len(current)==1
 content=(pathlib.Path(__file__).resolve().parents[1]/'sites/pluck.conf.template').read_text()
 network=json.loads(run('docker','network','inspect','makepad_scan_app'))[0]
 assert network['Driver']=='overlay'
 previous_configs={c['ConfigID'] for c in spec['Configs']}; previous_networks={n['Target'] for n in before['Spec']['TaskTemplate']['Networks']}
 assert not any(c['File']['Name'].endswith('/pluck.conf.template') for c in spec['Configs']), 'Pluck route already present; inspect before updating'
 with tempfile.TemporaryDirectory(prefix='scan-ingress-') as tmp:
  candidate=pathlib.Path(tmp)/'conf';candidate.mkdir()
  run('docker','cp',current[0]+':/etc/nginx/conf.d/.',str(candidate))
  (candidate/'pluck.conf').write_text(content)
  print(run('docker','run','--rm','--network','container:'+current[0],'--volumes-from',current[0]+':ro','--mount','type=bind,src='+str(candidate)+',dst=/etc/nginx/conf.d,readonly','--entrypoint','nginx',spec['Image'],'-t',stderr=subprocess.STDOUT))
  assert inspect()['Version']['Index']==before['Version']['Index'],'Edge changed during validation'
  name='scanner_pluck_conf_'+hashlib.sha256(content.encode()).hexdigest()[:16]
  exists=subprocess.run(['docker','config','inspect',name],capture_output=True)
  if exists.returncode: run('docker','config','create','--label','com.makepad.owner=Makepad-fr/nginx',name,'-',input=content.encode())
  changes=['--config-add','source='+name+',target=/etc/nginx/templates/pluck.conf.template,mode=0444']
  if network['Id'] not in previous_networks: changes+=['--network-add','makepad_scan_app']
  try:
   run('docker','service','update','--detach=false',*changes,service,stderr=subprocess.STDOUT)
   after=inspect();ac=after['Spec']['TaskTemplate']['ContainerSpec']
   assert previous_configs <= {c['ConfigID'] for c in ac['Configs']}
   assert previous_networks <= {n['Target'] for n in after['Spec']['TaskTemplate']['Networks']}
   for key in ['Image','Env','Mounts','Command','Args']: assert ac.get(key)==spec.get(key),key
   active=run('docker','ps','-q','--filter','label=com.docker.swarm.service.name='+service).split();assert len(active)==1
   print(run('docker','exec',active[0],'nginx','-t',stderr=subprocess.STDOUT))
   print('Pluck route added; existing configs, networks, mounts, image and environment preserved.')
  except BaseException:
   if inspect()['Version']['Index'] != before['Version']['Index']: print(run('docker','service','rollback','--detach=false',service,stderr=subprocess.STDOUT))
   raise
