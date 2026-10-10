# KD360S continuous shot capture roadmap

## Decision: instrument stock firmware for persistent export

Build on openprimus's analysis tools and documentation, with model-specific
addresses and evidence. The immediate goal is to capture every new shot's actual
measurements. Implementing an entire replacement controller first would add
unverified actuation, display and calibration work before proving data export.

The owner accepts modified firmware. Prioritize a reproducible stock-binary
instrumentation proof that connects during normal operation and exports actual
measurements. Passive UART capture is supporting validation, not a prerequisite
for this offline work. See the [firmware instrumentation plan](KD360S_INSTRUMENTATION.md)
for candidate hooks, connection work and the first hardware experiment.

Baseline reviewed: upstream commit `33e061054d80e72c24fe27e2a3ab973a850188b1`.
Its [roadmap](../README.md#-status--roadmap) targets KD360X replacement firmware.
`firmware/components/primus_brew/__init__.py` is a schema scaffold; no C++ brew
controller is present there. `firmware/packages/common.yaml` provides simulated
readings. `chart.yaml` is a RAM display buffer, not a durable shot recorder.

| Upstream work | Priority for Slim capture | Next proof |
| --- | --- | --- |
| ESP image / Ghidra workflow | First; adapted here | Reproduce the exact Slim image and reject wrong artifacts. |
| StateControl inner loop / PID investigation | Next, focusing on actual readings and lifecycle first | Trace writers, units, freshness and all end/abort transitions in Slim. PID gains alone do not export shots. |
| Sensor / actuator mapping | Investigate data sources now; physical confirmation later | Distinguish ADC pressure from UART flow/volume; verify units against hardware. |
| RGB display pins / remaining LVGL screens | Later | Required for a full replacement UI, not for proving a recorder input. |
| Real-time `primus_brew` controller | Later unless data access requires it | Confirm complete control behavior and hardware before considering deployment. |
| Gauges / charts | Reuse after recording works | Drive displays from stored actual measurements rather than simulator values. |

## Current status

The pinned firmware workflow is complete. The next offline milestone now includes
an [RX/TX contract](KD360S_UART.md), a [bounded lifecycle table](KD360S_LIFECYCLE.md),
and a tested [raw capture archive and decoder](KD360S_CAPTURE_TOOL.md). Protocol
review found float32 flow and a TX checksum loop that pseudocode misrepresented.
A later diagnostic override also disproved a proposed direct mapping from TX
activity code to brew-active state, so the decoder exports raw codes only.

Live acquisition has no hardware-validated source yet. The known passive
UART mapping lacks numeric actual pressure and unambiguous shot boundaries. The
archive is useful offline but does not complete the every-shot recorder or the
hardware validation stages below. An [experimental snapshot image](KD360S_SNAPSHOT_TEST.md) has now been built
and verified offline. It has not been flashed or tested on the machine.

The next instrumentation milestone now has a
[runtime import map](KD360S_RUNTIME_IMPORTS.md), a compiled
[inert task-entry probe](../experiments/kd360s_entry_probe/README.md), and a
read-only `scripts/kd360s_instrumentation.py` audit. The probe's instructions
were checked with GNU objdump and Ghidra. It does not produce an OTA image or
connect Wi-Fi. Post-setup initialization, worker ownership and runtime memory
remain to be established before the telemetry payload.

## Ordered investigation

1. **Reproducible baseline (this change):** pinned download, verify and extraction;
   Slim network/data/NVS findings; portable Ghidra helpers. No recorder yet.
2. **Decode the data contract offline:** follow `4200545c`, `42004210`, `42003da4`,
   `42005918` and `420088d8`. Produce a byte-field/scaling table, a table of actual
   value writers, and complete start/end/abort transitions for supported modes.
   Reimplement only the observed frame decoder with synthetic fixtures and
   label unknowns explicitly. Do not invent captured samples.
3. **Prove stock-firmware instrumentation offline:** establish the independent
   runtime Wi-Fi connection path, executable payload placement, calling convention
   and snapshot/lifecycle hooks. Produce a reproducible patch with exact input and
   hook-byte checks before planning a deployment. Keeping Wi-Fi connected alone
   does not provide an exporter. Do not call the blocking updater from the sampler.
4. **Build the recorder alongside the producer contract:** durable append, shot IDs,
   timestamps, sequence numbers, explicit gaps, reconnect/replay deduplication,
   and CSV/JSON export. Exercise these with a synthetic producer while integrating
   the real source. UI consumes the archive rather than owning persistence.
5. **Validate on the machine when available:** cross-check units/timing and all
   supported brew modes, aborted shots, reconnects, recorder restart and buffering
   limits. Document exactly which outages can be recovered.

## Data contract to establish

Keep targets separate from measured values. A sample needs a source session ID,
shot ID, monotonic time, sequence, actual pressure/flow/temperature/volume where
available, target values where useful, and validity/freshness per measurement.
Store source units and conversions alongside the firmware hash and decoder
version. Missing weight must remain missing unless a real scale supplies it;
integrated flow volume is not a measured beverage mass.

A shot needs start, end/abort reason and completeness status. Prefer lifecycle
transitions over arbitrary pressure thresholds; thresholds may miss preinfusion
or split a paused extraction. Do not choose an acquisition rate merely from the
25 ms scheduler tick: establish each producer's update cadence first.

To claim every shot, capture must run independently of the screen being shown
and retain data while the receiver is unavailable. Quantify buffer capacity and
loss behavior, including power loss. If a proposed transport lacks buffering or
replay, document its gaps rather than promising lossless collection.

## Verification gates

- A wrong model/version or corrupt image is rejected before extraction.
- Decoder fixtures exercise truncation, corruption and field scaling; a later
  physical capture must confirm that synthetic assumptions match the machine.
- Target changes never masquerade as actual samples, and repeated sensor values
  do not imply fresh samples without a producer timestamp or update indicator.
- Every supported start/end/abort mode produces one identifiable shot.
- Disconnects/restarts cause either complete replay without duplicates or an
  explicit incomplete record; an empty interval is not silently interpolated.

The existing NVS history is only ten compact profile records, so it cannot serve
as the durable raw archive. See [version-specific evidence](KD360S_1.0.13_HK.md).

## Design rationale

The first tool uses a fixed immutable target and verified image bytes before
extraction (Model the Domain), and is checked against synthetic malformed images
and the public pinned artifact (Prove It Works). A shared KD360X/KD360S downloader
was considered, but would change the legacy latest-version workflow without
helping establish Slim capture. Generalize when a second supported target needs it.
