# KC02 Firmware Asset Catalog

Asset table at `0x0D3200` — 97 entries, 8 bytes each (LE offset + LE size).
Absolute address = `0x0D3200 + relative_offset`.

## Photo Frame Overlays (1280x720 JPEGs, entries #0-#8)

These are composited over photos. Black = transparent region where the camera image shows through.

| # | Offset | Size | Description |
|---|--------|------|-------------|
| 0 | 0x0D3510 | 26,226 | Boat on water (bottom) |
| 1 | 0x0D9B82 | 31,393 | Santa Claus face cutout |
| 2 | 0x0E1623 | 34,562 | Beach chairs + umbrella (bottom) |
| 3 | 0x0E9D25 | 49,712 | Whale + ocean stripe border |
| 4 | 0x0F5F55 | 21,593 | Bear face filter (eyes/nose/cheeks) |
| 5 | 0x0FB3AE | 53,938 | Bees + orange border |
| 6 | 0x108660 | 62,125 | Penguin + heart frame |
| 7 | 0x11790D | 54,913 | Swans + blue water border |
| 8 | 0x124F8E | 95,655 | Sailboat + nautical frame |

## Screens (320x240 JPEGs)

| # | Offset | Size | Description |
|---|--------|------|-------------|
| 9 | 0x13C535 | 51,785 | Underwater coral scene (menu/game bg?) |
| 15 | 0x17DC54 | 16,192 | Space background — BLUE |
| 16 | 0x181B94 | 10,815 | Space background — PURPLE |
| 17 | 0x1845D3 | 32,313 | Space background — TEAL |
| 18 | 0x18C40C | 25,629 | Space background — DARK GRAY |
| 26 | 0x19B3BD | 7,035 | Tetris game UI (SCORE / NEXT) |
| 31 | 0x1A6102 | 41,106 | **MAIN MENU** (6 icons: camera, video, music, playback, games, settings) |
| 59 | 0x217078 | 28,719 | Welcome/startup screen (kids + animals) |
| 60 | 0x21E0A7 | 28,719 | Welcome/startup screen (duplicate) |
| 95 | 0x302E93 | 8,406 | "Insert SD card" prompt |
| 96 | 0x304F69 | 8,120 | Video camera icon (yellow bg) |

## Game Select Icons (120x120 BMPs)

| # | Offset | Size | Description |
|---|--------|------|-------------|
| 10 | 0x148F7E | 43,256 | Bouncing balls game |
| 11 | 0x153876 | 43,254 | Maze/puzzle game |
| 12 | 0x15E16C | 43,256 | Crate/box game |
| 13 | 0x168A64 | 43,256 | Block puzzle (Tetris) |
| 14 | 0x17335C | 43,256 | Snake game |

## Main Menu Icons — SELECTED (96x96 BMPs)

| # | Offset | Size | Description |
|---|--------|------|-------------|
| 30 | 0x19F4CA | 27,704 | Music (treble clef, green) |
| 32 | 0x1B0194 | 27,704 | Games (controller, blue) |
| 33 | 0x1B6DCC | 27,704 | Camera/Photo (pink) |
| 34 | 0x1BDA04 | 27,704 | Playback (TV+play, purple) |
| 35 | 0x1C463C | 27,704 | Settings (gears, orange) |
| 36 | 0x1CB274 | 27,704 | Video (camcorder, yellow) |

## Playback Controls (48x32 BMPs)

| # | Offset | Size | Description |
|---|--------|------|-------------|
| 28 | 0x19D05E | 4,662 | Rewind (<<) |
| 29 | 0x19E294 | 4,662 | Fast forward (>>) |

## Digit Font (16x32 BMPs)

| # | Offset | Size | Description |
|---|--------|------|-------------|
| 44-53 | 0x1F284A+ | 1,590 ea | Digits 0-9 |
| 54 | 0x1F6666 | 1,590 | Blank/space |
| 55 | 0x1F6C9C | 1,590 | Colon (:) |
| 56 | 0x1F72D2 | 1,590 | Slash (/) |

## Settings Menu Icons — SELECTED (112x112 BMPs)

| # | Offset | Size | Description |
|---|--------|------|-------------|
| 63 | 0x24E5C2 | 37,688 | White balance (orange) |
| 64 | 0x2578FA | 37,688 | Date/time (clock, blue) |
| 65 | 0x260C32 | 37,688 | Resolution (frame, purple) |
| 66 | 0x269F6A | 37,688 | Storage/SD card (green) |
| 67 | 0x2732A2 | 37,688 | Frequency/Hz (red) |
| 68 | 0x27C5DA | 37,688 | Photo mode (camera, navy) |
| 69 | 0x285912 | 37,688 | Language (speech bubble, black) |
| 70 | 0x28EC4A | 37,688 | Video mode (camcorder, lime) |
| 71 | 0x297F82 | 37,688 | Print (printer, white) |
| 72 | 0x2A12BA | 37,688 | Print style (printer, brown) |
| 73 | 0x2AA5F2 | 37,688 | Brightness/exposure (red) |
| 74 | 0x2B392A | 37,688 | Gallery (landscape, orange) |
| 75 | 0x2BCC62 | 37,688 | Info/about (green) |
| 76 | 0x2C5F9A | 37,688 | File browser (blue) |
| 77 | 0x2CF2D2 | 37,688 | Volume/sound (green) |

## Settings Menu Icons — UNSELECTED (64x64 BMPs, gray)

| # | Offset | Size | Description |
|---|--------|------|-------------|
| 78-91 | 0x2D860A+ | 12,344 ea | Same icons as #63-77 but smaller + gray (unselected state) |

## Small Indicators (10x20 BMPs)

| # | Offset | Size | Description |
|---|--------|------|-------------|
| 92 | 0x30291A | 696 | Small dark indicator |
| 93 | 0x302BD2 | 696 | Small dark indicator |

## Non-Image Assets

| # | Offset | Size | Type | Description |
|---|--------|------|------|-------------|
| 19 | 0x192829 | 18,536 | raw | Grayscale UI elements (buttons/frames) |
| 20 | 0x197091 | 3,396 | WAV | Sound effect |
| 21 | 0x197DD5 | 432 | raw | Unknown data |
| 22 | 0x197F85 | 3,300 | raw | Unknown data |
| 23 | 0x198C69 | 1,176 | raw | Unknown data |
| 24 | 0x199101 | 8,100 | raw | Unknown data |
| 25 | 0x19B0A5 | 792 | raw | Unknown data |
| 27 | 0x19CF38 | 294 | raw | Unknown data |
| 37 | 0x1D1EAC | 3,428 | raw | Font data (ASCII 8x16) |
| 38 | 0x1D2C10 | 1,932 | WAV | Sound effect (short) |
| 39 | 0x1D339C | 2,638 | WAV | Sound effect (short) |
| 40 | 0x1D3DEA | 2,064 | WAV | Sound effect (short) |
| 41 | 0x1D45FA | 58,352 | WAV | Jingle (~1.8s) |
| 42 | 0x1E29EA | 51,824 | WAV | Jingle (~1.6s) |
| 43 | 0x1EF45A | 13,296 | WAV | Sound effect (~0.4s) |
| 57 | 0x1F7908 | 127,856 | raw | Icon sprite sheet (88 indexed-color icons) |
| 58 | 0x216C78 | 1,024 | raw | Palette or lookup table |
| 61 | 0x2250D6 | 83,832 | raw | Font #2 (32px, variable width) |
| 62 | 0x23984E | 85,364 | raw | Font #3 (CJK characters) |
| 94 | 0x302E8A | 9 | raw | Version string "DV_V1.0.0" |
