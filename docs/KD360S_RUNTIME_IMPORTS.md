# KD360S runtime connection and entry-point evidence

Scope: pinned KD360S 1.0.13_HK application SHA-256
`ac94398fde154cd8d29709e82990fd6dce53faa69b28e713f2c2a5dd4c65f5b4`.
This extends the [instrumentation plan](KD360S_INSTRUMENTATION.md).
No runtime connection or modified-image boot has been tested.

## Connection functions

The image's application descriptor contains `v4.4.6-dirty`. Its embedded source
paths identify PlatformIO framework package `3.20014.231204`. Function behavior,
error messages and source line numbers match the official Arduino ESP32 2.0.14
[WiFiSTA.cpp](https://github.com/espressif/arduino-esp32/blob/2.0.14/libraries/WiFi/src/WiFiSTA.cpp)
and [WiFiGeneric.cpp](https://github.com/espressif/arduino-esp32/blob/2.0.14/libraries/WiFi/src/WiFiGeneric.cpp).
These are source-correlated identities, not recovered vendor symbols.

| Address | Source-correlated identity | Evidence in the pinned image |
| --- | --- | --- |
| `420561c0` | `WiFiSTAClass::begin(ssid, password, channel, bssid, connect)` | Enables STA, validates 32/64-byte limits, compares two 140-byte configurations, sets config/IP, calls connect, returns status. Error line numbers agree with the tagged source. |
| `420561a0` | `WiFiSTAClass::status()` | Returns stored status if the event group is absent; otherwise reads event-group bits with a zero clear mask. |
| `42056688` | `WiFiSTAClass::disconnect(wifioff, eraseap)` | Optionally clears config, disconnects, and conditionally disables STA. |
| `42055210` | `WiFiGenericClass::enableSTA(bool)` | Changes the STA bit through the mode function. |
| `42054f88` | `WiFiGenericClass::mode(mode)` | Initializes networking when moving from off, creates the station interface and starts Wi-Fi. |
| `42056034` | Functional event-handler registration overload | Copies a callback object into handler storage. This is not a plain C function-pointer interface. |
| `42055b38` | Generic event callback | Updates status, handles selected reconnect cases, then dispatches registered callbacks. |
| `42066150` | `xTaskCreateUniversal` wrapper | Forwards task arguments to `4037f8f4`; normalizes a core ID outside 0/1. |

At the updater's call site `4201d4da`, assembly supplies six windowed-ABI
arguments: Wi-Fi object in `a10`, SSID pointer in `a11`, password pointer in
`a12`, zero channel in `a13`, null BSSID in `a14`, and true connect in `a15`.
The object address is `3fca2128`, loaded through both `42000790` and `4203de98`.
The caller obtains character pointers from stock String objects; a raw String
object address is not interchangeable with a character pointer.

The SDK event path already contains conditional automatic reconnect behavior.
It is not a guarantee of continuous connectivity: reason codes, current mode,
explicit disconnects and registered application callbacks affect it. Do not run
a second reconnect loop concurrently without defining who owns connection state.

The updater registers its own callback for event 5 and subsequently enters UI,
delay and manifest-fetch branches. A telemetry worker should use a separate
connection state machine and avoid registering that OTA callback. Entering the
stock update or Wi-Fi settings screens later still needs an ownership policy.

## Task-entry alternative to an inline trampoline

Assembly and source comparison with Arduino's
[main.cpp](https://github.com/espressif/arduino-esp32/blob/2.0.14/cores/esp32/main.cpp)
identify this chain:

| Location | Observed behavior |
| --- | --- |
| `42067040` | Clears the loop watchdog flag, calls Arduino initialization, obtains loop stack size, then creates the task on core 1. |
| Literal `4203ec00`, file offset `2cec00` | Contains little-endian `42067014`; loaded at `42067065` as the task entry. |
| `42067014` | Calls stock setup `4200be5c`, then repeatedly services the watchdog when enabled, calls `4200cc98`, and calls serial-event handling `4200cc6c`. |

An inert task wrapper avoids relocating instructions from the sample loop.
The [standalone probe](../experiments/kd360s_entry_probe/README.md) compiles and
calls the original task through an absolute address. It is an offline calling
convention experiment, not an exporter. It runs before stock setup and never
regains control during normal operation. A later recorder needs a separately
proved post-setup entry point or a faithful replacement of the small loop wrapper.

The inline sample point remains useful for later work. The loop writes actual
pressure at `4200ce71` and actual flow at `4200ce7a`; the next observed call is
`420077a4` at `4200ce7f`. These are candidate observation boundaries, not approved
patch sites. An inline hook must preserve register state and overwritten behavior.

## Memory ownership and placement

Initialized DRAM ends at `3fc9c88c`. Startup clears BSS from `3fc9c890` to
`3fca9378`, as supplied by literals `403746d4` and `403746d0`. That interval
contains application and SDK runtime state. Absence of bytes from the OTA file
does not make it available for a recorder.

IROM ends at `42106e94`; its file payload ends at `396e94`. The final IRAM
segment starts with its header there and payload at `396e9c`. An appended-code
candidate at `42106ea0` requires insertion into the image structure. It cannot
overwrite the current bytes at the projected offset `396ea0`.

Startup also uses fixed mapping-boundary literals, including DROM start
`3c110020` and end `3c39765c`. Staying within a proposed 64 KiB page is only an
arithmetic bound. Actual cache/MMU mapping, partition capacity and boot acceptance
still need proof. The inert probe owns no static writable RAM; its extra stack
frame is separate from the unsolved recorder-buffer allocation.

## Reproduce the static work

Run `AuditSlim.java` on the pinned image as described in
[the firmware findings](KD360S_1.0.13_HK.md), then use `TraceSlim.java` for
`42056034 420561c0 420561a0 42056688 42055210 42054f88 42055b38 42066150
42067014 42067040 403777d8`. Trace literal addresses `4203ec00 403746e8
4203ebf0 4203ebf4` to reproduce the observed references. Use `DumpCapture.java`
on the caller functions to verify argument registers and instruction boundaries.
Keep full decompilation output local; the repository contains findings and tools.

Next proof: establish a post-setup initialization boundary and network ownership,
then an owned buffer/task context and an outbound sender. Device backup,
partition/security inspection and recovery preparation remain prerequisites for
deploying an experimental image.

## Experimental snapshot imports

The [snapshot payload](../experiments/kd360s_snapshot/snapshot.c) adds these
source-correlated addresses. They are fixed to the pinned image, not a stable API.

| Address | Identity and evidence |
| --- | --- |
| `403846c8` / `403846e8` | malloc/free wrappers; forward to `heap_caps_malloc_default` at `40378738` and heap free at `40378898`. |
| `42094a18` | `esp_fill_random`; function-name assertion, byte-copy loop and calls to random generator `40378c0c`. |
| `42082b44` | lwIP socket; Arduino WiFiUDP beginPacket calls it with `(2, 2, 0)`. |
| `42082978` | lwIP sendto; WiFiUDP endPacket passes socket, buffer, length, flags, sockaddr and 16-byte length. |
| `42082884` | lwIP recvfrom; WiFiUDP parsePacket passes a buffer, flag 8 and source sockaddr/length. |
| `420826b0` | lwIP close; function-name assertion and socket/netconn cleanup. |
| `4206617c` / `42066198` | Arduino millis/delay, correlated with stock call sites. |

The SDK [WiFiUDP implementation](https://github.com/espressif/arduino-esp32/blob/2.0.14/libraries/WiFi/src/WiFiUdp.cpp)
and [lwIP socket layout](https://github.com/espressif/esp-lwip/blob/2.1.3-esp/src/include/lwip/sockets.h)
corroborate the call shapes. The binary recvfrom helper explicitly handles flag 8
as nonblocking. Credentials are String objects at `3fc9ff3c` and `3fc9ff2c`;
post-setup code copies their inline or heap contents before starting the worker.

An early unconfirmed guess that `420e241c` was an allocation entry was rejected
by further decompilation; the payload uses the wrapper at `403846c8` instead.

Instruction memory must not supply byte-accessed constants. The task name is
constructed in owned stack RAM, and the linker rejects nonempty `.rodata`, `.data`
and `.bss`. This follows the S3
[memory-access restriction](https://docs.espressif.com/projects/esp-idf/en/v4.4/esp32s3/api-guides/memory-types.html).
The actual emitted payload and import behavior still need runtime validation.
