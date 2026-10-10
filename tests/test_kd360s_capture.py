"""SYNTHETIC transport events only. These tests do not validate real hardware."""

import csv
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import kd360s_capture as capture

ROOT = Path(__file__).resolve().parents[1]


def rx_frame():
    frame = bytearray.fromhex("671a043412550056570000a03f00000000030231300031077856")
    frame.extend((sum(frame) & 255, (sum(frame) & 255) ^ 255))
    return bytes(frame)


def tx_frame():
    frame = bytes((0x76, 0x22)) + bytes(range(33))
    return frame + bytes((sum(frame) & 255, (sum(frame) & 255) ^ 255))


def chunk(data, seq=0, direction="rx", session="SYNTHETIC", **fields):
    return {"schema_version": 1, "session_id": session, "seq": seq, "kind": "chunk",
            "direction": direction, "host_time_ns": seq * 100, "source_kind": "synthetic",
            "data_hex": data.hex(), **fields}


def gap(seq, direction="both"):
    event = chunk(b"x", seq, direction)
    event.pop("data_hex")
    event.update(kind="gap", reason="SYNTHETIC dropped transport bytes")
    return event


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "capture.sqlite"
        self.connection = capture.open_archive(self.database, writable=True)
        self.addCleanup(self.connection.close)

    def ingest(self, *events):
        return capture.import_events(self.connection, io.StringIO("".join(json.dumps(e) + "\n" for e in events)))

    def records(self):
        return list(capture.export_records(self.connection))

    def frames(self):
        return [r for r in self.records() if r["record_type"] == "frame"]

    def test_rx_every_split_preserves_offsets_and_fields(self):
        frame = rx_frame()
        self.assertEqual(len(frame), 28)
        for split in range(1, len(frame)):
            session = "SYNTHETIC-" + str(split)
            self.ingest(chunk(frame[:split], session=session), chunk(frame[split:], 1, session=session))
        frames = self.frames()
        self.assertEqual(len(frames), 27)
        for record in frames:
            split = int(record["session_id"].split("-")[1])
            self.assertEqual(record["raw_hex"], frame.hex())
            self.assertEqual(record["checksum_status"], "valid")
            self.assertEqual(record["raw_spans"], [
                {"seq": 0, "start_offset": 0, "end_offset": split},
                {"seq": 1, "start_offset": 0, "end_offset": 28 - split}])
            self.assertEqual(record["decoded"], {
                "flow_wire_float": 1.25, "flow_wire_bits_u32le": 1067450368,
                "raw_status": 4, "raw_flags_u16le": 0x1234,
                "raw_temperature_5": 85, "raw_temperature_7": 86, "raw_temperature_8": 87,
                "raw_volume_u16le": 515, "raw_volume_shifted": 257,
                "flag_19_ascii_one": True, "flag_20_ascii_one": False,
                "flag_21_ascii_one": False, "flag_22_ascii_one": True,
                "raw_echo": 7, "raw_value_24_u16le": 0x5678})
            self.assertIsNone(record["firmware_sha256"])

    def test_noise_corruption_and_overlapping_candidates_recover(self):
        frame = rx_frame()
        damaged = bytearray(frame)
        damaged[-1] ^= 1
        stream = b"\x99\x67\x00" + damaged + b"\x67\x1a" + frame
        self.ingest(chunk(stream))
        self.assertEqual([r["raw_hex"] for r in self.frames()], [frame.hex()])
        codes = {r.get("code") for r in self.records()}
        self.assertTrue({"noise", "length_mismatch", "checksum_mismatch"} <= codes)
        raw = [r for r in self.records() if r["record_type"] == "raw_event"]
        self.assertEqual(bytes.fromhex(raw[0]["data_hex"]), stream)

    def test_interleaved_directions_and_sessions(self):
        rx, tx = rx_frame(), tx_frame()
        self.ingest(chunk(rx[:10]), chunk(tx[:20], 1, "tx"), chunk(rx[10:], 2), chunk(tx[20:], 3, "tx"),
                    chunk(rx[:9], session="SYNTHETIC-other"))
        frames = self.frames()
        self.assertEqual([(r["direction"], r["start_seq"], r["end_seq"]) for r in frames], [("rx", 0, 2), ("tx", 1, 3)])
        self.assertEqual(frames[1]["raw_hex"], tx.hex())
        self.assertEqual(sum(r.get("code") == "incomplete_eof" for r in self.records()), 1)

    def test_tx_every_split_and_checksum(self):
        frame = tx_frame()
        for split in range(1, len(frame)):
            session = "SYNTHETIC-" + str(split)
            self.ingest(chunk(frame[:split], direction="tx", session=session),
                        chunk(frame[split:], 1, "tx", session))
        self.assertEqual(len(self.frames()), 36)
        bad = bytearray(frame)
        bad[-2] ^= 1
        self.ingest(chunk(bad + frame, direction="tx", session="SYNTHETIC-bad"))
        self.assertEqual(len(self.frames()), 37)

    def test_tx_reports_raw_activity_without_inferred_brew_lifecycle(self):
        cases = [(0, 6), (0, 2), (1, 0), (1, 1), (1, 2), (1, 3), (1, 4), (1, 5), (1, 6), (1, 99)]
        for seq, (state, activity) in enumerate(cases):
            frame = bytearray(tx_frame())
            frame[2:4] = bytes((state, activity))
            frame[35] = sum(frame[:35]) & 255
            frame[36] = frame[35] ^ 255
            self.ingest(chunk(frame, seq, "tx"))
        for record, (state, activity) in zip(self.frames(), cases):
            self.assertEqual(record["decoded"], {
                "machine_state_raw": state, "activity_code_raw": activity,
                "diagnostic_brew_distinguished": False})
            self.assertNotIn("brew_active_observed", record["decoded"])
            self.assertNotIn("shot_id", record)
        self.assertEqual(len(self.frames()), len(cases))

    def test_known_wrong_firmware_remains_raw_without_profile_decoding(self):
        event = chunk(rx_frame(), firmware_sha256="b" * 64, source_kind="uart")
        self.ingest(event)
        self.assertEqual(self.frames(), [])
        self.assertEqual(self.records()[0], {"record_type": "raw_event", **event})
        self.assertEqual(self.records()[1]["code"], "profile_firmware_mismatch")

    def test_matching_firmware_metadata_is_preserved(self):
        self.ingest(chunk(rx_frame(), firmware_sha256=capture.PROFILE_SHA256, source_kind="uart"))
        self.assertEqual(self.frames()[0]["firmware_sha256"], capture.PROFILE_SHA256)
        self.assertEqual(self.frames()[0]["source_kind"], "uart")

    def test_sequence_gap_breaks_both_streams(self):
        rx, tx = rx_frame(), tx_frame()
        self.ingest(chunk(rx[:10]), chunk(tx[:10], 1, "tx"), chunk(rx[10:], 3), chunk(tx[10:], 4, "tx"))
        self.assertEqual(self.frames(), [])
        records = self.records()
        self.assertEqual(sum(r.get("code") == "incomplete_sequence_gap" for r in records), 2)
        loss = next(r for r in records if r.get("code") == "sequence_gap")
        self.assertEqual(loss["decoded"], {"missing_start_seq": 2, "missing_end_seq": 2})
        with self.assertRaisesRegex(capture.CaptureError, "greater"):
            self.ingest(chunk(b"x", 2))

    def test_explicit_gap_only_breaks_named_direction(self):
        rx, tx = rx_frame(), tx_frame()
        self.ingest(chunk(rx[:10]), chunk(tx[:10], 1, "tx"), gap(2, "rx"),
                    chunk(rx[10:], 3), chunk(tx[10:], 4, "tx"))
        self.assertEqual([r["direction"] for r in self.frames()], ["tx"])
        self.assertTrue(any(r.get("code") == "incomplete_explicit_gap" for r in self.records()))

    def test_both_gap_breaks_both_and_eof_can_resume(self):
        rx, tx = rx_frame(), tx_frame()
        self.ingest(chunk(rx[:10]), chunk(tx[:10], 1, "tx"), gap(2),
                    chunk(rx[10:], 3), chunk(tx[10:], 4, "tx"), chunk(rx[:8], 5))
        self.assertEqual(self.frames(), [])
        tail = next(r for r in self.records() if r.get("code") == "incomplete_eof")
        self.assertTrue(tail["provisional"])
        self.ingest(chunk(rx[8:], 6))
        self.assertEqual(len(self.frames()), 1)
        self.assertFalse(any(r.get("code") == "incomplete_eof" for r in self.records()))

    def test_replay_restart_conflict_and_metadata(self):
        event = chunk(rx_frame())
        self.assertEqual(self.ingest(event), {"imported": 1, "replayed": 0})
        with closing(capture.open_archive(self.database, writable=True)) as reopened:
            self.assertEqual(capture.import_events(reopened, [json.dumps(event)]), {"imported": 0, "replayed": 1})
            for changed in (dict(event, data_hex="00"), dict(event, host_time_ns=999), dict(event, source_kind="uart")):
                with self.assertRaisesRegex(capture.CaptureError, "conflicting"):
                    capture.import_events(reopened, [json.dumps(changed)])
        for fields in ({"source_kind": "uart"}, {"firmware_sha256": "a" * 64}):
            with self.assertRaisesRegex(capture.CaptureError, "consistent"):
                self.ingest(chunk(b"x", 1, **fields))
        self.assertEqual(len(list(capture.iter_events(self.connection))), 1)
        for command in ("DELETE FROM events", "UPDATE events SET seq='10'"):
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                self.connection.execute(command)

    def test_sql_replace_cannot_overwrite_raw_event(self):
        event = chunk(rx_frame())
        self.ingest(event)
        original = self.connection.execute("SELECT event_json FROM events").fetchone()[0]
        for verb in ("INSERT OR REPLACE", "REPLACE"):
            with self.subTest(verb=verb), self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                self.connection.execute(
                    verb + " INTO events VALUES (?, ?, ?)",
                    (event["session_id"], str(event["seq"]), json.dumps(dict(event, data_hex="ff"))),
                )
            self.assertEqual(self.connection.execute("SELECT event_json FROM events").fetchone()[0], original)
        self.assertEqual(list(capture.iter_events(self.connection)), [event])

    def test_validation_rejects_unknown_malformed_and_boolean_values(self):
        valid = chunk(b"x")
        invalid = [dict(valid, seq=True), dict(valid, seq=-1), dict(valid, host_time_ns=True),
                   dict(valid, host_time_ns=-1), dict(valid, schema_version=True), dict(valid, schema_version=2),
                   dict(valid, source_kind="device"), dict(valid, session_id=" "), dict(valid, direction="both"),
                   dict(valid, firmware_sha256="g" * 64), dict(valid, data_hex=""), dict(valid, data_hex="1"),
                   dict(valid, data_hex="aa bb"), dict(valid, surprise=1), dict(valid, reason="unneeded"),
                   dict(gap(0), reason=""), dict(gap(0), data_hex="00"), dict(valid, kind="frame")]
        invalid.append({k: v for k, v in valid.items() if k != "host_time_ns"})
        for event in invalid:
            with self.subTest(event=event), self.assertRaises(capture.CaptureError):
                self.ingest(event)
        for raw in ('{}', '[]', '{', '', '{"kind":"gap","kind":"chunk"}'):
            with self.subTest(raw=raw), self.assertRaises(capture.CaptureError):
                capture.import_events(self.connection, [raw])
        self.assertEqual(list(capture.iter_events(self.connection)), [])

    def test_clock_reversal_ignores_null_and_keeps_order(self):
        self.ingest(chunk(b"x", 0, host_time_ns=100), chunk(b"x", 1, host_time_ns=None),
                    chunk(b"x", 2, host_time_ns=90))
        record = next(r for r in self.records() if r.get("code") == "timestamp_reversal")
        self.assertEqual(record["decoded"]["previous_host_time_ns"], 100)
        self.assertEqual([e["seq"] for e in capture.iter_events(self.connection)], [0, 1, 2])

    def test_nonfinite_flow_preserves_bits_and_exports_standard_json(self):
        for seq, bits in enumerate((0x7f800000, 0xff800000, 0x7fc12345)):
            frame = bytearray(rx_frame())
            frame[9:13] = struct.pack("<I", bits)
            frame[26] = sum(frame[:26]) & 255
            frame[27] = frame[26] ^ 255
            self.ingest(chunk(frame, seq))
        for record in self.frames():
            self.assertIsNone(record["decoded"]["flow_wire_float"])
            self.assertIn(record["decoded"]["flow_wire_bits_u32le"], (0x7f800000, 0xff800000, 0x7fc12345))
        output = io.StringIO()
        capture.write_export(self.records(), "jsonl", output)
        self.assertNotIn("NaN", output.getvalue())
        self.assertNotIn("Infinity", output.getvalue())
        self.assertEqual(sum(r.get("code") == "nonfinite_flow" for r in self.records()), 3)

    def test_unbounded_python_sequence_integers_remain_ordered(self):
        self.ingest(chunk(b"x", 9), chunk(b"y", 10), chunk(b"z", 2**80, host_time_ns=2**90))
        self.assertEqual([e["seq"] for e in capture.iter_events(self.connection)], [9, 10, 2**80])

    def test_inspect_missing_database_is_read_only(self):
        missing = self.database.with_name("absent.sqlite")
        with self.assertRaises(sqlite3.OperationalError):
            capture.open_archive(missing)
        self.assertFalse(missing.exists())

    def cli(self, *arguments, input=None):
        return subprocess.run([sys.executable, str(ROOT / "scripts/kd360s_capture.py"), *map(str, arguments)],
                              input=input, text=True, capture_output=True)

    def test_cli_prefix_durability_inspect_and_actual_exports(self):
        event = chunk(rx_frame())
        result = self.cli("import", self.database, "-", input=json.dumps(event) + "\n{broken\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("line 2", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        replay = self.cli("import", self.database, "-", input=json.dumps(event) + "\n")
        self.assertEqual(json.loads(replay.stdout), {"imported": 0, "replayed": 1})
        summary = self.cli("inspect", self.database)
        self.assertEqual(summary.returncode, 0, summary.stderr)
        self.assertEqual(json.loads(summary.stdout)["sessions"][0]["bytes"], 28)
        exported = self.cli("export", self.database, "--format", "jsonl")
        self.assertEqual(exported.returncode, 0, exported.stderr)
        records = [json.loads(line) for line in exported.stdout.splitlines()]
        self.assertEqual(records[0], {"record_type": "raw_event", **event})
        self.assertEqual(records[1]["decoded"]["raw_flags_u16le"], 0x1234)
        exported = self.cli("export", self.database, "--format", "csv", "--session", "SYNTHETIC")
        self.assertEqual(exported.returncode, 0, exported.stderr)
        rows = list(csv.DictReader(io.StringIO(exported.stdout)))
        self.assertEqual(json.loads(rows[0]["event_json"]), event)
        self.assertEqual(json.loads(rows[1]["decoded_json"])["flow_wire_float"], 1.25)
        self.assertEqual(rows[1]["raw_hex"], rx_frame().hex())
        absent = self.cli("export", self.database, "--format", "jsonl", "--session", "absent")
        self.assertEqual(absent.stdout, "")

    def test_example_is_synthetic_and_imports(self):
        result = self.cli("import", self.database, ROOT / "examples/kd360s-synthetic.jsonl")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(e["source_kind"] == "synthetic" for e in capture.iter_events(self.connection)))
        self.assertEqual(len(self.frames()), 2)


if __name__ == "__main__":
    unittest.main()
