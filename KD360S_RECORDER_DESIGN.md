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
Offsets 96..123 preserve the 28-byte accepted receive window. Offset 124 now
reports the buffer capacity in records (power of two, 128..8192). Older WPS2
packets contain zero, decoded as unknown. Update the collector before using this
firmware: older collectors reject this newly assigned field. Raw old archives
remain readable and unchanged. The accepted window is not a raw capture of
every UART byte.

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
periodic observations run at most every 100 ms during a shot. Between shots,
an observation is eligible every five seconds only when the queue is empty.
An unacknowledged idle record therefore prevents further idle records from
filling storage. A drained queue permits a later heartbeat to report cumulative
loss even if the last stop was dropped. Hooks never allocate or transmit.

The ring is single-producer in the bound loop task and single-consumer in the
network task. Full queues drop new records with sequence gaps and cumulative
loss. ACKs free records only after the collector commits identical bytes.
Finite RAM cannot promise unlimited outage or power-loss retention.

## Delivery and Wi-Fi ownership

The worker can send up to eight pending records every 10 ms. Each has its own
250 ms retry timer. Acknowledgments can arrive out of order, but the ring releases
only the contiguous acknowledged prefix. Duplicate, unsent, wrong-session and
wrong-sequence ACKs cannot release storage. Peer address/port validation remains
in the worker. A connection/socket failure resets the transmit window, retaining
all unreleased records for replay; the collector accepts identical replay after
commit. Neither data nor ACKs are authenticated.

This replaces stop-and-wait, which could need two 10 ms worker ticks per record.
The stock pressure-ready timer is nominally 25 ms (40 invocations/s before
coalescing); periodic observations can add 10/s and accepted RX adds further
events. A larger buffer alone cannot correct a sustained delivery deficit.
The eight-record window improves that constraint without claiming measured RF
or durable-storage throughput. Actual production and delivery rates remain a
hardware/collector acceptance test.

The pinned scheduler's pending-disconnect consumer has a 15-byte interior splice.
It keeps the stock path only when the active screen is nonzero and matches OTA,
Wi-Fi selection or password entry. Otherwise it clears the pending byte and
continues on the stock zero-flag path. Original callback code and its logging,
reason byte, restart/update branches and connection requests remain unchanged.
The patcher verifies all 100 bytes of guard instructions/literals plus stock
span, predecessor, referenced literals and callback digest. QEMU executes the
actual splice to both stock continuations, with live-register checks and a
deliberately incorrect continuation as a negative control.

The current worker still calls `WiFi.begin` once; subsequent SDK-native reconnect
does not guarantee recovery from every failure. The consumer guard is not a
complete ownership protocol. Autonomous retries require worker/UI serialization:
stock SDK commands can wait on an unbounded semaphore, so adding `begin` to the
control loop is rejected. The proposed task-scoped recursive gate and its four
API trampolines are under separate verification. They are not installed by this
unit. A late event after re-entering OTA can still be attributed to that screen.

## Bounded retention

The embedded fallback holds 128 records. At startup, before publishing the
context, the recorder queries the stock allocator for byte-addressable PSRAM
(caps 0x404). It makes at most one allocation: a power-of-two slab from 32 KiB
through 1 MiB, bounded by one quarter of reported free space and one half of
the largest contiguous block. Success selects 256..8192 records; insufficient
space or a returned NULL retains the embedded fallback. These fractions are
an experimental policy, not a measured safe memory reserve for this machine.
Availability queries do not reserve memory, and the stock allocator's failure
callback can have effects not established by this implementation. No deliberate
exhaustion, repeated allocation probe, PSRAM reinitialization, or flash write is
performed. Metadata and the embedded fallback still use the stock malloc policy.

The capacity and records pointer remain immutable after startup. Allocation
failure before context publication releases owned storage. Published or
uncertain bindings retain both allocations until reboot, including task creation
failure, so no hook or worker can observe freed memory.

Sixteen free slots are reserved for start/stop events: observations, pressure
and accepted RX packets drop once occupancy reaches capacity minus sixteen.
Boundaries can use the remaining slots. This preserves stop/start evidence under
sample saturation, but does not promise unlimited boundaries: sixteen slots can
hold at most eight complete additional start/stop pairs without delivery.
Reserved drops still advance sequence and cumulative loss; data is not silently
downsampled during a shot. ACK commits and ring order are unchanged.

No on-device durable journal or safe journal partition has been established.
Only packets committed by the collector survive machine power loss. Buffer
capacity, Wi-Fi ownership/retry, source and delivery rates, stock heap headroom,
and the real collector address must be validated before the every-shot goal can
be marked complete.

## Verification boundaries

Host tests exercise the production C event/context code. Actual Xtensa
disassembly and instruction execution test the adapters separately. Neither
proves control-loop timing or hardware recovery. Preflight must continue to
report those unverified gates rather than infer readiness from compilation.
