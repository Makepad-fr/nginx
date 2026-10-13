import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('guard', ROOT/'scripts/brio_release_guard.py')
guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)

class ReleaseGuardTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.base=Path(self.tmp.name)
  self.g=self.base/'guard';self.g.touch();self.g.chmod(0o660)
  self.l=self.base/'lease'
  self.addCleanup(patch.stopall)
  patch.object(guard,'GUARD',self.g).start();patch.object(guard,'LEASE',self.l).start()
  patch.object(guard.grp,'getgrnam',return_value=types.SimpleNamespace(gr_gid=os.getgid())).start()
  # Test the actual file type/mode/no-follow checks with current-user fixtures.
  original=guard.open_checked
  patch.object(guard,'open_checked',side_effect=lambda path,uid,gid,mode:original(path,os.getuid(),os.getgid(),mode)).start()
 def write_lease(self,value):
  self.l.write_text(value);self.l.chmod(0o644)
 def test_absent_and_expired_lease_allow_operation(self):
  with guard.deployment_guard():pass
  self.write_lease('test|'+('a'*40)+'|1000000000\n')
  with guard.deployment_guard():pass
 def test_active_or_malformed_lease_rejected(self):
  for value in ['test|'+('a'*40)+'|'+str(int(time.time())+300)+'\n','invalid\n','test|'+('a'*40)+'|1000000000|extra\n']:
   self.write_lease(value)
   with self.assertRaises(RuntimeError):
    with guard.deployment_guard():self.fail('entered guarded mutation')
 def test_missing_guard_does_not_create_authority(self):
  self.g.unlink()
  with self.assertRaises(FileNotFoundError):
   with guard.deployment_guard():self.fail('unguarded operation')
  self.assertFalse(self.g.exists())
 def test_unsafe_modes_and_symlinks_rejected(self):
  self.g.chmod(0o666)
  with self.assertRaises(RuntimeError):
   with guard.deployment_guard():pass
  self.g.chmod(0o660);self.l.symlink_to(self.g)
  with self.assertRaises(OSError):
   with guard.deployment_guard():pass
 def test_lock_contends_and_is_released_after_failure(self):
  with guard.deployment_guard():
   code='import fcntl,sys; f=open(sys.argv[1]); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)'
   p=subprocess.run([sys.executable,'-c',code,str(self.g)],capture_output=True)
   self.assertNotEqual(p.returncode,0)
  try:
   with guard.deployment_guard():raise ValueError('deployment failed')
  except ValueError:pass
  with guard.deployment_guard():pass

class GuardWiringTests(unittest.TestCase):
 def test_scoped_deployment_guards_before_inspection(self):
  source=(ROOT/'scripts/deploy-brio-ingress.py').read_text()
  self.assertIn('from brio_release_guard import deployment_guard',source)
  self.assertLess(source.index('with deployment_guard()'),source.index('before = inspect()'))
  workflow=(ROOT/'.github/workflows/deploy-brio-staging.yml').read_text()
  self.assertIn('scripts/deploy-brio-ingress.py scripts/brio_release_guard.py',workflow)
 def test_shared_deployment_wraps_entire_remote_mutation(self):
  workflow=(ROOT/'.github/workflows/manual-deploy.yml').read_text()
  self.assertIn('scripts/brio_release_guard.py "${remote_target}:${guard_script}"',workflow)
  self.assertIn('python3 "${guard_script}" bash -se --',workflow)
  self.assertLess(workflow.index('python3 "${guard_script}" bash -se --'),workflow.index('docker stack deploy'))

if __name__=='__main__':unittest.main()
