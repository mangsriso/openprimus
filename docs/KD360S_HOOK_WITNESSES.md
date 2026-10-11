# KD360S lifecycle hook witnesses

The following instruction witnesses apply to application SHA-256
`ac94398fde154cd8d29709e82990fd6dce53faa69b28e713f2c2a5dd4c65f5b4`.
They support the implemented adapters described below. They do not establish
complete writer coverage. Retained local analysis is in the task's
`evidence/flash-readiness-hooks/` directory, including `audit_hooks.py`, JSON,
assembly listings, and direct-call linker rejection results.

## Lifecycle boundaries

| Candidate | Static evidence | Limit |
| --- | --- | --- |
| Accepted RX helper `0x42005430` | Entry `36 41 00`; call `25 d8 ff` at parser `0x420056ac`; return at `0x42005457` | Parser checks precede the call, but buffer turnover and distinct-frame freshness remain unproved. |
| Start `0x42005918` | Entry `36 81 00`; 19 corroborated direct callsites in 11 function owners; common return at `0x42005bef` | Entry precedes initialization. A post-call wrapper must preserve the start parameter and distinguish diagnostic operation. |
| Stop `0x420031b0` | Entry `36 41 00`; callers `0x420089cd` and `0x42009f90`; return at `0x420031e8` | The diagnostic caller bypasses the normal history path. End reason cannot be inferred from this helper alone. |

A byte scan found 24 possible call8 encodings. Existing Ghidra function owners
and entry-anchored objdump corroborated 22. The other two, at `0x4200eabe` and
`0x4200eba3`, had data ownership and are not established callers. This scan does
not cover indirect references or prove that every candidate byte sequence is code.

The start function has an out-of-line path after its common return. Linear
disassembly also crosses padding near `0x42005b96`. Function-entry and branch-target
decoding must agree before using those bytes in a patch.

## Pressure acquisition and storage differ

At `0x4200ce4f`, bytes `65 f5 f6` call calibration `0x42003da4`. The return bits
arrive in a10, then `wfr f1,a10` at `0x4200ce52` copies them to a float register.
The caller applies a 15.0 clamp before the control store at `0x4200ce71`.
That store is `ssi f0,a4,8`, bytes `03 44 02`, followed by `memw`.

A calibration wrapper observes acquisition before the clamp. A hook at the
store observes the selected value after the clamp and must reproduce the
displaced store while preserving live integer and float state. Start also
writes control pressure, so a control-value write is not always a new acquisition.

## Direct calls cannot reach the existing payload tail

For these call8 instructions, the destination is
`((PC + 4) & ~3) + sign_extend(imm18) * 4`. The range is -524288 through +524284
bytes. RX, start, stop, and pressure caller sites cannot directly reach the
current tail at `0x42106ea0`. The RX call requires 1054704 bytes of displacement.

Four assembly fixtures anchored at representative original addresses also fail
to link to that tail. The reviewed toolchain reports `call target out of range`
with assembler transforms and linker relaxation disabled. The result is a
range rejection, not a runtime experiment.

Implementing lifecycle hooks therefore needs a reachable relay or an indirect
sequence with proven placement, ABI, and register preservation. An apparently
empty area is not evidence that stock never executes or references it.
No nearby code cave has been approved by this audit.

## Remaining recorder requirements

The earlier polling-only payload has been replaced by the explicit lifecycle
and acquisition adapters below. The [recorder contract](../KD360S_RECORDER_DESIGN.md)
describes its bounded RAM/PSRAM retention and delivery window. Continuous
reconnect, actual source coverage, heap margin and control-loop timing still
need verification. Power loss can erase records not committed by the collector.
Passing an inert first boot cannot establish complete every-shot recording.

## Executable recorder adapters

The direct-call range limit is now addressed by four original-entry detours.
Each jumps over an inline destination literal, loads it into a9, and transfers
to a CALL8 wrapper in the appended page. The wrapper calls a relocated prefix,
which rejoins the untouched original function; its return then reaches the
recorder callback. The original start function's out-of-line branches remain
in place. `scripts/kd360s_hooks.py` fixes entry spans and literal witnesses to
the pinned image, and the image builder reverses every patch to stock.

The wrapper uses an 80-byte frame. Independent review found overlap between
saved inputs and Xtensa's register-window spill area in an earlier 64-byte
design. The final wrapper saves only its first input, preserves six register
arguments and four return words, and uses no before callback.

The [portable QEMU check](../experiments/kd360s_snapshot/verification/README.md)
executed 13 cases using the production assembly. RX, stop and pressure use
their actual stock bodies. Start uses its actual relocated prefix followed by
a synthetic return suffix. It exercised 208 window overflows and 208 underflows
and rejected a negative control that clobbered a10 instead of a9. These tests
do not execute the whole stock application or prove interrupt/control timing.

The recorder binds its allocated context through a genuine pthread key.
Stock exposes only TLS slot 0 and pthread already owns it. The private key
registry head is at `0x3fca3184`, its lock at `0x3fc973dc`, and each node holds
key/destructor/next at offsets 0/4/8. A distinct, inert destructor identifies
the recorder's key during locked lookup. The marker is checked in the final
ELF; `pthread_getspecific` runs after unlocking. The sender holds the same
boot-long context through its task argument. Host tests include concurrent
registry mutations and more than 256 legitimate keys.

Stock imports are key-create `0x42073374`, key-delete `0x420733bc`, get-specific
`0x42073400`, set-specific `0x42073428`, lock-enter `0x403811b0`, and lock-exit
`0x403812f4`. Their stock disassembly was compared with the official Arduino
2.0.14 SDK and [IDF pthread source](https://github.com/espressif/esp-idf/blob/v4.4.6/components/pthread/pthread_local_storage.c).
This adapter is specific to the pinned binary, not a portable pthread API.

The [recorder contract](../KD360S_RECORDER_DESIGN.md) documents the selected
design, event timing, source counters, loss behavior and remaining gates.
