# KD360S continuous shot capture roadmap

## Decision: reuse upstream, prioritize the data path

Build on openprimus's analysis tools and documentation, with model-specific
addresses and evidence. The immediate goal is to capture every new shot's actual
measurements. Implementing an entire replacement controller first would add
unverified actuation, display and calibration work before proving data export.

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

## Ordered investigation

1. **Reproducible baseline (this change):** pinned download, verify and extraction;
   Slim network/data/NVS findings; portable Ghidra helpers. No recorder yet.
2. **Decode the data contract offline:** follow `4200545c`, `42004210`, `42003da4`,
   `42005918` and `420088d8`. Produce a byte-field/scaling table, a table of actual
   value writers, and complete start/end/abort transitions for supported modes.
   Reimplement only the observed frame decoder with synthetic fixtures and
   label unknowns explicitly. Do not invent captured samples.
3. **Prove an export transport:** stock network paths inspected so far do not
   expose one. Evaluate debug output, passive internal UART and minimal firmware
   instrumentation against the required fields. Passive UART is not yet known
   to expose actual pressure. Instrumentation would need a separate validation
   plan for image acceptance, runtime impact and recovery before use on hardware.
4. **Build the recorder once a real source exists:** durable append, shot IDs,
   timestamps, sequence numbers, explicit gaps, reconnect/replay deduplication,
   and CSV/JSON export. UI consumes the archive rather than owning persistence.
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
