# Check a KD360S experimental package offline

Use this check before planning a supervised firmware trial. It reads files only.
It does not connect to a machine, upload firmware, or establish flash readiness.

## Verify the package

From the repository root, run:

```sh
python3 scripts/kd360s_preflight.py \
  --package private/firmware-preparation/entry-probe-trial \
  --trial-defer-confirm
```

For an ordinary image, omit `--trial-defer-confirm`. Selecting the wrong mode
fails the byte comparison. Supply `--stock PATH` if the pinned stock image is
outside the default cache.

New builds retain `payload.elf` beside `firmware.experimental.bin` and
`manifest.json`. For an older package, supply `--elf PATH`, or run its original
build command again. The builder can add the missing ELF only when the existing
files match exactly. It does not replace differing files. A manifest alone is
never enough for this check.

Expect `status: offline_package_verified`, `package_reproduced: true`, and
`ready_to_flash: false`. The command recreates the image from stock and ELF and
compares every image byte and builder-owned manifest field. The builder also
checks ESP integrity and reverses its changes to the exact pinned stock.
Exit 0 means that offline analysis succeeded. Exit 1 means rejected input.
Exit 2 means incorrect command arguments. None authorizes a flash.

The check does not establish that an ELF was compiled from reviewed source.
Keep the build record and review the source separately. Snapshot experiment
metadata is returned under `unverified_metadata`; its compiler and destination
claims are not proved by reconstructing an image from an ELF. The reference
snapshot uses `192.0.2.1`, which is not a usable collector destination.

## Inspect a private device dump

After obtaining a flash dump through a confirmed board-specific read/recovery
interface, keep it under `private/`. Record the interface, device identity,
physical flash size, read range, acquisition time, security report, and the
evidence for its partition-table offset. This tool assumes offset zero is the
first byte in the dump. It cannot verify that acquisition claim.

Set the two paths and the measured table offset before running:

```sh
python3 scripts/kd360s_preflight.py \
  --package private/firmware-preparation/entry-probe-trial \
  --trial-defer-confirm \
  --flash-backup "$DEVICE_DUMP" \
  --partition-table-offset "$PARTITION_TABLE_OFFSET"
```

Do not substitute a guessed table offset. The parser checks entry magic,
terminator, checksum when present, bounds, alignment, duplicate names, and
overlaps. It supports the binary table format described by the
[ESP-IDF 4.4.7 partition tool](https://github.com/espressif/esp-idf/blob/v4.4.7/components/partition_table/gen_esp32part.py).
Unrecognized partition flags are rejected. A table without MD5 is reported as
`not_present`, not checksum-verified.

Read each application partition's `candidate_fits` and
`pinned_stock_prefix_match`. Capacity is rounded to 4096-byte sectors. A small
slot or absent stock match appears as a negative finding in a valid report;
exit 0 does not mean those findings passed. The tool does not select an upload
destination. A pinned stock match in one slot does not identify the running slot.

The report includes hashes, partition labels, offsets, and sizes. It does not
copy NVS or credential bytes. Keep the full dump private. An apparently valid
table does not prove a complete backup, eFuse state, bootloader rollback, or a
working recovery route.

## Continue the hardware preparation

Follow [deployment preparation](KD360S_DEPLOYMENT_PREPARATION.md). Establish
independent recovery and actual image acceptance before the inert entry-probe
trial. Keep the first boot separate from networking and brewing tests.

For complete recording, follow the remaining
[lifecycle hook work](KD360S_HOOK_WITNESSES.md) and
[network ownership findings](KD360S_BOOT_AND_UPDATE.md#network-ownership-remains-unresolved).
The current snapshot remains a short-buffer experiment with unknown freshness.
