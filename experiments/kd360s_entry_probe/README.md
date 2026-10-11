# Inert KD360S task-entry probe

This experiment compiles a standalone Xtensa function that calls the stock
`loopTask`. The compile step produces an ELF. The packaging step below creates
a local experimental application image. Neither step contacts a device or
establishes that the modified application can boot. The probe exports no data. It runs before stock `setup`
if a future image redirects the task entry to it. Code placed after the call
would not run during normal operation because `loopTask` does not return.

The experiment tests a smaller integration boundary than overwriting instructions
in the sample loop. The proposed pointer site is `0x4203ec00`, whose current
value is `0x42067014`. The original application loads that pointer at `0x42067065`
before creating the task. Ghidra found one reference to the pointer site; that
does not exclude unknown computed references.

## Build and inspect

Use Espressif's `xtensa-esp32s3-elf-gcc` 8.4.0,
`esp-2021r2-patch5`, listed in the official
[Arduino ESP32 2.0.14 package manifest](https://github.com/espressif/arduino-esp32/blob/2.0.14/package/package_esp32_index.template.json).
The tested macOS arm64 archive SHA-256 is
`c64b05be25d26916c65dcfe11de9e60b96d58980b2df706d3074cb70b1ef6cb9`.
Set `TOOLCHAIN_PREFIX` to the absolute prefix ending in
`/bin/xtensa-esp32s3-elf-`, then run from the repository root:

```sh
mkdir -p analysis/extracted/entry-probe
"${TOOLCHAIN_PREFIX}gcc" -Os -mlongcalls -ffreestanding -fno-builtin \
  -fno-stack-protector -fno-optimize-sibling-calls -nostdlib \
  -Wl,--abi-windowed -Wl,--build-id=none \
  -Wl,-T,experiments/kd360s_entry_probe/probe.ld \
  experiments/kd360s_entry_probe/entry.c \
  -o analysis/extracted/entry-probe/probe.elf
"${TOOLCHAIN_PREFIX}readelf" -h -r -l analysis/extracted/entry-probe/probe.elf
"${TOOLCHAIN_PREFIX}objdump" -h -d analysis/extracted/entry-probe/probe.elf
"${TOOLCHAIN_PREFIX}objcopy" -O binary --only-section=.text \
  analysis/extracted/entry-probe/probe.elf \
  analysis/extracted/entry-probe/probe-text.bin
python3 scripts/kd360s_instrumentation.py \
  --probe-text analysis/extracted/entry-probe/probe-text.bin
```

Fetch the pinned stock image with `scripts/kd360s_firmware.py fetch` first if it
is not cached. The audit command reads the image and prints JSON; it does not
write a replacement. It recognizes only this exact 18-byte probe. It is not a
general ELF or payload validator.

Expected `.text` SHA-256:
`0404ee0ac0eed761bb10c2e115a44032fc2585b7aae6ff1de51912532cae5158`.
The section starts at `0x42106ea0`, but the function entry is `0x42106ea4`.
The first four bytes are the original function pointer, not instructions.
The function uses a 32-byte stack frame, passes incoming `a2` in outgoing `a10`,
loads the pointer with `l32r`, calls with `callx8`, and has a `retw.n` return.
GNU objdump and Ghidra independently decoded that sequence.

No static writable data or BSS is emitted. The wrapper's stack frame remains
allocated while the original task runs. This is not a proof of stack headroom.
The linked ELF has no unresolved relocations. Its program header also includes
ELF header bytes below `.text`; do not treat the entire `PT_LOAD` range as the
payload or overwrite the stock instructions underneath it.

## Placement limits

The proposed section is beyond the current IROM segment's end. At its projected
file offset, `0x396ea0`, the original file already contains part of the final
IRAM segment. The offline image builder extends IROM by inserting bytes, moves the following
segment header and payload, and regenerates integrity fields. Writing the probe over the existing file offset would corrupt it.

The proposed start has 37,216 bytes before the next 64 KiB boundary. This is
address arithmetic, not a free-space or runtime MMU guarantee. The device's
partition capacity, bootloader acceptance and recovery path remain unknown.
The audit always reports `ready_to_flash: false`.

## Package an offline image

After compiling the ELF above, package it locally:

```sh
python3 scripts/kd360s_test_image.py \
  --elf analysis/extracted/entry-probe/probe.elf \
  --trial-defer-confirm \
  --output private/entry-probe-trial
```

The opt-in trial flag suppresses stock early OTA confirmation. It adds no later
confirmation and does not prove bootloader rollback. Without the flag, the
package retains stock confirmation behavior. Both variants remain experimental
and report `ready_to_flash: false`. Read the
[boot evidence](../../docs/KD360S_BOOT_AND_UPDATE.md) and
[preparation procedure](../../docs/KD360S_DEPLOYMENT_PREPARATION.md) before
planning any hardware test.
