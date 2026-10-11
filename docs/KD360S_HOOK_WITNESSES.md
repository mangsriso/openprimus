# KD360S lifecycle hook witnesses

The following instruction witnesses apply to application SHA-256
`ac94398fde154cd8d29709e82990fd6dce53faa69b28e713f2c2a5dd4c65f5b4`.
They refine candidate instrumentation points. They do not implement hooks or
establish complete writer coverage. Retained local analysis is in the task's
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

The current payload polls every 100 ms or later. It has no explicit lifecycle
events and declares freshness unknown. A 128-record RAM ring cannot preserve
all observations across long receiver outages or power loss. These remain code
and design work, in addition to the separate hardware validation. Passing an
inert first boot cannot make this payload a complete every-shot recorder.
