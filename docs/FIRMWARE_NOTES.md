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

### Handler/Callback Table (0x07DF08)
First 6 entries are menu item selection callbacks:

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
