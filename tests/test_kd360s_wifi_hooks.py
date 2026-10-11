"""Wi-Fi detours against pinned stock and compiled C wrappers/original assembly."""
import copy
import re
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
import kd360s_wifi_hooks as hooks

WRAPPERS = '''
#include <stdint.h>
extern uint32_t original_explicit_begin(uint32_t,uint32_t,uint32_t,uint32_t,uint32_t,uint32_t);
extern uint32_t original_saved_begin(uint32_t);
extern uint32_t original_disconnect(uint32_t,uint32_t,uint32_t);
extern uint32_t original_mode(uint32_t);
uint32_t wrap_wifi_explicit(uint32_t a,uint32_t b,uint32_t c,uint32_t d,uint32_t e,uint32_t f) {
 return original_explicit_begin(a,b,c,d,e,f);
}
uint32_t wrap_wifi_saved(uint32_t a) { return original_saved_begin(a); }
uint32_t wrap_wifi_disconnect(uint32_t a,uint32_t b,uint32_t c) { return original_disconnect(a,b,c); }
uint32_t wrap_wifi_mode(uint32_t a) { return original_mode(a); }
void recorder_after(uint32_t a,uint32_t b,uint32_t c) { (void)a; (void)b; (void)c; }
'''


def compile_fixture(compiler, directory):
    directory = Path(directory)
    (directory / 'entry.S').write_text('.file "entry.S"\n.section .text.entry,"ax"\n.align 4\n'
                                     '.global snapshot_entry\n.type snapshot_entry,@function\n'
                                     'snapshot_entry:\nentry a1,32\nretw\n'
                                     '.size snapshot_entry,.-snapshot_entry\n')
    (directory / 'wrappers.c').write_text(WRAPPERS)
    source = ROOT / 'experiments/kd360s_snapshot'
    output = directory / 'fixture.elf'
    subprocess.run([str(compiler), '-std=c11', '-DWIFI_HOOKS=1', '-Os', '-mlongcalls', '-nostdlib',
                    '-fno-optimize-sibling-calls', '-fno-ipa-icf', '-Wl,--build-id=none',
                    '-Wl,-T,' + str(source / 'snapshot.ld'), str(directory / 'entry.S'),
                    str(directory / 'wrappers.c'), str(source / 'wifi_hooks.S'),
                    str(source / 'wifi_guard.S'), str(source / 'hooks.S'), '-o', str(output)],
                   check=True, capture_output=True)
    return output.read_bytes()


class WifiHookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stock = ROOT / 'firmware_binary/kd360s/KD360S_OTA_1.0.13_HK_20260630.bin'
        compiler = ROOT.parent / 'tools/xtensa-esp32s3-elf-esp-2021r2-patch5/bin/xtensa-esp32s3-elf-gcc'
        if not stock.exists() or not compiler.exists():
            raise unittest.SkipTest('requires pinned stock and reviewed Xtensa compiler')
        cls.stock = stock.read_bytes()
        cls.image = firmware.verify_bytes(cls.stock)
        with tempfile.TemporaryDirectory(prefix='wifi-hooks-test-') as temporary:
            cls.elf = compile_fixture(compiler, temporary)
        cls.payload = builder.parse_elf(cls.elf)

    def test_default_false_preserves_legacy_build(self):
        baseline = builder.build_image(self.stock, self.elf)
        self.assertEqual(baseline, builder.build_image(self.stock, self.elf, wifi_hooks=False))
        self.assertNotIn('wifi_hooks', baseline[1]['changes'])
        self.assertFalse(preflight.verify_package(self.stock, self.elf, *baseline)['wifi_hooks'])

    def test_four_exact_spans_and_combined_package_reversal(self):
        for trial in (False, True):
            options = dict(trial_defer_confirm=trial, recorder_hooks=True, wifi_guard=True)
            baseline, _ = builder.build_image(self.stock, self.elf, trial_defer_confirm=trial, recorder_hooks=True)
            result, manifest = builder.build_image(self.stock, self.elf, wifi_hooks=True, **options)
            self.assertTrue(preflight.verify_package(self.stock, self.elf, result, manifest,
                                                     trial_defer_confirm=trial)['wifi_hooks'])
            self.assertTrue(manifest['checks']['reverse_to_pinned_stock'])
            body = bytearray(baseline[:self.image.payload_end + manifest['changes']['insertion']['size']])
            plan = manifest['changes']['wifi_hooks']
            self.assertEqual(len(plan['patches']), 4)
            for item, hook in zip(plan['patches'], hooks.HOOKS):
                data = bytes.fromhex(item['after_hex'])
                self.assertEqual(len(data), len(hook.expected))
                if hook.name == 'mode':
                    self.assertEqual(data, bytes.fromhex('911521a009000000'))
                    self.assertEqual(result[item['file_offset'] + 8:item['file_offset'] + 15],
                                     self.stock[item['file_offset'] + 8:item['file_offset'] + 15])
                else:
                    self.assertEqual(data[:4], bytes.fromhex('06010000'))
                    self.assertEqual(data[8:14], bytes.fromhex('91ffffa00900'))
                    self.assertEqual(struct.unpack_from('<I', data, 4)[0], int(item['wrapper_address'], 16))
                self.assertEqual(item['wrapper_stack_bytes'], 32)
                offset = item['file_offset']; body[offset:offset + len(data)] = data
            guard = manifest['changes']['wifi_guard']['patches'][0]
            data = bytes.fromhex(guard['after_hex'])
            self.assertEqual(len(data), 21)
            self.assertEqual(struct.unpack_from('<I', data, 16)[0],
                             int(plan['patches'][3]['wrapper_address'], 16))
            body[guard['file_offset']:guard['file_offset'] + len(data)] = data
            self.assertEqual(builder._seal(bytes(body)), result)
            self.assertFalse(manifest['ready_to_flash'])

    def test_every_original_trampoline_byte_is_verified(self):
        text = self.payload.sections[0]
        symbol = next(s for s in self.payload.symbols if s.name == hooks.HOOKS[0].original)
        start = symbol.address - hooks.HOOKS[0].original_offset - text.address
        for index in range(len(hooks.TRAMPOLINE_BLOCK)):
            data = bytearray(text.data); data[start + index] ^= 1
            with self.subTest(index=index), self.assertRaisesRegex(firmware.FirmwareError, 'trampoline instructions'):
                hooks.plan_wifi_hooks(self.image, replace(self.payload, sections=(replace(text, data=bytes(data)),)))

    def test_stock_prefixes_and_mutex_literal_witnesses_are_pinned(self):
        addresses = [hook.address + i for hook in hooks.HOOKS for i in range(len(hook.expected))]
        addresses += [address + i for _, address, data in hooks.IMPORTS for i in range(len(data))]
        addresses += [address + i for address, _ in hooks.LITERALS for i in range(4)]
        for address in addresses:
            segment = next(s for s in self.image.segments if s.address <= address < s.address + s.size)
            data = bytearray(self.stock); data[segment.offset + address - segment.address] ^= 1
            with self.subTest(address=hex(address)), self.assertRaisesRegex(firmware.FirmwareError, 'stock'):
                hooks.plan_wifi_hooks(replace(self.image, data=bytes(data)), self.payload)

    def test_original_symbol_layout_binding_bounds_and_uniqueness(self):
        for hook in hooks.HOOKS:
            symbol = next(s for s in self.payload.symbols if s.name == hook.original)
            others = tuple(s for s in self.payload.symbols if s.name != symbol.name)
            cases = [others, self.payload.symbols + (symbol,)]
            for edits in ({'info': 0x02}, {'section_name': '.rodata'}, {'size': symbol.size + 1},
                          {'address': symbol.address + 4}, {'address': builder.PAYLOAD_LIMIT}, {'size': 0}):
                cases.append(others + (replace(symbol, **edits),))
            for symbols in cases:
                with self.subTest(hook=hook.name, symbols=symbols), self.assertRaises(firmware.FirmwareError):
                    hooks.plan_wifi_hooks(self.image, replace(self.payload, symbols=symbols))

    def test_wrapper_function_bounds_alias_and_entry_frame_validation(self):
        first = next(s for s in self.payload.symbols if s.name == 'wrap_wifi_explicit')
        second = next(s for s in self.payload.symbols if s.name == 'wrap_wifi_saved')
        original = next(s for s in self.payload.symbols if s.name == hooks.HOOKS[0].original)
        others = tuple(s for s in self.payload.symbols if s.name != first.name)
        cases = [others, self.payload.symbols + (first,)]
        for edits in ({'info': 0x10}, {'section_name': '.rodata'}, {'size': 0}, {'size': 100000},
                      {'address': builder.PAYLOAD_START - 4}, {'address': second.address},
                      {'address': original.address}, {'address': first.address + 1}):
            cases.append(others + (replace(first, **edits),))
        for symbols in cases:
            with self.subTest(symbols=symbols), self.assertRaises(firmware.FirmwareError):
                hooks.plan_wifi_hooks(self.image, replace(self.payload, symbols=symbols))
        for entry in (bytes.fromhex('362100'), bytes.fromhex('365100'), bytes.fromhex('1df000')):
            text = self.payload.sections[0]; data = bytearray(text.data)
            offset = first.address - text.address; data[offset:offset + 3] = entry
            with self.subTest(entry=entry.hex()), self.assertRaisesRegex(firmware.FirmwareError, 'frame|ENTRY'):
                hooks.plan_wifi_hooks(self.image, replace(self.payload, sections=(replace(text, data=bytes(data)),)))

    def test_manifest_cannot_hide_or_supply_arbitrary_hook_patches(self):
        result, manifest = builder.build_image(self.stock, self.elf, wifi_hooks=True, wifi_guard=True)
        for value in (False, True, None, {}, []):
            bad = copy.deepcopy(manifest); bad['changes']['wifi_hooks'] = value
            with self.subTest(value=value), self.assertRaisesRegex(firmware.FirmwareError, 'manifest differs'):
                preflight.verify_package(self.stock, self.elf, result, bad)
        for key, value in [('file_offset', 0), ('before_hex', '00'), ('after_hex', '00'),
                           ('original_address', '0x420031b0'), ('wrapper_address', '0x420031b0')]:
            bad = copy.deepcopy(manifest); bad['changes']['wifi_hooks']['patches'][0][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(firmware.FirmwareError, 'manifest differs'):
                preflight.verify_package(self.stock, self.elf, result, bad)
        bad = copy.deepcopy(manifest); del bad['changes']['wifi_hooks']
        with self.assertRaises(firmware.FirmwareError):
            preflight.verify_package(self.stock, self.elf, result, bad)

    def test_runtime_import_macros_match_pinned_functions(self):
        source = (ROOT / 'experiments/kd360s_snapshot/stock_runtime.h').read_text()
        names = {'wifi_current_task': 'current_task', 'wifi_gate_create': 'create_mutex',
                 'wifi_gate_take': 'take_recursive', 'wifi_gate_give': 'give_recursive',
                 'wifi_get_mode': 'get_mode'}
        expected = {name: address for name, address, _ in hooks.IMPORTS}
        for macro, witness in names.items():
            match = re.search(r'#define ' + macro + r' IMPORT\((0x[0-9a-f]+)u,', source)
            self.assertIsNotNone(match, macro)
            self.assertEqual(int(match[1], 16), expected[witness], macro)

    def test_hooks_require_coupled_guard(self):
        with self.assertRaisesRegex(firmware.FirmwareError, 'coupled'):
            builder.build_image(self.stock, self.elf, wifi_hooks=True)

    def test_resealed_detour_change_is_rejected(self):
        result, manifest = builder.build_image(self.stock, self.elf, wifi_hooks=True, wifi_guard=True)
        body = bytearray(result[:self.image.payload_end + manifest['changes']['insertion']['size']])
        body[manifest['changes']['wifi_hooks']['patches'][0]['file_offset'] + 4] ^= 1
        with self.assertRaisesRegex(firmware.FirmwareError, 'package bytes differ'):
            preflight.verify_package(self.stock, self.elf, builder._seal(bytes(body)), manifest)


if __name__ == '__main__':
    unittest.main()
