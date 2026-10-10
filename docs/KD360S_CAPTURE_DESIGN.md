# Offline capture design

This tool archives transport events for analysis. It is not yet an every-shot
recorder: no live Slim transport or complete lifecycle detector is validated.

## Shape and interface

Use Python 3.9+ and SQLite from the standard library. `scripts/kd360s_capture.py`
offers `import`, `inspect` and `export` commands. Input is JSONL from a file or
stdin, with immutable event identity `(session_id, seq)` across both directions.
A transport session is not an espresso shot. Each event retains source kind,
host timestamp, direction, raw bytes or an explicit gap reason.

Raw events are the source of truth. Decode on export with a versioned profile,
so corrected protocol interpretations do not rewrite captures. Replayed identical
events are ignored; conflicting identities fail instead of overwriting evidence.
Per-event SQLite transactions preserve the accepted prefix after an interrupted
import. Receiver-side losses cannot be recovered by database durability.

Framing buffers are isolated by session and direction. Sequence gaps and explicit
gap events stop reassembly across missing data. Host timestamps describe receipt,
not sensor sample time. Clock reversal is a diagnostic rather than a sort key.
All unrecognized, corrupt and incomplete bytes retain references to raw events.
An EOF tail remains provisional and can decode differently after later import.

## Alternatives and synthesis

Two independent candidates compared append-only JSONL archives against SQLite.
Both selected SQLite for transactions and replay conflict detection. Candidate A
keeps raw events and derives decoding at export. Candidate B also persists decoded
observations and checkpoints. An independent cross-judge selected A to avoid
projection migrations and dual-state consistency, and retained B's concise
import/inspect/export interface and diagnostics. Nullable sequence identities and
capture-file line numbers as fallback IDs were rejected: the source must supply
a stable session and event sequence to promise deduplication.

This applies Model the Domain by separating transport sessions, frames and shots.
Prove It Works requires CLI tests for split/corrupt input, replay, conflicts,
restart, gaps and exported values. Synthetic inputs remain labelled synthetic;
passing these tests does not verify acquisition from a machine.
