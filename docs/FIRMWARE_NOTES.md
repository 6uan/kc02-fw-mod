# Firmware Technical Notes — KC02

## Flashing (DestBin.bin)

### How It Works
1. Place `DestBin.bin` on SD card root (FAT32)
2. Power on camera — bootloader detects file, flashes it
3. Camera reboots with new firmware

### Validation (What Gets Checked)
The upgrade process has MINIMAL validation:
- `BLDR` magic at offset 0x04
- Boot signature `0x55AA` at offset 0x1FE
- Validity markers `0x01234567` at offsets 0x30 and 0x34
- `SFAT` magic at offset 0x304
- 8-bit additive checksum on first 512 bytes (sum mod 256 must = 0)

### What Is NOT Checked
- No CRC32 on the full image
- No digital signatures
- No version comparison / anti-rollback
- No explicit file size validation
- Firmware is NOT encrypted

### Checksum Fix (only needed if header bytes change)
```python
# After modifying any byte in offset 0x00-0x1FF:
data = bytearray(open('DestBin.bin', 'rb').read())
data[0x0C] = 0  # zero out checksum byte first
header_sum = sum(data[0:512]) % 256
data[0x0C] = (256 - header_sum) % 256
open('DestBin.bin', 'wb').write(data)
```

### Flash Test Results

| Flash | Change | Result |
|-------|--------|--------|
| #15 | Dispatch table swap (entry 4 games→camera) | NO EFFECT — table not used for menu selection |
| #16 | ADC button remap (UP↔DOWN key_id swap) | SUCCESS — buttons swapped as expected |
| #17 | Asset index swap (entry 0↔4, camera↔games) | SUCCESS — icons visually swapped, handlers stay positional |
| #18 | Key event handler entry 1 (NULL→0x02004CC8) | NO EFFECT — long-press OK still does nothing |
| #19 | Key event handler swap (entry 0↔9) | NO EFFECT — OK and UP unchanged |

### Safety
- DestBin.bin IS the full flash image (starts at offset 0)
- The bootloader IS overwritten during flashing — no recovery if interrupted
- An empty or corrupt DestBin.bin WILL BRICK the camera
- Always keep original_firmware_backup.bin safe — it's your only recovery via SPI programmer
- File can be < 4MB (padding to 4MB with 0xFF is safe but not required)

## Flash #9 Failure Investigation (2026-05-13)

### Symptom
Camera stuck on boot screen with SD inserted. Never wrote to SPI flash.
Removing SD restored Flash #8. Flash #9 was the first build without 2x2
layout patching (reverted to stock 3x2).

### Binary Analysis of DestBin.bin (MD5: e41ddcdb5ce681152a74fe0a9bab5c84)

**Structurally valid — no corruption found:**
- Header (0x0000-0x01FF): byte-for-byte identical to stock dump1.bin
- Code section (0x0200-0x0D31FF): CRC32 match with stock (0xCCA5748A)
- SFAT table (0x0D3200-0x0D350F): byte-for-byte identical to stock
- Menu coord table (0x07E160): all 6 entries match stock
- Menu asset table (0x07E178): all 6 entries match stock
- Menu handler table (0x07DF08): all 6 entries match stock
- Count/max_idx immediates: stock values (6/5) intact at all 8 offsets
- Header checksum: verifies (sum mod 256 = 0)
- No secondary CRC/hash found covering data section
- Total changes: 1,661,942 bytes across 15,902 regions, ALL in asset data (0x0D3510+)

**What changed between Flash #8 (SUCCESS) and Flash #9 (FAILED):**
1. Layout patches removed (stock 3x2 vs patched 2x2)
2. Boot screen JPEG modified (35_boot_screen_320x240.jpg)
3. Menu icons modified (photo, video, gallery_stack, settings)
4. Menu background JPEG modified (05_menu_bg_320x240.jpg)
5. New music.bmp and games.bmp added (for 3x2 layout slots)
6. games.bmp was 27,702 bytes (slot expects 27,704) — padded with 0x00

**This violated the one-change-per-flash rule — 6 simultaneous changes.**

### BMP Size Discrepancy

Original firmware BMPs have file_size=27,704 in their BMP headers.
Theme replacement BMPs have file_size=27,702. Both use identical header
structures (14-byte file header + 40-byte BITMAPINFOHEADER = 54 data offset).
The 2-byte difference is trailing padding in the originals:
- Original: 54 + 27,648 pixels + 2 padding = 27,704
- Theme: 54 + 27,648 pixels = 27,702

music.bmp was manually padded to 27,704 (matching slot), games.bmp was not.
build.py correctly zero-pads undersized assets, so both render identically.

### Root Cause (CONFIRMED)

**The 2x2 layout code patches broke the SD update routine.**

Flash #10 confirmed: even flashing the unmodified stock dump1.bin (MD5 verified
on SD card) fails when Flash #8 is on SPI. The camera cannot self-update via SD.

The count patch at 0x002F6C (6→4) is ~108 bytes into the SD update routine
(~0x002F00+). The value 6 is not exclusively a menu item count — it is shared
by (or adjacent to) the update routine. Changing it to 4 corrupts update logic.

This is the same class of failure as Flash #4 (code patches disabling SD updates),
but more subtle: the immediate operand change was "verified safe" for menu
rendering, but the same code offset serves double duty in the update routine.

**2x2 layout via code patches is permanently ruled out.**

### Recovery

CH341A SPI programmer → flash original_firmware_backup.bin → stock 3x2 restored.
Then proceed with asset-only modifications (no code section changes).

---

## Menu Structure

### Main Menu Layout (0x07E160)
6 entries, each has (x, y) coordinates + asset index:

| Pos | Coords | Asset # | Item |
|-----|--------|---------|------|
| 0 | (12, 18) | #34 | Camera |
| 1 | (112, 18) | #37 | Video |
| 2 | (212, 18) | #31 | Music/MP3 |
| 3 | (12, 126) | #35 | Playback |
| 4 | (112, 126) | #33 | Games |
| 5 | (212, 126) | #36 | Settings |

Grid: 2 rows x 3 columns, 100px horizontal spacing, 108px vertical spacing.

### Menu Asset Index Table (0x07E178) — PATCHABLE
Confirmed patchable (Flash #17). Swapping entries 0↔4 visually swapped the
camera and games icons. Handlers remain positional — selecting position 0 still
launches camera regardless of which icon is drawn there. This table controls
rendering only, not functionality.

### Handler/Callback Table (0x07DF08)
**NOT used for menu item selection** — patching entry 4 (Games→Camera) had no
visible effect (Flash #15). The handlers are likely called via coded switch/case
with relative branch offsets. This table is used by the UI state machine's
main event loop, not for initial mode entry from the menu.

First 6 entries correspond to UI states:

| Entry | RAM Addr | Flash Code | Item |
|-------|----------|-----------|------|
| 0 | 0x0200411C | 0x00411C | Camera |
| 1 | 0x02003E74 | 0x003E74 | Video |
| 2 | 0x02003EF0 | 0x003EF0 | Music |
| 3 | 0x02003F90 | 0x003F90 | Playback |
| 4 | 0x02004014 | 0x004014 | Games |
| 5 | 0x02004098 | 0x004098 | Settings |

### Menu Item Count (Hardcoded in Code)
The value 6 is NOT in a data table — it's immediate operands in pi32 instructions:

**Must patch these for 6→4 change:**
- 0x002F6C: change 06→04 (main loop cursor bounds)
- 0x003B5C: change 06→04 (post-handler validation)
- 0x003E64: change 06→04 (post-handler validation)
- 0x004030: change 06→04 (post-handler validation)
- 0x004094: change 06→04 (pre-handler validation)
- 0x004288: change 06→04 (post-handler validation)

**Max index (6-1=5, change to 4-1=3):**
- 0x004064: change 05→03
- 0x00407C: change 05→03

**Row count (2 rows in 2x3, change to 2 rows in 2x2):**
- Addresses: 0x002F74, 0x003B64, 0x003E6C, 0x004038, 0x004058, 0x004090
- Keep as 2 if using 2x2 grid

## Hardware (Confirmed via SELFTEST.bin)

| Component | ID | Notes |
|-----------|-----|-------|
| SoC | AX3292 | JieLi AC2546 family, pi32 RISC core |
| Image sensor | H63P | |
| LCD | st3030b | 320x240 IPS |
| G-sensor | NULL | Not populated |
| U-sensor | offline | Not active |
| Buttons | ADC resistor ladder | Idle ~1019 |

### Button ADC Values
| Button | ADC Value |
|--------|-----------|
| Power | 22-23 |
| Left | 251-252 |
| OK | 387-390 |
| Down | 509-512 |
| Up | 655-660 |
| Right | 783-785 |

### ADC Button Threshold Table (0x0C4C80)
The firmware uses an ADC resistor ladder for button input. A threshold table at
file offset `0x0C4C80` maps ADC readings to key IDs. 7 entries × 8 bytes each:
`[u32 reserved (0)] [u16 key_id] [u16 adc_center]`

| Entry | Offset | Key ID | Button | ADC Center | Patch Bytes |
|-------|--------|--------|--------|------------|-------------|
| 0 | 0x0C4C80 | 0 | SENTINEL | 0 | -- |
| 1 | 0x0C4C88 | 26 | OK | 386 | 0x0C4C8C-8D |
| 2 | 0x0C4C90 | 30 | RIGHT | 776 | 0x0C4C94-95 |
| 3 | 0x0C4C98 | 29 | LEFT | 252 | 0x0C4C9C-9D |
| 4 | 0x0C4CA0 | 27 | UP | 657 | 0x0C4CA4-A5 |
| 5 | 0x0C4CA8 | 28 | DOWN | 510 | 0x0C4CAC-AD |
| 6 | 0x0C4CB0 | 36 | POWER | 12 | 0x0C4CB4-B5 |

**Remapping buttons**: Change the key_id field to remap physical buttons.
E.g., swapping UP/DOWN key_ids (27↔28) makes them act as each other.
Use `build.py --remap-button up=down --remap-button down=up`.

### ADC Driver Structure (0x0C4C44)
- Driver name: "ad-key" (string at 0x0C4C44)
- Init function: 0x002E0C
- Handler function: 0x003A60
- Key scan function: 0x004CC8

### Key Event Handlers (0x0C4CBC) — NOT PATCHABLE
19 x u32 values originally assumed to be function pointers (3 per button × 6 + sentinel).
Two patch tests (Flash #18: set entry, Flash #19: swap entries) had NO EFFECT.
Either copied to RAM at init (flash copy ignored) or misidentified as key event handlers.

## Hidden Firmware Modes

| Filename | Offset | Behavior |
|----------|--------|----------|
| DestBin.bin | 0x07DE87 | Standard firmware update |
| SELFTEST.bin | 0x07DFE5 | Factory diagnostic screen (shows hardware IDs, ADC, battery, sensor) |
| exmend.bin | 0x07DF94 | Unknown — possibly alternate update file, untested |

Place file on SD card root (FAT32) and power on. Camera checks for these filenames during boot.

## Memory Map

| Range | Size | Content |
|-------|------|---------|
| 0x000000-0x0001FF | 512 B | BLDR header |
| 0x000200-0x0002FF | 256 B | SFC timing params |
| 0x000300-0x0003FF | 256 B | SFAT header |
| 0x000400-0x0025FF | 8.5 KB | Boot init code |
| 0x002600-0x0C87FF | 835 KB | Application code |
| 0x0C8800-0x0D31FF | 43.5 KB | Flash chip config |
| 0x0D3200-0x0D3510 | 784 B | Asset table (97x8 byte entries) |
| 0x0D3510-0x3071FF | 2,256 KB | Asset data (JPEGs, BMPs, WAVs, etc.) |
| 0x307200-0x3FFFFF | 995 KB | Erased flash (0xFF) |

## UI State Machine (0x07DF08)

The dispatch table has **35 entries**, not just 6. Entries 0-5 are the main menu handlers.
16 entries point to the null/default handler at 0x02006DD4. ~13 unique active handlers exist.

| Entry | Handler | Function |
|-------|---------|----------|
| 0 | 0x0200411C | handler_camera |
| 1 | 0x02003E74 | handler_video |
| 2 | 0x02003EF0 | handler_music |
| 3 | 0x02003F90 | handler_playback |
| 4 | 0x02004014 | handler_games |
| 5 | 0x02004098 | handler_settings |
| 6 | 0x02006D58 | handler_state_06 |
| 12 | 0x02006CCC | handler_state_12 |
| 13 | 0x02006D00 | handler_state_13 |
| 14 | 0x02006CE0 | handler_state_14 |
| 17 | 0x02006CBC | handler_state_17 |
| 19 | 0x02006D10 | handler_state_19 |
| 20 | 0x02006CF8 | handler_state_20 |
| 21 | 0x02006D60 | handler_state_21 |
| 22 | 0x02006D6C | handler_state_22 |
| 27 | 0x02006DC0 | handler_state_27 |
| 32 | 0x02006D78 | handler_state_32 |
| 33 | 0x02006D90 | handler_state_33 |
| 34 | 0x02006DA8 | handler_state_34 |
| * | 0x02006DD4 | handler_null_default (16 entries) |

## Disassembly Setup (Ghidra)

**Processor**: JieLi pi32 — use [kagaimiq/ghidra-jieli](https://github.com/kagaimiq/ghidra-jieli)
**Import**: Raw binary, language `pi32:LE:32:default`, base address `0x02000000`
**Labels**: Run `tools/ghidra_label.py` via Script Manager (20 functions + 25 data labels)
**Generate**: `python3 tools/ghidra_import.py` regenerates the label script from source data

## Key Data Tables

| Offset | Name | Size | Content |
|--------|------|------|---------|
| 0x07DF08 | ui_state_dispatch | 35 x u32 | Function pointers for UI states |
| 0x07E000 | init_callback_table | 10 x u32 | Boot initialization callbacks |
| 0x07E160 | menu_coord_table | 6 x (u16,u16) | Menu icon x,y positions |
| 0x07E178 | menu_asset_index | 6 x u32 | SFAT indices for menu icons |
| 0x07E19C | mode_callback_table | 5 x u32 | Mode transition callbacks |
| 0x082A4C | dispatch_148 | 148 x u32 | Large dispatch table (unknown) |
| 0x0844E4 | dispatch_191 | 191 x u32 | Largest dispatch table (unknown) |
| 0x0C4C44 | adc_driver_struct | ~60 B | ADC button driver (name, init, handler) |
| 0x0C4C80 | adc_button_table | 7 x 8 B | Button ADC thresholds + key IDs |
| 0x0C4CBC | key_event_handlers | 19 x u32 | Button event callbacks (short/long/hold) |
| 0x0CA660 | lookup_399 | 399 entries | Possibly sensor register table |
