# Prepare a KD360S firmware trial

The offline trial targets only KD360S 1.0.13_HK application SHA-256
`ac94398fde154cd8d29709e82990fd6dce53faa69b28e713f2c2a5dd4c65f5b4`.
A matching version string on the display does not establish an identical installed binary.
The local packages are not cleared for installation. No machine has booted them.

Run the [offline preflight](KD360S_PREFLIGHT.md) to reproduce each package from
its retained ELF and inspect a private flash dump when available. This check
does not clear the device requirements below.

## Establish recovery before installing

Record the actual board and the recovery interface that works without the application.
The owner's stock DFU screen is an application web updater. Its availability while
the stock program runs does not demonstrate recovery from a broken program.
Do not infer the board's USB wiring, button sequence, or exposed boot pins from
the ESP32-S3 chip family. Obtain board-specific evidence first.

Using that confirmed interface, obtain a private backup where device protections
permit. Record the bootloader, partition table, OTA selection data, application
slots, and NVS. Keep passwords and calibration data out of Git. Compare the
installed application to the pinned image. Check security state and the usable
capacity of the inactive OTA slot against the generated image size.

Read the bootloader configuration or establish its rollback behavior independently.
Two OTA slots and an application that writes OTA state are not proof that the
bootloader performs rollback. Keep a tested independent recovery path even when
rollback is available. The application OTA file contains no device partition table,
bootloader, or eFuse dump.

## Distinguish the available update paths

The stock Wi-Fi DFU screen starts an HTTP updater. Offline analysis of its embedded
page and handlers identifies an OTA start request followed by a multipart upload.
This is separate from ESP32-S3 ROM USB DFU and serial download mode.
No deployment command is provided until the machine's recovery and image-acceptance
conditions are established. Details and addresses are in the
[boot and updater evidence](KD360S_BOOT_AND_UPDATE.md).

The official [KD-360S user manual](https://www.wpmcoffee.com/cdn/shop/files/KD-360S_EN_IB.pdf?v=1786689868)
describes Wi-Fi firmware update on printed page 17. The manual reviewed on
2026-10-11 supplies no USB/ROM recovery procedure. It cannot establish the board
wiring or the independent recovery conditions for this trial.

## Use staged experiments

1. Preserve the stock image and verify its exact hash. The public OTA application
   is a source artifact, not a full device backup.
2. Build the inert entry probe with the commands in its
   [README](../experiments/kd360s_entry_probe/README.md). Package its ELF using
   `scripts/kd360s_test_image.py`. This adds a wrapper that calls the original
   loop task. It adds no network connection or sensor reads.
3. After recovery is established, the inert image is the first supervised boot
   experiment. Its purpose is to isolate image acceptance, code placement, task
   entry, and stack effects from networking. Do not brew during this first boot.
4. Only after that experiment passes, consider the
   [snapshot experiment](KD360S_SNAPSHOT_TEST.md) with the actual collector address.
   The reference build uses `192.0.2.1`, which is not a working destination.
5. Check incoming packets, heap, stack, watchdog behavior, and timing while idle.
   Compare the stock baseline before any supervised brewing test.
6. Keep the recorder experiment on the normal screen. Do not enter Wi-Fi, Update,
   or DFU screens during that run. Stock network callbacks can interfere with
   the telemetry worker. Reboot into a known image before another isolated trial.

The trial confirmation option described in the boot evidence is conditional on
a rollback-capable bootloader. It does not mark the image valid, so a trial may
last only one boot. It is not a permanent recorder installation.

## Record the limits of the result

Host tests execute the snapshot C logic with stock API and memory stubs. They
do not execute the ESP32 SDK or prove RF, hardware, or control-loop timing.
The SDL simulator exercises a separate host bridge.

The recorder payload attempts reconnect in its worker, with a recursive gate
against loop-task network mutations and a screen/service ownership check. Native
SDK tasks retain their own behavior. It combines polling with start/stop, pressure and accepted-RX
hooks, and stores 128 records in its fallback RAM buffer or 256..8192 when its
optional PSRAM allocation succeeds. Idle records do not accumulate behind an
unacknowledged record, and sixteen slots are reserved for shot boundaries.
Eight records can be in flight with individual commit ACKs. The OTA pending
consumer is guarded. The software gate does not establish a bound on an SDK
call or a UI wait behind it. Host and isolated Xtensa tests cover implemented paths, while
hardware coverage, heap margin and latency remain unverified. Power loss removes
uncommitted buffer contents.
Passing the first boot experiment does not establish continuous every-shot
recording. Verifying network owner transitions, hardware coverage, and retention
remains required for that goal.
