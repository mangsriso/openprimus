from contextlib import ExitStack
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_kd360s_test_image as fixtures
from test_kd360s_firmware import make_image, target_for
import kd360s_firmware as firmware
import kd360s_preflight as preflight
import kd360s_test_image as builder


class PackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.ImageTests.setUpClass()
        cls.stock = fixtures.ImageTests.stock
        cls.target = fixtures.ImageTests.target
        cls.elf = fixtures.elf_fixture()

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        def verify(data, target=None):
            return firmware.verify_bytes(data, self.target if target is None else target)
        stack.enter_context(patch.object(builder, 'verify_bytes', side_effect=verify))
        stack.enter_context(patch.object(builder, 'audit_bytes', return_value={'status': 'synthetic_audit'}))
        stack.enter_context(patch.object(preflight, 'TARGET', self.target))
        self.binary, self.manifest = builder.build_image(self.stock, self.elf)

    def test_both_modes_reproduce_but_do_not_claim_device_or_source_proof(self):
        for trial in (False, True):
            binary, manifest = builder.build_image(self.stock, self.elf, trial_defer_confirm=trial)
            report = preflight.verify_package(self.stock, self.elf, binary, manifest, trial_defer_confirm=trial)
            self.assertTrue(report['package_reproduced'])
            self.assertFalse(report['ready_to_flash'])
            self.assertFalse(report['source_to_elf_verified'])
            self.assertIsNone(report['backup_inspection'])
            self.assertEqual(report['unresolved_hardware_gates'], builder.HARDWARE_GATES)
            with self.assertRaisesRegex(firmware.FirmwareError, 'package bytes differ'):
                preflight.verify_package(self.stock, self.elf, binary, manifest, trial_defer_confirm=not trial)

    def test_validly_resealed_unplanned_edit_is_rejected(self):
        target = firmware.Target('synthetic', len(self.binary), hashlib.sha256(self.binary).hexdigest(), 9, 5)
        image = firmware.verify_bytes(self.binary, target)
        body = bytearray(self.binary[:image.payload_end])
        body[32] ^= 1
        changed = builder._seal(bytes(body))
        firmware.verify_bytes(changed, target_for(changed))
        with self.assertRaisesRegex(firmware.FirmwareError, 'package bytes differ'):
            preflight.verify_package(self.stock, self.elf, changed, self.manifest)

    def test_manifest_spoof_and_wrong_json_types_are_rejected(self):
        for key, value in [('ready_to_flash', True), ('ready_to_flash', 0), ('schema_version', True),
                           ('schema_version', 1.0), ('unexpected', 'value'), ('experiment', [])]:
            manifest = copy.deepcopy(self.manifest)
            manifest[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(firmware.FirmwareError):
                preflight.verify_package(self.stock, self.elf, self.binary, manifest)
        with self.assertRaisesRegex(firmware.FirmwareError, 'JSON object'):
            preflight.verify_package(self.stock, self.elf, self.binary, [])

    def test_experiment_claims_remain_unverified_and_nan_is_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        manifest['experiment'] = {'collector_ipv4': '192.0.2.1', 'every_shot_verified': True}
        report = preflight.verify_package(self.stock, self.elf, self.binary, manifest)
        self.assertEqual(report['unverified_metadata']['experiment'], manifest['experiment'])
        self.assertFalse(report['ready_to_flash'])
        manifest['schema_version'] = float('nan')
        with self.assertRaises(ValueError):
            preflight.verify_package(self.stock, self.elf, self.binary, manifest)

    def test_different_elf_and_corrupt_stock_are_rejected(self):
        other = self.elf.replace(b'payload constant', b'another constant')
        with self.assertRaisesRegex(firmware.FirmwareError, 'package bytes differ'):
            preflight.verify_package(self.stock, other, self.binary, self.manifest)
        with self.assertRaisesRegex(firmware.FirmwareError, 'SHA-256'):
            preflight.verify_package(bytes([0]) + self.stock[1:], self.elf, self.binary, self.manifest)

    def test_cli_keeps_inputs_unchanged_and_rejects_missing_elf_and_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stock = root / 'stock.bin'
            stock.write_bytes(self.stock)
            package = root / 'package'
            builder.write_package(package, self.binary, self.manifest, elf=self.elf)
            args = ['--stock', str(stock), '--package', str(package)]
            before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in package.iterdir()}
            with patch('sys.stdout', io.StringIO()) as output:
                self.assertEqual(preflight.main(args), 0)
            self.assertTrue(json.loads(output.getvalue())['package_reproduced'])
            self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in package.iterdir()})
            (package / 'payload.elf').unlink()
            with patch('sys.stdout', io.StringIO()) as output:
                self.assertEqual(preflight.main(args), 1)
            self.assertEqual(json.loads(output.getvalue())['status'], 'preflight_rejected')
            (package / 'payload.elf').write_bytes(self.elf)
            (package / 'manifest.json').write_text('{"schema_version":1,"schema_version":1}')
            with patch('sys.stdout', io.StringIO()) as output:
                self.assertEqual(preflight.main(args), 1)
            self.assertIn('duplicate JSON key', json.loads(output.getvalue())['error'])


def flash_fixture(entries=None, *, md5=True):
    entries = entries if entries is not None else [
        (1, 2, 0x9000, 0x3000, 'nvs', 0),
        (1, 0, 0xc000, 0x2000, 'otadata', 0),
        (0, 0x10, 0x10000, 0x10000, 'ota_0', 0),
        (0, 0x11, 0x20000, 0x10000, 'ota_1', 0),
    ]
    table = b''.join(preflight.PARTITION_ENTRY.pack(0x50aa, kind, subtype, offset, size,
                                                  name.encode(), flags)
                     for kind, subtype, offset, size, name, flags in entries)
    if md5:
        table += preflight.MD5_MARKER + hashlib.md5(table).digest()
    table += b'\xff' * (0x1000 - len(table))
    flash = bytearray(b'\xff' * 0x40000)
    flash[0x8000:0x9000] = table
    return flash


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.stock = make_image()
        target = target_for(self.stock)
        verifier = patch.object(preflight, 'verify_bytes',
                                side_effect=lambda data: firmware.verify_bytes(data, target))
        verifier.start()
        self.addCleanup(verifier.stop)

    def inspect(self, backup, size=0x10000):
        return preflight.inspect_flash_backup(bytes(backup), partition_table_offset=0x8000,
                                              stock=self.stock, candidate_size=size)

    def test_stock_match_and_capacity_do_not_prove_active_slot_or_recovery(self):
        flash = flash_fixture()
        flash[0x10000:0x10000 + len(self.stock)] = self.stock
        result = self.inspect(flash)
        self.assertEqual(result['pinned_stock_app_partitions'], ['ota_0'])
        self.assertEqual(result['partition_table_md5'], 'verified')
        self.assertTrue(result['partitions'][2]['candidate_fits'])
        self.assertIsNone(result['selected_ota_destination'])
        for key in ['complete_device_backup_verified', 'active_application_verified',
                    'security_state_verified', 'bootloader_rollback_verified', 'independent_recovery_verified']:
            self.assertFalse(result[key])
        self.assertFalse(self.inspect(flash, size=0x10001)['partitions'][2]['candidate_fits'])

    def test_stock_outside_an_app_partition_is_not_an_installed_app_match(self):
        flash = flash_fixture(md5=False)
        flash[0x9000:0x9000 + len(self.stock)] = self.stock
        result = self.inspect(flash)
        self.assertEqual(result['pinned_stock_app_partitions'], [])
        self.assertEqual(result['partition_table_md5'], 'not_present')

    def test_corrupt_md5_missing_terminator_and_truncated_dump_are_rejected(self):
        wrong = flash_fixture()
        wrong[0x8010] ^= 1
        with self.assertRaisesRegex(firmware.FirmwareError, 'MD5'):
            self.inspect(wrong)
        with self.assertRaisesRegex(firmware.FirmwareError, 'outside the dump'):
            self.inspect(flash_fixture()[:0x25000])
        wrong = flash_fixture()
        wrong[0x8000:0x8c00] = preflight.PARTITION_ENTRY.pack(0x50aa, 1, 2, 0x9000, 0x1000, b'x', 0) * 96
        with self.assertRaisesRegex(firmware.FirmwareError, 'no terminator'):
            self.inspect(wrong)

    def test_invalid_layouts_are_rejected_even_with_correct_md5(self):
        layouts = [
            ([(0, 0x10, 0x10000, 0x20000, 'a', 0), (0, 0x11, 0x20000, 0x10000, 'b', 0)], 'overlap'),
            ([(0, 0x10, 0x11000, 0x10000, 'a', 0)], 'alignment'),
            ([(1, 2, 0x8000, 0x1000, 'a', 0)], 'partition table'),
            ([(0, 0x10, 0x10000, 0, 'a', 0)], 'outside'),
            ([(1, 2, 0x9000, 0x1000, 'a', 2)], 'flags'),
            ([(1, 0, 0x9000, 0x1000, 'ota', 0)], 'OTA data'),
            ([(1, 2, 0x9000, 0x1000, 'a', 0), (1, 2, 0xa000, 0x1000, 'a', 0)], 'unique'),
        ]
        for entries, message in layouts:
            with self.subTest(message=message), self.assertRaisesRegex(firmware.FirmwareError, message):
                self.inspect(flash_fixture(entries))

    def test_wrong_table_offset_rejected_and_cli_requires_paired_backup_arguments(self):
        with self.assertRaisesRegex(firmware.FirmwareError, 'empty'):
            preflight.inspect_flash_backup(bytes(flash_fixture()), partition_table_offset=0x9000,
                                            stock=self.stock, candidate_size=100)
        with patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit) as result:
            preflight.main(['--package', 'unused', '--flash-backup', 'unused'])
        self.assertEqual(result.exception.code, 2)
