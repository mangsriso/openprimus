"""Synthetic ESP images only; no vendor firmware or network is needed."""

from dataclasses import FrozenInstanceError, replace
import hashlib
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import kd360s_firmware as firmware


def make_image() -> bytes:
    header = bytearray(24)
    header[0:2] = bytes([0xE9, 5])
    struct.pack_into("<I", header, 4, 0x40370000)
    struct.pack_into("<H", header, 12, 9)
    header[23] = 1
    data = header
    checksum = 0xEF
    for index, size in enumerate((1, 3, 7, 16, 23)):
        payload = bytes((value + index) % 256 for value in range(size))
        data.extend(struct.pack("<II", 0x3C000020 + index * 0x100, size))
        data.extend(payload)
        for value in payload:
            checksum ^= value
    checksum_end = (len(data) + 16) & ~15
    data.extend(bytes(checksum_end - len(data) - 1))
    data.append(checksum)
    return bytes(data) + hashlib.sha256(data).digest()


def target_for(data: bytes) -> firmware.Target:
    return replace(firmware.TARGET, size=len(data), sha256=hashlib.sha256(data).hexdigest())


class Response(io.BytesIO):
    def __init__(self, data: bytes, *, length=None, url="https://example.test/image"):
        super().__init__(data)
        self.headers = {} if length is None else {"Content-Length": str(length)}
        self.url = url

    def geturl(self):
        return self.url


class FirmwareTests(unittest.TestCase):
    def setUp(self):
        self.data = make_image()
        self.target = target_for(self.data)
        self.image = firmware.verify_bytes(self.data, self.target)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def assert_invalid(self, data, message):
        with self.assertRaisesRegex(firmware.FirmwareError, message):
            firmware.verify_bytes(data, target_for(data))

    def test_valid_image_boundaries_and_immutable_snapshot(self):
        mutable = bytearray(self.data)
        image = firmware.verify_bytes(mutable, self.target)
        mutable[32] ^= 1
        self.assertEqual(image.data, self.data)
        self.assertEqual(image.payload_end, 114)
        self.assertEqual(image.checksum_end, 128)
        self.assertEqual(tuple(segment.size for segment in image.segments), (1, 3, 7, 16, 23))
        self.assertIsInstance(image.segments, tuple)
        with self.assertRaises(FrozenInstanceError):
            image.segments[0].size = 100
        with self.assertRaises(FrozenInstanceError):
            image.data = b""
        with self.assertRaises(FrozenInstanceError):
            image.target.sha256 = "untrusted"

    def test_pinned_identity_size_and_hash(self):
        for data, message in ((self.data[:-1], "size mismatch"), (bytes(len(self.data)), "pinned SHA-256")):
            with self.subTest(message=message):
                with self.assertRaisesRegex(firmware.FirmwareError, message):
                    firmware.verify_bytes(data, self.target)
        with self.assertRaisesRegex(firmware.FirmwareError, "size mismatch"):
            firmware.verify_bytes(self.data)

    def test_header_checks(self):
        for offset, value, message in ((0, 0, "magic"), (1, 4, "segment count"), (12, 0, "chip ID"), (13, 1, "chip ID"), (23, 0, "flag"), (23, 2, "flag")):
            with self.subTest(offset=offset, value=value):
                data = bytearray(self.data)
                data[offset] = value
                self.assert_invalid(data, message)

    def test_truncated_headers_and_payloads_at_every_segment(self):
        self.assert_invalid(self.data[:23], "ESP header")
        for segment in self.image.segments:
            with self.subTest(segment=segment.index):
                self.assert_invalid(self.data[:segment.offset - 1], f"segment {segment.index} header")
                self.assert_invalid(self.data[:segment.offset + segment.size - 1], f"segment {segment.index} payload")

    def test_address_overflow_and_excessive_segment_length(self):
        data = bytearray(self.data)
        struct.pack_into("<I", data, self.image.segments[1].offset - 8, 0xFFFFFFFF)
        self.assert_invalid(data, "address overflow")
        data = bytearray(self.data)
        struct.pack_into("<I", data, 28, 0xFFFFFFFF)
        self.assert_invalid(data, "address overflow")
        struct.pack_into("<I", data, 24, 0)
        self.assert_invalid(data, "segment 0 payload")

    def test_checksum_hash_and_trailing_bytes(self):
        data = bytearray(self.data)
        data[self.image.checksum_end - 1] ^= 1
        self.assert_invalid(data, "XOR checksum")
        data = bytearray(self.data)
        data[-1] ^= 1
        self.assert_invalid(data, "appended SHA-256 mismatch")
        self.assert_invalid(self.data[:self.image.payload_end], "checksum block")
        self.assert_invalid(self.data[:-1], "truncated appended SHA-256")
        self.assert_invalid(self.data + b"trailer", "trailing bytes")

    def test_padding_is_covered_by_appended_hash(self):
        data = bytearray(self.data)
        data[self.image.payload_end] ^= 1
        self.assert_invalid(data, "appended SHA-256 mismatch")

    def test_verified_file_is_not_reread_during_extraction(self):
        source = self.root / "input.bin"
        source.write_bytes(self.data)
        image = firmware.verify_file(source, self.target)
        source.write_bytes(b"changed after validation")
        output = firmware.extract(image, self.root / "output")
        for segment in image.segments:
            self.assertEqual((output / segment.filename).read_bytes(), self.data[segment.offset:segment.offset + segment.size])
        details = json.loads((output / "metadata.json").read_text())
        self.assertEqual(details["sha256"], self.target.sha256)
        self.assertEqual(details["entry_point"], "0x40370000")
        self.assertEqual(details["appended_sha256_offset"], 128)
        self.assertEqual(len(details["segments"]), 5)

    def test_extract_is_idempotent_and_refuses_changed_output(self):
        output = firmware.extract(self.image, self.root / "output")
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in output.iterdir()}
        self.assertEqual(firmware.extract(self.image, output), output)
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in output.iterdir()})
        segment_path = output / self.image.segments[0].filename
        segment_path.write_bytes(b"do not overwrite")
        with self.assertRaisesRegex(firmware.FirmwareError, "refusing to overwrite"):
            firmware.extract(self.image, output)
        self.assertEqual(segment_path.read_bytes(), b"do not overwrite")

    def test_extract_refuses_extra_files_and_symlinks(self):
        output = firmware.extract(self.image, self.root / "output")
        extra = output / "unexpected.txt"
        extra.write_text("preserve")
        with self.assertRaises(firmware.FirmwareError):
            firmware.extract(self.image, output)
        extra.unlink()
        link = self.root / "linked-output"
        link.symlink_to(output, target_is_directory=True)
        with self.assertRaises(firmware.FirmwareError):
            firmware.extract(self.image, link)
        segment_path = output / self.image.segments[0].filename
        external = self.root / "external.bin"
        segment_path.rename(external)
        segment_path.symlink_to(external)
        with self.assertRaises(firmware.FirmwareError):
            firmware.extract(self.image, output)

    def test_extract_write_failure_leaves_no_partial_output(self):
        output = self.root / "output"
        original_write = Path.write_bytes

        def fail_on_metadata(path, data):
            if path.name == "metadata.json":
                raise OSError("simulated full disk")
            return original_write(path, data)

        with patch.object(Path, "write_bytes", fail_on_metadata):
            with self.assertRaisesRegex(OSError, "full disk"):
                firmware.extract(self.image, output)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_fetch_reuses_verified_cache_without_network(self):
        cache = self.root / "cache.bin"
        cache.write_bytes(self.data)
        with patch.object(firmware.urllib.request, "urlopen") as opener:
            self.assertEqual(firmware.fetch(cache, target=self.target, opener=opener), cache)
        opener.assert_not_called()

    def test_fetch_falls_back_after_transport_or_content_failure(self):
        cases = (
            urllib.error.URLError("offline"),
            Response(b"bad content"),
            Response(bytes(len(self.data))),
            Response(self.data, length=1),
            Response(self.data + b"too large"),
            Response(self.data, url="http://example.test/image"),
        )
        for first in cases:
            with self.subTest(first=first):
                cache = self.root / "cache.bin"
                cache.write_bytes(b"invalid old cache")
                with patch.object(firmware.urllib.request, "urlopen", side_effect=[first, Response(self.data)]) as opener:
                    self.assertEqual(firmware.fetch(cache, target=self.target, opener=opener), cache)
                self.assertEqual(cache.read_bytes(), self.data)
                self.assertEqual(opener.call_count, 2)
                self.assertEqual(opener.call_args_list[1].args[0], firmware.BASE_URLS[1] + "KD360S/" + self.target.filename)
                self.assertEqual(opener.call_args_list[0].kwargs["timeout"], firmware.SOCKET_TIMEOUT)
                self.assertEqual(list(self.root.iterdir()), [cache])

    def test_failed_fetch_preserves_existing_cache_and_cleans_temporary_files(self):
        cache = self.root / "cache.bin"
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    cache.write_bytes(b"preserve existing cache")
                with patch.object(firmware.urllib.request, "urlopen", side_effect=[Response(b"short"), Response(bytes(len(self.data)))]) as opener:
                    with self.assertRaisesRegex(firmware.FirmwareError, "all firmware sources failed"):
                        firmware.fetch(cache, target=self.target, opener=opener)
                self.assertEqual(list(self.root.iterdir()), [cache] if existing else [])
                if existing:
                    self.assertEqual(cache.read_bytes(), b"preserve existing cache")

    def test_download_deadline_discards_partial_file_and_tries_fallback(self):
        cache = self.root / "cache.bin"
        with patch.object(firmware.urllib.request, "urlopen", side_effect=[Response(self.data), Response(self.data)]) as opener:
            with patch.object(firmware.time, "monotonic", side_effect=[0, 121, 122, 122, 122]):
                firmware.fetch(cache, target=self.target, opener=opener)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(cache.read_bytes(), self.data)
        self.assertEqual(list(self.root.iterdir()), [cache])

    def test_cli_rejects_unpinned_image_without_traceback_or_output(self):
        source = self.root / "synthetic.bin"
        source.write_bytes(self.data)
        for command in ("verify", "extract"):
            with self.subTest(command=command):
                result = subprocess.run([sys.executable, firmware.__file__, command, str(source)], capture_output=True, text=True, cwd=self.root)
                self.assertEqual(result.returncode, 1)
                self.assertIn("error: size mismatch", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(result.stdout, "")
        self.assertEqual(list(self.root.iterdir()), [source])


if __name__ == "__main__":
    unittest.main()
