import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch, Mock

spec = importlib.util.spec_from_file_location('upgrade', Path(__file__).parents[1]/'scripts/upgrade-shared-image.py')
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)


class ImageUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.before = {'ID':'abc123','Version':{'Index':17},'UpdateStatus':{'State':'completed'},
                       'Spec':{'Name':u.SERVICE,'Mode':{'Replicated':{'Replicas':1}},'UpdateConfig':{'FailureAction':'rollback'},
                               'TaskTemplate':{'ContainerSpec':{'Image':'old','Env':['PRIVATE=value'],
                                   'Configs':[{'ConfigID':'other-app'}], 'Mounts':[{'Target':'/certs'}]},
                                   'Networks':[{'Target':'isolated-app'}]}}}

    def test_image_only_and_original_not_modified(self):
        original = copy.deepcopy(self.before)
        candidate = u.candidate_spec(self.before,'old','new')
        self.assertEqual(self.before,original)
        candidate['TaskTemplate']['ContainerSpec']['Image']='old'
        self.assertEqual(candidate,self.before['Spec'])

    def test_prior_image_drift_rejected(self):
        with self.assertRaises(RuntimeError):u.candidate_spec(self.before,'different','new')

    def test_unresolved_update_rejected(self):
        for state in ['updating','paused','rollback_started','rollback_completed']:
            self.before['UpdateStatus']['State']=state
            with self.assertRaises(RuntimeError):u.candidate_spec(self.before,'old','new')

    def test_rollback_required(self):
        self.before['Spec']['UpdateConfig']['FailureAction']='pause'
        with self.assertRaises(RuntimeError):u.candidate_spec(self.before,'old','new')

    def test_unreviewed_topology_rejected(self):
        self.before['Spec']['Mode']['Replicated']['Replicas']=2
        with self.assertRaises(RuntimeError):u.candidate_spec(self.before,'old','new')

    def test_unrelated_config_removal_rejected(self):
        after=copy.deepcopy(self.before)
        after['Spec']['TaskTemplate']['ContainerSpec']['Configs']=[]
        with self.assertRaises(RuntimeError):u.verify_spec(after,self.before['Spec'])

    def test_rollback_never_counts_as_success(self):
        self.before['UpdateStatus']['State']='rollback_completed'
        with self.assertRaises(RuntimeError):u.verify_spec(self.before,self.before['Spec'])

    def test_only_immutable_image_accepted(self):
        digest='a'*64
        self.assertEqual(u.image_from_compose('    image: nginx:1.30@sha256:'+digest),'nginx:1.30@sha256:'+digest)
        for value in ['    image: nginx:latest','    image: attacker/nginx:1@sha256:'+digest]:
            with self.assertRaises(RuntimeError):u.image_from_compose(value)

    @patch.object(u,'run',return_value='1.53')
    @patch.object(u,'LocalDocker')
    def test_atomic_version_and_daemon_rejection(self,connection,_):
        client=connection.return_value
        client.getresponse.return_value.status=409
        with self.assertRaisesRegex(RuntimeError,'409'):u.update(self.before,self.before['Spec'])
        self.assertIn('version=17',client.request.call_args.args[1])
        client.close.assert_called_once()


if __name__=='__main__':unittest.main()
