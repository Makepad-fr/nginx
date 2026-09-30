import importlib.util
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
spec = importlib.util.spec_from_file_location('vif_ingress', Path(__file__).resolve().parents[1] / 'scripts/deploy-brio-ingress.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


class VifIngressTests(unittest.TestCase):
    def test_vif_selection_never_replaces_legacy_or_production_routes(self):
        names, networks = m.selection(True, {'MAKEPAD_PROXY_VIF_PLATFORM_STAGING_APP_NETWORK':'makepad_vif_platform_staging_edge'})
        self.assertEqual(names, ('vif-staging.conf.template',))
        self.assertEqual(networks, ['makepad_brio_staging_app','makepad_vif_platform_staging_edge'])
        with self.assertRaises(RuntimeError): m.selection(True, {})
        with self.assertRaises(RuntimeError): m.selection(True, {'MAKEPAD_PROXY_VIF_PLATFORM_STAGING_APP_NETWORK':'makepad_vif_prod_app'})
    def test_existing_brio_selection_is_unchanged(self):
        names, networks = m.selection(False, {})
        self.assertEqual(names, ('brio-staging.conf.template','maildev-brio-staging.conf.template'))
        self.assertNotIn('makepad_vif_platform_staging_edge', networks)
    def test_rollback_only_reverts_owned_update(self):
        before={'Spec':{'image':'original'}}
        self.assertFalse(m.rollback_owned(before, before))
        self.assertTrue(m.rollback_owned(before, {'Spec':{'image':'candidate'},'PreviousSpec':before['Spec']}))
        with self.assertRaises(RuntimeError): m.rollback_owned(before, {'Spec':{'image':'other'},'PreviousSpec':{'image':'candidate'}})


if __name__ == '__main__': unittest.main()
