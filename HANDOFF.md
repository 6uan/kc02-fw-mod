# KC02 Firmware Mod — Chat Handoff

## Project Summary

Modifying the firmware of a HiMont KC02 Kids Instant Print Camera to replace the colorful kids UI with a modern, sleek design. The 4MB SPI flash (Zetta 25VQ32) was dumped, analyzed, and a build tool created for iterative firmware modification.

## Hardware

- **SoC**: JieLi AC2546 (杰理 2546T-92), pi32 custom core
- **Flash**: Zetta 25VQ32, 4MB SPI NOR
- **LCD**: 320x240 2.4" IPS
- **Flash method**: `DestBin.bin` on FAT32 SD card root -> power on
- **Recovery**: CH341A SPI programmer + SOIC8 clip (flashrom on macOS)

## Current State (2026-05-13)

- Camera running **Flash #8** firmware (2x2 grid, theme assets, no text)
- **Flash #9 FAILED** — First build without layout patching (stock 3x2). Camera stuck on boot screen with SD inserted, never wrote to SPI. Removing SD restored Flash #8. Needs investigation.
- Original dump: `/Users/mac/dump1.bin` (MD5: `7236c4c1bd3f12b1d85d33a9bebdf510`)
- NEVER-MODIFY backup: `/Users/mac/original_firmware_backup.bin`
- Project repo: `/Users/mac/Developer/kc02-fw-mod/` -> GitHub private: `6uan/kc02-fw-mod`

## CRITICAL OPEN ISSUE

**Flash #9 failure: stock 3x2 layout doesn't boot.**
- Flash #8 (2x2 patched layout) works fine with all 72 replacement assets
- Flash #9 (stock 3x2, same assets, only difference is no layout patching) gets stuck on boot screen
- The build starts from dump1.bin (original dump), so stock 3x2 values should be intact
- Text labels are removed (num_chars=0) in both builds
- The 2x2 layout code was removed from build.py — but the camera still has Flash #8 (2x2) on SPI
- **Possible causes to investigate:**
  1. dump1.bin may have been modified in a previous session (verify MD5 matches original)
  2. The stock 3x2 layout references 6 menu assets — one might be malformed or wrong size
  3. Stock layout references asset indices 0x1F (music) and 0x21 (games) which were being hidden in 2x2 — their theme replacements may be problematic
  4. Some other data table dependency that the 2x2 patch was accidentally fixing
- **To test:** Flash #9 DestBin.bin again to reproduce, then compare hex of Flash #8 vs #9 DestBin.bin to find differences beyond the layout tables

## What Works (Tested & Verified)

- Asset replacement (BMP/JPEG swap in DATA section) — fully safe
- Menu text label removal (num_chars=0 in string resource) — safe
- 2x2 layout patching (count/max_idx/coords/assets/handlers) — safe, VERIFIED WORKING
- Boot screen replacement — safe
- Build tool: `python3 tools/build.py build --sd`
- Web UI: `python3 tools/compare.py` at localhost:8080

## What Broke (DO NOT REPEAT)

- **Blind-patching `e007` bytes** at 6 code offsets thinking they were standalone RGB565 green color values. They are part of pi32 instruction words. Changing them corrupted boot code AND the SD update routine, bricking the device (Flash #4).
- **The SD firmware update routine lives in the APP CODE section, NOT the BLDR bootloader.** Corrupting code = no self-recovery from SD.
- **Stock 3x2 layout from dump1.bin** — Flash #9 stuck on boot screen. Cause unknown. (Flash #8 with 2x2 patching works fine.)

## Safe Patch Zones

| Region | Offset Range | Safe? |
|--------|-------------|-------|
| Asset pixel data | 0x0D3510+ | YES |
| Menu coord table | 0x07E160 (24 bytes) | YES — data table |
| Menu asset index table | 0x07E178 (24 bytes) | YES — data table |
| Menu handler table | 0x07DF08 (24 bytes) | YES — must point to valid functions |
| Menu label records | 0x239C5A+ | YES — string resource |
| Count immediates (first byte only) | 0x002F6C, 0x003B5C, etc. | YES — verified operand |
| Max idx immediates (first byte only) | 0x004064, 0x00407C | YES — verified operand |
| **EVERYTHING ELSE in 0x000200-0x0D3200** | **Code section** | **NO — pi32 instructions, do not touch** |

## Firmware Structure

- 0x000000-0x0001FF: Header (BLDR magic, checksum, boot sig)
- 0x000200-0x0D31FF: Executable code (pi32) — DANGER ZONE
- 0x0D3200-0x0D350F: Asset table (SFAT) — 97 entries x 8 bytes
- 0x0D3510-0x3FFFFF: Asset data (BMP, JPEG, WAV, raw)

## SFAT Numbering

**SLOT N in build.py = SFAT entry N+1 in the firmware.**
Menu asset indices in the asset_table use raw SFAT numbers. Example: `0x22` = SFAT#34 = SLOT 33 = "photo" icon.

## Build Tool Usage

```bash
cd /Users/mac/Developer/kc02-fw-mod

# Build with stock 3x2 layout, copy to SD, eject
python3 tools/build.py build --sd

# Keep text labels (diagnostic/debugging)
python3 tools/build.py build --keep-text --sd

# List all asset slots
python3 tools/build.py list
```

## Web UI (compare.py)

```bash
python3 tools/compare.py  # localhost:8080
```

Features:
- **Asset Compare**: Side-by-side original vs theme for all 73 slots
- **Drag & Drop Convert**: Drop any image onto a card's THEME side → auto-resize, reformat (24bpp BMP / baseline JPEG), apply chroma key (#8C8C8C), pad to exact byte size
- **Move Tool**: Click "Move" on any BMP card → drag/arrow-key icon within canvas, crosshair + bounding box guides, lossless pixel-shift save
- **Build & Flash**: One-click button runs build.py build --sd

## Theme Assets

Active theme: `themes/modern_pixel_light/firmware_exports/` (72 files)
Source pixel art: `themes/modern_pixel_light/sources/`
Original extracted: `originals/bmp/`, `originals/jpeg/`, `originals/wav/`

### Icon Generation Prompt

```
Pixel art icon, 32-bit retro style, chunky pixels with visible pixel grid,
warm earthy color palette (browns, oranges, deep reds, cream), thick dark outlines,
soft shading, cute kawaii aesthetic, single centered subject on a plain solid
gray (#8C8C8C) background, no text, no UI elements, square composition
```

### Chroma Key Colors (firmware transparency)
- Most icons (menu, settings, games, controls): RGB(140, 140, 140) / #8C8C8C
- Digit font (16x32): RGB(128, 128, 128) / #808080
- Tiny indicators (10x20): RGB(35, 35, 35) / #232323

The convert pipeline auto-detects background from corner pixels and replaces with the correct chroma key (tolerance ±30).

## Menu Layout Reference (stock 3x2)

```python
MENU_LAYOUT_REF = {
    "coord_table":  0x07E160,   # 6 × (u16 x, u16 y)
    "asset_table":  0x07E178,   # 6 × u32 asset_index
    "handler_table": 0x07DF08,  # 6 × u32 function_ptr
    "coords": [(12,18),(112,18),(212,18),(12,126),(112,126),(212,126)],
    "assets": [0x22, 0x25, 0x1F, 0x23, 0x21, 0x24],  # Photo, Video, Music, Playback, Games, Settings
    "handlers": [0x0200411C, 0x02003E74, 0x02003EF0, 0x02003F90, 0x02004014, 0x02004098],
    "count": 6, "max_idx": 5,
}
```

## Flash History (Summary)

| Flash | Result | Notes |
|-------|--------|-------|
| #1 | SUCCESS | White boot screen only |
| #2 | SUCCESS | Theme assets, 3x2 grid |
| #3 | PARTIAL | 2x2 works but green highlight + nav issues |
| #4 | BRICKED | Code patches corrupted pi32 instructions |
| #5 | FAILED | SD recovery impossible (update routine corrupted) |
| #6 | SUCCESS | SPI recovery via CH341A |
| #7 | SUCCESS | 2x2 layout, all theme assets |
| #8 | SUCCESS | 2x2 layout, all theme assets (current on camera) |
| #9 | FAILED | Stock 3x2 from dump1.bin — stuck on boot screen, never wrote to SPI |

## Key Docs

- `docs/ASSET_CATALOG.md` — All 97 firmware assets mapped
- `docs/FIRMWARE_NOTES.md` — Menu structure, memory map, code patch locations
- `docs/IMAGE_GEN_RULES.md` — BMP/JPEG format specs for asset generation

## Rules for Next Session

1. **ONE change per flash.** Test each independently.
2. **Never patch code section bytes** unless you have pi32 disassembly confirming instruction boundaries.
3. **Document flash results** in conversation — Claude generates flash logs on request.
4. **Always have CH341A ready** as backup recovery.
5. Data table patches are safe. Code patches need RE work first.
6. **Investigate Flash #9 failure** before removing 2x2 layout support permanently.
7. The user cannot read raw binary — Claude owns all firmware documentation.
8. Bricking is a minor setback, not a blocker. Don't be overly cautious.
