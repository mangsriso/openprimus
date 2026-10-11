# Execute lifecycle detours in ESP32-S3 QEMU

This harness compiles the production `../hooks.S` and executes its machine code
with the pinned stock RX, stop, and pressure-calibration continuations. It tests
register-window behavior that a native C mock cannot exercise.

## Run

Provide the reviewed Espressif GCC 8.4.0 `esp-2021r2-patch5` toolchain, an
[Espressif QEMU build supporting ESP32-S3](https://github.com/espressif/esp-toolchain-docs/blob/main/qemu/esp32s3/README.md),
and the original KD360S application image. This was verified with
`esp_develop_9.0.0_20240606` QEMU on macOS arm64. The QEMU executable must run
successfully with `--version`; install its platform dependencies separately.
`--qemu` also accepts an executable wrapper that sets a local library path.

```sh
python3 experiments/kd360s_snapshot/verification/verify_hooks.py \
  --toolchain /path/to/xtensa-esp32s3-elf/bin \
  --qemu /path/to/qemu-system-xtensa \
  --stock firmware_binary/kd360s/KD360S_OTA_1.0.13_HK_20260630.bin \
  --output /path/to/private/hook-qemu-results
```

The command starts a local emulator and GDB, then stops both. It does not open a
serial device, contact the coffee machine, or change a hardware flash image.
Stock SHA-256 must be
`ac94398fde154cd8d29709e82990fd6dce53faa69b28e713f2c2a5dd4c65f5b4`.

A successful run returns zero and writes `results.json` with `passed: true`.
The output includes two test ELFs containing stock application code, debugger
scripts, and logs. Keep this output private; do not commit or flash these ELFs.

## What it checks

Thirteen positive cases cover three start arguments, five pressure-calibration
paths, three RX comparisons, and both stop branches. The harness checks:

- Return address, stack pointer, register window base and live-window mask.
- Caller a2-a7 canaries and all four integer return words a10-a13, including the
  raw floating-point return bits of pressure calibration.
- Callback event kind, original first argument, and original result.
- RX flag writes and stop state/mirror writes from the stock instructions.
- Twenty nested CALL8 frames inside each test callback. The reviewed run
  executed 208 window-overflow and 208 window-underflow handlers in total.

The callback is an assembly test fixture. It forces real Xtensa register-window
spills, records its three arguments, and writes outgoing argument registers
before returning. Window handlers are taken from ESP-IDF v4.4.6; the original
Cadence license notice is retained in `vectors.S`.

The negative control deliberately uses a10 instead of a9 as the entry detour's
scratch register. It must fail the first start return assertion. An emulator
startup failure or debugger error does not count as detecting the negative
control. The outer command succeeds only when the positive cases pass and this
specific argument-corruption failure is observed.

## Scope

The production assembly is compiled directly from `../hooks.S`; source hashes
and compiler/QEMU versions appear in the report. The harness independently
constructs the four entry patches, so it does not validate the production image
builder's patch placement or the final packaged firmware.

RX, stop, and pressure calibration execute their full original bodies against
controlled RAM values. Start executes its actual relocated prefix, followed by
a synthetic return suffix at `0x42005928` that exposes its sixth argument. The
full start function controls other subsystems and is **not** executed here.

This isolates the CPU ABI and chosen instruction paths. It does not test the
real `recorder_after` C implementation, FreeRTOS scheduling, Wi-Fi, flash/cache
mapping, bootloader, rollback, control-loop deadlines, or hardware interrupts.
The debugger initializes a CALL8 entry state; CALL4/CALL12 entry compatibility
is outside this contract. Every report retains `hardware_verified: false` and
`ready_to_flash: false`.

## Execute the Wi-Fi pending consumer guard

Use the same toolchain, QEMU and pinned stock inputs with `verify_wifi_guard.py`:

```sh
python3 experiments/kd360s_snapshot/verification/verify_wifi_guard.py \
  --toolchain /path/to/xtensa-esp32s3-elf/bin \
  --qemu /path/to/qemu-system-xtensa \
  --stock firmware_binary/kd360s/KD360S_OTA_1.0.13_HK_20260630.bin \
  --output /path/to/private/wifi-guard-qemu-results
```

This compiles production `wifi_guard.S` and uses the production image builder to
install its pinned splice. QEMU starts at the original predecessor instruction
and stops at the two original continuation addresses. Twenty-seven cases cross
pending values 0, 1 and 255 with OTA, Wi-Fi, password, normal, DFU, null, settings,
preferences and repeated normal-screen inputs. Null-screen cases also zero the
three owner object globals. Checks cover continuation, live registers, stack
pointer, register-window state, SAR, the pending byte, and preserved reason,
connect/progress/update/restart state. The negative control changes the owned
continuation literal and must fail the OTA nonzero case.

This does not execute the complete scheduler, callback or SDK and does not
simulate concurrent event arrival. Repeated normal inputs are repeated CPU
cases, not an asynchronous race test. It does not prove reconnect, control-loop
latency, packet retention or hardware behavior. Test ELFs contain private stock
code; do not commit or flash them.
