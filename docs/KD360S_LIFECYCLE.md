# KD360S brew lifecycle evidence

This table describes selected paths in the pinned 1.0.13_HK application image.
It is not complete runtime coverage or an automatic shot segmentation contract.

## State model

Keep these values separate:

| Value | Address / location |
| --- | --- |
| Application flags F | `0x3fc9ca60` |
| Brew runtime R | `0x3fc9cb00` |
| Brew mode M | `0x3fc9cfe1` |
| Machine state S | `0x3fc9ce52`, serialized as TX byte 2 |
| Diagnostic structure D | `0x3fc9ff64` |
| Brew active | `F+6` |
| Pending brew stop | `F+0x0a` |
| Paddle kickoff / release events | `F+0x1f` / `F+0x20` |

`StartMakeCoffee(3)`, brew mode 3 and TX activity code 3 are different values.
The decoder must not put them into one enum.

## Observed transitions

| Transition | Trigger | Effect / evidence |
| --- | --- | --- |
| Inactive to active | Call `42005918(param)` from preparation, UI, paddle or diagnostic path | Reset counters/graph, set volume baseline, store param at R+4, set F+6 and F+0x27. |
| Paddle kickoff | `42004694` crosses calibrated paddle thresholds outside diagnostic mode | Set F+0x1f; state routine chooses preparation/start according to UI and mode. |
| Active to pending stop | Paddle release event on the selected brew screen | Set F+0x0a. |
| Active to pending stop | Runtime counter exceeds 4799 | Nominal 120 s limit from counter +4 per 100 ms tick. |
| Active to pending stop | Volume cap or profile endpoint, except modes 2 and 12 | Set F+0x0a; predicates depend on profile type. |
| Active to pending stop | Water-empty flag F+0x0e | Save mode and set F+0x0a. |
| Pending stop to inactive | Finalization branch in `420088d8` | Clear pending flag, call `420031b0`, clear F+6, set F+0x2e and hold timer 1200; update usage/history according to mode. |
| BrewHold to active | Kickoff while brew and hot-water flags are inactive | Restore mode and call `42005918(3)`, resetting runtime again. |
| Diagnostic to active | D.word==0x7777, stage zero and diagnostic enable | Select mode 5, start brew, set 30 s deadline. |
| Diagnostic to inactive | Stage one deadline | Call `420031b0` directly, bypassing normal pending-stop history path. |

Primary functions are start `42005918`, state machine `420088d8`, stop helper
`420031b0`, paddle event detection `42004694`, and diagnostic cycle `42009ea0`.
Mode preparation identifies 1 as manual VPS, 2 as semi-manual VPS, 5 as 9-bar,
12 as manual flow/VFS, and 16 as drip. Profile-derived mode selection at
`42004fd4` also distinguishes library/history sources and profile types.

For profile types 1 or 3, an elapsed-record count reaching profile length minus
one can request stop; manual VPS bypasses the type-1 branch. Type 0 uses either
elapsed duration, integrated flow, or a conjunction of duration and integrated
flow according to its fields. Units for all profile fields are not established.
Modes 2 and 12 skip volume/profile limits but still pass timeout and water-empty
checks. These paths should be tested on hardware before assigning end reasons.

## What a passive observer can infer

The base builder branch uses TX activity code 2 or 3 when S!=0 and F+6 is
active. Codes 0, 1, 4 and 5 accompany inactive F+6 in that branch, while S==0
initially selects code 6. **Later diagnostic branches can overwrite TX byte 3**
without consulting either S or F+6. The base mapping is not a contract for the
final transmitted value. Changing 2 to 3 also does not establish a shot boundary.

Diagnostic brewing uses the same active flag as beverage preparation. Manual
stop, timeout, volume/profile completion and water-empty causes converge on one
pending-stop flag and do not appear as separate reasons in TX byte 3. Valid RX
sets pending TX; the main loop sends the reply later. Brief transitions can be
missed between replies, and recording can begin partway through a brew.

Consequently the offline tool reports **raw machine state and activity code**,
without a derived brew-active Boolean, shot IDs, shot counts, or inferred end
reasons. UART silence is not an end event. Missing frames, power loss, diagnostic
operation and capture startup need explicit unknown/incomplete handling in a
future shot detector. Inspect the whole builder including its tail before using
an early branch as a lifecycle contract.

## Remaining gate for every-shot recording

A source must provide actual pressure and other required measurements together
with trustworthy lifecycle events and acquisition timestamps. The known passive
UART fields alone do not yet satisfy that contract. No stock Wi-Fi exporter has
been found. Firmware instrumentation is a candidate, but deployment would need
separate image-acceptance, recovery and runtime-impact validation.

When hardware is available, correlate both UART directions with normal start,
manual stop, timeout, volume/profile end, water-empty, BrewHold restart, drip and
diagnostic operation. Then test dropped frames, capture restart and outages.
Only that evidence can establish which shot boundaries are reliable and which
losses a source-side buffer can recover.
