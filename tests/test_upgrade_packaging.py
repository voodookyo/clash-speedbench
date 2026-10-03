from pathlib import Path
import unittest


class UpgradePackagingTest(unittest.TestCase):
    def test_new_runtime_modules_are_explicitly_packaged(self):
        root = Path(__file__).resolve().parents[1]
        for filename in ('build_app.sh','.github/workflows/release.yml','.github/workflows/test.yml'):
            content = (root/filename).read_text(encoding='utf-8')
            for module in ('speedbench_identity.py','speedbench_sources.py','speedbench_tasks.py','speedbench_jobs.py',
                           'speedbench_process.py','speedbench_progress.py'):
                with self.subTest(filename=filename,module=module):
                    self.assertIn(module,content)

    def test_private_seed_is_ignored_not_a_release_input(self):
        root = Path(__file__).resolve().parents[1]
        self.assertIn('identity-seed',(root/'.gitignore').read_text(encoding='utf-8'))
        for filename in ('build_app.sh','.github/workflows/release.yml'):
            self.assertNotIn('identity-seed',(root/filename).read_text(encoding='utf-8'))
