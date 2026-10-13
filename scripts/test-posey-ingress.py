#!/usr/bin/env python3
"""Exercise failure gates without touching Docker or the live shared ingress."""
import importlib.util
from pathlib import Path
import unittest
spec=importlib.util.spec_from_file_location('ingress',Path(__file__).with_name('deploy-posey-ingress.py'))
ingress=importlib.util.module_from_spec(spec)
spec.loader.exec_module(ingress)
class Acceptance(unittest.TestCase):
    def snapshot(self):
        return {'UpdateStatus':{'State':'completed'},'Spec':{'TaskTemplate':{'ContainerSpec':{'Configs':[{'File':{'Name':'/etc/nginx/templates/posey-prod.conf.template'},'ConfigID':'expected'}]},'Networks':[{'Target':'isolated'}]}}}
    def test_completed_target(self):
        ingress.verify_applied(self.snapshot(),{'/etc/nginx/templates/posey-prod.conf.template':'expected'},{'isolated'})
    def test_reject_wrong_config(self):
        with self.assertRaises(RuntimeError):ingress.verify_applied(self.snapshot(),{'/etc/nginx/templates/posey-prod.conf.template':'different'},{'isolated'})
    def test_reject_missing_network(self):
        with self.assertRaises(RuntimeError):ingress.verify_applied(self.snapshot(),{}, {'missing'})
    def test_reject_rolled_back_update(self):
        value=self.snapshot();value['UpdateStatus']['State']='rollback_completed'
        with self.assertRaises(RuntimeError):ingress.verify_applied(value,{}, {'isolated'})
if __name__=='__main__':unittest.main()
