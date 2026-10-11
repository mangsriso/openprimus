# KD360S boot and updater evidence

These findings apply only to application SHA-256
`ac94398fde154cd8d29709e82990fd6dce53faa69b28e713f2c2a5dd4c65f5b4`.
They come from the public application binary and source correlation. They do
not establish the bootloader, partitions, security state, or wiring of a machine.

## The stock DFU page accepts an application upload

The DFU/AP callback at `0x420145f0` calls server setup at `0x4200305c`.
Route registration at `0x42061150` installs the update handlers. The embedded
gzip page at `0x3c367086`, length `0x2d59`, expands to 46,600 bytes. Its SHA-256 is
`480b0432ab039ece0e8d3b84bb7323a6668a36f37743b8081def4ed9b21a393d`.
The page is not redistributed in this repository.

| Operation | Evidence |
| --- | --- |
| Serve the update page | `/update`, callback `0x42060fe4` |
| Start firmware update | `GET /ota/start?mode=fr&hash=...`, callback `0x42060cf8` |
| Choose application storage | Start calls `Update.begin(-1, U_FLASH)` at `0x42060eb2`; `fs` selects filesystem instead |
| Upload bytes | `POST /ota/upload`, multipart field `file`; chunk callback `0x42060b28` calls write at `0x42060ba2` |
| Finish | Final chunk calls `Update.end(true)` at `0x42060c42`; boot-partition selection follows image validation |

The updater selects the next application partition at runtime. The URL does not
contain a flash offset. This establishes a stock application upload path without
redirecting the vendor's update server. It does not prove acceptance or execution
of a modified image on the owner's machine. See the
[Arduino 2.0.14 updater](https://github.com/espressif/arduino-esp32/blob/2.0.14/libraries/Update/src/Updater.cpp).

The start handler calls `setMD5` at `0x42060e32` before `Update.begin`.
The begin path at `0x4206037b` through `0x4206038a` clears `_target_md5`.
The final MD5 comparison is conditional on that string being nonempty. Therefore
the page's supplied hash does not establish an end-to-end expected-MD5 check on
this path. ESP image validation remains separate; this finding does not bypass
image validation or secure boot.

## Stock confirms the image before the patched task runs

`app_main`, at `0x42067040`, calls `initArduino`, at `0x420661f8`, before
creating the loop task. The initialization path checks the running OTA partition
for state `PENDING_VERIFY`, then calls the default `verifyOta`, which returns true.
It calls mark-valid at `0x4206621c`, before stock setup or the recorder wrapper.

| Function or reference | Pinned bytes or behavior |
| --- | --- |
| `verifyOta`, `0x42100f68` | `36 41 00 0c 12 1d f0 00`, returns one |
| `verifyRollbackLater`, `0x42100f70` | `36 41 00 0c 02 1d f0 00`, returns zero |
| Literal `0x4203eafc` | Pointer to `0x42100f70` |
| Call site | `l32r` at `0x420661fb`, `callx8` at `0x420661fe` |
| Nonzero result | Branch at `0x42066201` skips early confirmation |
| Boot-partition activation | `0x4206ed64` writes OTA state `NEW`, value zero, before updating OTA selection data |

A scan of all initialized segments found one literal occurrence of the deferred
confirmation function pointer, and one candidate load of that literal in
executable segments. It found no direct call to that function. This does not
exclude computed runtime call targets. The behavior agrees with
[Arduino's initialization code](https://github.com/espressif/arduino-esp32/blob/2.0.14/cores/esp32/esp32-hal-misc.c#L189-L223).

Even with a rollback-capable bootloader, a crash in the patched loop task can
occur after stock has marked the image valid. Automatic rollback cannot be
assumed to protect that experiment.

## A deferred-confirmation trial has a narrower purpose

The optional trial patch changes `movi.n a2,0` at `0x42100f73` to `movi.n a2,1`.
Only file byte `0x390f74` changes from `02` to `12`, before recalculating the
image checksum and digest. The exact input hash and full original function
bytes must match. The builder must reverse this edit together with its payload
insertion and task pointer change to reproduce the stock image byte for byte.

This suppresses the identified early confirmation path. It adds no later
confirmation or rollback implementation. If the actual bootloader implements
rollback and a valid previous slot exists, the unconfirmed trial is expected to
roll back on a later reset. Without those conditions, this patch supplies no
recovery. OTA APIs can also reject updates while the running image is pending.
See Espressif's [OTA state and rollback contract](https://docs.espressif.com/projects/esp-idf/en/v4.4.7/esp32s3/api-reference/system/ota.html).

A deferred trial is not a permanent recorder installation. Hardware acceptance,
rollback behavior, and recovery remain unverified.

## The insertion fits the application's static MMU allocation

The startup sequence at `0x40377854` through `0x4037787b` loads these constants:

| Literal | Value |
| --- | --- |
| `0x403744d0` | `0xffff0000`, page mask |
| `0x403746e0` | `0x3c110020`, rodata start |
| `0x403746e8` | `0x3c39765c`, rodata end |
| `0x403746e4` | `0xc4000000`, negative DROM base |
| `0x4037454c` | `0x0000ffff`, page rounding |
| `0x40374720` | `0x40001914`, ROM cache MMU size function |

The code computes 17 instruction entries, or 68 table bytes, and 956 data table
bytes. Its instruction virtual range ends at `0x42110000`. The existing linker
limit is therefore consistent with the pinned startup allocation, rather than
only an arbitrary round-number boundary. The stock IROM and both experimental
payloads fit in the same 17 instruction pages.

The ROM target matches the
[official ESP32-S3 ROM symbol](https://github.com/espressif/esp-idf/blob/v4.4.6/components/esp_rom/esp32s3/ld/esp32s3.rom.ld#L415).
The calculation matches the allocation in
[ESP-IDF startup](https://github.com/espressif/esp-idf/blob/v4.4.6/components/esp_system/port/cpu_start.c#L363-L375).
The standard
[bootloader maps image segment lengths](https://github.com/espressif/esp-idf/blob/v4.4.6/components/bootloader_support/src/bootloader_utility.c#L614-L653).
The machine's actual bootloader and successful runtime mapping remain unverified.

## Stock network conflict and recorder ownership

Stock settings and update paths can explicitly disconnect. The early snapshot
worker called station begin only once; the current recorder adds worker retries
through a task-scoped recursive gate. See the current
[recorder contract](../KD360S_RECORDER_DESIGN.md) for ownership and credential rules.

An additional conflict exists in the updater's event callback, `0x4201ba94`.
At `0x4201bae8` it saves the disconnect reason at `0x3fca1515`. For a reason other
than eight, it sets `0x3fca1514` at `0x4201baf6`. The updater routine checks that
flag at `0x4201d3c9` and can call disconnect at `0x4201d3f6`. The main loop calls
the updater routine every iteration. The SDK reconnect path runs before
application callbacks.

This permits an SDK reconnect attempt followed by an application disconnect.
Callback lifetime after leaving the screen is not fully established. Checking
only the current screen or cleared one-shot flags does not prove ownership.
The current recorder guards this pending-flag consumer and checks screen/service
ownership under its gate before retrying. The native SDK event task bypasses
the gate. This avoids making that task wait behind a recorder SDK call; it does
not establish a bound on SDK duration or on a UI call waiting for the gate.
Physical transition and timing tests remain required.

## Evidence still required from the machine

The application file cannot establish OTA slot capacity, the active and previous
slots, bootloader rollback, eFuse security state, or an independent recovery
interface. Use the exact candidate's preflight `output_size` and `sector_rounded_size` for
its capacity requirement. These are requirements, not measured partition sizes.

The WPM web DFU mode and
[ESP32-S3 ROM USB DFU](https://docs.espressif.com/projects/esp-idf/en/v4.4.7/esp32s3/api-guides/dfu.html)
are different facilities. ROM recovery depends on board wiring, boot mode, and
security settings. Follow the [preparation procedure](KD360S_DEPLOYMENT_PREPARATION.md)
before planning an installation.
