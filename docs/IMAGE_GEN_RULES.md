# Image Generation Rules — KC02 Firmware Mod

Feed these constraints to your image generation model or use them when creating assets manually.

## Universal Rules

1. Every replacement MUST be the EXACT same byte size as the original it replaces
2. If smaller, pad with 0xFF bytes at the end to reach exact size
3. If larger, re-encode at lower quality until it fits — NEVER exceed original size
4. No alpha channels — the firmware doesn't support them
5. The LCD is 320x240 at 2.4" — design for low-res clarity

## BMP Assets (Icons, Digits, Controls)

### Format Requirements
- Windows BMP, BITMAPINFOHEADER (40-byte DIB header)
- 24-bit color, BGR byte order (Blue, Green, Red)
- Uncompressed (BI_RGB, no RLE)
- Bottom-up row order (standard BMP default)
- Rows padded to 4-byte boundary (standard)
- No palette, no ICC profile

### Background Color (Chroma Key)
- Most icons: RGB(140, 140, 140) / #8C8C8C — this is the transparency color
- Digit font (16x32): RGB(128, 128, 128) / #808080
- Tiny indicators (10x20): RGB(35, 35, 35) / #232323
- The firmware composites icons onto backgrounds using this gray as transparent

### Size Slots

| Use | Dimensions | Exact File Size | Notes |
|-----|-----------|----------------|-------|
| Main menu icons (selected) | 96x96 | 27,704 bytes | 6 slots (#30, 32-36) |
| Settings icons (selected) | 112x112 | 37,688 bytes | 15 slots (#63-77) |
| Settings icons (unselected) | 64x64 | 12,344 bytes | 14 slots (#78-91) |
| Game select icons | 120x120 | 43,256 bytes | 5 slots (#10-14) |
| Playback controls | 48x32 | 4,662 bytes | 2 slots (#28-29) |
| Counter digits | 16x32 | 1,590 bytes | 13 slots (#44-56) |
| Tiny indicators | 10x20 | 696 bytes | 2 slots (#92-93) |

### BMP Generation Command (ImageMagick example)
```bash
# Create a 96x96 BMP with exact format
convert input.png -resize 96x96! -depth 8 -type TrueColor BMP3:output.bmp

# Verify size
stat -f%z output.bmp  # must be exactly 27,704
```

## JPEG Assets (Screens, Backgrounds, Frames)

### Format Requirements
- Baseline sequential JPEG (SOF0) — NEVER progressive
- YCbCr color space, 3 components, 8-bit precision
- Chroma subsampling: 4:2:0 (preferred, works in all slots)
- JFIF APP0 marker (recommended but optional)
- No EXIF, no ICC profile, no thumbnails (waste bytes)
- Must end with FFD9 (EOI marker)

### Size Slots (each slot has a unique max size)

| # | Dimensions | Exact Size | Current Content |
|---|-----------|-----------|-----------------|
| 0-8 | 1280x720 | 21,593-95,655 | Photo frame overlays |
| 9 | 320x240 | 51,785 | Underwater scene |
| 15 | 320x240 | 16,192 | Blue space bg |
| 16 | 320x240 | 10,815 | Purple space bg |
| 17 | 320x240 | 32,313 | Teal space bg |
| 18 | 320x240 | 25,629 | Dark space bg |
| 26 | 320x240 | 7,035 | Tetris game UI |
| 31 | 320x240 | 41,106 | Main menu composite |
| 59 | 320x240 | 28,719 | Welcome screen |
| 60 | 320x240 | 28,719 | Welcome screen (dup) |
| 95 | 320x240 | 8,406 | SD card prompt |
| 96 | 320x240 | 8,120 | Video camera icon |

### Frame Overlays (1280x720)
- Pure black RGB(0,0,0) = transparent (camera image shows through)
- The firmware uses black as chroma key with slight threshold
- Avoid near-black colors (RGB 1-5) near transparent edges
- Keep decorative elements away from the center viewing area

### JPEG Size Targeting
```bash
# Encode to exact target size using cjpeg quality iteration
# Start high, decrease until file fits
cjpeg -quality 85 -baseline -sample 2x2 input.ppm > output.jpg
stat -f%z output.jpg  # adjust quality until exact match

# Pad with 0xFF if slightly under target
python3 -c "
import sys
target = 41106
data = open('output.jpg','rb').read()
if len(data) > target:
    sys.exit('TOO LARGE')
# Insert FF padding before EOI marker
pad = target - len(data)
patched = data[:-2] + b'\xff' * pad + data[-2:]
open('output_padded.jpg','wb').write(patched)
"
```
