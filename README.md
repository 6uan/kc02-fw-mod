# KC02 Firmware Mod

Replace the stock kids UI on the [HiMont KC02 (U8) Kids Instant Print Camera](https://www.himont.us/products/kids-instant-camera-u8) with custom themes. Swap every icon, background, boot screen, photo frame overlay, and digit — no soldering required for flashing (SD card), but you need a SPI programmer to dump the original firmware first.

Also sold on [Amazon](https://www.amazon.com/HiMont-Camera-Instant-Selfie-Digital/dp/B0DDGWPVJM) (not an affiliate link).

<table align="center">
  <tr>
    <th align="center">Before (stock)</th>
    <th align="center">After (modded)</th>
  </tr>
  <tr>
    <td align="center"><img src="originals/jpeg/031_0x1A6102.jpg" width="320" alt="Stock UI"/></td>
    <td align="center"><img src="themes/modern_pixel_light/firmware_exports/07_menu_bg_320x240.jpg" width="320" alt="Modded UI"/></td>
  </tr>
</table>

## Background

I bought two of these cameras (one to sacrifice) and decided to reverse engineer the firmware to fully revamp UI. 

Here are some of the things and challenges I went through:

1. **Disassembly** — Pried off the black front cover (the side with the display and buttons), unscrewed the PCB, and located the flash chip (Zetta 25VQ32).
2. **Firmware dump** — Clipped an SOIC8 clip onto the flash chip, connected a CH341A USB programmer, and used `flashrom` to dump the full 4MB SPI flash to a `.bin` file.
3. **Reverse engineering** — Built a suite of Python tools to parse the firmware structure, locate the asset table (SFAT), extract all embedded images and sounds, and patch replacements back in.
4. **Theme creation** — Designed pixel art in [Aseprite](https://www.aseprite.org/) and built tooling to handle all the firmware format quirks (exact byte sizes, chroma key transparency, JPEG subsampling requirements).
5. **Iteration** — Lots of test flashes, a few bricks (recovered via CH341A), and gradually mapped out what's safe to modify and what isn't.

### Practical tips

- **Buy two cameras.** Use one for testing and keep the other stock. If you brick the test unit, you can recover with the SPI programmer, but having a working reference saves time.
- **Disconnect the battery before dumping.** The camera should not be powered during SPI reads/writes — remove or disconnect the battery first.
- **Keep your original dump backed up.** If the SD update routine gets corrupted (it lives in the app code, not the bootloader), the camera can't self-recover. The CH341A + your backup dump is the only way back.

## Hardware

| Part | Detail |
|------|--------|
| SoC | JieLi AC2546 (pi32 core) |
| Flash | Zetta 25VQ32, 4MB SPI NOR |
| LCD | 320x240, 2.4" IPS |
| Sensor | H63P |
| Buttons | ADC-based (6 buttons on single analog pin) |

This camera is sold under various brand names. If your camera has the same internals (check the SoC and flash chip), this toolkit will likely work.

## Requirements

- Python 3.8+
- Pillow (`pip install Pillow`) — for menu background compositing
- A CH341A USB SPI programmer + SOIC8 clip — for dumping original firmware
- A FAT32-formatted SD card — for flashing

## Getting Started

### 1. Dump your original firmware

**This is required.** The build tool patches your theme assets onto your original firmware dump — it can't generate a firmware from scratch. Without the dump, nothing else works.

We don't distribute the stock firmware (it's copyrighted). You need to dump it yourself using a CH341A SPI programmer and SOIC8 clip:


```bash
# macOS (install flashrom via Homebrew)
brew install flashrom
flashrom -p ch341a_spi -r firmware/original.bin

# Verify — dump it twice and compare
flashrom -p ch341a_spi -r firmware/verify.bin
md5 firmware/original.bin firmware/verify.bin  # must match
```

The dump should be exactly 4,194,304 bytes (4MB).

**Keep a backup somewhere safe.** If you brick the camera, this is your only recovery path (flash it back via CH341A).

### 2. Extract the original assets

```bash
python3 tools/extract_assets.py firmware/original.bin
```

This reads the SFAT asset table from the firmware and extracts all 97 embedded assets (BMPs, JPEGs, WAVs, raw data) into `originals/`. You'll use these as size references when creating replacement art.

### 3. Build a themed firmware

```bash
# Build using the included theme
python3 tools/build.py build

# Build and copy straight to SD card
python3 tools/build.py build --sd

# See all available slots
python3 tools/build.py list
```

The build tool reads your original dump, replaces matching assets from the theme directory, removes menu text labels, and writes `patched/DestBin.bin`.

### 4. Flash it

1. Format an SD card as FAT32
2. Copy `patched/DestBin.bin` to the SD card root
3. Insert the SD card and power on the camera
4. The camera flashes itself and reboots with the new firmware

If `--sd` is passed to the build command, it copies to `/Volumes/NO NAME` (the default FAT32 volume name on macOS) and ejects automatically.

## Tools

| Tool | What it does |
|------|-------------|
| `tools/build.py` | Build `DestBin.bin` from dump + theme assets. Supports button remapping, handler swaps, icon repositioning, and raw data patches. |
| `tools/extract_assets.py` | Extract all assets from a firmware dump using the SFAT table |
| `tools/compare.py` | Web UI (localhost:8080) — the main workspace for theme development |
| `tools/analyze.py` | Firmware structure analysis — find assets, map regions, detect formats |
| `tools/pi32_disasm.py` | pi32 disassembler for the JieLi CPU core |
| `tools/xrefdb.py` | Cross-reference database for firmware code analysis |
| `tools/ghidra_import.py` | Import labels/comments into Ghidra for RE work |
| `tools/ghidra_label.py` | Generate Ghidra label scripts |
| `tools/KC02Labels.java` | Ghidra script for applying KC02-specific labels |

### Compare UI (`tools/compare.py`)

The compare tool is the main workspace for theme development:

```bash
python3 tools/compare.py  # opens at localhost:8080
```

- **Side-by-side comparison** — view every original asset next to its theme replacement
- **Drag-and-drop conversion** — drop any image onto a slot and it auto-converts to the correct format (24bpp BMP or baseline JPEG), applies the chroma key background, and pads to the exact byte size
- **Icon positioning** — move icons around within their canvas using drag or arrow keys, with crosshair and bounding box guides
- **One-click build & flash** — builds the firmware and copies it to your SD card directly from the browser

For creating the pixel art itself, I used [Aseprite](https://www.aseprite.org/) (paid). Any pixel art editor works — the compare tool handles all the firmware format conversion when you drop images in.

### Menu background auto-generation

The main menu background is a composite: the 6 menu icons get rendered on top of a base image. This is necessary because the firmware draws the background first, then overlays icons using chroma key transparency. If the icons aren't baked into the background JPEG, they'll flash or be invisible.

The build tool handles this automatically. Place a `menu_bg_base.png` (or `.jpg`) in your theme's `firmware_exports/` directory and the builder composites all 6 menu icons at their grid positions, then encodes to a correctly-sized JPEG.

## Creating a Theme

A theme is a directory under `themes/` containing replacement assets. See `themes/modern_pixel_light/` for a complete example.

### Directory structure

```
themes/your_theme/
  firmware_exports/     # Ready-to-flash BMPs and JPEGs (exact firmware sizes)
  sources/              # Source artwork (PNGs, any resolution)
  previews/             # Preview images for the theme
  MANIFEST.md           # Asset listing
```

### Asset format rules

**Every replacement must be the exact same byte size as the original.**

**BMP** (icons, digits, controls):
- 24-bit uncompressed, BGR byte order, bottom-up rows
- Chroma key background: `#8C8C8C` (most icons), `#808080` (digits), `#232323` (tiny indicators)
- If your BMP is smaller than the slot, it gets zero-padded automatically

**JPEG** (screens, backgrounds, frames):
- Baseline sequential (SOF0), never progressive
- 4:2:0 chroma subsampling only (4:4:4 causes green screen)
- No EXIF, no ICC profile
- If smaller than the slot, padded with `0xFF` before the EOI marker

### Asset sizes

| Category | Dimensions | Exact bytes | Count |
|----------|-----------|------------|-------|
| Menu icons | 96x96 | 27,704 | 6 |
| Settings selected | 112x112 | 37,688 | 15 |
| Settings unselected | 64x64 | 12,344 | 14 |
| Game icons | 120x120 | 43,256 | 5 |
| Playback controls | 48x32 | 4,662 | 2 |
| Counter digits | 16x32 | 1,590 | 13 |
| Photo frames | 1280x720 | varies | 9 |
| Screens/BGs | 320x240 | varies | ~10 |

Run `python3 tools/build.py list` for the full slot table with exact sizes and firmware offsets.

### File naming

Name files to match slot names (`photo.bmp`, `boot_screen.jpg`) or use the numbered alias format (`01_photo_96.bmp`). The build tool auto-matches both. See `build.py` for the full alias table.

## Build Options

```bash
# Keep stock menu text labels
python3 tools/build.py build --keep-text

# Remap physical buttons
python3 tools/build.py build --remap-button up=down --remap-button down=up

# Swap menu icon positions
python3 tools/build.py build --swap-asset 0=4

# Reposition menu icons
python3 tools/build.py build --move-icons "12,18;112,18;212,18;12,126;112,126;212,126"

# Dry run (show what would change without writing)
python3 tools/build.py build --dry-run

# Write raw data patches (for firmware experiments)
python3 tools/build.py build --raw-patch 0x085010=80020000
```

## Firmware Structure

```
0x000000 - 0x0001FF  Header (BLDR magic, checksum, boot signature)
0x000200 - 0x0D31FF  Executable code (pi32) — DO NOT MODIFY
0x0D3200 - 0x0D350F  Asset table (SFAT) — 97 entries x 8 bytes
0x0D3510 - 0x3FFFFF  Asset data (BMP, JPEG, WAV, raw)
```

The asset data region (0x0D3510+) is the only safe zone for replacement. The code section contains pi32 instructions — modifying bytes there without disassembly will corrupt the firmware and potentially brick the camera.

## Recovery

If you brick the camera (SD update no longer works):

1. Open the camera case
2. Clip the SOIC8 onto the flash chip
3. Flash your backup: `flashrom -p ch341a_spi -w firmware/original.bin`
4. Reassemble and power on

The SD update routine lives in the app code section, not the bootloader. If code gets corrupted, the camera can't self-recover from SD — you'll need the SPI programmer.

## Reverse Engineering

The `reference/ghidra-jieli/` directory contains SLEIGH processor specifications for JieLi's pi32 and pi32v2 CPU cores (originally from [kagaimiq's ghidra-jieli](https://github.com/kagaimiq/ghidra-jieli)). These let you load the firmware into Ghidra for disassembly and analysis.

The `docs/` directory contains detailed firmware documentation:
- `ASSET_CATALOG.md` — all 97 assets mapped with offsets, sizes, and descriptions
- `FIRMWARE_NOTES.md` — flash validation, safe patch zones, memory layout, test results
- `IMAGE_GEN_RULES.md` — exact format specs for BMP and JPEG assets

## Related

- **[canon-2-thermal](https://github.com/6uan/canon-2-thermal)** — Convert images into a format the camera's thermal printer can read and print.

## License

Tools and documentation: MIT. Theme artwork in `themes/` is original and freely usable. The ghidra-jieli SLEIGH specs are from [kagaimiq](https://github.com/kagaimiq/ghidra-jieli) — see that repo for its license terms.

This project does not distribute any copyrighted firmware or original manufacturer assets.
