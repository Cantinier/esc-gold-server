import os
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

            self.assertEqual(run('\n\n').returncode, 0)
            self.assertIn('BIND_ADDRESS=127.0.0.1\nPORT=4113', (root / '.env').read_text())
            with (root / '.env').open('a') as stream:
                stream.write('COMPOSE_PROJECT_NAME=test-project\n')
            self.assertEqual(run('999.1.1.1\n0.0.0.0\n0\n65536\n04114\n').returncode, 0)
            saved = (root / '.env').read_text()
            self.assertIn('PORT=4114', saved)
            self.assertIn('COMPOSE_PROJECT_NAME=test-project', saved)
            self.assertEqual(saved.count('BIND_ADDRESS='), 1)
            self.assertEqual(run('\n\n').returncode, 0)
            self.assertEqual((root / '.env').read_text().strip(), saved.strip())
            before = (root / '.env').read_bytes()
            self.assertNotEqual(run('127.0.0.1\n').returncode, 0)
            self.assertEqual((root / '.env').read_bytes(), before)
