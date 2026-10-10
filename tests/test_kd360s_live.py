"""Synthetic protocol/storage/UDP tests; no machine or shot validation."""

from contextlib import closing
import csv
import io
import json
from pathlib import Path
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import kd360s_live as live

ROOT = Path(__file__).resolve().parents[1]


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "live.sqlite"
        self.connection = live.open_archive(self.database, writable=True)
        self.addCleanup(self.connection.close)
        self.sender = ("127.0.0.1", 12345)

    def test_exact_wire_offsets_and_ack_support_full_uint64(self):
        packet = bytearray(live.synthetic_packet(0xffffffff, 0xffffffffffffffff))
        struct.pack_into("<III", packet, 20, 0xfffffffe, 1234, 56)
        struct.pack_into("<4I", packet, 32, 0x41100000, 0x3fa00000, 0xc1000000, 0)
        struct.pack_into("<H4BH", packet, 48, 65535, 2, 3, 4, 5, 7777)
        row = live.decode(packet)
        self.assertEqual(row["session"], "ffffffffffffffff")
        self.assertEqual(row["seq"], 0xffffffff)
        self.assertEqual(row["observation_ms"], 0xfffffffe)
        self.assertEqual(row["dropped_cumulative"], 1234)
        self.assertEqual(row["loop_duration_ms"], 56)
        self.assertEqual([row[k] for k in ("pressure", "flow", "target_pressure", "target_flow")],
                         [9.0, 1.25, -8.0, 0.0])
        self.assertEqual([row[k] for k in ("volume_raw", "mode_raw", "state_raw", "active_raw", "pending_stop_raw", "diagnostic_raw")],
                         [65535, 2, 3, 4, 5, 7777])
        ack = live.record(self.connection, bytes(packet), self.sender)
        self.assertEqual(ack, b"WPA1" + b"\xff" * 12)
        self.assertEqual(len(ack), 16)

    def test_malformed_never_stored(self):
        valid = live.synthetic_packet()
        invalid = [valid[:-1], valid + b"x", b"", b"bad!" + valid[4:]]
        for offset, value in ((4, 2), (5, 0), (6, 63), (7, 1), (56, 0), (56, 3), (57, 1), (60, 1)):
            packet = bytearray(valid)
            packet[offset] = value
            invalid.append(bytes(packet))
        for packet in invalid:
            with self.subTest(packet=packet.hex()), self.assertRaises(live.ProtocolError):
                live.record(self.connection, packet, self.sender)
        self.assertEqual(list(live.iter_snapshots(self.connection)), [])

    def test_identical_replay_after_reopen_preserves_first_metadata_and_raw(self):
        packet = live.synthetic_packet()
        expected = live.record(self.connection, packet, self.sender, 123456)
        with closing(live.open_archive(self.database, writable=True)) as reopened:
            self.assertEqual(live.record(reopened, packet, ("127.0.0.2", 54321), 999999), expected)
        rows = list(live.iter_snapshots(self.connection))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["raw_hex"], packet.hex())
        self.assertEqual(rows[0]["received_ns"], 123456)
        self.assertEqual(rows[0]["sender_ip"], "127.0.0.1")
        self.assertEqual(rows[0]["sender_port"], 12345)
        conflict = bytearray(packet)
        conflict[32] ^= 1
        with self.assertRaises(live.ConflictError):
            live.record(self.connection, bytes(conflict), self.sender)
        self.assertEqual(list(live.iter_snapshots(self.connection)), rows)

    def test_raw_records_immutable_including_replace(self):
        live.record(self.connection, live.synthetic_packet(), self.sender)
        for statement in ("DELETE FROM snapshots", "UPDATE snapshots SET seq=2",
                          "INSERT OR REPLACE INTO snapshots SELECT * FROM snapshots",
                          "REPLACE INTO snapshots SELECT * FROM snapshots"):
            with self.subTest(statement=statement), self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                self.connection.execute(statement)
            self.connection.rollback()
        self.assertEqual(len(list(live.iter_snapshots(self.connection))), 1)

    def test_failed_commit_never_sends_ack(self):
        live.record(self.connection, live.synthetic_packet(), self.sender)
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript("""
            CREATE TABLE parent (id INTEGER PRIMARY KEY);
            CREATE TABLE child (id INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED);
            CREATE TRIGGER fail_at_commit AFTER INSERT ON snapshots
            BEGIN INSERT INTO child VALUES (99); END;
        """)
        statements, sent = [], []
        self.connection.set_trace_callback(statements.append)

        class Datagram:
            def recvfrom(inner, size):
                return live.synthetic_packet(1), self.sender

            def sendto(inner, packet, sender):
                sent.append((packet, sender))

        with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY"):
            live.receive_one(self.connection, Datagram())
        self.assertIn("COMMIT", statements)
        self.assertEqual(sent, [])
        self.assertEqual([r["seq"] for r in live.iter_snapshots(self.connection)], [0])

    def test_transient_ack_failure_keeps_record_for_replay(self):
        import errno
        sent = []
        class Datagram:
            fail = True
            def recvfrom(inner, size):
                return live.synthetic_packet(1), self.sender
            def sendto(inner, packet, sender):
                if inner.fail:
                    raise OSError(errno.ENETUNREACH, "simulated outage")
                sent.append(packet)
        udp = Datagram()
        self.assertEqual(live.receive_one(self.connection, udp)["result"], "ack_deferred")
        self.assertEqual(len(list(live.iter_snapshots(self.connection))), 1)
        udp.fail = False
        self.assertEqual(live.receive_one(self.connection, udp)["result"], "acknowledged")
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(list(live.iter_snapshots(self.connection))), 1)

    def test_real_udp_ack_has_committed_record_visible_to_other_connection(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            server.bind(("127.0.0.1", 0))
            server.settimeout(1)
            client.settimeout(1)
            client.sendto(live.synthetic_packet(), server.getsockname())
            self.assertEqual(live.receive_one(self.connection, server), {"result": "acknowledged"})
            self.assertEqual(client.recvfrom(64)[0], live.ACK.pack(b"WPA1", 0xfedcba9876543210, 0))
            with closing(live.open_archive(self.database)) as reader:
                self.assertEqual(len(list(live.iter_snapshots(reader))), 1)

    def test_nonfinite_preserves_bits_and_standard_json_csv(self):
        packet = bytearray(live.synthetic_packet())
        struct.pack_into("<4I", packet, 32, 0x7fc12345, 0x7f800000, 0xff800000, 0x80000000)
        live.record(self.connection, bytes(packet), self.sender)
        output = io.StringIO()
        live.write_export(self.connection, "jsonl", output)
        self.assertNotIn("NaN", output.getvalue())
        self.assertNotIn("Infinity", output.getvalue())
        row = json.loads(output.getvalue())
        for name, bits in (("pressure", 0x7fc12345), ("flow", 0x7f800000), ("target_pressure", 0xff800000)):
            self.assertIsNone(row[name])
            self.assertEqual(row[name + "_bits"], bits)
        self.assertEqual(row["target_flow_bits"], 0x80000000)
        self.assertNotIn("shot_id", row)
        self.assertEqual(row["freshness"], "unknown")
        csv_output = io.StringIO()
        live.write_export(self.connection, "csv", csv_output)
        csv_row, = csv.DictReader(io.StringIO(csv_output.getvalue()))
        self.assertEqual(csv_row["pressure"], "")
        self.assertEqual(csv_row["raw_hex"], packet.hex())

    def test_out_of_order_records_and_new_session_not_rejected(self):
        for seq, session in ((9, 10), (3, 10), (0, 11)):
            live.record(self.connection, live.synthetic_packet(seq, session), self.sender)
        self.assertEqual([(r["session"], r["seq"]) for r in live.iter_snapshots(self.connection)],
                         [("000000000000000a", 3), ("000000000000000a", 9), ("000000000000000b", 0)])
        self.assertEqual(len(list(live.iter_snapshots(self.connection, "000000000000000b"))), 1)

    def test_read_only_missing_and_other_database_rejected(self):
        missing = self.database.with_name("missing.sqlite")
        with self.assertRaises(sqlite3.OperationalError):
            live.open_archive(missing)
        self.assertFalse(missing.exists())
        other = self.database.with_name("other.sqlite")
        with sqlite3.connect(other) as connection:
            connection.execute("CREATE TABLE other(x)")
        with self.assertRaisesRegex(ValueError, "not a supported"):
            live.open_archive(other, writable=True)

    def test_cli_self_test_inspect_and_exports(self):
        def cli(*args):
            return subprocess.run([sys.executable, str(ROOT / "scripts/kd360s_live.py"), *map(str, args)],
                                  capture_output=True, text=True, timeout=15)
        result = cli("self-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["passed"])
        self.assertFalse(json.loads(result.stdout)["hardware_verified"])
        live.record(self.connection, live.synthetic_packet(), self.sender)
        result = cli("inspect", self.database)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["sessions"][0]["records"], 1)
        for format_name in ("csv", "jsonl"):
            result = cli("export", self.database, "--format", format_name)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(live.synthetic_packet().hex(), result.stdout)
        result = cli("serve", self.database, "--port", "99999")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
