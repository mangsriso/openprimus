# Recorder implementation contract

The target is the pinned KD360S V1.0.13_HK image. The recorder must observe
stock start, stop, accepted RX and pressure-calibration execution, retain raw
values, and deliver immutable records to a collector. Flash readiness still
requires the device gates listed in docs/KD360S_DEPLOYMENT_PREPARATION.md.

## Selected hook design

Candidate A relocates only complete entry instructions and calls the original
through a wrapper. Candidate B relocates whole functions. A was selected because
it keeps the original branches and returns in place and requires less instruction
equivalence evidence. Both candidates were produced by Codex, following the
user's instruction not to call Opus/Fable. This reduces model diversity.

The pre-ENTRY dispatch uses a9 under the CALL8 ABI, preserving the return address
and six register arguments. Each wrapper allocates 80 bytes. The design proof
saved arguments at 16..36, below the window spill region at 48..60. Independent
review found that the original 64-byte proposal violated this rule. Production
needs only the first input at offset 16 because it has no before callback, while
retaining the tested 80-byte frame. No stack arguments
are accepted by the four pinned target functions. Assembly wrappers preserve
four integer return words, including the calibrated float's unchanged bits.

The builder verifies the stock digest, entry spans and literal witnesses before
patching four entry points. It reverses every edit and compares all stock bytes.
It does not permit arbitrary patches. The retained ELF is the preflight oracle.

## Context ownership

A genuine pthread key stores the heap context in the replacement loop task.
Hooks find that key through its unique destructor address under the stock key
registry lock, release the lock, then call pthread_getspecific. Existing TLS
slot 0 belongs to pthread and is never overwritten directly. Hooks in other
tasks or before binding return no context. The sender receives the context as
its task argument. The context and key remain allocated until reboot.

Binding returns success, failure with safe release, or cleanup anomaly requiring
retention. Only success starts recording. Stock setup/loop continue on failure.
Private registry layout, lock latency and stack/heap headroom are firmware and
hardware verification obligations.

## Event contract

WPS2 records are 128 bytes, little-endian. Offsets 0..59 retain WPS1's field
positions. Magic/version/length become WPS2/2/128. Kinds are observation=1,
start=2, stop=3, pressure acquisition=4, accepted RX=5. Offsets 60..95 contain
shot ID, pressure generation/time, RX generation/time, start argument,
calibrated pressure bits, filtered ADC input, and captured RX byte count.
Offsets 96..123 preserve the 28-byte accepted receive window. Offset 124 is
reserved zero. The accepted window is not a raw capture of every UART byte.

Flags are bit 0 provisional field interpretation, bit 1 pressure observed,
bit 2 RX observed, bit 3 unmatched stop, bit 4 start while a shot was open,
and bit 5 a source/shot counter saturated. Source generations advance for
every hooked invocation even if its numeric result repeats. They are distinct
from packet sequence and observation time. RX acceptance does not prove a
new physical sensor conversion. Pressure records contain the calibration
return before the caller clamps/stores it; common control fields can therefore
still describe the preceding update.

Each start invocation opens a new ID and emits after stock initialization.
Each stop emits after stock stop, then closes that ID. Unmatched and repeated
boundaries remain visible. Acquisition events emit while a shot is open;
periodic observations continue between shots. Hooks never allocate or transmit.

The ring is single-producer in the bound loop task and single-consumer in the
network task. Full queues drop new records with sequence gaps and cumulative
loss. ACKs free records only after the collector commits identical bytes.
Finite RAM cannot promise unlimited outage or power-loss retention. Buffer
capacity, Wi-Fi ownership/retry, source rates and the real collector address
must be validated before the every-shot goal can be marked complete.

## Verification boundaries

Host tests exercise the production C event/context code. Actual Xtensa
disassembly and instruction execution test the adapters separately. Neither
proves control-loop timing or hardware recovery. Preflight must continue to
report those unverified gates rather than infer readiness from compilation.
