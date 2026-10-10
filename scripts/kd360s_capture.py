#!/usr/bin/env python3
"""Archive transport events and export provisional KD360S decoding offline."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import sqlite3
import struct
import sys

DECODER = "kd360s-1.0.13-hk-provisional-v1"
PROFILE_SHA256 = "ac94398fde154cd8d29709e82990fd6dce53faa69b28e713f2c2a5dd4c65f5b4"
SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE events (
    session_id TEXT NOT NULL,
    seq TEXT NOT NULL,
    event_json TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);
CREATE INDEX event_order ON events (session_id, length(seq), seq);
CREATE TRIGGER immutable_replace BEFORE INSERT ON events
WHEN EXISTS (SELECT 1 FROM events WHERE session_id = NEW.session_id AND seq = NEW.seq)
BEGIN SELECT RAISE(ABORT, 'raw events are immutable'); END;
CREATE TRIGGER immutable_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'raw events are immutable'); END;
CREATE TRIGGER immutable_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'raw events are immutable'); END;
PRAGMA user_version = 1;
COMMIT;
"""


class CaptureError(ValueError):
    """Invalid capture input or archive contract."""


def validate_event(value):
    if not isinstance(value, dict):
        raise CaptureError("event must be an object")
    required = {"schema_version", "session_id", "seq", "kind", "direction",
                "host_time_ns", "source_kind"}
    kind = value.get("kind")
    if kind not in ("chunk", "gap"):
        raise CaptureError("kind must be chunk or gap")
    required.add("data_hex" if kind == "chunk" else "reason")
    if required - value.keys():
        raise CaptureError("missing fields: " + ", ".join(sorted(required - value.keys())))
    if value.keys() - required - {"firmware_sha256"}:
        raise CaptureError("unknown fields: " + ", ".join(sorted(value.keys() - required - {"firmware_sha256"})))
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise CaptureError("schema_version must be 1")
    if not isinstance(value["session_id"], str) or not value["session_id"].strip():
        raise CaptureError("session_id must be a nonempty string")
    if type(value["seq"]) is not int or value["seq"] < 0:
        raise CaptureError("seq must be a nonnegative integer")
    stamp = value["host_time_ns"]
    if stamp is not None and (type(stamp) is not int or stamp < 0):
        raise CaptureError("host_time_ns must be a nonnegative integer or null")
    if value["direction"] not in (("rx", "tx") if kind == "chunk" else ("rx", "tx", "both")):
        raise CaptureError("invalid direction")
    if value["source_kind"] not in ("synthetic", "uart", "unknown"):
        raise CaptureError("source_kind must be synthetic, uart or unknown")
    digest = value.get("firmware_sha256")
    if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)):
        raise CaptureError("firmware_sha256 must be 64 hex characters or null")
    if kind == "chunk":
        data = value["data_hex"]
        if not isinstance(data, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2})+", data):
            raise CaptureError("data_hex must be nonempty even-length hex")
    elif not isinstance(value["reason"], str) or not value["reason"].strip():
        raise CaptureError("gap reason must be a nonempty string")
    return value


def unique_fields(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise CaptureError("duplicate JSON field: " + key)
        value[key] = item
    return value


def open_archive(path, writable=False):
    path = Path(path).resolve()
    mode = "rwc" if writable else "ro"
    connection = sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True)
    try:
        connection.execute("PRAGMA synchronous = FULL")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0 and writable:
            tables = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            if tables:
                raise CaptureError("database is not an empty capture archive")
            connection.executescript(SCHEMA)
        elif version != 1:
            raise CaptureError("unsupported archive schema")
        connection.execute("SELECT session_id, seq, event_json FROM events LIMIT 0")
        return connection
    except Exception:
        connection.close()
        raise


def import_events(connection, source):
    added = replayed = 0
    for line_number, line in enumerate(source, 1):
        try:
            event = validate_event(json.loads(line, object_pairs_hook=unique_fields))
            encoded = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            session, seq = event["session_id"], str(event["seq"])
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                previous = connection.execute(
                    "SELECT event_json FROM events WHERE session_id=? AND seq=?", (session, seq)
                ).fetchone()
                if previous:
                    if previous[0] != encoded:
                        raise CaptureError("conflicting event identity")
                    replayed += 1
                    continue
                latest = connection.execute(
                    "SELECT seq, event_json FROM events WHERE session_id=? ORDER BY length(seq) DESC, seq DESC LIMIT 1",
                    (session,),
                ).fetchone()
                if latest:
                    if event["seq"] <= int(latest[0]):
                        raise CaptureError("new seq must be greater than the session maximum")
                    metadata = json.loads(latest[1])
                    if any(event.get(key) != metadata.get(key) for key in ("source_kind", "firmware_sha256")):
                        raise CaptureError("session source_kind and firmware_sha256 must stay consistent")
                connection.execute("INSERT INTO events VALUES (?, ?, ?)", (session, seq, encoded))
            added += 1
        except (ValueError, TypeError, RecursionError) as error:
            raise CaptureError(f"line {line_number}: {error}") from error
    return {"imported": added, "replayed": replayed}


def iter_events(connection, session=None):
    sql = "SELECT event_json FROM events"
    args = ()
    if session is not None:
        sql += " WHERE session_id=?"
        args = (session,)
    sql += " ORDER BY session_id, length(seq), seq"
    for row in connection.execute(sql, args):
        yield json.loads(row[0])


@dataclass(frozen=True)
class ByteRef:
    value: int
    seq: int
    offset: int
    host_time_ns: int | None


def byte_record(event, direction, data, record_type, code, checksum_status="not_checked", decoded=None):
    spans = []
    for byte in data:
        if spans and spans[-1]["seq"] == byte.seq and spans[-1]["end_offset"] == byte.offset:
            spans[-1]["end_offset"] += 1
        else:
            spans.append({"seq": byte.seq, "start_offset": byte.offset, "end_offset": byte.offset + 1})
    return {
        "record_type": record_type, "session_id": event["session_id"], "direction": direction,
        "start_seq": data[0].seq if data else event["seq"],
        "end_seq": data[-1].seq if data else event["seq"],
        "host_time_ns": data[0].host_time_ns if data else event["host_time_ns"],
        "raw_hex": bytes(byte.value for byte in data).hex(), "raw_spans": spans,
        "code": code, "checksum_status": checksum_status, "decoded": decoded or {},
        "decoder_version": DECODER, "profile_firmware_sha256": PROFILE_SHA256,
        "firmware_sha256": event.get("firmware_sha256"), "source_kind": event["source_kind"],
    }


def decode_rx(frame):
    flow = struct.unpack("<f", frame[9:13])[0]
    return {
        "flow_wire_float": flow if math.isfinite(flow) else None,
        "flow_wire_bits_u32le": int.from_bytes(frame[9:13], "little"),
        "raw_status": frame[2], "raw_flags_u16le": int.from_bytes(frame[3:5], "little"),
        "raw_temperature_5": frame[5], "raw_temperature_7": frame[7], "raw_temperature_8": frame[8],
        "raw_volume_u16le": int.from_bytes(frame[17:19], "little"),
        "raw_volume_shifted": int.from_bytes(frame[17:19], "little") >> 1,
        "flag_19_ascii_one": frame[19] == 0x31, "flag_20_ascii_one": frame[20] == 0x31,
        "flag_21_ascii_one": frame[21] == 0x31, "flag_22_ascii_one": frame[22] == 0x31,
        "raw_echo": frame[23], "raw_value_24_u16le": int.from_bytes(frame[24:26], "little"),
    }


def decode_tx(frame):
    return {"machine_state_raw": frame[2], "activity_code_raw": frame[3],
            "diagnostic_brew_distinguished": False}


class FrameStream:
    def __init__(self, direction):
        self.direction = direction
        self.pending = []

    def flush(self, event, reason, provisional=False):
        if self.pending:
            record = byte_record(event, self.direction, self.pending, "diagnostic", reason)
            record["provisional"] = provisional
            self.pending = []
            yield record

    def feed(self, event):
        marker, length, size = (0x67, 26, 28) if self.direction == "rx" else (0x76, 0x22, 37)
        for offset, value in enumerate(bytes.fromhex(event["data_hex"])):
            self.pending.append(ByteRef(value, event["seq"], offset, event["host_time_ns"]))
            while self.pending:
                if self.pending[0].value != marker:
                    yield byte_record(event, self.direction, self.pending[:1], "diagnostic", "noise")
                    del self.pending[0]
                elif len(self.pending) < 2:
                    break
                elif self.pending[1].value != length:
                    yield byte_record(event, self.direction, self.pending[:2], "diagnostic", "length_mismatch")
                    del self.pending[0]
                elif len(self.pending) < size:
                    break
                else:
                    frame = bytes(byte.value for byte in self.pending[:size])
                    checksum_offset = size - 2
                    if (sum(frame[:checksum_offset]) & 255 != frame[checksum_offset]
                            or frame[-1] != (frame[checksum_offset] ^ 255)):
                        yield byte_record(event, self.direction, self.pending[:size], "diagnostic", "checksum_mismatch", "invalid")
                        del self.pending[0]
                    else:
                        decoded = decode_rx(frame) if self.direction == "rx" else decode_tx(frame)
                        yield byte_record(event, self.direction, self.pending[:size], "frame",
                                          "provisional_" + self.direction, "valid", decoded)
                        if self.direction == "rx" and decoded["flow_wire_float"] is None:
                            yield byte_record(event, self.direction, self.pending[:size], "diagnostic", "nonfinite_flow", "valid")
                        del self.pending[:size]


def export_records(connection, session=None):
    current = None
    streams = {}
    previous_seq = previous_time = None
    last_event = None
    for event in iter_events(connection, session):
        if current != event["session_id"]:
            if last_event:
                for stream in streams.values():
                    yield from stream.flush(last_event, "incomplete_eof", provisional=True)
            current = event["session_id"]
            streams = {direction: FrameStream(direction) for direction in ("rx", "tx")}
            previous_seq = previous_time = None
        yield {"record_type": "raw_event", **event}
        if previous_seq is not None and event["seq"] != previous_seq + 1:
            for stream in streams.values():
                yield from stream.flush(event, "incomplete_sequence_gap")
            yield byte_record(event, "both", [], "diagnostic", "sequence_gap", decoded={
                "missing_start_seq": previous_seq + 1, "missing_end_seq": event["seq"] - 1})
        stamp = event["host_time_ns"]
        if stamp is not None:
            if previous_time is not None and stamp < previous_time:
                yield byte_record(event, event["direction"], [], "diagnostic", "timestamp_reversal",
                                  decoded={"previous_host_time_ns": previous_time})
            previous_time = stamp
        if event["kind"] == "gap":
            for direction, stream in streams.items():
                if event["direction"] in (direction, "both"):
                    yield from stream.flush(event, "incomplete_explicit_gap")
            yield byte_record(event, event["direction"], [], "diagnostic", "explicit_gap", decoded={"reason": event["reason"]})
        elif event.get("firmware_sha256") is not None and event["firmware_sha256"].lower() != PROFILE_SHA256:
            yield byte_record(event, event["direction"], [], "diagnostic", "profile_firmware_mismatch")
        else:
            yield from streams[event["direction"]].feed(event)
        previous_seq = event["seq"]
        last_event = event
    if last_event:
        for stream in streams.values():
            yield from stream.flush(last_event, "incomplete_eof", provisional=True)


def inspect_archive(connection):
    sessions = []
    current = None
    for event in iter_events(connection):
        if current is None or current["session_id"] != event["session_id"]:
            current = {"session_id": event["session_id"], "source_kind": event["source_kind"],
                       "firmware_sha256": event.get("firmware_sha256"), "events": 0,
                       "bytes": 0, "explicit_gaps": 0, "missing_sequences": 0,
                       "first_seq": event["seq"], "last_seq": event["seq"]}
            sessions.append(current)
        elif event["seq"] != current["last_seq"] + 1:
            current["missing_sequences"] += event["seq"] - current["last_seq"] - 1
        current["last_seq"] = event["seq"]
        current["events"] += 1
        current["bytes"] += len(event.get("data_hex", "")) // 2
        current["explicit_gaps"] += event["kind"] == "gap"
    return {"schema_version": 1, "decoder_version": DECODER, "sessions": sessions}


def write_export(records, format_name, output):
    if format_name == "jsonl":
        for record in records:
            output.write(json.dumps(record, sort_keys=True) + "\n")
        return
    columns = ["record_type", "direction", "session_id", "start_seq", "end_seq", "host_time_ns",
               "raw_hex", "checksum_status", "code", "source_kind", "firmware_sha256",
               "decoder_version", "profile_firmware_sha256", "provisional",
               "decoded_json", "raw_spans_json", "event_json"]
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for record in records:
        row = {key: record.get(key, "") for key in columns}
        if record["record_type"] == "raw_event":
            row.update(start_seq=record["seq"], end_seq=record["seq"], raw_hex=record.get("data_hex", ""),
                       event_json=json.dumps({key: value for key, value in record.items() if key != "record_type"}, sort_keys=True))
        else:
            row.update(decoded_json=json.dumps(record["decoded"], sort_keys=True),
                       raw_spans_json=json.dumps(record["raw_spans"], sort_keys=True))
        writer.writerow(row)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("import", help="append strict JSONL events; - reads stdin")
    ingest.add_argument("database", type=Path)
    ingest.add_argument("input")
    inspect = commands.add_parser("inspect", help="summarize an existing archive")
    inspect.add_argument("database", type=Path)
    export = commands.add_parser("export", help="write raw evidence and provisional decoding to stdout")
    export.add_argument("database", type=Path)
    export.add_argument("--format", choices=("jsonl", "csv"), required=True)
    export.add_argument("--session")
    args = parser.parse_args(argv)
    try:
        connection = open_archive(args.database, writable=args.command == "import")
        try:
            if args.command == "import":
                if args.input == "-":
                    result = import_events(connection, sys.stdin)
                else:
                    with open(args.input, encoding="utf-8") as source:
                        result = import_events(connection, source)
                print(json.dumps(result, sort_keys=True))
            elif args.command == "inspect":
                print(json.dumps(inspect_archive(connection), sort_keys=True))
            else:
                write_export(export_records(connection, args.session), args.format, sys.stdout)
        finally:
            connection.close()
    except (CaptureError, OSError, sqlite3.Error, UnicodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
