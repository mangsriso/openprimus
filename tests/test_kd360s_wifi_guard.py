"""Pinned stock + compiled production assembly; skips when local build inputs are absent."""
import copy
from dataclasses import replace
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import kd360s_firmware as firmware
import kd360s_preflight as preflight
import kd360s_test_image as builder
import kd360s_wifi_guard as guard


class WifiGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stock = ROOT / 'firmware_binary/kd360s/KD360S_OTA_1.0.13_HK_20260630.bin'
        compiler = ROOT.parent / 'tools/xtensa-esp32s3-elf-esp-2021r2-patch5/bin/xtensa-esp32s3-elf-gcc'
        if not stock.exists() or not compiler.exists():
            raise unittest.SkipTest('requires pinned stock and reviewed local Xtensa compiler')
        cls.stock = stock.read_bytes()
        cls.image = firmware.verify_bytes(cls.stock)
        with tempfile.TemporaryDirectory(prefix='wifi-guard-test-') as temporary:
            work = Path(temporary)
            (work / 'entry.S').write_text('.section .text.entry,"ax"\n.align 4\n.global snapshot_entry\n'
                                         '.type snapshot_entry,@function\nsnapshot_entry:\n'
                                         'entry a1,32\nretw\n.size snapshot_entry,.-snapshot_entry\n')
            subprocess.run([str(compiler), '-nostdlib', '-Wl,--build-id=none',
                            '-Wl,-T,' + str(ROOT / 'experiments/kd360s_snapshot/snapshot.ld'),
                            str(work / 'entry.S'), str(ROOT / 'experiments/kd360s_snapshot/wifi_guard.S'),
                            '-o', str(work / 'guard.elf')], check=True, capture_output=True)
            cls.elf = (work / 'guard.elf').read_bytes()
        cls.payload = builder.parse_elf(cls.elf)

    def test_default_and_false_are_identical_and_leave_stock_callback_untouched(self):
        result, manifest = builder.build_image(self.stock, self.elf)
        self.assertEqual((result, manifest), builder.build_image(self.stock, self.elf, wifi_guard=False))
        self.assertNotIn('wifi_guard', manifest['changes'])
        self.assertFalse(preflight.verify_package(self.stock, self.elf, result, manifest)['wifi_guard'])

    def test_guard_trial_combinations_reproduce_and_reverse_exact_stock(self):
        for trial in (False, True):
            with self.subTest(trial=trial):
                baseline, _ = builder.build_image(self.stock, self.elf, trial_defer_confirm=trial)
                result, manifest = builder.build_image(self.stock, self.elf, wifi_guard=True,
                                                        trial_defer_confirm=trial)
                report = preflight.verify_package(self.stock, self.elf, result, manifest,
                                                   trial_defer_confirm=trial)
                self.assertTrue(report['wifi_guard'])
                self.assertTrue(manifest['checks']['reverse_to_pinned_stock'])
                self.assertFalse(report['ready_to_flash'])
                plan = manifest['changes']['wifi_guard']
                patch = plan['patches'][0]
                offset = patch['file_offset']
                expected = bytearray(baseline[:self.image.payload_end + manifest['changes']['insertion']['size']])
                expected[offset:offset + len(guard.EXPECTED)] = bytes.fromhex(patch['after_hex'])
                self.assertEqual(builder._seal(bytes(expected)), result)
                callback = plan['preserved_callback']
                position, size = callback['file_offset'], callback['size']
                self.assertEqual(result[position:position + size], self.stock[position:position + size])
                pointer = next(w for w in plan['stock_witnesses'] if w['name'] == 'callback_pointer')
                self.assertEqual(result[pointer['file_offset']:pointer['file_offset'] + 4], bytes.fromhex('94ba0142'))
                self.assertEqual(struct.unpack_from('<I', bytes.fromhex(patch['after_hex']), 4)[0],
                                 int(patch['guard_address'], 16))

    def test_span_predecessor_every_literal_and_callback_are_pinned(self):
        for address in [guard.ADDRESS + i for i in range(len(guard.EXPECTED))] + [
                a + i for _, a, data in guard.WITNESSES for i in range(len(data))] + [
                guard.CALLBACK_ADDRESS, guard.CALLBACK_ADDRESS + guard.CALLBACK_SIZE - 1]:
            modified = bytearray(self.stock)
            modified[0x290000 + address - 0x42000000] ^= 1
            with self.subTest(address=hex(address)), self.assertRaises(firmware.FirmwareError):
                guard.plan_wifi_guard(replace(self.image, data=bytes(modified)), self.payload)

    def test_requires_unique_bounded_global_function_symbol(self):
        symbol = next(s for s in self.payload.symbols if s.name == guard.SYMBOL)
        others = tuple(s for s in self.payload.symbols if s.name != guard.SYMBOL)
        cases = [others, self.payload.symbols + (symbol,)]
        for values in ({'info': 0x10}, {'info': 0x02}, {'section_name': '.rodata'}, {'size': 0},
                       {'size': guard.FUNCTION_BYTES + 1}, {'address': builder.PAYLOAD_START},
                       {'address': builder.PAYLOAD_LIMIT}, {'address': symbol.address + 1}):
            cases.append(others + (replace(symbol, **values),))
        for symbols in cases:
            with self.subTest(symbols=symbols), self.assertRaises(firmware.FirmwareError):
                guard.plan_wifi_guard(self.image, replace(self.payload, symbols=symbols))

    def test_every_guard_instruction_and_literal_byte_is_verified(self):
        symbol = next(s for s in self.payload.symbols if s.name == guard.SYMBOL)
        section = self.payload.sections[0]
        start = symbol.address - guard.LITERAL_BYTES - section.address
        for i in range(len(guard.GUARD_BLOCK)):
            data = bytearray(section.data)
            data[start + i] ^= 1
            with self.subTest(byte=i), self.assertRaisesRegex(firmware.FirmwareError, 'instructions/literals'):
                guard.plan_wifi_guard(self.image, replace(self.payload, sections=(replace(section, data=bytes(data)),)))

    def test_manifest_cannot_authorize_other_patches_or_hide_guard(self):
        result, manifest = builder.build_image(self.stock, self.elf, wifi_guard=True)
        for value in (False, True, None, {}, []):
            bad = copy.deepcopy(manifest)
            bad['changes']['wifi_guard'] = value
            with self.subTest(value=value), self.assertRaisesRegex(firmware.FirmwareError, 'manifest differs'):
                preflight.verify_package(self.stock, self.elf, result, bad)
        for key, value in [('file_offset', 0), ('after_hex', '00'), ('guard_address', '0x420031b0')]:
            bad = copy.deepcopy(manifest)
            bad['changes']['wifi_guard']['patches'][0][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(firmware.FirmwareError, 'manifest differs'):
                preflight.verify_package(self.stock, self.elf, result, bad)
        bad = copy.deepcopy(manifest)
        del bad['changes']['wifi_guard']
        with self.assertRaisesRegex(firmware.FirmwareError, 'package bytes differ'):
            preflight.verify_package(self.stock, self.elf, result, bad)

    def test_resealed_callback_modification_is_rejected(self):
        result, manifest = builder.build_image(self.stock, self.elf, wifi_guard=True)
        body = bytearray(result[:self.image.payload_end + manifest['changes']['insertion']['size']])
        body[0x290000 + guard.CALLBACK_ADDRESS - 0x42000000] ^= 1
        with self.assertRaisesRegex(firmware.FirmwareError, 'package bytes differ'):
            preflight.verify_package(self.stock, self.elf, builder._seal(bytes(body)), manifest)


if __name__ == '__main__':
    unittest.main()
