from contextlib import ExitStack
import copy
from dataclasses import replace
import hashlib
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import kd360s_firmware as firmware
import kd360s_hooks as hooks
import kd360s_preflight as preflight
import kd360s_test_image as builder
import test_kd360s_test_image as fixtures


def synthetic_stub_elf():
    names = b"\0.text\0.symtab\0.strtab\0.shstrtab\0"
    labels = ("entry", "wrap_rx", "wrap_start", "wrap_stop", "wrap_pressure")
    strings = b"\0" + b"".join(name.encode() + b"\0" for name in labels)
    code = bytes.fromhex("36a1001df0000000") * len(labels)
    symbols = bytes(16) + b"".join(
        builder.SYMBOL.pack(strings.index(name.encode()), builder.PAYLOAD_START + 8 * index,
                            5, 0x12, 0, 1) for index, name in enumerate(labels))
    data = bytearray(52)
    headers = [(0,) * 10]
    for name, kind, flags, address, contents, link, alignment, size in (
        (".text", 1, 6, builder.PAYLOAD_START, code, 0, 4, 0),
        (".symtab", 2, 0, 0, symbols, 3, 4, 16),
        (".strtab", 3, 0, 0, strings, 0, 1, 0),
        (".shstrtab", 3, 0, 0, names, 0, 1, 0),
    ):
        headers.append((names.index(name.encode()), kind, flags, address, len(data), len(contents),
                        link, 0, alignment, size))
        data.extend(contents)
    shoff = len(data)
    for header in headers:
        data.extend(builder.SECTION_HEADER.pack(*header))
    builder.ELF_HEADER.pack_into(data, 0, b"\x7fELF\x01\x01\x01" + bytes(9), 2, 94, 1,
                                builder.PAYLOAD_START, 0, shoff, 0x300, 52, 0, 0, 40, len(headers), 4)
    return bytes(data)


class HookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.ImageTests.setUpClass()
        base = firmware.verify_bytes(fixtures.ImageTests.stock, fixtures.ImageTests.target)
        body = bytearray(fixtures.ImageTests.stock[:base.payload_end])
        for hook in hooks.HOOKS:
            offset = 0x290000 + hook.address - 0x42000000
            body[offset:offset + len(hook.expected)] = hook.expected
        for _, address, value in hooks.LITERALS:
            struct.pack_into("<I", body, 0x290000 + address - 0x42000000, value)
        cls.stock = builder._seal(bytes(body))
        cls.target = firmware.Target("synthetic", len(cls.stock), hashlib.sha256(cls.stock).hexdigest(), 9, 5)
        cls.image = firmware.verify_bytes(cls.stock, cls.target)
        cls.elf = synthetic_stub_elf()

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        def verify(data, target=None):
            return firmware.verify_bytes(data, self.target if target is None else target)
        stack.enter_context(patch.object(builder, "verify_bytes", side_effect=verify))
        stack.enter_context(patch.object(builder, "audit_bytes", return_value={"status": "synthetic_audit"}))

    def test_default_preserves_original_entries_and_opt_in_edits_only_four_spans(self):
        baseline, manifest = builder.build_image(self.stock, self.elf)
        self.assertEqual((baseline, manifest), builder.build_image(self.stock, self.elf, recorder_hooks=False))
        self.assertNotIn("recorder_hooks", manifest["changes"])
        result, manifest = builder.build_image(self.stock, self.elf, recorder_hooks=True)
        expected = bytearray(baseline[:self.image.payload_end + manifest["changes"]["insertion"]["size"]])
        for item in manifest["changes"]["recorder_hooks"]["patches"]:
            offset = item["file_offset"]
            replacement = bytes.fromhex(item["after_hex"])
            self.assertEqual(replacement[:4], bytes.fromhex("06010000"))
            self.assertEqual(replacement[8:14], bytes.fromhex("91ffffa00900"))
            self.assertEqual(struct.unpack_from("<I", replacement, 4)[0], int(item["wrapper_address"], 16))
            self.assertEqual(result[offset:offset + len(replacement)], replacement)
            expected[offset:offset + len(replacement)] = replacement
        self.assertEqual(builder._seal(bytes(expected)), result)
        self.assertTrue(manifest["checks"]["reverse_to_pinned_stock"])
        self.assertFalse(manifest["ready_to_flash"])

    def test_hook_and_confirmation_modes_reproduce_in_preflight(self):
        for trial in (False, True):
            result, manifest = builder.build_image(self.stock, self.elf, recorder_hooks=True,
                                                    trial_defer_confirm=trial)
            report = preflight.verify_package(self.stock, self.elf, result, manifest, trial_defer_confirm=trial)
            self.assertTrue(report["recorder_hooks"])
            self.assertTrue(report["package_reproduced"])
            self.assertFalse(report["ready_to_flash"])
            self.assertEqual(len(manifest["changes"]["recorder_hooks"]["literal_witnesses"]), 6)

    def test_missing_duplicate_alias_wrong_section_or_binding_wrapper_rejected(self):
        payload = builder.parse_elf(self.elf)
        first = payload.symbols[1]
        cases = [payload.symbols[:1] + payload.symbols[2:], payload.symbols + (first,),
                 (payload.symbols[0], replace(first, address=payload.symbols[2].address)) + payload.symbols[2:]]
        for changes in ({"section_name": ".rodata"}, {"info": 0x02}, {"info": 0x11},
                        {"address": builder.PAYLOAD_START - 4}, {"address": builder.PAYLOAD_LIMIT},
                        {"address": first.address + 1}, {"size": 100000}):
            cases.append((payload.symbols[0], replace(first, **changes)) + payload.symbols[2:])
        for symbols in cases:
            with self.subTest(symbols=symbols), self.assertRaises(firmware.FirmwareError):
                hooks.plan_recorder_hooks(self.image, replace(payload, symbols=symbols))

    def test_bad_wrapper_entry_or_spill_overlapping_frame_rejected(self):
        payload = builder.parse_elf(self.elf)
        for entry in (bytes.fromhex("368100"), bytes.fromhex("36b100"), bytes.fromhex("1df000")):
            code = bytearray(payload.sections[0].data)
            code[8:11] = entry
            section = replace(payload.sections[0], data=bytes(code))
            with self.subTest(entry=entry.hex()), self.assertRaises(firmware.FirmwareError):
                hooks.plan_recorder_hooks(self.image, replace(payload, sections=(section,)))

    def test_every_stock_span_and_literal_is_guarded(self):
        payload = builder.parse_elf(self.elf)
        addresses = [hook.address + len(hook.expected) - 1 for hook in hooks.HOOKS]
        addresses += [address for _, address, _ in hooks.LITERALS]
        for address in addresses:
            data = bytearray(self.image.data)
            data[0x290000 + address - 0x42000000] ^= 1
            with self.subTest(address=hex(address)), self.assertRaisesRegex(firmware.FirmwareError, "stock|literal"):
                hooks.plan_recorder_hooks(replace(self.image, data=bytes(data)), payload)

    def test_manifest_cannot_supply_an_arbitrary_patch_or_hide_hook_changes(self):
        result, manifest = builder.build_image(self.stock, self.elf, recorder_hooks=True)
        for field, value in (("file_offset", 0), ("after_hex", "00"),
                             ("wrapper_address", "0x420031b0"), ("before_hex", "00")):
            bad = copy.deepcopy(manifest)
            bad["changes"]["recorder_hooks"]["patches"][0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(firmware.FirmwareError, "manifest differs"):
                preflight.verify_package(self.stock, self.elf, result, bad)
        for value in (False, True, None, {}, []):
            bad = copy.deepcopy(manifest)
            bad["changes"]["recorder_hooks"] = value
            with self.subTest(value=value), self.assertRaisesRegex(firmware.FirmwareError, "manifest differs"):
                preflight.verify_package(self.stock, self.elf, result, bad)
        bad = copy.deepcopy(manifest)
        del bad["changes"]["recorder_hooks"]
        with self.assertRaisesRegex(firmware.FirmwareError, "package bytes differ"):
            preflight.verify_package(self.stock, self.elf, result, bad)

    def test_resealed_unplanned_hook_edit_is_rejected(self):
        result, manifest = builder.build_image(self.stock, self.elf, recorder_hooks=True)
        size = self.image.payload_end + manifest["changes"]["insertion"]["size"]
        body = bytearray(result[:size])
        body[0x295430 + 4] ^= 4
        with self.assertRaisesRegex(firmware.FirmwareError, "package bytes differ"):
            preflight.verify_package(self.stock, self.elf, builder._seal(bytes(body)), manifest)


if __name__ == "__main__":
    unittest.main()
