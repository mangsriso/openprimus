# Experimental Slim UDP collector

`scripts/kd360s_live.py` receives experimental WPS1 observations and WPS2 recorder events using Python 3.9+ and the standard library. It writes each accepted raw datagram to SQLite before acknowledging it. WPS2 carries explicit shot boundaries and source invocation counters. The collector does not connect to stock firmware or establish physical sensor freshness. The stock firmware must first be instrumented with a compatible sender.

## Test without a machine

```sh
python3 scripts/kd360s_live.py self-test
python3 -m unittest discover -s tests -p 'test_kd360s_live.py' -v
```

The self-test creates a temporary SQLite archive and a real UDP listener on loopback. It sends synthetic packets, discards an acknowledgment at the simulated sender, retries, checks duplicate replay and conflict rejection, restarts the collector, and exports both formats. It deletes its own archive and stops its listener. Success means the host transport/storage path passed; `hardware_verified` remains `false`.

## Receive and export

Start locally to check the command:

```sh
python3 scripts/kd360s_live.py serve snapshots.sqlite
```

The default address is `127.0.0.1:47653`. For a machine on the same LAN, explicitly replace `YOUR_MAC_LAN_IP` with the Mac's address and configure the firmware sender to use that address and port:

```sh
python3 scripts/kd360s_live.py serve snapshots.sqlite --bind YOUR_MAC_LAN_IP --port 47653
```

`run` is an alias for `serve`. Stop with Ctrl-C; rerun against the same database to resume. Accepted records persist individually. A storage failure stops the collector without acknowledging the failing packet. A packet committed immediately before a network failure can be replayed safely.

```sh
python3 scripts/kd360s_live.py inspect snapshots.sqlite
python3 scripts/kd360s_live.py shots snapshots.sqlite
python3 scripts/kd360s_live.py export snapshots.sqlite --format jsonl > snapshots.jsonl
python3 scripts/kd360s_live.py export snapshots.sqlite --format csv > snapshots.csv
```

Optionally filter an export with `--session fedcba9876543210`. Sessions are fixed-width hexadecimal text, preserving the full unsigned 64-bit nonce. Exports sort by session and sequence, not wall-clock chronology across sessions. CSV writes null numeric values as empty cells; JSONL writes them as `null`. Both retain the exact float bits and raw datagram.

This protocol has no authentication or encryption. Use a trusted LAN; any reachable sender can submit a valid snapshot or forge an ACK. Sender IP/port record where a packet arrived, not authenticated device identity. Do not expose the listener to the Internet.

## Wire contract

Every integer is little-endian. Legacy WPS1 observations are exactly 64 bytes.
Unsupported headers, flags, reserved values and lengths receive no acknowledgment.

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 4 | ASCII `WPS1` |
| 4 | 1 | Version `1` |
| 5 | 1 | Kind `1` |
| 6 | 2 | Length `64` |
| 8 | 8 | Session nonce |
| 16 | 4 | Sequence |
| 20 | 4 | Device observation time, milliseconds |
| 24 | 4 | Cumulative dropped-record counter |
| 28 | 4 | Loop duration, milliseconds |
| 32 | 4 | Pressure IEEE-754 float bits |
| 36 | 4 | Flow IEEE-754 float bits |
| 40 | 4 | Target pressure float bits |
| 44 | 4 | Target flow float bits |
| 48 | 2 | Raw volume |
| 50 | 1 | Raw mode |
| 51 | 1 | Raw machine state |
| 52 | 1 | Raw active flag |
| 53 | 1 | Raw pending-stop flag |
| 54 | 2 | Raw diagnostic value |
| 56 | 4 | Flags: exactly `1` (freshness unknown) |
| 60 | 4 | Reserved: exactly `0` |

The ACK is exactly 16 bytes: ASCII `WPA1`, session nonce (8 bytes), sequence (4 bytes). A sender should accept only the matching identity from its configured collector, retain the record until that ACK, and retry after timeout. It must choose a new session nonce at every boot and before sequence wrap. Loss of the device's RAM buffer, nonce collisions, or overflow can still lose records; this collector cannot repair those losses.

The primary key is `(session, sequence)`. An identical duplicate receives the same ACK without changing its first receive timestamp, sender metadata, or raw bytes. Different bytes with the same identity are rejected with no ACK. SQLite `synchronous=FULL` is set, and `fullfsync=ON` is requested where supported; durability still depends on the OS and storage honoring flushes. Table triggers prohibit update, delete and replacement of archived rows through normal SQL writes.

`observation_ms` is when the instrumentation observed memory, not a sensor acquisition timestamp. Values may be stale, counters may wrap, and polling may miss lifecycle transitions. The collector preserves raw mode/state/flags and makes no unit calibration, sample-rate guarantee, shot ID, or every-shot completeness claim.

## WPS2 recorder events

Records are 128 bytes. Offsets 0..59 use the same field positions, with magic
`WPS2`, version 2 and length 128. Kind is 1=observation, 2=start, 3=stop,
4=pressure calibration, 5=accepted RX. Flags are bit 0=provisional decoding,
bit 1=pressure observed, bit 2=RX observed, bit 3=unmatched stop,
bit 4=start while another shot was open, bit 5=saturated source/shot counter.

| Offset | Bytes | Meaning |
| --- | --- | --- |
| 60 | 4 | Shot ID, zero before the first observed start |
| 64 | 4 | Pressure hook generation |
| 68 | 4 | Pressure hook timestamp, milliseconds |
| 72 | 4 | Accepted RX hook generation |
| 76 | 4 | Accepted RX hook timestamp, milliseconds |
| 80 | 4 | Start argument |
| 84 | 4 | Calibrated pressure return, unchanged float bits |
| 88 | 4 | Calibration input argument, filtered ADC bits |
| 92 | 4 | Accepted RX bytes present, 0 or 28 |
| 96 | 28 | Accepted RX window, zero until first accepted RX |
| 124 | 4 | Reserved zero |

Source generations count calls even when values repeat. RX acceptance does not
prove a new physical conversion. The pressure callback runs before the caller
clamps and stores its return; use `calibrated_pressure` for that event's result,
not the common control-pressure field. Unobserved or nonfinite calibrated values
export as null, with raw bits preserved. WPS2 and WPS1 share the existing WPA1 ACK.

`shots` emits one JSON object per observed shot ID with start/stop sequences,
source record counts, gaps and anomalies. `boundaries_observed_no_reported_gaps`
means only that the archive contains those boundaries without observed loss.
It cannot detect a wholly missing shot, an unreported terminal overflow, or
records lost during power failure. A missing boundary or reported loss yields
`incomplete_or_uncertain`. Hardware coverage remains unverified.

Opening a version-1 archive for writing migrates it transactionally to accept
both lengths. Raw rows and arrival metadata are preserved, with all immutability
triggers restored before commit. Read-only access leaves version-1 archives
unchanged. Mixed exports use the same columns, with empty WPS2 fields for WPS1.
