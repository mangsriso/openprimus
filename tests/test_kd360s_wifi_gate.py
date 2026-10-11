import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("cc"), "host C compiler required")
class WifiGateTests(unittest.TestCase):
    def test_recursive_gate_retry_ownership_and_thread_contention(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = pathlib.Path(temporary) / "wifi-gate"
            subprocess.run([
                "cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread",
                "-fsanitize=address,undefined", str(ROOT / "tests/wifi_gate_driver.c"), "-o", str(binary),
            ], check=True)
            result = subprocess.run([str(binary)], check=True, text=True,
                                    capture_output=True, timeout=20)
            self.assertIn("native bypass and UI contention passed", result.stdout)
