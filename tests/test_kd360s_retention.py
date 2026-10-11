import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("cc"), "host C compiler required")
class RetentionTests(unittest.TestCase):
    def test_policy_storage_reserve_wrap_and_concurrency(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = pathlib.Path(temporary) / "retention"
            subprocess.run([
                "cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread",
                "-fsanitize=address,undefined",
                "-I", str(ROOT / "experiments/kd360s_snapshot"),
                str(ROOT / "tests/retention_driver.c"), "-o", str(binary),
            ], check=True)
            result = subprocess.run([str(binary)], check=True, text=True,
                                    capture_output=True, timeout=30)
            self.assertIn("50000 concurrent external deliveries passed", result.stdout)


class RetentionImportTests(unittest.TestCase):
    @unittest.skipUnless((ROOT / "firmware_binary/kd360s/KD360S_OTA_1.0.13_HK_20260630.bin").is_file(),
                         "pinned stock cache required")
    def test_imports_resolve_to_audited_stock_bodies(self):
        import hashlib
        import json
        import re

        stock = (ROOT / "firmware_binary/kd360s/KD360S_OTA_1.0.13_HK_20260630.bin").read_bytes()
        self.assertEqual(hashlib.sha256(stock).hexdigest(),
                         "ac94398fde154cd8d29709e82990fd6dce53faa69b28e713f2c2a5dd4c65f5b4")
        segments = json.loads((ROOT / "analysis/kd360s/1.0.13_HK/image-segments.json").read_text())["segments"]
        imports = (ROOT / "experiments/kd360s_snapshot/stock_runtime.h").read_text()
        expected = {
            "caps_allocate": (32, "c52a17b254b3cf7c313003ddf1b5e05e93cde5334da6401f703f06c3eb2574c9"),
            "caps_free_size": (39, "0b34646087884805b384c8e8cfa5b727d8e9050eff6b3ff431f15f55f4255e20"),
            "caps_largest_block": (17, "7e8c07a463b677e71b5ceccd8c4a3a596245d11c1f4eb4d1a866472fefa3451d"),
        }
        for name, (size, digest) in expected.items():
            with self.subTest(name=name):
                match = re.search(r"#define " + name + r" IMPORT\((0x[0-9a-f]+)u,", imports)
                self.assertIsNotNone(match)
                address = int(match[1], 16)
                segment = next(s for s in segments if int(s["address"], 16) <= address
                               and address + size <= int(s["address"], 16) + s["size"])
                offset = segment["offset"] + address - int(segment["address"], 16)
                self.assertEqual(hashlib.sha256(stock[offset:offset + size]).hexdigest(), digest)
