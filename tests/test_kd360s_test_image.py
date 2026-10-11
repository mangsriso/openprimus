"""Builder fixtures are synthetic; the optional pinned-image smoke uses a local cache."""

from dataclasses import replace
import hashlib
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
import kd360s_test_image as builder
import kd360s_build_snapshot as snapshot
from test_kd360s_instrumentation import synthetic_layout


def elf_fixture():
    names = b"\0.text\0.rodata\0.symtab\0.strtab\0.shstrtab\0.rela.text\0.bss\0.data\0"
    strings = b"\0entry\0"
    text = audit.PROBE_TEXT
    rodata = b"payload constant\0"
    symbols = bytes(16) + builder.SYMBOL.pack(1, builder.PAYLOAD_START + 4, 14, 0x12, 0, 1)
    blob = bytearray(84)
    headers = [(0,) * 10]
    for name, stype, flags, address, contents, link, alignment, entsize in (
        (".text", 1, 6, builder.PAYLOAD_START, text, 0, 4, 0),
        (".rodata", 1, 2, builder.PAYLOAD_START + 32, rodata, 0, 4, 0),
        (".symtab", 2, 0, 0, symbols, 4, 4, 16),
        (".strtab", 3, 0, 0, strings, 0, 1, 0),
        (".shstrtab", 3, 0, 0, names, 0, 1, 0),
    ):
        headers.append((names.index(name.encode()), stype, flags, address, len(blob), len(contents), link, 0, alignment, entsize))
        blob.extend(contents)
    shoff = len(blob)
    for header in headers:
        blob.extend(builder.SECTION_HEADER.pack(*header))
    builder.ELF_HEADER.pack_into(blob, 0, b"\x7fELF\x01\x01\x01" + bytes(9), 2, 94, 1,
                                builder.PAYLOAD_START + 4, 52, shoff, 0x300, 52, 32, 1, 40, len(headers), 5)
    # PT_LOAD contains ELF headers, deliberately starting before the candidate tail.
    struct.pack_into("<IIIIIIII", blob, 52, 1, 0, builder.PAYLOAD_START - 84,
                     builder.PAYLOAD_START - 84, shoff, shoff, 5, 4)
    return bytes(blob)


def edit_header(data, field, value):
    values = list(builder.ELF_HEADER.unpack_from(data))
    fields = "ident kind machine version entry phoff shoff flags ehsize phsize phnum shsize shnum shstr".split()
    values[fields.index(field)] = value
    result = bytearray(data)
    builder.ELF_HEADER.pack_into(result, 0, *values)
    return bytes(result)


def edit_section(data, index, field, value):
    shoff = builder.ELF_HEADER.unpack_from(data)[6]
    offset = shoff + index * 40
    values = list(builder.SECTION_HEADER.unpack_from(data, offset))
    fields = "name stype flags address offset size link info alignment entsize".split()
    values[fields.index(field)] = value
    result = bytearray(data)
    builder.SECTION_HEADER.pack_into(result, offset, *values)
    return bytes(result)


class ElfTests(unittest.TestCase):
    def setUp(self):
        self.elf = elf_fixture()

    def test_extracts_only_allocated_sections_and_fills_gaps(self):
        payload = builder.parse_elf(self.elf)
        self.assertEqual(payload.entry, builder.PAYLOAD_START + 4)
        self.assertEqual(payload.entry_stack_bytes, 32)
        self.assertEqual(payload.data, audit.PROBE_TEXT + bytes(14) + b"payload constant\0")
        self.assertEqual([s.name for s in payload.sections], [".text", ".rodata"])
        self.assertNotIn(b"\x7fELF", payload.data)

    def test_rejects_wrong_format_headers_and_out_of_bounds_tables(self):
        cases = [("kind", 1), ("kind", 3), ("machine", 3), ("version", 0), ("ehsize", 64),
                 ("flags", 0x301), ("flags", 0x80000000), ("shsize", 64), ("shnum", 0),
                 ("shnum", 4097), ("shoff", 0xFFFFFFFF), ("shstr", 0), ("shstr", 6),
                 ("phsize", 0), ("phnum", 4097), ("phoff", 0xFFFFFFFF)]
        for field, value in cases:
            with self.subTest(field=field, value=value), self.assertRaises(firmware.FirmwareError):
                builder.parse_elf(edit_header(self.elf, field, value))
        for offset in range(16):
            wrong = bytearray(self.elf)
            wrong[offset] ^= 0x80
            with self.subTest(ident_byte=offset), self.assertRaises(firmware.FirmwareError):
                builder.parse_elf(bytes(wrong))

    def test_rejects_truncations_and_oversized_elf(self):
        for size in (0, 51, 83, len(self.elf) - 1):
            with self.subTest(size=size), self.assertRaises(firmware.FirmwareError):
                builder.parse_elf(self.elf[:size])
        with self.assertRaisesRegex(firmware.FirmwareError, "16 MiB"):
            builder.parse_elf(bytes(builder.MAX_ELF_SIZE + 1))

    def test_rejects_writable_bss_relocation_dynamic_and_unknown_allocated_sections(self):
        cases = [(1, "flags", 7), (2, "flags", 3), (2, "flags", 6), (1, "flags", 2),
                 (1, "stype", 8), (1, "stype", 4), (1, "stype", 9), (1, "stype", 6),
                 (1, "stype", 11), (1, "name", 1 + len(".text\0.rodata\0")),
                 (2, "name", 1), (1, "flags", 0x406), (3, "stype", 8)]
        for index, field, value in cases:
            with self.subTest(index=index, field=field, value=value), self.assertRaises(firmware.FirmwareError):
                builder.parse_elf(edit_section(self.elf, index, field, value))

    def test_rejects_overlaps_bad_alignment_and_outside_candidate_tail(self):
        cases = [(1, "address", builder.PAYLOAD_START - 4), (1, "address", builder.PAYLOAD_LIMIT),
                 (2, "address", builder.PAYLOAD_START + 16), (2, "address", builder.PAYLOAD_START + 33),
                 (2, "alignment", 3), (2, "offset", 0xFFFFFFFF), (2, "size", 0),
                 (2, "size", 0xFFFFFFFF), (0, "alignment", 1)]
        for index, field, value in cases:
            with self.subTest(index=index, field=field, value=value), self.assertRaises(firmware.FirmwareError):
                builder.parse_elf(edit_section(self.elf, index, field, value))

    def test_entry_must_be_an_executable_windowed_entry_with_aligned_stack(self):
        for entry in (0, builder.PAYLOAD_START, builder.PAYLOAD_START + 16, builder.PAYLOAD_START + 32):
            with self.subTest(entry=entry), self.assertRaises(firmware.FirmwareError):
                builder.parse_elf(edit_header(self.elf, "entry", entry))
        for instruction in (b"\x36\x00\x00", b"\x36\x01\x00", b"\x36\x31\x00", b"\x00\x41\x00"):
            wrong = bytearray(self.elf)
            wrong[88:91] = instruction
            with self.subTest(instruction=instruction), self.assertRaises(firmware.FirmwareError):
                builder.parse_elf(bytes(wrong))

    def test_rejects_unresolved_weak_common_and_extended_symbols(self):
        shoff = builder.ELF_HEADER.unpack_from(self.elf)[6]
        symbol_offset = builder.SECTION_HEADER.unpack_from(self.elf, shoff + 3 * 40)[4] + 16
        for info, index in ((0x12, 0), (0x22, 0), (0x12, 0xFFF2), (0x12, 0xFFFF), (0x12, 6)):
            wrong = bytearray(self.elf)
            builder.SYMBOL.pack_into(wrong, symbol_offset, 1, 0, 0, info, 0, index)
            with self.subTest(info=info, index=index), self.assertRaises(firmware.FirmwareError):
                builder.parse_elf(bytes(wrong))

    def test_rejects_missing_and_malformed_symbol_table(self):
        for field, value in (("stype", 1), ("entsize", 8), ("size", 31), ("link", 0), ("link", 6), ("link", 1)):
            with self.subTest(field=field), self.assertRaises(firmware.FirmwareError):
                builder.parse_elf(edit_section(self.elf, 3, field, value))

    def test_rejects_invalid_strings(self):
        for index, field, value in ((1, "name", 0xFFFFFFFF), (5, "stype", 1), (5, "size", 0)):
            with self.subTest(field=field), self.assertRaises(firmware.FirmwareError):
                builder.parse_elf(edit_section(self.elf, index, field, value))

    def test_rejects_dynamic_program_header_and_file_memory_mismatch(self):
        for field_offset, value in ((52, 2), (52, 3), (52 + 16, 0xFFFFFFFF), (52 + 20, 0)):
            wrong = bytearray(self.elf)
            struct.pack_into("<I", wrong, field_offset, value)
            with self.subTest(field=field_offset), self.assertRaises(firmware.FirmwareError):
                builder.parse_elf(bytes(wrong))


class ImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        layout = synthetic_layout()
        body = bytearray(layout.data[:layout.payload_end])
        body[0x390F70:0x390F78] = bytes.fromhex("3641000c021df000")
        body[0] = 0xE9
        body[23] = 1
        struct.pack_into("<H", body, 12, 9)
        cls.stock = builder._seal(bytes(body))
        cls.target = firmware.Target(firmware.TARGET.filename, len(cls.stock), hashlib.sha256(cls.stock).hexdigest(), 9, 5)

    def synthetic_build(self, elf=None, *, stock=None, **kwargs):
        stock = self.stock if stock is None else stock
        source_target = firmware.Target("synthetic", len(stock), hashlib.sha256(stock).hexdigest(), 9, 5)
        def verify(data, target=None):
            return firmware.verify_bytes(data, source_target if target is None else target)
        with patch.object(builder, "verify_bytes", side_effect=verify), patch.object(builder, "audit_bytes", return_value={"status": "synthetic_audit"}):
            return builder.build_image(stock, elf_fixture() if elf is None else elf, **kwargs)

    def test_patch_inserts_bytes_and_preserves_each_other_original_byte(self):
        result, manifest = self.synthetic_build()
        insertion = manifest["changes"]["insertion"]
        self.assertEqual(insertion["file_offset"], 0x396E94)
        self.assertEqual(insertion["leading_padding"], 12)
        self.assertEqual(insertion["size"], 64)
        old = firmware.verify_bytes(self.stock, self.target)
        new_target = firmware.Target("result", len(result), hashlib.sha256(result).hexdigest(), 9, 5)
        new = firmware.verify_bytes(result, new_target)
        for before, after in zip(old.segments, new.segments):
            if before.index == 3:
                continue
            self.assertEqual(self.stock[before.offset:before.offset + before.size], result[after.offset:after.offset + after.size])
        restored = bytearray(result[:0x396E94] + result[0x396E94 + 64:new.payload_end])
        struct.pack_into("<I", restored, 0x29001C, old.segments[3].size)
        restored[0x2CEC00:0x2CEC04] = bytes.fromhex("14700642")
        self.assertEqual(restored, self.stock[:old.payload_end])
        self.assertEqual(result[0x396EA0:0x396EA0 + 18], audit.PROBE_TEXT)
        self.assertEqual(struct.unpack_from("<I", result, 0x2CEC00)[0], builder.PAYLOAD_START + 4)
        self.assertTrue(manifest["checks"]["reverse_to_pinned_stock"])
        self.assertFalse(manifest["ready_to_flash"])
        self.assertFalse(manifest["elf"]["whole_payload_abi_proven"])
        self.assertTrue(manifest["unresolved_hardware_gates"])

    def test_wrong_stock_is_rejected_before_elf_interpretation(self):
        with patch.object(builder, "parse_elf") as parse:
            with self.assertRaises(firmware.FirmwareError):
                builder.build_image(self.stock, elf_fixture())
        parse.assert_not_called()

    def test_repeated_build_is_byte_identical(self):
        self.assertEqual(self.synthetic_build(), self.synthetic_build())

    def test_trial_default_false_preserves_binary_and_manifest(self):
        default = self.synthetic_build()
        self.assertEqual(default, self.synthetic_build(trial_defer_confirm=False))
        self.assertNotIn("trial_defer_confirm", default[1]["changes"])

    def test_trial_changes_only_instruction_checksum_and_digest(self):
        default, _ = self.synthetic_build()
        trial, manifest = self.synthetic_build(trial_defer_confirm=True)
        self.assertEqual(len(default), len(trial))
        checksum = manifest["changes"]["checksum"]["offset_after"]
        self.assertEqual(trial[:0x390F74], default[:0x390F74])
        self.assertEqual((default[0x390F74], trial[0x390F74]), (0x02, 0x12))
        self.assertEqual(trial[0x390F75:checksum], default[0x390F75:checksum])
        self.assertEqual(default[checksum] ^ trial[checksum], 0x10)
        self.assertEqual(hashlib.sha256(trial[:checksum + 1]).digest(), trial[checksum + 1:])
        change = manifest["changes"]["trial_defer_confirm"]
        self.assertEqual(change["expected_function_hex"], "3641000c021df000")
        self.assertEqual(change["function_address"], "0x42100f70")
        self.assertEqual(change["file_offset"], 0x390F74)
        self.assertFalse(change["mark_valid_added"])
        self.assertFalse(change["rollback_verified_on_device"])
        self.assertFalse(manifest["ready_to_flash"])
        self.assertTrue(manifest["checks"]["reverse_to_pinned_stock"])
        old = firmware.verify_bytes(self.stock, self.target)
        insertion = manifest["changes"]["insertion"]
        restored = bytearray(trial[:insertion["file_offset"]] + trial[insertion["file_offset"] + insertion["size"]:])
        struct.pack_into("<I", restored, old.segments[3].offset - 4, old.segments[3].size)
        restored[0x2CEC00:0x2CEC04] = bytes.fromhex("14700642")
        restored[0x390F74] = 0x02
        self.assertEqual(builder._seal(bytes(restored[:old.payload_end])), self.stock)

    def test_trial_checks_every_function_byte_including_already_patched_input(self):
        old = firmware.verify_bytes(self.stock, self.target)
        for offset in range(8):
            body = bytearray(self.stock[:old.payload_end])
            body[0x390F70 + offset] ^= 0x10
            wrong = builder._seal(bytes(body))
            with self.subTest(offset=offset), self.assertRaisesRegex(firmware.FirmwareError, "function bytes"):
                self.synthetic_build(stock=wrong, trial_defer_confirm=True)

    def test_trial_rejects_unexpected_file_mapping_and_non_irom_range(self):
        original = firmware.verify_bytes(self.stock, self.target)
        code = original.segments[3]
        cases = [replace(code, offset=code.offset + 4),
                 replace(code, address=0x3C000020),
                 replace(code, size=0x42100F74 - code.address)]
        images = [replace(original, segments=original.segments[:3] + (changed,) + original.segments[4:])
                  for changed in cases]
        images.append(replace(original, segments=original.segments + (replace(code, index=5),)))
        for image in images:
            with self.subTest(segments=image.segments), patch.object(builder, "audit_bytes", return_value={}), \
                    patch.object(builder, "verify_bytes", return_value=image), \
                    self.assertRaises(firmware.FirmwareError):
                builder.build_image(self.stock, elf_fixture(), trial_defer_confirm=True)

    def test_trial_keeps_stock_pin_before_interpretation(self):
        with patch.object(builder, "parse_elf") as parse, self.assertRaises(firmware.FirmwareError):
            builder.build_image(self.stock, elf_fixture(), trial_defer_confirm=True)
        parse.assert_not_called()

    def test_trial_reverse_verification_detects_an_extra_validly_sealed_edit(self):
        seal = builder._seal
        def extra_edit(body):
            changed = bytearray(body)
            changed[0x390F75] ^= 1
            return seal(bytes(changed))
        with patch.object(builder, "_seal", side_effect=extra_edit), self.assertRaises(firmware.FirmwareError):
            self.synthetic_build(trial_defer_confirm=True)

    def test_trial_and_default_packages_cannot_overwrite_each_other(self):
        default = self.synthetic_build()
        trial = self.synthetic_build(trial_defer_confirm=True)
        for first, second in ((default, trial), (trial, default)):
            with self.subTest(trial_first=first == trial), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "package"
                builder.write_package(output, *first)
                builder.write_package(output, *first)
                with self.assertRaises(firmware.FirmwareError):
                    builder.write_package(output, *second)
                self.assertEqual((output / "firmware.experimental.bin").read_bytes(), first[0])
                self.assertEqual(json.loads((output / "manifest.json").read_text()), first[1])

    def test_both_clis_forward_trial_option_and_default(self):
        for enabled in (False, True):
            flag = ["--trial-defer-confirm"] if enabled else []
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                stock, elf = root / "stock", root / "payload"
                stock.write_bytes(b"stock")
                elf.write_bytes(b"elf")
                manifest = {"status": "test", "output": {"sha256": "test"}}
                with patch.object(builder, "build_image", return_value=(b"image", manifest)) as build, \
                        patch.object(builder, "write_package"), patch("sys.stdout", io.StringIO()):
                    self.assertEqual(builder.main(["--stock", str(stock), "--elf", str(elf),
                                                   "--output", str(root / "result")] + flag), 0)
                build.assert_called_once_with(b"stock", b"elf", trial_defer_confirm=enabled)
                with patch.object(snapshot, "build", return_value=manifest) as build, patch("sys.stdout", io.StringIO()):
                    self.assertEqual(snapshot.main(["--stock", str(stock), "--toolchain", str(root),
                                                    "--collector-ip", "192.0.2.1", "--output", str(root / "result")] + flag), 0)
                self.assertEqual(build.call_args.kwargs, {"trial_defer_confirm": enabled})

    def test_snapshot_wrapper_forwards_trial_and_keeps_experiment_metadata(self):
        def compile_fixture(command, **kwargs):
            Path(command[command.index("-o") + 1]).write_bytes(elf_fixture())
        manifest = {"status": "test", "changes": {"trial_defer_confirm": {"mark_valid_added": False}}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stock = root / "stock.bin"
            stock.write_bytes(b"stock")
            with patch.object(snapshot.subprocess, "check_output", return_value="gcc 8.4.0 esp-2021r2-patch5"), \
                    patch.object(snapshot.subprocess, "run", side_effect=compile_fixture), \
                    patch.object(snapshot, "build_image", return_value=(b"image", manifest)) as build, \
                    patch.object(snapshot, "write_package") as publish:
                result = snapshot.build(root, stock, "192.0.2.1", 47653, root / "out", trial_defer_confirm=True)
            build.assert_called_once_with(b"stock", elf_fixture(), trial_defer_confirm=True)
            self.assertEqual(result["experiment"]["protocol"], "WPS1")
            self.assertEqual(result["experiment"]["collector_ipv4"], "192.0.2.1")
            self.assertFalse(result["changes"]["trial_defer_confirm"]["mark_valid_added"])
            publish.assert_called_once_with(root / "out", b"image", result)

    def test_output_publication_is_idempotent_and_does_not_overwrite(self):
        binary, manifest = self.synthetic_build()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "package"
            builder.write_package(output, binary, manifest)
            before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in output.iterdir()}
            builder.write_package(output, binary, manifest)
            after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in output.iterdir()}
            self.assertEqual(before, after)
            for contents in (binary + b"x", b"wrong"):
                with self.assertRaises(firmware.FirmwareError):
                    builder.write_package(output, contents, manifest)
            self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in output.iterdir()})

    def test_existing_file_and_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "file").write_bytes(b"original")
            (root / "empty").mkdir()
            (root / "link").symlink_to(root / "empty")
            for name in ("file", "link"):
                with self.subTest(name=name), self.assertRaises(firmware.FirmwareError):
                    builder.write_package(root / name, b"binary", {})
            self.assertEqual((root / "file").read_bytes(), b"original")

    def test_interrupted_package_resumes_without_replacing_bytes(self):
        binary, manifest = self.synthetic_build()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "package"
            output.mkdir()
            partial = output / "firmware.experimental.bin"
            partial.write_bytes(binary)
            before = partial.stat().st_mtime_ns
            builder.write_package(output, binary, manifest)
            self.assertEqual(partial.stat().st_mtime_ns, before)
            self.assertTrue((output / "manifest.json").is_file())
            builder.write_package(output, binary, manifest)

    def test_failed_publication_cleans_up_only_own_files(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "package"
            real_link = builder.os.link
            calls = []
            def failing_link(source, destination):
                calls.append(destination)
                if len(calls) == 2:
                    raise OSError("test failure")
                real_link(source, destination)
            with patch.object(builder.os, "link", side_effect=failing_link):
                with self.assertRaises(OSError):
                    builder.write_package(output, b"binary", {})
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_cli_failure_cannot_overwrite_stock(self):
        with tempfile.TemporaryDirectory() as directory:
            stock = Path(directory) / "stock.bin"
            stock.write_bytes(b"original")
            output = io.StringIO()
            with patch("sys.stdout", output):
                status = builder.main(["--stock", str(stock), "--elf", str(stock), "--output", str(stock)])
            self.assertEqual(status, 1)
            self.assertEqual(stock.read_bytes(), b"original")
            self.assertFalse(json.loads(output.getvalue())["ready_to_flash"])

    def test_actual_pinned_stock_smoke_when_cached(self):
        cache = firmware.DEFAULT_CACHE
        if not cache.exists():
            self.skipTest("pinned vendor firmware is not cached in this checkout")
        stock = cache.read_bytes()
        result, manifest = builder.build_image(stock, elf_fixture())
        self.assertEqual(hashlib.sha256(stock).hexdigest(), firmware.TARGET.sha256)
        self.assertEqual(hashlib.sha256(result).hexdigest(), manifest["output"]["sha256"])
        self.assertTrue(manifest["checks"]["reverse_to_pinned_stock"])
        trial, trial_manifest = builder.build_image(stock, elf_fixture(), trial_defer_confirm=True)
        self.assertEqual((stock[0x390F74], trial[0x390F74]), (0x02, 0x12))
        self.assertTrue(trial_manifest["checks"]["reverse_to_pinned_stock"])
        self.assertEqual((result, manifest), builder.build_image(stock, elf_fixture(), trial_defer_confirm=False))


if __name__ == "__main__":
    unittest.main()
