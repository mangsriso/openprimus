# KD360S UART observations

These findings apply to the pinned `KD360S_OTA_1.0.13_HK_20260630.bin` image in
[KD360S findings](KD360S_1.0.13_HK.md). They come from Ghidra decompilation and
instruction inspection, not a physical UART recording. All offsets below are
zero-based wire offsets, starting at the header byte.

## Receive frames

The parser at `4200545c` checks header `0x67`, checksum at wire offset 26 and
complement at 27. With `L = wire[1]`, it sums `wire[0:L]` modulo 256 and compares
against `wire[26]`. It also requires `wire[27] == wire[26] ^ 255`.

The original receive structure reserves two bytes before the wire buffer.
Decompiler expressions `buffer + 0x1c` and `buffer + 0x1d` therefore mean wire
26 and 27, not wire 28 and 29. The firmware does not explicitly guard `L == 26`
or verify the received byte count here. The offline decoder supports the
**provisional 28-byte profile with L=26**. Other lengths remain raw evidence and
produce diagnostics. This policy is narrower than the firmware parser and must
be checked against real captures before claiming protocol compatibility.

| Wire offsets | Representation | Interpretation / limit |
| --- | --- | --- |
| 0 | `0x67` | Header |
| 1 | unsigned byte | Checksum span L; decoder supports 26 |
| 2 | unsigned byte | Raw status |
| 3..4 | little-endian unsigned 16-bit | Raw flags |
| 5, 7, 8 | unsigned bytes | Temperature-like fields; sensor identity and units unverified |
| 9..12 | little-endian IEEE-754 float32 | Wire flow value before firmware correction |
| 17..18 | little-endian unsigned 16-bit | Firmware shifts right one bit into sensor volume field; units unverified |
| 19..22 | ASCII `0x31` means true | Four Boolean inputs |
| 23 | unsigned byte | Echoed at TX offset 34 |
| 24..25 | little-endian unsigned 16-bit | Unknown field |
| 26, 27 | unsigned bytes | Checksum and complement |
| Other offsets | opaque bytes | Retain without assigning meaning |

The float32 interpretation requires instruction inspection. The decompiler
prints an integer expression and numeric cast. Instructions at `42005514..4200551c` compose bits, `42005527` stores them,
and `4200552f` transfers them to a float register. The effective
four bytes are 9..12. This is not an integer with a decimal scale. Nonfinite
values retain raw bits and produce diagnostics rather than nonstandard JSON.

The firmware clears raw flag bit 0x02 unless byte 5 is 0x96 or 0x19. It similarly
clears bits 0x04 and 0x08 unless bytes 7 and 8 are 0xdc or 0x19. Raw flags and
these normalized flags are distinct observations.

## Transmit frames

The main loop writes 37 bytes. Builder `420032ac` sets header `0x76` and byte 1
to `0x22`. Checksum at 35 is `sum(wire[0:35]) & 255`; complement at 36 is checksum
XOR 255. The hardware loop at `42003693` executes 35 iterations. Ghidra's
pseudocode omitted that loop and showed only a partial sum, so the decoder uses
the instruction-verified formula. Length byte 34 does not mean a 34-byte frame.

| Wire offsets | Meaning established from builder |
| --- | --- |
| 2 | Machine state byte |
| 3 | Aggregate activity code |
| 8, 9, 10 | Echoes of the three received temperature-like bytes |
| 31..32 | Low 16 bits from sensor volume field |
| 33 | State-dependent control code; one branch copies control structure +0x18 |
| 34 | Echo of RX byte 23 |
| 35, 36 | Checksum and complement |

Other fields remain raw unless their source and semantics are established.
The known RX and TX mapping does not expose numeric actual pressure. Helper
`42006618`, used for TX byte 14, compares sensor actual pressure against target
pressure in one branch. That produces a control Boolean, not a pressure sample.
Other TX bytes derive from heater settings, state and control commands. These
fields cannot reconstruct a numeric pressure curve without further evidence.

## Calibration and freshness

Pressure uses a separate GPIO19 ADC path. Assembly for `42003da4` establishes
`filtered_adc * slope + intercept`, floored at zero, with zero also returned
below the configured ADC threshold. The main loop clamps the result to 15.
The threshold is not subtracted from ADC in this function. The coefficients
live in runtime settings, outside initialized image segments; they must not be
replaced with guessed defaults from KD360X documentation.

`42004670` shifts seven prior values within an eight-element history and returns the input flow value.
Its pseudocode omits the hardware loop. It does not average those samples in
this function. The parser first checks wire flow plausibility and applies
`4200bb38`, a pressure-dependent correction, before the main loop copies flow
into control state. Therefore a decoded wire float is not the final calibrated
actual flow shown or used by firmware.

A nominal 25 ms control tick can copy cached UART data repeatedly. Timestamp a
captured frame by its host receive event and retain that provenance. Do not
label every control tick a fresh sensor sample.

## How to reproduce the investigation

Use the pinned import workflow in [KD360S findings](KD360S_1.0.13_HK.md), then
run `TraceSlim.java` against these function addresses:

```text
4200545c 420032ac 42004210 42003da4 42004670 4200bb38 42005430 420fe20c
```

Inspect the instruction listing as well as pseudocode. In particular, verify
RX float bit transfers and the TX loop at `42003693`. The accompanying
[capture literals](../analysis/kd360s/1.0.13_HK/capture-literals.json) record
selected words resolved through the exact image segment map. Neither these
literal values nor the offline test fixtures are measurements from a machine.

`DumpCapture.java <output-file> <hex-function-address>...` exports instruction
listings from the mapped project. For example, after the pinned image import:

```bash
analyzeHeadless ghidra_project SlimAudit \
  -process KD360S_OTA_1.0.13_HK_20260630.bin -noanalysis \
  -scriptPath ghidra_scripts \
  -postScript DumpCapture.java ghidra_out/kd360s/uart-assembly.txt \
    4200545c 420032ac 42004210 42004670 42003da4
```

Like TraceSlim, this helper can create missing function definitions in the
analysis project. Generated instruction listings stay under ignored `ghidra_out/`.
