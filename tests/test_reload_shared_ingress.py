import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ReloadSharedIngress(unittest.TestCase):
    def run_reload(self, containers, syntax_ok=True):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            log = directory / 'calls.jsonl'
            # Execute only the guard's child command; lock semantics have their
            # own suite. Assert that the production script still invokes it.
            interpreter = directory / 'python3'
            interpreter.write_text('#!' + sys.executable + '\nimport os,sys\nassert sys.argv[1].endswith("/brio_release_guard.py")\nos.execvp(sys.argv[2], sys.argv[2:])\n')
            interpreter.chmod(0o700)
            docker = directory / 'docker'
            docker.write_text('#!' + sys.executable + '\nimport json,os,sys\nwith open(os.environ["CALL_LOG"],"a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\nif sys.argv[1] == "ps": print(os.environ["CONTAINERS"])\nelif sys.argv[1] == "exec": sys.exit(int(os.environ["SYNTAX_EXIT"]))\nelif sys.argv[1:4] != ["kill","-s","HUP"]: sys.exit(99)\n')
            docker.chmod(0o700)
            environment = dict(os.environ, PATH=tmp + os.pathsep + os.environ['PATH'],
                               CALL_LOG=str(log), CONTAINERS=containers,
                               SYNTAX_EXIT='0' if syntax_ok else '1')
            result = subprocess.run(['bash', str(ROOT / 'scripts/reload-shared-ingress.sh')],
                                    env=environment, text=True, capture_output=True)
            calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            return result, calls

    def test_one_healthy_configuration_reloads(self):
        result, calls = self.run_reload('fixture-container')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[-2:], [['exec', 'fixture-container', 'nginx', '-t'],
                                      ['kill', '-s', 'HUP', 'fixture-container']])

    def test_missing_or_ambiguous_container_never_reloads(self):
        for containers in ('', 'first\nsecond'):
            with self.subTest(containers=containers):
                result, calls = self.run_reload(containers)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(call[0] == 'kill' for call in calls))

    def test_invalid_configuration_never_reloads(self):
        result, calls = self.run_reload('fixture-container', syntax_ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call[0] == 'kill' for call in calls))


if __name__ == '__main__':
    unittest.main()
