# Factory USB loader backup

`tools/dump_firmware_loader.py` provides a separate backup route for the exact
`BuildWin / Video050Loader / 1.00` factory-loader profile. It avoids calling the
application functions at the fixed addresses used by `tools/dump_firmware.py`.
The initial host implementation supports macOS only.

The underlying method was verified on one HiMont unit with menu version
`2026/0526 v1.0`. That menu label does not classify its application as this
repository's `v1` or `v2` layout. The `2025/11/06` v2 unit, other loader versions,
Linux, and Windows have not been tested with this implementation. An existing
firmware backup also does not establish a working flash-restoration route.

## macOS setup and use

Use a USB data cable and leave the powered camera on its USB/SD screen.
Keep the card inserted. Remove any `DestBin.bin` update file before this
workflow, and save the backup on the computer. The tool refuses an output
directory on a mounted camera card, an existing output directory, ambiguous
device selection, or a card containing the update filename.

Install the same dependencies as the application-mode dumper:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install pyusb libusb-package
```

The tool requires an externally supplied factory RAM driver. It is not bundled
here. Retrieve the exact pinned file as data, without running the vendor tools:

```bash
mkdir -p firmware/loader-backups
curl --fail --location \
  https://raw.githubusercontent.com/Cool2Feel/Dwl_ISP/a76bb27ed569984b0f2f801ef431c0aea2aae45d/UpgradeTool/src/UpgradeTool.Core/Resources/ThunderSE.elf \
  --output firmware/loader-backups/ThunderSE.elf
shasum -a 256 firmware/loader-backups/ThunderSE.elf
```

Required SHA-256:

```text
85e9a4c512352b8e451af9e430384b8ceac120cb65ca2e03319b30284637434e
```

The reader checks that digest, the ELF load segment, and the relevant symbols
before USB access. A different file is rejected without an override.

Run from Terminal, using the absolute path to the virtual environment's Python
if needed:

```bash
sudo .venv/bin/python tools/dump_firmware_loader.py \
  --enter-loader \
  --driver firmware/loader-backups/ThunderSE.elf \
  --firmware-version '2026/0526 v1.0'
```

`--firmware-version` is optional local provenance supplied by the operator; it
does not select or validate a profile. `--output-dir` chooses a new host
directory. The default is `firmware/loader-backups/<UTC timestamp>/`.
If the device is already in this exact loader mode, use `--from-loader` instead
of `--enter-loader`. Unknown identities stop before SRAM upload.

macOS can keep the storage driver attached after a volume is unmounted.
Administrator execution alone does not release that driver. This tool maps the
camera's USB registry entry to its own disk, unmounts only that disk, and
explicitly captures the driver before claiming the interface. libusb's macOS
capture affects the whole camera, even though an interface number is supplied.
If Terminal reports a removable-volume permission error, resolve that OS
permission first; no alternate firmware addresses or automatic retry are used.

The output contains `read1.bin`, `read2.bin`, `sram-before.bin`, and
`report.json`. A failed or interrupted run may retain incomplete files.
Only a report with `two_full_reads_match: true` and two verified full-size reads
establishes a verified backup. The report includes the phase of a failed USB
transaction, received lengths, command status, hashes, and any cleanup error.
It does not collect the USB serial descriptor.

After the run, unplug and power-cycle the camera to return from temporary loader
mode to normal camera/card mode. The tool sends no automatic reset or command
retry. A successful dump is not an instruction to flash the image.

## Protocol and bounds

All commands use mass-storage Bulk-Only Transport, bulk OUT `0x01` and IN
`0x81`. Multibyte function, buffer, and callback fields are little-endian.

1. In normal `0219:3280` mode, claim interface 4 and require standard INQUIRY
   `Buildwin / Media-Player / 1.00`. Send one 16-byte CDB beginning `0xDA`, with
   IN direction and zero data length. Require its valid CSW.
2. Acquire the newly appeared `1908:3319` device immediately and retain that
   device object. Require USB `BUILDWIN / BLDR v1.00`, interface 0, and fresh raw
   INQUIRY fields `BuildWinVideo050Loader  1.00`. Do not fall back to a driver
   based on a partial name or matching VID/PID alone.
3. Through `0xCB`, function `RBC_mem_rwex_CPU/DMA=0x00100008`, preserve the
   driver-owned SRAM and upload the exact ELF PT_LOAD segment to
   `[0x0000, 0x0F00)`. Callback `0xFFFFFFFF` selects memory transfer without an
   L2 callback. Use seven 512-byte chunks and one 256-byte chunk; read every byte
   back before executing anything. The upload is 3,840 bytes, not a rounded
   4,096-byte image.
4. Initialize SPI with Func1=`0x24`, zero remaining CDB fields, OUT direction,
   and zero data length. The inspected driver path configures controller/pin
   state and completes through its loader ROM API.
5. Use `l1_func_signal_drive=0x74` with Ctrl=3, SiLen=1, and SPI `0x9F` to read
   the chip ID. Require a known manufacturer/type/density tuple from the pinned
   factory NOR table. Never infer a default 4 MiB capacity from an invalid ID.
6. Read the whole chip twice using Ctrl=7, SiLen=4, SPI `0x03`, and a three-byte
   **big-endian** flash address in CDB bytes 9–11. Each input chunk is 512 bytes.
   Ctrl=7 adds an SPI `0x05` busy-status read. It does not issue write-enable.

Example flash-read CDB at offset `0x123400`:

```text
CB 74 00 00 00 00 07 04 03 12 34 00 00 00 00 00
```

The bytes sometimes described as `DataAddr=0x04070000` encode Ctrl and SiLen
in this L1 command. They are not a device RAM address. The inspected driver's
actual read scratch buffer is `0x4800`, bounded by the host's 512-byte chunks.

Every transaction validates its data length and CSW signature, tag, residue,
and status. Short data, an early CSW, a failed mode switch, mismatched SRAM,
an unknown chip, or a USB error stops without replay. Flash program, erase,
write-enable, flash-status-write, and SD `WRITE(10)` are not exposed. Data-out
payloads are restricted by the caller to the exact verified SRAM driver.

Full images must have BLDR, boot `55 AA`, and SFAT markers and match byte for
byte across both fresh address passes. The boot-header sum is recorded, but
is not required to be zero: an original matching backup can have a nonzero
sum. This change does not loosen the original application dumper's guards.
Reading the beginning of ROM is not a compatibility test; the first ROM
kilobyte can read as zeros on this family.

## Sanitized hardware evidence

Observed on a Mac with PyUSB 1.3.1 and libusb 1.0.30:

| Item | Observed result |
|------|-----------------|
| Version-screen text | `2026/0526 v1.0` |
| Application USB | `0219:3280`, `GENERAL - AUDIO`, storage interface 4 |
| Loader USB after `0xDA` | `1908:3319`, `BUILDWIN`, `BLDR v1.00`, interface 0 |
| Fresh loader INQUIRY | `BuildWin / Video050Loader / 1.00` |
| Verified SRAM upload | 3,840 bytes read back identically before execution |
| SPI RDID | `85 60 16 85` |
| Capacity evidence | Factory table entry 14, `ID-9F=0x85601685`, `Capacity=0x400000` |
| Read 1 / read 2 | 4,194,304 bytes each; all bytes identical |
| SHA-256, both reads | `03bea62bc531ac0eec2b387069ceb30ba4256ec824d474bf3b719e2bef4e6519` |
| Independent host verification | `shasum -a 256` and `cmp` agreed after the files closed |
| Boot header | BLDR, `55 AA`, SFAT present; sum modulo 256 was 72 |
| Return to normal mode | Normal `0219:3280` and card mount returned after power-cycle; owner reported normal operation |

The earlier application-dumper log reported an I/O error inside its broad
"Checking compatibility" stage. That stage contains signature checks and
later RAM upload/execution steps, so the nonverbose log does not identify
the precise failed call. It does not prove that the camera crashed on the
first signature read. The original's three expected routine signatures were
absent at their reference file offsets in the recovered image.

The successful hardware session used a project-local reader with the same
driver, wire encodings, upload/readback, initialization, and full-read loops.
This upstream packaging and the stricter mode-failure, raw-identity, NOR-ID,
and immediate-handoff guards were validated with offline tests. The packaged
CLI has not been run against hardware again. No firmware image, SRAM dump,
raw USB trace, serial number, or local machine path is included here.

## Local tests and source provenance

After installing the USB dependencies, run locally:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Tests use synthetic firmware and fake endpoints. They cover wire layouts,
forbidden operations, framing/status errors, incompatible loader and NOR IDs,
SRAM verification before execution, complete matching/mismatching reads,
mode-switch failure and handoff, host-output checks, and interruption handling.
No camera, network download, or hosted workflow is needed for the tests.

Factory source pin: [`Cool2Feel/Dwl_ISP` at
`a76bb27ed569984b0f2f801ef431c0aea2aae45d`](https://github.com/Cool2Feel/Dwl_ISP/tree/a76bb27ed569984b0f2f801ef431c0aea2aae45d).

- [Device/driver table](https://github.com/Cool2Feel/Dwl_ISP/blob/a76bb27ed569984b0f2f801ef431c0aea2aae45d/UpgradeTool/src/UpgradeTool.Core/Resources/DeviceLib.ini).
- [Mode-switch construction](https://github.com/Cool2Feel/Dwl_ISP/blob/a76bb27ed569984b0f2f801ef431c0aea2aae45d/isp_tool/Device/Usb/UsbCmd/UsbFunction.cpp).
- [Original upload, verification, initialization, and reads](https://github.com/Cool2Feel/Dwl_ISP/blob/a76bb27ed569984b0f2f801ef431c0aea2aae45d/isp_tool/Device/Data/AX327X.cpp).
- [NOR identification/capacity table](https://github.com/Cool2Feel/Dwl_ISP/blob/a76bb27ed569984b0f2f801ef431c0aea2aae45d/UpgradeTool/src/UpgradeTool.Core/Resources/FlashLib.ini).
- [Pinned ELF driver](https://github.com/Cool2Feel/Dwl_ISP/blob/a76bb27ed569984b0f2f801ef431c0aea2aae45d/UpgradeTool/src/UpgradeTool.Core/Resources/ThunderSE.elf).
- [Independent family investigation, including zero-mapped ROM prefix](https://habr.com/ru/companies/ruvds/articles/779626/).

Static inspection of the exact ELF's selected init, signal-drive, and
status-read paths found no persistent flash-write operation. It cannot prove
all loader ROM internals or compatibility with another unit. Profile gates
and readback reduce that uncertainty; a hang/reset can still require a
power-cycle. Firmware restoration and theme-builder compatibility remain
separate work.
