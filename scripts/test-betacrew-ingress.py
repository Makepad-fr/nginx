#!/usr/bin/env python3
"""Exercise failure gates without touching Docker or the live shared ingress."""
import importlib.util
from pathlib import Path
import unittest
spec=importlib.util.spec_from_file_location('ingress',Path(__file__).with_name('deploy-betacrew-ingress.py'))
ingress=importlib.util.module_from_spec(spec)
spec.loader.exec_module(ingress)
class Acceptance(unittest.TestCase):
    def snapshot(self):
        return {'UpdateStatus':{'State':'completed'},'Spec':{'TaskTemplate':{'ContainerSpec':{'Configs':[{'File':{'Name':'/etc/nginx/templates/betacrew-prod.conf.template'},'ConfigID':'expected'}]},'Networks':[{'Target':'isolated'}]}}}
    def test_completed_target(self):
        ingress.verify_applied(self.snapshot(),{'/etc/nginx/templates/betacrew-prod.conf.template':'expected'},{'isolated'})
    def test_reject_wrong_config(self):
        with self.assertRaises(RuntimeError):ingress.verify_applied(self.snapshot(),{'/etc/nginx/templates/betacrew-prod.conf.template':'different'},{'isolated'})
    def test_reject_missing_network(self):
        with self.assertRaises(RuntimeError):ingress.verify_applied(self.snapshot(),{}, {'missing'})
    def test_reject_rolled_back_update(self):
        value=self.snapshot();value['UpdateStatus']['State']='rollback_completed'
        with self.assertRaises(RuntimeError):ingress.verify_applied(value,{}, {'isolated'})
    def test_reject_incomplete_states(self):
        for state in ('updating','paused','rollback_started','rollback_paused'):
            with self.subTest(state=state):
                value=self.snapshot();value['UpdateStatus']['State']=state
                with self.assertRaises(RuntimeError):ingress.verify_applied(value,{},set())
    def test_reject_missing_target(self):
        value=self.snapshot();value['Spec']['TaskTemplate']['ContainerSpec']['Configs']=[]
        with self.assertRaises(RuntimeError):
            ingress.verify_applied(value,{'/etc/nginx/templates/betacrew-prod.conf.template':'expected'},set())
    def test_template_preserves_nginx_variables(self):
        result=ingress.render_template('server_name ${BETACREW_PROD_SERVER_NAME}; proxy_set_header Host $host;', ['BETACREW_PROD_SERVER_NAME=betacrew.app'])
        self.assertIn('$host',result)
        self.assertNotIn('${BETACREW_',result)
    def test_template_rejects_drift_or_missing_value(self):
        for env in ([],['BETACREW_PROD_SERVER_NAME=other.example']):
            with self.assertRaises(RuntimeError):ingress.render_template('${BETACREW_PROD_SERVER_NAME}',env)
    def test_failed_validation_does_not_reload(self):
        from unittest.mock import patch
        with patch.object(ingress,'run',side_effect=RuntimeError('invalid')) as run:
            with self.assertRaises(RuntimeError):ingress.reload_certificate('fixture')
            self.assertEqual(run.call_count,1)
    def test_reload_never_redeploys_stack(self):
        from unittest.mock import patch
        with patch.object(ingress,'run') as run:
            ingress.reload_certificate('fixture')
            self.assertEqual([c.args for c in run.call_args_list],[('docker','exec','fixture','nginx','-t'),('docker','exec','fixture','nginx','-s','reload')])
if __name__=='__main__':unittest.main()

