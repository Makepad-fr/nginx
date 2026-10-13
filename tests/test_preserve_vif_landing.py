"""Exercise the actual shared-release renderer with a deployed landing network."""
import json
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import patch

class LandingReleaseTests(unittest.TestCase):
    def test_shared_release_retains_deployed_landing_route_and_network(self):
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/manual-deploy.yml').read_text()
        marker = "          targets = {'/etc/nginx/templates/visitaki-identity.conf.template'"
        target = workflow.index(marker)
        start = workflow.rfind('          import json', 0, target)
        end = workflow.index('          PY', target)
        code = textwrap.dedent(workflow[start:end])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = {'TaskTemplate': {'ContainerSpec': {'Configs': [{'ConfigName':'vif_staging_test','File':{'Name':'/etc/nginx/templates/vif-staging.conf.template','Mode':292}}]}, 'Networks':[{'Target':'landing-network','Aliases':['edge']} ]}}
            (root/'before.json').write_text(json.dumps(before))
            (root/'candidate.json').write_text(json.dumps({'services':{'nginx':{}}}))
            def inspect(args):
                self.assertEqual(args, ['docker','network','inspect','landing-network'])
                return json.dumps([{'Name':'makepad_vif_landing_staging_edge'}]).encode()
            with patch('sys.argv',['renderer',str(root/'before.json'),str(root/'candidate.json'),str(root/'result.json')]), patch('subprocess.check_output',side_effect=inspect):
                exec(compile(code, 'shared-release-renderer', 'exec'), {})
            result=json.loads((root/'result.json').read_text())
            self.assertEqual(result['networks']['makepad_vif_landing_staging_edge'], {'external':True,'name':'makepad_vif_landing_staging_edge'})
            self.assertEqual(result['services']['nginx']['networks']['makepad_vif_landing_staging_edge'], {'aliases':['edge']})
            self.assertEqual(result['services']['nginx']['configs'][0]['source'], 'vif_staging_test')
