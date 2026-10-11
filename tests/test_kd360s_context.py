import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('cc'), 'host C compiler required')
class RecorderContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='kd360s-context-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.binary = pathlib.Path(cls.temporary.name) / 'context'
        subprocess.run(['cc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                        '-pthread', str(ROOT / 'tests/recorder_context_driver.c'),
                        '-o', str(cls.binary)], check=True)

    def run_case(self, case):
        result = subprocess.run([str(self.binary), case], check=True, capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.stdout, f'{case} passed\n')

    def test_preserves_other_keys_and_task_isolation_without_rebinding(self):
        self.run_case('isolation')

    def test_key_list_head_and_value_allocation_failures_are_rolled_back(self):
        self.run_case('failures')

    def test_failed_cleanup_requires_retaining_context(self):
        self.run_case('rollback')

    def test_large_registry_preserves_context_and_duplicate_marker_is_rejected(self):
        self.run_case('registry')

    def test_lookup_during_other_task_key_creation_and_deletion(self):
        self.run_case('concurrency')
