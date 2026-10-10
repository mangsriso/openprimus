#!/usr/bin/env python3
"""Receive experimental KD360S snapshots; freshness and shot coverage are unknown."""

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
SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE snapshots (
    session TEXT NOT NULL,
    seq INTEGER NOT NULL,
    received_ns INTEGER NOT NULL,
    sender_ip TEXT NOT NULL,
    sender_port INTEGER NOT NULL,
    packet BLOB NOT NULL CHECK(length(packet) = 64),
    PRIMARY KEY(session, seq)
);
CREATE TRIGGER immutable_replace BEFORE INSERT ON snapshots
WHEN EXISTS (SELECT 1 FROM snapshots WHERE session=NEW.session AND seq=NEW.seq)
BEGIN SELECT RAISE(ABORT, 'raw snapshots are immutable'); END;
CREATE TRIGGER immutable_update BEFORE UPDATE ON snapshots
BEGIN SELECT RAISE(ABORT, 'raw snapshots are immutable'); END;
CREATE TRIGGER immutable_delete BEFORE DELETE ON snapshots
BEGIN SELECT RAISE(ABORT, 'raw snapshots are immutable'); END;
PRAGMA application_id = 1464881969;
PRAGMA user_version = 1;
COMMIT;
"""


class ProtocolError(ValueError):
    """Malformed or unsupported datagram."""


class ConflictError(ValueError):
    """An existing identity was reused with different bytes."""


def decode(packet):
    if len(packet) != PACKET.size:
        raise ProtocolError("snapshot length must be 64 bytes")
    (magic, version, kind, length, session, seq, observed, dropped, loop_ms,
     pressure, flow, target_pressure, target_flow, volume, mode, state, active,
     pending_stop, diagnostic, flags, reserved) = PACKET.unpack(packet)
    if (magic, version, kind, length) != (b"WPS1", 1, 1, 64):
        raise ProtocolError("unsupported snapshot header")
    if flags != 1 or reserved != 0:
        raise ProtocolError("freshness-unknown flag required; other flags/reserved unsupported")
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
        elif application != APPLICATION_ID or version != 1:
            raise ValueError("not a supported WPS1 snapshot archive")
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
                              "protocol": "WPS1", "freshness": "unknown"}), flush=True)
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
        columns = list(decode(synthetic_packet())) + ["received_ns", "sender_ip", "sender_port"]
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


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
                else:
                    sessions = connection.execute(
                        "SELECT session,COUNT(*),MIN(seq),MAX(seq) FROM snapshots GROUP BY session ORDER BY session")
                    print(json.dumps({"protocol": "WPS1", "freshness": "unknown", "sessions": [
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
