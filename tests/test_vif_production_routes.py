from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1]
class Routes(unittest.TestCase):
 def test_production_has_explicit_namespaces_and_no_catchall_proxy(self):
  s=(ROOT/'sites/vif-production.conf.template').read_text()
  self.assertIn('location / { return 404; }',s)
  for path in ('/walking-club','/platform','/assets/vif/'):
   self.assertIn(path,s)
  self.assertNotIn('noindex',s)
  self.assertIn('if ($host != vif.io) { return 421; }',s)
 def test_shared_origin_and_credential_boundaries(self):
  s=(ROOT/'sites/vif-production.conf.template').read_text()
  self.assertIn('proxy_set_header X-Forwarded-For "";',s)
  self.assertIn('proxy_set_header Forwarded "";',s)
  self.assertIn('proxy_set_header Cookie "";',s)
  self.assertIn('proxy_set_header Authorization "";',s)
  self.assertNotIn('rewrite ',s)
  for name in ('vif-walking-club-production-app','vif-platform-production-app','vif-landing-production-app'):
   self.assertIn(name,s)
if __name__=='__main__':unittest.main()
