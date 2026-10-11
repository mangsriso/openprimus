#!/usr/bin/env python3
"""Archive WPS1 observations and WPS2 recorder events without discarding raw bytes."""

from __future__ import annotations

import argparse
from contextlib import closing
import csv
import errno
import io
import json
import math
from pathlib import Path
import socket
import sqlite3
import struct
import sys
import tempfile
import threading
import time

PACKET = struct.Struct("<4sBBHQ8IH4BHII")
ACK = struct.Struct("<4sQI")
PORT = 47653
APPLICATION_ID = 0x57505331
TRANSIENT_NETWORK_ERRORS = {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN,
                            errno.ENOBUFS, errno.EAGAIN, errno.ECONNREFUSED}
TABLE_SQL = """
CREATE TABLE snapshots (
    session TEXT NOT NULL,
    seq INTEGER NOT NULL,
    received_ns INTEGER NOT NULL,
    sender_ip TEXT NOT NULL,
    sender_port INTEGER NOT NULL,
    packet BLOB NOT NULL CHECK(length(packet) IN (64, 128)),
    PRIMARY KEY(session, seq)
);
"""
IMMUTABILITY_SQL = """
CREATE TRIGGER immutable_replace BEFORE INSERT ON snapshots
WHEN EXISTS (SELECT 1 FROM snapshots WHERE session=NEW.session AND seq=NEW.seq)
BEGIN SELECT RAISE(ABORT, 'raw snapshots are immutable'); END;
CREATE TRIGGER immutable_update BEFORE UPDATE ON snapshots
BEGIN SELECT RAISE(ABORT, 'raw snapshots are immutable'); END;
CREATE TRIGGER immutable_delete BEFORE DELETE ON snapshots
BEGIN SELECT RAISE(ABORT, 'raw snapshots are immutable'); END;
"""
SCHEMA = "BEGIN IMMEDIATE;" + TABLE_SQL + IMMUTABILITY_SQL + """
PRAGMA application_id = 1464881969;
PRAGMA user_version = 2;
COMMIT;
"""
KINDS = {1: "observation", 2: "start", 3: "stop", 4: "pressure", 5: "accepted_rx"}
V2_COLUMNS = ["protocol", "kind", "shot_id", "pressure_generation", "pressure_acquired_ms",
              "rx_generation", "rx_accepted_ms", "start_argument", "calibrated_pressure_bits",
              "calibrated_pressure", "filtered_adc_bits", "accepted_rx_hex", "boundary_anomaly",
              "counter_saturated", "pressure_observed", "rx_observed", "buffer_record_capacity"]


class ProtocolError(ValueError):
    """Malformed or unsupported datagram."""


class ConflictError(ValueError):
    """An existing identity was reused with different bytes."""


def decode(packet):
    if len(packet) not in (PACKET.size, 128):
        raise ProtocolError("record length must be 64 or 128 bytes")
    (magic, version, kind, length, session, seq, observed, dropped, loop_ms,
     pressure, flow, target_pressure, target_flow, volume, mode, state, active,
     pending_stop, diagnostic, flags, reserved) = PACKET.unpack(packet[:64])
    if len(packet) == 64:
        if (magic, version, kind, length) != (b"WPS1", 1, 1, 64):
            raise ProtocolError("unsupported snapshot header")
        if flags != 1 or reserved != 0:
            raise ProtocolError("freshness-unknown flag required; other flags/reserved unsupported")
    elif ((magic, version, length) != (b"WPS2", 2, 128) or kind not in KINDS
          or flags & ~63 or not flags & 1):
        raise ProtocolError("unsupported recorder header or flags")
    result = {
        "session": f"{session:016x}", "seq": seq, "observation_ms": observed,
        "dropped_cumulative": dropped, "loop_duration_ms": loop_ms,
        "volume_raw": volume, "mode_raw": mode, "state_raw": state,
        "active_raw": active, "pending_stop_raw": pending_stop,
        "diagnostic_raw": diagnostic, "flags": flags, "freshness": "unknown",
        "raw_hex": packet.hex(),
    }
    for name, bits in (("pressure", pressure), ("flow", flow),
                       ("target_pressure", target_pressure), ("target_flow", target_flow)):
        value = struct.unpack("<f", struct.pack("<I", bits))[0]
        result[name + "_bits"] = bits
        result[name] = value if math.isfinite(value) else None
    if len(packet) == 128:
        capacity, = struct.unpack_from("<I", packet, 124)
        if capacity and (not 128 <= capacity <= 8192 or capacity & (capacity - 1)):
            raise ProtocolError("invalid recorder buffer capacity")
        pressure_seq, pressure_ms, rx_seq, rx_ms, start_arg, calibrated, adc, rx_size = struct.unpack_from("<8I", packet, 64)
        if (bool(flags & 2) != bool(pressure_seq) or bool(flags & 4) != bool(rx_seq)
                or (kind == 4 and not flags & 2) or (kind == 5 and not flags & 4)):
            raise ProtocolError("source kind, presence flags and generations disagree")
        if rx_size != (28 if flags & 4 else 0) or (not flags & 4 and any(packet[96:124])):
            raise ProtocolError("accepted RX presence and byte count disagree")
        value = struct.unpack("<f", struct.pack("<I", calibrated))[0]
        result.update(protocol="WPS2", kind=KINDS[kind], shot_id=reserved,
                      pressure_generation=pressure_seq, pressure_acquired_ms=pressure_ms,
                      rx_generation=rx_seq, rx_accepted_ms=rx_ms, start_argument=start_arg,
                      calibrated_pressure_bits=calibrated,
                      calibrated_pressure=value if flags & 2 and math.isfinite(value) else None,
                      filtered_adc_bits=adc, accepted_rx_hex=packet[96:96 + rx_size].hex(),
                      boundary_anomaly=bool(flags & 24), counter_saturated=bool(flags & 32),
                      pressure_observed=bool(flags & 2), rx_observed=bool(flags & 4),
                      buffer_record_capacity=capacity or None,
                      freshness="source_hook_invocations")
    return result


def open_archive(path, writable=False):
    path = Path(path).resolve()
    connection = sqlite3.connect(path.as_uri() + ("?mode=rwc" if writable else "?mode=ro"), uri=True)
    try:
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA fullfsync=ON")
        application, = connection.execute("PRAGMA application_id").fetchone()
        version, = connection.execute("PRAGMA user_version").fetchone()
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if writable and application == 0 and version == 0 and not tables:
            connection.executescript(SCHEMA)
        elif application != APPLICATION_ID or version not in (1, 2):
            raise ValueError("not a supported WPS snapshot archive")
        elif version == 1 and writable:
            connection.executescript("""
                BEGIN IMMEDIATE;
                DROP TRIGGER immutable_replace;
                DROP TRIGGER immutable_update;
                DROP TRIGGER immutable_delete;
                ALTER TABLE snapshots RENAME TO snapshots_v1;
            """ + TABLE_SQL + "INSERT INTO snapshots SELECT * FROM snapshots_v1; DROP TABLE snapshots_v1;"
                + IMMUTABILITY_SQL + "PRAGMA user_version=2; COMMIT;")
        connection.execute("SELECT session,seq,received_ns,sender_ip,sender_port,packet FROM snapshots LIMIT 0")
        return connection
    except Exception:
        connection.close()
        raise


def record(connection, packet, sender, received_ns=None):
    """Return an ACK only once the insert or identical replay transaction commits."""
    snapshot = decode(packet)
    identity = (snapshot["session"], snapshot["seq"])
    try:
        connection.execute("BEGIN IMMEDIATE")
        previous = connection.execute(
            "SELECT packet FROM snapshots WHERE session=? AND seq=?", identity).fetchone()
        if previous is not None:
            if previous[0] != packet:
                raise ConflictError("conflicting snapshot identity")
        else:
            connection.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?)", (
                *identity, time.time_ns() if received_ns is None else received_ns,
                sender[0], sender[1], packet))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return ACK.pack(b"WPA1", int(identity[0], 16), identity[1])


def receive_one(connection, udp):
    packet, sender = udp.recvfrom(65535)
    try:
        ack = record(connection, packet, sender)
    except (ProtocolError, ConflictError) as error:
        return {"result": "rejected", "reason": str(error)}
    try:
        udp.sendto(ack, sender)
    except OSError as error:
        if error.errno not in TRANSIENT_NETWORK_ERRORS:
            raise
        return {"result": "ack_deferred", "reason": str(error)}
    return {"result": "acknowledged"}


def serve(database, bind, port, stop=None, ready=None):
    with closing(open_archive(database, writable=True)) as connection, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.bind((bind, port))
        udp.settimeout(0.2)
        if ready is not None:
            ready(udp.getsockname())
        else:
            print(json.dumps({"listening": list(udp.getsockname()), "database": str(database),
                              "protocols": ["WPS1", "WPS2"]}), flush=True)
        while stop is None or not stop.is_set():
            try:
                result = receive_one(connection, udp)
            except socket.timeout:
                continue
            except OSError as error:
                if error.errno not in TRANSIENT_NETWORK_ERRORS:
                    raise
                time.sleep(0.05)
                continue
            if result["result"] != "acknowledged" and ready is None:
                print(json.dumps(result), file=sys.stderr, flush=True)


def iter_snapshots(connection, session=None):
    sql = "SELECT packet,received_ns,sender_ip,sender_port FROM snapshots"
    arguments = ()
    if session is not None:
        sql += " WHERE session=?"
        arguments = (session,)
    sql += " ORDER BY session,seq"
    for packet, stamp, ip, port in connection.execute(sql, arguments):
        yield {**decode(packet), "received_ns": stamp, "sender_ip": ip, "sender_port": port}


def write_export(connection, format_name, output, session=None):
    rows = iter_snapshots(connection, session)
    if format_name == "jsonl":
        for row in rows:
            output.write(json.dumps(row, allow_nan=False, sort_keys=True) + "\n")
    else:
        columns = list(decode(synthetic_packet())) + V2_COLUMNS + ["received_ns", "sender_ip", "sender_port"]
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def iter_shots(connection, session=None):
    """Summarize observed WPS2 boundaries; never infer unseen shots or power-loss data."""
    shot = None
    previous = None
    for row in iter_snapshots(connection, session):
        if row.get("protocol") != "WPS2" or row["shot_id"] == 0:
            continue
        identity = (row["session"], row["shot_id"])
        if shot is None or identity != (shot["session"], shot["shot_id"]):
            if shot is not None:
                yield _finish_shot(shot)
            shot = {"session": row["session"], "shot_id": row["shot_id"], "start_seq": None,
                    "stop_seq": None, "start_ms": None, "stop_ms": None, "starts": 0, "stops": 0,
                    "pressure_records": 0, "rx_records": 0, "missing_records": 0,
                    "reported_drops": 0, "boundary_anomaly": False, "counter_saturated": False,
                    "preceding_gap": bool(previous and previous["session"] == row["session"]
                                          and row["seq"] != previous["seq"] + 1)}
        if shot["stop_seq"] is None:
            if previous and previous["session"] == row["session"] and previous["shot_id"] == row["shot_id"]:
                shot["missing_records"] += max(0, row["seq"] - previous["seq"] - 1)
                shot["reported_drops"] += max(0, row["dropped_cumulative"] - previous["dropped_cumulative"])
            shot["pressure_records"] += row["kind"] == "pressure"
            shot["rx_records"] += row["kind"] == "accepted_rx"
            shot["boundary_anomaly"] |= row["boundary_anomaly"]
            shot["counter_saturated"] |= row["counter_saturated"]
        if row["kind"] == "start":
            shot["starts"] += 1
            if shot["start_seq"] is None:
                shot["start_seq"], shot["start_ms"] = row["seq"], row["observation_ms"]
        if row["kind"] == "stop":
            shot["stops"] += 1
            shot["stop_seq"], shot["stop_ms"] = row["seq"], row["observation_ms"]
        previous = row
    if shot is not None:
        yield _finish_shot(shot)


def _finish_shot(shot):
    reasons = []
    if shot["starts"] != 1:
        reasons.append("missing_or_repeated_start")
    if shot["stops"] != 1:
        reasons.append("missing_or_repeated_stop")
    if shot["start_seq"] is not None and shot["stop_seq"] is not None and shot["start_seq"] >= shot["stop_seq"]:
        reasons.append("boundary_order")
    for field in ("missing_records", "reported_drops", "boundary_anomaly", "counter_saturated", "preceding_gap"):
        if shot[field]:
            reasons.append(field)
    return {**shot, "coverage": "incomplete_or_uncertain" if reasons else "boundaries_observed_no_reported_gaps",
            "reasons": reasons, "hardware_coverage_verified": False}


def synthetic_packet(seq=0, session=0xfedcba9876543210):
    """Synthetic fixture, never a claimed hardware observation."""
    return PACKET.pack(b"WPS1", 1, 1, 64, session, seq, (seq * 100) & 0xffffffff, 0, 1,
                       0x41100000, 0x3f800000, 0x41100000, 0x3f800000,
                       0, 0, 0, 0, 0, 0, 1, 0)


def self_test():
    """Exercise the real UDP receiver on loopback, with an isolated temporary DB."""
    with tempfile.TemporaryDirectory(prefix="kd360s-live-test-") as temporary:
        database = Path(temporary) / "synthetic.sqlite"
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.bind(("127.0.0.1", 0))
            client.settimeout(0.3)
            for restart in (False, True):
                stop, ready = threading.Event(), threading.Event()
                address, errors = [], []

                def listen():
                    try:
                        serve(database, "127.0.0.1", 0, stop,
                              lambda value: (address.append(value), ready.set()))
                    except Exception as error:
                        errors.append(error)
                        ready.set()

                worker = threading.Thread(target=listen, name="synthetic-loopback")
                worker.start()
                try:
                    if not ready.wait(5) or errors:
                        raise RuntimeError(f"self-test receiver failed: {errors}")
                    packet = synthetic_packet()
                    expected = ACK.pack(b"WPA1", 0xfedcba9876543210, 0)
                    client.sendto(packet, address[0])
                    if client.recvfrom(1024)[0] != expected:
                        raise RuntimeError("incorrect ACK")
                    # Treat this ACK as lost at the sender and retransmit identical bytes.
                    client.sendto(packet, address[0])
                    if client.recvfrom(1024)[0] != expected:
                        raise RuntimeError("replay was not acknowledged")
                    conflict = bytearray(packet)
                    conflict[20] ^= 1
                    for invalid in (bytes(conflict), packet + b"x"):
                        client.sendto(invalid, address[0])
                        try:
                            client.recvfrom(1024)
                        except socket.timeout:
                            pass
                        else:
                            raise RuntimeError("rejected packet received an ACK")
                    if restart:
                        client.sendto(synthetic_packet(1), address[0])
                        if client.recvfrom(1024)[0] != ACK.pack(b"WPA1", 0xfedcba9876543210, 1):
                            raise RuntimeError("new packet after restart failed")
                finally:
                    stop.set()
                    worker.join(6)
                if worker.is_alive() or errors:
                    raise RuntimeError(f"self-test receiver failed: {errors}")
            with closing(open_archive(database)) as connection:
                rows = list(iter_snapshots(connection))
                if [row["seq"] for row in rows] != [0, 1]:
                    raise RuntimeError("replay/restart archive mismatch")
                for format_name in ("jsonl", "csv"):
                    output = io.StringIO()
                    write_export(connection, format_name, output)
                    if not output.getvalue():
                        raise RuntimeError("empty export")
    return {"passed": True, "source": "synthetic_loopback_only", "records": 2,
            "checks": ["udp", "lost_ack_retry", "duplicate", "conflict_no_ack",
                       "invalid_no_ack", "collector_restart", "jsonl", "csv"],
            "hardware_verified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listener = commands.add_parser("serve", aliases=["run"], help="receive UDP snapshots; Ctrl-C to stop")
    listener.add_argument("database", type=Path)
    listener.add_argument("--bind", default="127.0.0.1", help="IPv4 address; default loopback only")
    listener.add_argument("--port", type=int, default=PORT)
    export = commands.add_parser("export")
    export.add_argument("database", type=Path)
    export.add_argument("--format", choices=("csv", "jsonl"), default="jsonl")
    export.add_argument("--session", help="16 lowercase hexadecimal digits")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("database", type=Path)
    shots = commands.add_parser("shots", help="summarize WPS2 shot boundaries and observed loss")
    shots.add_argument("database", type=Path)
    shots.add_argument("--session")
    commands.add_parser("self-test", help="synthetic loopback test, no machine access")
    args = parser.parse_args(argv)
    try:
        if args.command in ("serve", "run"):
            if not 0 <= args.port <= 65535:
                raise ValueError("port must be between 0 and 65535")
            serve(args.database, args.bind, args.port)
        elif args.command == "self-test":
            print(json.dumps(self_test(), sort_keys=True))
        else:
            with closing(open_archive(args.database)) as connection:
                if args.command == "export":
                    write_export(connection, args.format, sys.stdout, args.session)
                elif args.command == "shots":
                    for shot in iter_shots(connection, args.session):
                        print(json.dumps(shot, sort_keys=True))
                else:
                    sessions = connection.execute(
                        "SELECT session,COUNT(*),MIN(seq),MAX(seq) FROM snapshots GROUP BY session ORDER BY session")
                    print(json.dumps({"protocols": ["WPS1", "WPS2"], "sessions": [
                        {"session": session, "records": count, "first_seq": first, "last_seq": last}
                        for session, count, first, last in sessions]}))
    except KeyboardInterrupt:
        return 0
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
