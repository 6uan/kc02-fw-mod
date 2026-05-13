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
