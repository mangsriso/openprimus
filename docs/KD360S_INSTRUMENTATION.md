# KD360S stock-firmware instrumentation plan

Status: an [experimental snapshot package](KD360S_SNAPSHOT_TEST.md) now builds
and passes host tests. Modified-image boot and machine behavior are untested.

Offline progress: [runtime function matching](KD360S_RUNTIME_IMPORTS.md) now
identifies station begin/status/disconnect and task creation. An
[inert entry probe](../experiments/kd360s_entry_probe/README.md) compiles to
18 bytes with the matching Xtensa toolchain. It calls the original task and
does not connect Wi-Fi or create a modified OTA image.

## Decision and evidence

Add continuous telemetry to the pinned stock application before attempting a
complete replacement controller. This route aims to retain existing control,
calibration and display behavior while adding a recorder. A binary patch can
still affect those functions; preserving behavior requires runtime measurement.

Target SHA-256:
`ac94398fde154cd8d29709e82990fd6dce53faa69b28e713f2c2a5dd4c65f5b4`.
All addresses below belong only to this KD360S 1.0.13_HK image.

| Route | Evidence and decision |
| --- | --- |
| Stock Wi-Fi/API | Inspected application paths provide onboarding and updates. No continuous shot export endpoint has been established. |
| Passive internal UART | Useful for validating raw traffic. The current mapping lacks numeric actual pressure and reliable shot boundaries. |
| Instrument stock binary | Preferred feasibility study. Actual values and lifecycle state have been located, but executable placement and network integration remain unproved. |
| Openprimus replacement | Fallback if instrumentation cannot be maintained or validated. Upstream KD360X code at `33e061054d80e72c24fe27e2a3ab973a850188b1` has a brew schema scaffold and simulated readings; it is not a working Slim controller. |

See [data and network evidence](KD360S_1.0.13_HK.md),
[UART findings](KD360S_UART.md) and [lifecycle findings](KD360S_LIFECYCLE.md).

## Intended runtime behavior

A short snapshot hook copies measurements into an owned, bounded buffer.
A separate worker connects to the configured LAN during normal operation,
reconnects after outages and sends records to a collector that durably stores them.
The control path must not wait for sockets, allocate memory per sample or run the
update workflow. Reading RAM periodically does not establish sensor freshness.

Records need boot/session identity, sequence, monotonic time, source-update
indicators, actual and target values, and explicit lifecycle events. Acknowledgment
must mean the collector has persisted the record. Replayed records retain their
identity. Buffer overflow must leave an explicit gap.

The existing offline UART archive can preserve supporting captures, but its wire
decoder is not a decoder for these proposed telemetry records. A telemetry
contract and importer can be developed alongside the producer using synthetic
events. This does not establish live acquisition.

RAM buffering covers only a bounded outage while powered. Power-loss survival
needs a separate storage design based on the device's partition map and available
space. Do not claim lossless recording across arbitrary outages. The collector
must remain available independently of a sleeping laptop, or the documented
device buffer must cover that interval.

## Candidate integration points

These are function-level investigation targets, not approved patch offsets.
Exact instruction boundaries, overwritten bytes and all relevant callers/writers
must be checked before selecting a trampoline.

| Location | Intended use | Remaining proof |
| --- | --- | --- |
| Main loop `4200cc98`, after actual values are copied | Snapshot control structure `3fc9d0b8`: actual pressure `+8`, actual flow `+0x10`, targets `+4/+0x0c` | Exact hook instruction, coherent sample and execution cost |
| RX parser `4200545c` | Advance RX freshness sequence after an accepted frame | Acquisition timestamp and parser acceptance behavior |
| Start `42005918` | Record start after initialization, including mode and start parameter separately | Normal, diagnostic and resume callers |
| State handling `420088d8`, stop helper `420031b0` | Record final values and stop/abort before reset | Diagnostic bypass, hold/resume and every supported abort path |

Direct flags at `3fc9ca60`, mode at `3fc9cfe1`, machine state at `3fc9ce52`
and diagnostic state at `3fc9ff64` can support lifecycle events. Polling flags can
miss short transitions. The final UART TX activity code is not a brew-active
Boolean because diagnostic logic can overwrite it.

## Establish the connection path first

The updater at `4201d334` contains candidate event registration `42056034`,
station connection `420561c0` and status `420561a0`. These identities are inferred
from call context; recover their arguments, object ownership and initialization
requirements before calling them from injected code.

The stock main loop already calls this routine, with flags controlling its
branches. Activating its connection/update branch during brewing can block
sampling with UI progress delays and synchronous manifest retrieval.
Removing the disconnect at `4201bd80` alone supplies neither a
boot connection nor reconnect, buffering or data export. Other disconnect paths
also need review; see the
[network call inventory](../analysis/kd360s/1.0.13_HK/application-network-calls.tsv).

Next offline work should recover those callees and startup initialization, then
identify socket/task APIs with verified calling conventions. Prefer an outbound
connection to a collector. Avoid relying on unknown C++ object layouts and
callback lifetimes merely because a similar SDK exposes the same methods.

## Offline proof artifact

The locally reproducible patch package must demonstrate:

1. Exact input hash, expected bytes at every patch site, import addresses and a
   reverse patch. A different input must fail before any output is produced.
2. Xtensa windowed ABI compatibility, register/stack preservation, relocated
   instructions and literals, and return paths checked in disassembly.
3. Executable placement with valid IROM/DROM mapping and alignment. An unused
   byte pattern does not prove free executable space or free runtime RAM.
4. Owned buffer allocation and worker lifetime. Host tests should cover lifecycle,
   stale RX, overflow, reconnect and replay; they cannot establish device timing.
5. Independent image validation and an enumerated diff. Recomputed checksum and
   appended SHA establish image integrity, not permission to boot modified code.

Do not publish vendor binary copies or private device backups. If placement,
imports or ownership cannot be established, publish the exact blocker and evaluate
replacement firmware with the additional control-porting work made explicit.

## What the first hardware session must answer

Read-only identification and recovery preparation can precede a completed patch.
Confirm the board and running image, obtain the flash/partition/NVS backup where
device protections permit, inspect secure-boot/encryption state, and establish
the available recovery interface. An in-application DFU web page cannot by itself
recover an application that no longer boots.

The first modified-image experiment should prove a heartbeat from the normal
screen, connection after boot and reconnection after a network outage. Measure
heap/stack headroom, watchdog behavior and control-loop timing before treating
Wi-Fi as safe to run during brewing. Then validate sensor units, all supported
start/stop/abort paths and receiver-outage replay against real shots.

ESP-IDF rollback requires the corresponding bootloader configuration and image
state handling; it is not established for this machine. See Espressif's
[OTA documentation](https://docs.espressif.com/projects/esp-idf/en/v4.4.6/esp32s3/api-reference/system/ota.html)
and [image format](https://docs.espressif.com/projects/esptool/en/latest/esp32s3/advanced-topics/firmware-image-format.html).
