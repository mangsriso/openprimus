from contextlib import closing
import csv
import io
from pathlib import Path
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import kd360s_live as live


@unittest.skipUnless(shutil.which('cc'), 'host C compiler required')
class RecorderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix='recorder-wire-')
        cls.addClassCleanup(cls.work.cleanup)
        binary = Path(cls.work.name) / 'producer'
        subprocess.run(['cc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                        str(ROOT / 'tests/snapshot_runtime_driver.c'), '-o', str(binary)], check=True)
        raw = subprocess.check_output([str(binary), 'wire'], timeout=5)
        assert len(raw) == 512
        cls.packets = [raw[i:i + 128] for i in range(0, len(raw), 128)]

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix='recorder-db-')
        self.addCleanup(self.work.cleanup)
        self.path = Path(self.work.name) / 'events.sqlite'
        self.connection = live.open_archive(self.path, writable=True)
        self.addCleanup(self.connection.close)
        self.sender = ('127.0.0.1', 12345)

    def test_production_wire_through_udp_replay_restart_and_exports(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            server.bind(('127.0.0.1', 0)); server.settimeout(2); client.settimeout(2)
            for packet in [*self.packets, self.packets[2]]:
                client.sendto(packet, server.getsockname())
                self.assertEqual(live.receive_one(self.connection, server), {'result': 'acknowledged'})
                ack = client.recvfrom(64)[0]
                self.assertEqual(ack, b'WPA1' + packet[8:20])
                with closing(live.open_archive(self.path)) as committed:
                    self.assertIn(packet.hex(), [r['raw_hex'] for r in live.iter_snapshots(committed)])
        with closing(live.open_archive(self.path, writable=True)) as reopened:
            live.record(reopened, self.packets[0], self.sender)
            rows = list(live.iter_snapshots(reopened))
            self.assertEqual([r['kind'] for r in rows], ['start', 'pressure', 'accepted_rx', 'stop'])
            self.assertIsNone(rows[0]['calibrated_pressure'])
            self.assertFalse(rows[0]['pressure_observed'])
            self.assertTrue(rows[1]['pressure_observed'])
            self.assertEqual(rows[1]['calibrated_pressure'], 9.5)
            self.assertEqual(rows[1]['filtered_adc_bits'], 456)
            self.assertEqual(rows[2]['accepted_rx_hex'], bytes(range(28)).hex())
            self.assertEqual(rows[3]['pressure_generation'], 1)
            self.assertEqual(rows[3]['rx_generation'], 1)
            self.assertEqual(rows[3]['start_argument'], 17)
            shot, = live.iter_shots(reopened)
            self.assertEqual(shot['coverage'], 'boundaries_observed_no_reported_gaps')
            self.assertEqual((shot['start_ms'], shot['stop_ms']), (0, 30))
            self.assertEqual((shot['pressure_records'], shot['rx_records']), (1, 1))
            for fmt in ('csv', 'jsonl'):
                output = io.StringIO(); live.write_export(reopened, fmt, output)
                self.assertIn(self.packets[2].hex(), output.getvalue())
            output = io.StringIO(); live.write_export(reopened, 'csv', output)
            self.assertEqual(list(csv.DictReader(io.StringIO(output.getvalue())))[2]['kind'], 'accepted_rx')

    def test_gap_missing_boundaries_and_saturation_never_claim_complete(self):
        for packet in (self.packets[0], self.packets[2]):
            live.record(self.connection, packet, self.sender)
        shot, = live.iter_shots(self.connection)
        self.assertIn('missing_records', shot['reasons'])
        self.assertIn('missing_or_repeated_stop', shot['reasons'])
        last = bytearray(self.packets[3]); struct.pack_into('<I', last, 56, 39)
        live.record(self.connection, bytes(last), self.sender)
        shot, = live.iter_shots(self.connection)
        self.assertIn('counter_saturated', shot['reasons'])
        self.assertEqual(shot['coverage'], 'incomplete_or_uncertain')

    def test_malformed_extensions_rejected_without_write(self):
        for offset, value in ((4, 1), (5, 6), (6, 127), (7, 1), (56, 0), (56, 128),
                              (92, 27), (124, 1)):
            packet = bytearray(self.packets[2]); packet[offset] = value
            with self.subTest(offset=offset, value=value), self.assertRaises(live.ProtocolError):
                live.record(self.connection, bytes(packet), self.sender)
        packet = bytearray(self.packets[0]); packet[96] = 1
        with self.assertRaises(live.ProtocolError):
            live.record(self.connection, bytes(packet), self.sender)
        for kind in (4, 5):
            packet = bytearray(self.packets[0]); packet[5] = kind
            with self.assertRaisesRegex(live.ProtocolError, 'generations disagree'):
                live.record(self.connection, bytes(packet), self.sender)
        packet = bytearray(self.packets[2]); packet[64:68] = bytes(4)
        with self.assertRaisesRegex(live.ProtocolError, 'generations disagree'):
            live.record(self.connection, bytes(packet), self.sender)
        self.assertEqual(list(live.iter_snapshots(self.connection)), [])

    def test_v1_migration_preserves_all_raw_bytes_metadata_and_immutability(self):
        old = Path(self.work.name) / 'old.sqlite'
        with sqlite3.connect(old) as connection:
            connection.executescript(live.SCHEMA.replace('IN (64, 128)', '= 64').replace('user_version = 2', 'user_version = 1'))
            live.record(connection, live.synthetic_packet(), self.sender, 123)
        with closing(live.open_archive(old)) as readonly:
            before = list(live.iter_snapshots(readonly))
            self.assertEqual(readonly.execute('PRAGMA user_version').fetchone()[0], 1)
        with closing(live.open_archive(old, writable=True)) as migrated:
            self.assertEqual(migrated.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertEqual(list(live.iter_snapshots(migrated)), before)
            live.record(migrated, self.packets[0], self.sender)
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable'):
                migrated.execute('DELETE FROM snapshots')
            migrated.rollback()
            output = io.StringIO(); live.write_export(migrated, 'csv', output)
            self.assertEqual(len(list(csv.DictReader(io.StringIO(output.getvalue())))), 2)

    def test_failed_schema_migration_rolls_back_old_archive(self):
        old = Path(self.work.name) / 'old-broken.sqlite'
        with sqlite3.connect(old) as connection:
            connection.executescript(live.SCHEMA.replace('IN (64, 128)', '= 64').replace('user_version = 2', 'user_version = 1'))
            live.record(connection, live.synthetic_packet(), self.sender, 123)
            connection.execute('CREATE TABLE snapshots_v1 (sentinel TEXT)')
        with self.assertRaises(sqlite3.OperationalError):
            live.open_archive(old, writable=True)
        with sqlite3.connect(old) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT packet FROM snapshots').fetchone()[0], live.synthetic_packet())
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable'):
                connection.execute('DELETE FROM snapshots')
