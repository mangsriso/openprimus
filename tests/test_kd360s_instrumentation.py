"""Synthetic layout fixtures with a mocked pin verifier; no vendor image is included."""

from dataclasses import replace
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import kd360s_firmware as firmware
import kd360s_instrumentation as audit


def synthetic_layout() -> firmware.VerifiedImage:
    """Construct zero-filled mapped bytes, not a valid ESP image or vendor firmware."""
    segments = (
        firmware.Segment(0, 0x3C110020, 32, 2651708),
        firmware.Segment(1, 0x3FC96990, 2651748, 24316),
        firmware.Segment(2, 0x40374000, 2676072, 10928),
        firmware.Segment(3, 0x42000020, 2687008, 1076852),
        firmware.Segment(4, 0x40376AB0, 3763868, 65244),
    )
    data = bytearray(firmware.TARGET.size)
    data[1] = 5
    for segment in segments:
        struct.pack_into("<II", data, segment.offset - 8, segment.address, segment.size)
    for offset, value in (
        (0x2CEC00, bytes.fromhex("14700642")),
        (0x290790, bytes.fromhex("2821ca3f")),
        (0x2CDE98, bytes.fromhex("2821ca3f")),
        (2676072 + 0x6D4, bytes.fromhex("90c8c93f")),
        (2676072 + 0x6D0, bytes.fromhex("7893ca3f")),
        (0x2F7014, bytes.fromhex("36410010112025e4a4")),
        (0x2F7065, bytes.fromhex("a1e65e")),
    ):
        data[offset : offset + len(value)] = value
    return firmware.VerifiedImage(bytes(data), firmware.TARGET, segments, 3829112, 3829120)


class InstrumentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = synthetic_layout()

    def synthetic_audit(self, image=None, probe_text=None):
        image = self.image if image is None else image
        with patch.object(audit, "verify_bytes", return_value=image) as verifier:
            result = audit.audit_bytes(image.data, probe_text)
        verifier.assert_called_once_with(image.data)
        return result

    def test_production_pin_rejects_wrong_and_modified_sources_before_interpretation(self):
        modified = bytearray(self.image.data)
        modified[0x2CEC00] ^= 1
        for source in (b"", self.image.data, bytes(modified)):
            with self.subTest(length=len(source)):
                with patch.object(audit, "_audit_verified") as interpret:
                    with self.assertRaises(firmware.FirmwareError):
                        audit.audit_bytes(source, audit.PROBE_TEXT)
                interpret.assert_not_called()

    def test_layout_report_separates_bytes_interpretation_and_unproven_placement(self):
        report = self.synthetic_audit()
        self.assertFalse(report["ready_to_flash"])
        self.assertEqual(report["status"], "offline_audit_passed")
        self.assertEqual(report["witnesses"]["loop_task_pointer"]["file_offset"], 0x2CEC00)
        self.assertEqual(report["witnesses"]["loop_task_pointer"]["uint32_le"], "0x42067014")
        xref = report["task_pointer_interpretation"]
        self.assertEqual(xref["evidence_class"], "external_static_ghidra_analysis")
        self.assertFalse(xref["independently_decoded_by_this_tool"])
        placement = report["candidate_placement"]
        self.assertEqual(placement["evidence_class"], "layout_arithmetic_only")
        value = placement["value"]
        self.assertEqual(value["proposed_start_address"], "0x42106ea0")
        self.assertEqual(value["prospective_file_offset"], 0x396EA0)
        self.assertTrue(value["overlaps_existing_segment_payload"])
        self.assertTrue(value["requires_insertion_and_shift"])
        self.assertFalse(value["runtime_mmu_mapping_proven"])
        self.assertTrue(report["blockers"])
        self.assertIsNone(report["probe_text"]["value"])

    def test_mapping_reads_first_and_last_initialized_bytes(self):
        segment = self.image.segments[3]
        for address, expected_offset in (
            (segment.address, segment.offset),
            (segment.address + segment.size - 1, segment.offset + segment.size - 1),
        ):
            self.assertEqual(
                audit.mapped_bytes(self.image, address, 1),
                (expected_offset, self.image.data[expected_offset : expected_offset + 1]),
            )

    def test_mapping_refuses_unmapped_crossing_and_invalid_ranges(self):
        segment = self.image.segments[3]
        cases = (
            (segment.address - 1, 1),
            (segment.address - 1, 2),
            (segment.address + segment.size - 1, 2),
            (segment.address + segment.size, 1),
            (0x3FCA2128, 4),
            (0xFFFFFFFF, 2),
            (-1, 1),
            (segment.address, 0),
            (segment.address, -1),
        )
        for address, size in cases:
            with self.subTest(address=address, size=size):
                with self.assertRaises(firmware.FirmwareError):
                    audit.mapped_bytes(self.image, address, size)

    def test_mapping_refuses_ambiguous_or_out_of_file_segments(self):
        segment = self.image.segments[3]
        duplicate = replace(self.image, segments=(segment, segment))
        with self.assertRaisesRegex(firmware.FirmwareError, "ambiguous"):
            audit.mapped_bytes(duplicate, segment.address, 4)
        for offset in (-1, len(self.image.data)):
            image = replace(self.image, segments=(replace(segment, offset=offset),))
            with self.assertRaisesRegex(firmware.FirmwareError, "outside"):
                audit.mapped_bytes(image, segment.address, 4)

    def test_audit_refuses_changed_segment_map(self):
        for field in ("address", "offset", "size", "index"):
            with self.subTest(field=field):
                segments = list(self.image.segments)
                segments[3] = replace(segments[3], **{field: getattr(segments[3], field) + 1})
                with self.assertRaisesRegex(firmware.FirmwareError, "segment layout"):
                    self.synthetic_audit(replace(self.image, segments=tuple(segments)))

    def test_audit_refuses_map_header_disagreement(self):
        for offset in (1,) + tuple(segment.offset - 8 for segment in self.image.segments):
            with self.subTest(offset=offset):
                data = bytearray(self.image.data)
                data[offset] ^= 1
                with self.assertRaises(firmware.FirmwareError):
                    self.synthetic_audit(replace(self.image, data=bytes(data)))

    def test_audit_refuses_changed_target_or_truncated_verified_snapshot(self):
        for image in (
            replace(self.image, target=replace(firmware.TARGET, sha256="0" * 64)),
            replace(self.image, data=self.image.data[:-1]),
        ):
            with self.assertRaisesRegex(firmware.FirmwareError, "pinned KD360S"):
                self.synthetic_audit(image)

    def test_every_source_witness_fails_closed_on_corruption(self):
        for offset in (0x2CEC00, 0x290790, 0x2CDE98, 2676072 + 0x6D4, 2676072 + 0x6D0, 0x2F7014, 0x2F7065):
            with self.subTest(offset=offset):
                data = bytearray(self.image.data)
                data[offset] ^= 1
                with self.assertRaisesRegex(firmware.FirmwareError, "source witness mismatch"):
                    self.synthetic_audit(replace(self.image, data=bytes(data)))

    def test_exact_probe_is_accepted_without_flash_or_runtime_claims(self):
        probe = bytes.fromhex("1470064236410020a22081fdffe008001df0")
        report = self.synthetic_audit(probe_text=probe)
        result = report["probe_text"]["value"]
        self.assertEqual(result["size"], 18)
        self.assertEqual(result["proposed_entry"], "0x42106ea4")
        self.assertEqual(result["additional_stack_frame_bytes"], 32)
        self.assertFalse(result["runtime_tested"])
        self.assertFalse(result["general_elf_validation"])
        self.assertFalse(report["ready_to_flash"])

    def test_probe_rejects_empty_truncated_extended_and_each_mutated_byte(self):
        malformed = [b"", audit.PROBE_TEXT[:-1], audit.PROBE_TEXT + b"\0", b"\0" * 1024]
        for index in range(len(audit.PROBE_TEXT)):
            data = bytearray(audit.PROBE_TEXT)
            data[index] ^= 1
            malformed.append(bytes(data))
        for probe in malformed:
            with self.subTest(probe=probe[:19].hex(), size=len(probe)):
                with self.assertRaisesRegex(firmware.FirmwareError, "exact 18-byte"):
                    self.synthetic_audit(probe_text=probe)

    def test_cli_preserves_inputs_and_creates_no_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "synthetic-not-vendor.bin"
            probe = root / "probe.text"
            source.write_bytes(self.image.data)
            probe.write_bytes(audit.PROBE_TEXT)
            before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in root.iterdir()}
            stdout = io.StringIO()
            with patch.object(audit, "verify_bytes", return_value=self.image) as verifier:
                with patch("sys.stdout", stdout):
                    result = audit.main([str(source), "--probe-text", str(probe)])
            self.assertEqual(result, 0)
            verifier.assert_called_once_with(self.image.data)
            self.assertFalse(json.loads(stdout.getvalue())["ready_to_flash"])
            after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in root.iterdir()}
            self.assertEqual(before, after)

    def test_cli_failures_are_json_and_keep_flash_gate_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "wrong.bin"
            for contents in (None, b"not firmware"):
                if contents is not None:
                    source.write_bytes(contents)
                stdout = io.StringIO()
                with patch("sys.stdout", stdout):
                    result = audit.main([str(source)])
                self.assertEqual(result, 1)
                report = json.loads(stdout.getvalue())
                self.assertEqual(report["status"], "audit_rejected")
                self.assertFalse(report["ready_to_flash"])
                self.assertTrue(report["error"])

    def test_cli_rejects_oversized_probe_and_preserves_both_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "synthetic-not-vendor.bin"
            probe = Path(directory) / "extended.text"
            source.write_bytes(self.image.data)
            probe.write_bytes(audit.PROBE_TEXT + bytes(1024))
            before = (source.read_bytes(), probe.read_bytes())
            stdout = io.StringIO()
            with patch.object(audit, "verify_bytes", return_value=self.image):
                with patch("sys.stdout", stdout):
                    result = audit.main([str(source), "--probe-text", str(probe)])
            self.assertEqual(result, 1)
            self.assertIn("exact 18-byte", json.loads(stdout.getvalue())["error"])
            self.assertEqual(before, (source.read_bytes(), probe.read_bytes()))


if __name__ == "__main__":
    unittest.main()
