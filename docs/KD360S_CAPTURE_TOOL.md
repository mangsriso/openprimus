# Archive and decode offline UART captures

`scripts/kd360s_capture.py` stores raw transport events in SQLite and exports
provisional RX/TX decoding as JSONL or CSV. It uses Python 3.9+ standard libraries.
It does not connect to the machine, read a serial port, activate Wi-Fi, or flash
firmware. A capture adapter must supply the input events. No such adapter has
been validated with a physical KD360S yet.

The tool preserves evidence for later protocol corrections. It does not count
shots, infer brew-active state, or supply a numeric actual-pressure measurement.
Read the [UART contract](KD360S_UART.md) and [lifecycle limits](KD360S_LIFECYCLE.md)
before interpreting data.

## Try the synthetic example

```bash
mkdir -p captures
python3 scripts/kd360s_capture.py import captures/demo.sqlite examples/kd360s-synthetic.jsonl
python3 scripts/kd360s_capture.py inspect captures/demo.sqlite
python3 scripts/kd360s_capture.py export captures/demo.sqlite --format jsonl
python3 scripts/kd360s_capture.py export captures/demo.sqlite --format csv --session SYNTHETIC
```

The fixture contains five explicitly synthetic events. It produces two valid
frames, including a wire flow float of 1.25 with unverified units, an explicit
gap and an incomplete tail. These are test values, not machine measurements.
Re-importing the same example reports five replays and zero inserted events.
`captures/` and SQLite files are ignored by git.

Use `-` instead of an input filename to read JSONL from stdin. Each accepted
event commits separately. This supports incremental input from a future adapter;
it does not make the tool a serial receiver. Import prints its summary when the
input ends. Export writes to stdout. Shell redirection can save that stream, but
is not an atomic-file export mechanism.

## Input contract

One JSON object per line, with no blank lines or duplicate/unknown fields:

```json
{"schema_version":1,"session_id":"SYNTHETIC","seq":0,"kind":"chunk","direction":"rx","host_time_ns":0,"source_kind":"synthetic","data_hex":"671a","firmware_sha256":null}
```

| Field | Contract |
| --- | --- |
| `schema_version` | Integer 1 |
| `session_id` | Stable nonempty transport session identifier; not a shot ID |
| `seq` | Nonnegative integer event sequence across RX, TX and gap events together |
| `kind` | `chunk` or `gap` |
| `direction` | `rx` or `tx`; a gap may use `both` |
| `host_time_ns` | Nonnegative integer or null; host receipt time, not sensor sample time |
| `source_kind` | `synthetic`, `uart` or `unknown`; a source declaration, not attestation |
| `data_hex` | Chunk only: nonempty even-length hexadecimal bytes |
| `reason` | Gap only: nonempty description of missing data |
| `firmware_sha256` | Optional 64-digit hex hash or null; keep null when source firmware is unknown |

Keep source kind and firmware metadata constant within a session. Preserve
session ID, event sequence and event contents when replaying input. Starting a
receiver after an untracked interruption should create a new session, rather
than invent continuity. The tool does not synchronize different host clocks.

New events must have a sequence greater than that session's previous maximum.
Identical existing events are replayed without insertion; different contents
with the same identity fail. The archive retains a valid committed prefix if a
later input line fails. Import is not an all-or-nothing file transaction. It
rejects late backfilling of missing sequence numbers. Re-export after appending
new events can complete a previously incomplete EOF tail.

## Decode and export behavior

Raw events remain the source of truth. Decoding runs afresh during export and
uses `kd360s-1.0.13-hk-provisional-v1`. RX currently supports the assumed length
26 / total 28-byte profile; TX uses the instruction-verified 37-byte layout.
Known different source firmware hashes disable decoding and produce a mismatch
diagnostic. Unknown source hashes allow provisional decoding and remain null;
the separate profile hash does not certify the capture's origin.

Buffers are separate for each direction within a session. Missing event sequence
numbers break both buffers. Explicit gaps break the named direction or both.
Bad checksum/length candidates trigger bytewise resynchronization. Unknown,
corrupt and incomplete bytes remain in raw events, with diagnostic references.
Timestamp reversals produce diagnostics; sequence numbers determine event order.
The first observed sequence need not be zero, so an archive does not establish
what happened before capture began.

JSONL records have types `raw_event`, `frame`, or `diagnostic`. A frame refers to
source event/byte spans; span end offsets are exclusive. Its host timestamp comes
from the first contributing byte's chunk. Raw bytes and frame views overlap by
design: do not concatenate every exported row to reconstruct a stream. Use the
raw-event rows, ordered by session and sequence. Nonfinite wire floats export as
null with their original bits and a diagnostic.

CSV includes record type, provenance, raw hex, checksum status, `decoded_json`,
`raw_spans_json`, and `event_json`. It is an analysis export, not one row per shot.
No field is labelled pressure, mass, flow in ml/s or temperature in Celsius
without evidence. SQLite durability protects accepted events; it cannot recover
bytes the receiver missed or guarantee collection across machine power loss.

## Validation

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

The combined suite has 35 tests and passed on Python 3.9.6 and 3.12.14. Capture
tests cover every split point of the supported RX/TX frames, interleaving,
corruption/resynchronization, explicit/sequence gaps, replay/conflict, restart,
provenance and CLI exports. An independent review found that SQLite REPLACE could
bypass the original update/delete triggers. A before-insert trigger now rejects
existing identities, and a regression test checks that the original row survives.

The [CLI validation record](../analysis/kd360s/1.0.13_HK/capture-cli-validation.json)
records a separate subprocess run with the synthetic fixture. Firmware structure
and protocol instructions were inspected in the real pinned public image.
No physical UART trace or end-to-end machine recording has been validated.
