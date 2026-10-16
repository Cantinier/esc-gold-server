import os
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipIf(os.name == 'nt' and not os.environ.get('BASH_FOR_TEST'), 'Requires Bash')
class ConfigureTests(unittest.TestCase):
    def test_defaults_custom_validation_preservation_and_interrupted_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            shutil.copyfile(Path(__file__).resolve().parents[1] / 'scripts/configure.sh', root / 'scripts/configure.sh')

            def run(answers):
                return subprocess.run([os.environ.get('BASH_FOR_TEST', 'bash'), 'scripts/configure.sh'],
                                      cwd=root, input=answers.encode('utf-8'), capture_output=True)

            self.assertEqual(run('\n\n\n\n').returncode, 0)
            self.assertIn('BIND_ADDRESS=127.0.0.1\nADMIN_PORT=4113\nWS_PORT=4114', (root / '.env').read_text())
            with (root / '.env').open('a') as stream:
                stream.write('COMPOSE_PROJECT_NAME=test-project\n')
            self.assertEqual(run('999.1.1.1\n0.0.0.0\n0\n65536\n04114\n4114\n0\n4115\n').returncode, 0)
            saved = (root / '.env').read_text()
            self.assertIn('ADMIN_PORT=4114', saved)
            self.assertIn('WS_PORT=4115', saved)
            self.assertIn('COMPOSE_PROJECT_NAME=test-project', saved)
            self.assertEqual(saved.count('BIND_ADDRESS='), 1)
            self.assertEqual(run('\n\n\n').returncode, 0)
            self.assertEqual((root / '.env').read_text().strip(), saved.strip())
            before = (root / '.env').read_bytes()
            self.assertNotEqual(run('127.0.0.1\n').returncode, 0)
            self.assertEqual((root / '.env').read_bytes(), before)

    def test_initial_telegram_input_is_hidden_and_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'scripts').mkdir()
            shutil.copyfile(Path(__file__).resolve().parents[1]/'scripts/configure.sh',root/'scripts/configure.sh')
            token='123456789:'+'x'*35
            result=subprocess.run([os.environ.get('BASH_FOR_TEST','bash'),'scripts/configure.sh'],cwd=root,
                                  input=('\n\n\ninvalid\n'+token+'\ninvalid\n-1001234567890\n').encode(),capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertNotIn(token.encode(),result.stdout+result.stderr)
            self.assertEqual(json.loads((root/'state/telegram.json').read_text()),{'token':token,'channel':'-1001234567890'})
