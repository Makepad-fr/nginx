#!/usr/bin/env python3
import copy
import importlib.util
from pathlib import Path
import unittest
spec = importlib.util.spec_from_file_location('ingress', Path(__file__).with_name('deploy-airloom-ingress.py'))
ingress = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ingress)
class AppliedRouteTests(unittest.TestCase):
    def setUp(self):
        self.target = '/etc/nginx/templates/airloom-prod.conf.template'
        self.after = {'UpdateStatus': {'State': 'completed'}, 'Spec': {'TaskTemplate': {
            'ContainerSpec': {'Configs': [{'File': {'Name': self.target}, 'ConfigID': 'new-route'}]},
            'Networks': [{'Target': 'airloom-network'}]}}}
    def verify(self):
        ingress.verify_applied(self.after, {self.target: 'new-route'}, {'airloom-network'})
    def test_completed_update_with_expected_resources(self): self.verify()
    def test_healthy_automatic_rollback_is_not_success(self):
        self.after['UpdateStatus']['State'] = 'rollback_completed'
        with self.assertRaises(RuntimeError): self.verify()
    def test_paused_update_is_not_success(self):
        self.after['UpdateStatus']['State'] = 'paused'
        with self.assertRaises(RuntimeError): self.verify()
    def test_old_or_missing_route_is_not_success(self):
        for configs in [[], [{'File': {'Name': self.target}, 'ConfigID': 'old-route'}]]:
            self.after['Spec']['TaskTemplate']['ContainerSpec']['Configs'] = configs
            with self.assertRaises(RuntimeError): self.verify()
    def test_wrong_mount_target_is_not_success(self):
        self.after['Spec']['TaskTemplate']['ContainerSpec']['Configs'][0]['File']['Name'] = '/wrong.conf'
        with self.assertRaises(RuntimeError): self.verify()
    def test_missing_application_network_is_not_success(self):
        self.after['Spec']['TaskTemplate']['Networks'] = [{'Target': 'unrelated-network'}]
        with self.assertRaises(RuntimeError): self.verify()
if __name__ == '__main__': unittest.main()
