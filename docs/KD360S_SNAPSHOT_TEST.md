# Experimental KD360S recorder test package

This package builds a modified image and provides a working host collector. It has
passed offline image checks and host transport tests. It has **not booted on a
KD360S**. WPS2 adds explicit start/stop, pressure-calibration and accepted-RX
hooks. These record source invocations, not proof of physical sensor freshness.
The owner's available access is Wi-Fi and the stock application's DFU
screen; neither establishes recovery if the application fails to boot.

## Run the host test now

From the repository root, with Python 3.9 or newer:

```sh
python3 scripts/kd360s_live.py self-test
python3 -m unittest discover -s tests -v
```

The self-test runs a real UDP socket and SQLite archive on loopback, then removes
its files and listener. It checks replay after a lost ACK, duplicate suppression,
conflict rejection, collector restart and CSV/JSONL exports. Its packets are
synthetic. The C ring test separately exercises buffer overflow, stale ACKs,
index wrap and 50,000 ordered deliveries between two host threads.

## Run the payload control flow on the host

```sh
python3 -m unittest discover -s tests -p test_kd360s_runtime.py -v
```

These eight tests compile the actual `snapshot.c` with deterministic replacements
for stock memory reads and SDK calls. They exercise credential copying, failure
fallback to the stock loop, call order, 100 ms observation spacing, raw fields,
clock wrap, socket recovery, ACK validation and the 250 ms retry interval.
The driver stops infinite loops after a fixed number of iterations.

The lifecycle test also covers repeated sensor values, rapid start/stop/start,
diagnostic fields, missing context, clock wrap, source counter saturation, and
queue overflow. `test_kd360s_recorder.py` sends the production C records through
real loopback UDP into SQLite and checks replay, reopening, mixed WPS1/WPS2
exports and transactional archive migration.

The tests preserve an existing limitation. If the initial `WiFi.begin` fails and
the SDK never reports a connection, the worker does not call `begin` again.
The main task marks capture ready after context binding and task creation, before
waiting for a network connection, and retains records until the ring fills. The tests
also verify that a simulated SDK reconnection allows the same pending record to
be sent again. They do not establish that the real stock SDK reconnects.

`stock_runtime.h` holds the fixed-address imports, memory access and Xtensa ABI
requirements. The host driver substitutes that boundary and includes the same
payload source and `protocol.h`. No host test executes those import addresses,
SDK scheduling, physical networking or machine control.
The native test result does not satisfy the hardware gates below.

The [QEMU verification](../experiments/kd360s_snapshot/verification/README.md)
executes the production Xtensa adapters separately. Thirteen cases cover pressure,
RX, stop and the start prefix, including register-window spills and a negative
control. Start's full body, SDK scheduling and packaged-image boot are outside
that emulator test.

## Process recovery and independent image checks

The process integration test compiles a host producer against the actual
`protocol.h`, sends its records through UDP to the collector CLI, kills the
collector after an acknowledged record, restarts it and replays that record.
It verifies 129 exact raw records, no duplicate rows, 32 explicitly dropped
observations after ring overflow, both export formats and SQLite integrity.

```sh
python3 -m unittest discover -s tests -p test_kd360s_process.py -v
```

This test requires a POSIX host and C compiler. It uses loopback only and removes
its child processes and temporary database. It tests recovery of acknowledged,
committed data after process death. It does not kill SQLite mid-transaction or
execute the ESP32 network worker on hardware.

Independent `esptool` 4.8.1 `image_info --version 2` checks passed for both the
pinned stock image and the reference snapshot image. A copy with one modified
payload byte produced invalid checksum and hash results. Check the reported
validity fields: `image_info` can return exit code zero for this corrupt control.
The image header's flash size and secure version are declarations in the file,
not measurements of physical flash, eFuses or partition capacity. These checks
follow the official [image_info documentation](https://github.com/espressif/esptool/blob/v4.8.1/docs/en/esptool/basic-commands.rst#output-bin-image-details-image_info).

## Build for a specified collector

Use Espressif's `esp-2021r2-patch5` Xtensa ESP32-S3 GCC 8.4.0 toolchain. The reviewed
[macOS ARM64 archive](https://github.com/espressif/crosstool-NG/releases/download/esp-2021r2-patch5/xtensa-esp32s3-elf-gcc8_4_0-esp-2021r2-patch5-macos-arm64.tar.gz)
has SHA-256 `c64b05be25d26916c65dcfe11de9e60b96d58980b2df706d3074cb70b1ef6cb9`.
Verify the download before extracting it. Supply its `bin` directory below.

```sh
python3 scripts/kd360s_firmware.py fetch
python3 scripts/kd360s_build_snapshot.py \
  --toolchain /path/to/xtensa-esp32s3-elf/bin \
  --collector-ip YOUR_MAC_LAN_IP \
  --output private/snapshot-test
```

For a conditional one-boot trial, add `--trial-defer-confirm`. This suppresses
the stock early OTA confirmation and adds no later confirmation. It does not
establish rollback on the device. Read the [boot evidence](KD360S_BOOT_AND_UPDATE.md)
and [preparation procedure](KD360S_DEPLOYMENT_PREPARATION.md) before choosing a
hardware experiment.

The destination must be the collector's IPv4 address, not the espresso machine's
address. No network contact occurs during this build. The output contains
`firmware.experimental.bin`, `payload.elf`, and `manifest.json`. The manifest records the exact
stock, payload and result hashes, destination, compiler and source hashes, and
unresolved hardware checks. It always reports `ready_to_flash: false`.
Use the [offline preflight](KD360S_PREFLIGHT.md) to verify these files together.

The builder inserts code into segment 3, shifts segment 4 without changing its
payload, changes the loop-task entry pointer, and adds four byte-guarded function
entry detours. It regenerates ESP checksum and
SHA-256, then reverses those changes and requires a byte-identical stock image.
It rejects the wrong input version, unresolved ELF symbols, writable static
sections and payloads outside the candidate tail. Those checks do not establish
bootloader acceptance or runtime safety. A partly published package can be resumed
only when every existing file matches the requested bytes; differing files are
never replaced.

The reference offline build uses `192.0.2.1`, a documentation address. It is an
image-format test, not a usable destination for the owner's machine. Build again
with the real collector address before considering a hardware experiment.

## What the payload attempts

After stock setup, it allocates a context containing a 16,384-byte record array,
ring/state metadata and credential copies, and creates a worker with a
4,096-byte requested stack. SDK task metadata and networking consume additional
memory. A genuine pthread key binds that context only to the loop task. Allocation,
binding or task-creation failure disables capture and continues the stock loop.
A published or uncertain context remains allocated until reboot to avoid dangling
pointers. Heap and stack headroom on the physical machine remain unknown.
Before publishing that context, it attempts one bounded PSRAM allocation from
the stock capability allocator. See the [retention policy](../KD360S_RECORDER_DESIGN.md#bounded-retention)
for size limits and unverified memory margins.

The worker calls the recovered stock `WiFi.begin` once with copies of the saved
credentials. It erases its password copy after that call. It does not register the
updater callback or repeatedly force a connection. Later reconnect behavior is
owned by the stock SDK. Opening Wi-Fi settings, OTA or DFU can disconnect it;
concurrent access to those screens is outside this first experiment.
The new consumer guard suppresses the stock pending-error disconnect outside the
three OTA-flow screens. It preserves the original callback and does not implement
worker/UI serialization or autonomous retries.

The loop wrapper preserves setup, conditional watchdog reset, stock loop and
serial-event calls. It polls raw values after an iteration, with a minimum 100 ms
between observations while a shot is open. Idle observations require five seconds
and an empty queue. Additional records come from start/stop and source hooks
during a shot. Each record includes actual/target pressure and flow bits,
raw volume, mode/state/active/stop/diagnostic fields, observation time and coarse
loop duration. Units need physical confirmation. Millisecond loop duration is
not a worst-case latency measurement. Snapshot collection and timer calls add
work to the control task and still require timing validation. WPS2 also preserves
the calibrated pressure return before the caller's clamp, ADC argument, source
generations/timestamps, and the 28-byte accepted receive window. The pressure
event's common control fields may still contain the preceding update.

The ring retains unacknowledged records, using 128 fallback slots or 256..8192
slots after successful PSRAM allocation. Offset 124 of each WPS2 packet reports
the selected capacity; update the collector before using this extension.
Telemetry stops consuming slots when sixteen remain; start/stop packets can use
those remaining slots. Dropped records still advance sequence and cumulative
loss. Finite outage retention depends on the actual incoming event rate and
available capacity. Between shots, one retained idle record prevents further idle
accumulation. A later idle record after the queue drains reports cumulative loss.
Records disappear on power loss; a reboot before such reporting can still hide
the final drop count and completely unseen shots.
The sender stops accepting records before its 32-bit sequence wraps. The session
nonce comes from the recovered SDK random generator after stock setup.

The collector acknowledges only after SQLite commit. The worker accepts only a
matching ACK from its configured destination. Up to eight records can be in
flight, each retried after 250 ms. Out-of-order ACKs are retained; slots are freed
only as the contiguous acknowledged prefix advances. Transient network errors do not discard
committed rows or stop the collector. Neither protocol nor ACKs are authenticated.
See [the collector contract](KD360S_LIVE_COLLECTOR.md) for ports, storage and exports.

## Hardware test gates and order

Before installing an image, establish the actual bootloader/partition layout,
OTA slot capacity, security configuration and a recovery route independent of the
running application. An application-level DFU screen is not evidence of ROM
recovery. The public OTA binary cannot answer those device-specific questions.
This package contains no flashing command and has not changed the machine.

Once those prerequisites are established:

1. Start the collector with an explicit LAN bind address:
   `python3 scripts/kd360s_live.py serve private/snapshots.sqlite --bind YOUR_MAC_LAN_IP`.
2. Perform the first supervised boot with no brewing. Check the normal screen,
   machine behavior and incoming records. Compare against the unmodified baseline.
3. Restart the collector, interrupt Wi-Fi briefly, and verify replay, sequence gaps
   and overflow behavior. Do not enter Wi-Fi/OTA/DFU screens during this experiment.
4. Validate heap/stack headroom and control-task timing before a supervised brew.
5. Compare values and transitions against the display and independently observed
   shots, including aborts and diagnostics. Explicit hooks still need on-device
   coverage and control-task timing verification.

Complete every-shot capture still needs hardware validation of the source and
lifecycle hooks, tested reconnect ownership across screens, and a longer-retention design
for receiver outages. A sleeping laptop exceeds this experiment's small RAM buffer.
