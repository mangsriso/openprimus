import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("cc"), "host C compiler required")
class TransportTests(unittest.TestCase):
    def test_window_ack_retry_and_slot_ownership(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = pathlib.Path(temporary) / "transport"
            subprocess.run([
                "cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                "-fsanitize=address,undefined", "-I", str(ROOT / "experiments/kd360s_snapshot"),
                str(ROOT / "tests/transport_driver.c"), "-o", str(binary),
            ], check=True)
            result = subprocess.run([str(binary)], check=True, text=True,
                                    capture_output=True, timeout=20)
            self.assertIn("out-of-order ACK, retry, reconnect and slot reuse passed", result.stdout)
