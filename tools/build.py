#!/usr/bin/env python3
"""
KC02 Firmware Builder
=====================
Builds DestBin.bin from original dump + replacement assets.

Usage:
    python3 build.py                    # build from default asset dir
    python3 build.py --assets <dir>     # build from specific asset dir
    python3 build.py --no-text          # remove menu text labels
    python3 build.py --dry-run          # show what would change, don't write
    python3 build.py --sd               # copy to SD card after build

Asset directory should contain files named to match slots:
    photo_96.bmp, video_96.bmp, menu_bg.jpg, boot_screen.jpg, etc.
    Or use firmware asset index: asset_031.jpg, asset_033.bmp, etc.
    Any file matching a known slot will be patched in.
"""

import struct
import os
import sys
import shutil
import argparse
import hashlib
import json
from pathlib import Path

# ─── Paths ───
PROJECT = Path("/Users/mac/Developer/kc02-fw-mod")
DUMP = Path("/Users/mac/dump1.bin")
BACKUP = Path("/Users/mac/original_firmware_backup.bin")
OUTPUT = PROJECT / "patched" / "DestBin.bin"

# ─── Asset Slot Definitions ───
# Each slot: (asset_index, flash_offset, byte_size, format, name, tags[])
SLOTS = [
    # Photo frame overlays (1280x720 JPEG)
    (0,  0x0D3510, 26226, "jpg", "frame_1",         ["frame"]),
    (1,  0x0D9B82, 31393, "jpg", "frame_2",         ["frame"]),
    (2,  0x0E1623, 34562, "jpg", "frame_3",         ["frame"]),
    (3,  0x0E9D25, 49712, "jpg", "frame_4",         ["frame"]),
    (4,  0x0F5F55, 21593, "jpg", "frame_5",         ["frame"]),
    (5,  0x0FB3AE, 53938, "jpg", "frame_6",         ["frame"]),
    (6,  0x108660, 62125, "jpg", "frame_7",         ["frame"]),
    (7,  0x11790D, 54913, "jpg", "frame_8",         ["frame"]),
    (8,  0x124F8E, 95655, "jpg", "frame_9",         ["frame"]),

    # Game icons (120x120 BMP)
    (10, 0x148F7E, 43256, "bmp", "game_1",          ["game"]),
    (11, 0x153876, 43254, "bmp", "game_2",          ["game"]),
    (12, 0x15E16C, 43256, "bmp", "game_3",          ["game"]),
    (13, 0x168A64, 43256, "bmp", "game_4",          ["game"]),
    (14, 0x17335C, 43256, "bmp", "game_5",          ["game"]),

    # Background screens (320x240 JPEG)
    (15, 0x17DC54, 16192, "jpg", "bg_1",            ["bg"]),
    (16, 0x181B94, 10815, "jpg", "bg_2",            ["bg"]),
    (17, 0x1845D3, 32313, "jpg", "bg_3",            ["bg"]),
    (18, 0x18C40C, 25629, "jpg", "bg_4",            ["bg"]),

    # Playback controls (48x32 BMP)
    (28, 0x19D05E, 4662,  "bmp", "rewind",          ["control"]),
    (29, 0x19E294, 4662,  "bmp", "forward",         ["control"]),

    # Main menu icons (96x96 BMP)
    (30, 0x19F4CA, 27704, "bmp", "music",           ["menu", "menu_icon"]),
    (31, 0x1A6102, 41106, "jpg", "menu_bg",         ["menu"]),
    (32, 0x1B0194, 27704, "bmp", "games",           ["menu", "menu_icon"]),
    (33, 0x1B6DCC, 27704, "bmp", "photo",           ["menu", "menu_icon"]),
    (34, 0x1BDA04, 27704, "bmp", "playback",        ["menu", "menu_icon"]),
    (35, 0x1C463C, 27704, "bmp", "settings",        ["menu", "menu_icon"]),
    (36, 0x1CB274, 27704, "bmp", "video",           ["menu", "menu_icon"]),

    # Digits (16x32 BMP)
    (44, 0x1F284A, 1590,  "bmp", "digit_0",         ["digit"]),
    (45, 0x1F2E80, 1590,  "bmp", "digit_1",         ["digit"]),
    (46, 0x1F34B6, 1590,  "bmp", "digit_2",         ["digit"]),
    (47, 0x1F3AEC, 1590,  "bmp", "digit_3",         ["digit"]),
    (48, 0x1F4122, 1590,  "bmp", "digit_4",         ["digit"]),
    (49, 0x1F4758, 1590,  "bmp", "digit_5",         ["digit"]),
    (50, 0x1F4D8E, 1590,  "bmp", "digit_6",         ["digit"]),
    (51, 0x1F53C4, 1590,  "bmp", "digit_7",         ["digit"]),
    (52, 0x1F59FA, 1590,  "bmp", "digit_8",         ["digit"]),
    (53, 0x1F6030, 1590,  "bmp", "digit_9",         ["digit"]),
    (54, 0x1F6666, 1590,  "bmp", "blank",           ["digit"]),
    (55, 0x1F6C9C, 1590,  "bmp", "colon",           ["digit"]),
    (56, 0x1F72D2, 1590,  "bmp", "slash",           ["digit"]),

    # Boot / prompt screens (320x240 JPEG)
    (59, 0x217078, 28719, "jpg", "boot_screen",     ["screen"]),
    (60, 0x21E0A7, 28719, "jpg", "boot_screen_2",   ["screen"]),
    (95, 0x302E93, 8406,  "jpg", "insert_sd",       ["screen"]),
    (96, 0x304F69, 8120,  "jpg", "video_rec",       ["screen"]),

    # Settings selected (112x112 BMP)
    (63, 0x24E5C2, 37688, "bmp", "white_balance_sel",   ["settings", "sel"]),
    (64, 0x2578FA, 37688, "bmp", "date_time_sel",       ["settings", "sel"]),
    (65, 0x260C32, 37688, "bmp", "resolution_sel",      ["settings", "sel"]),
    (66, 0x269F6A, 37688, "bmp", "storage_sel",         ["settings", "sel"]),
    (67, 0x2732A2, 37688, "bmp", "frequency_sel",       ["settings", "sel"]),
    (68, 0x27C5DA, 37688, "bmp", "photo_mode_sel",      ["settings", "sel"]),
    (69, 0x285912, 37688, "bmp", "language_sel",         ["settings", "sel"]),
    (70, 0x28EC4A, 37688, "bmp", "video_mode_sel",      ["settings", "sel"]),
    (71, 0x297F82, 37688, "bmp", "print_sel",           ["settings", "sel"]),
    (72, 0x2A12BA, 37688, "bmp", "print_style_sel",     ["settings", "sel"]),
    (73, 0x2AA5F2, 37688, "bmp", "brightness_sel",      ["settings", "sel"]),
    (74, 0x2B392A, 37688, "bmp", "gallery_sel",         ["settings", "sel"]),
    (75, 0x2BCC62, 37688, "bmp", "info_sel",            ["settings", "sel"]),
    (76, 0x2C5F9A, 37688, "bmp", "file_browser_sel",    ["settings", "sel"]),
    (77, 0x2CF2D2, 37688, "bmp", "volume_sel",          ["settings", "sel"]),

    # Settings unselected (64x64 BMP)
    (78, 0x2D860A, 12344, "bmp", "white_balance_unsel", ["settings", "unsel"]),
    (79, 0x2DB642, 12344, "bmp", "date_time_unsel",     ["settings", "unsel"]),
    (80, 0x2DE67A, 12344, "bmp", "resolution_unsel",    ["settings", "unsel"]),
    (81, 0x2E16B2, 12344, "bmp", "storage_unsel",       ["settings", "unsel"]),
    (82, 0x2E46EA, 12344, "bmp", "frequency_unsel",     ["settings", "unsel"]),
    (83, 0x2E7722, 12344, "bmp", "photo_mode_unsel",    ["settings", "unsel"]),
    (84, 0x2EA75A, 12344, "bmp", "language_unsel",       ["settings", "unsel"]),
    (85, 0x2ED792, 12344, "bmp", "video_mode_unsel",    ["settings", "unsel"]),
    (86, 0x2F07CA, 12344, "bmp", "print_unsel",         ["settings", "unsel"]),
    (87, 0x2F3802, 12344, "bmp", "brightness_unsel",    ["settings", "unsel"]),
    (88, 0x2F683A, 12344, "bmp", "gallery_unsel",       ["settings", "unsel"]),
    (89, 0x2F9872, 12344, "bmp", "info_unsel",          ["settings", "unsel"]),
    (90, 0x2FC8AA, 12344, "bmp", "file_browser_unsel",  ["settings", "unsel"]),
    (91, 0x2FF8E2, 12344, "bmp", "volume_unsel",        ["settings", "unsel"]),
]

# ─── Text label offsets (menu labels in string resource) ───
MENU_LABEL_OFFSETS = {
    "Settings": 0x239C5A,
    "Video":    0x239C62,
    "Photo":    0x239C6A,
    "Playback": 0x239C72,
    "MP3":      0x239C7A,
    "Games":    0x239C82,
}

# ─── Menu layout code patches ───
MENU_LAYOUT = {
    "coord_table":  0x07E160,   # 6 × (u16 x, u16 y)
    "asset_table":  0x07E178,   # 6 × u32 asset_index
    "handler_table": 0x07DF08,  # 6 × u32 function_ptr
    "count_offsets": [0x002F6C, 0x003B5C, 0x003E64, 0x004030, 0x004094, 0x004288],
    "max_idx_offsets": [0x004064, 0x00407C],
    "3x2": {
        "coords": [(12,18),(112,18),(212,18),(12,126),(112,126),(212,126)],
        "assets": [0x22, 0x25, 0x1F, 0x23, 0x21, 0x24],
        "handlers": [0x0200411C, 0x02003E74, 0x02003EF0, 0x02003F90, 0x02004014, 0x02004098],
        "count": 6, "max_idx": 5,
    },
    "2x2": {
        "coords": [(32,16),(192,16),(32,128),(192,128)],
        "assets": [0x22, 0x25, 0x23, 0x24],  # Photo, Video, Playback, Settings
        "handlers": [0x0200411C, 0x02003E74, 0x02003F90, 0x02004098],
        "count": 4, "max_idx": 3,
    },
}

# ─── Name alias mapping (generated asset filenames → slot names) ───
ALIASES = {
    "01_photo_96":                "photo",
    "02_video_96":                "video",
    "03_gallery_stack_96":        "playback",
    "04_settings_96":             "settings",
    "05_menu_bg_320x240":         "menu_bg",
    "06_white_balance_selected_112":  "white_balance_sel",
    "07_date_time_selected_112":      "date_time_sel",
    "08_resolution_selected_112":     "resolution_sel",
    "09_storage_selected_112":        "storage_sel",
    "10_frequency_selected_112":      "frequency_sel",
    "11_photo_mode_selected_112":     "photo_mode_sel",
    "12_language_selected_112":       "language_sel",
    "13_video_mode_selected_112":     "video_mode_sel",
    "14_print_selected_112":          "print_sel",
    "15_print_style_selected_112":    "print_style_sel",
    "16_brightness_selected_112":     "brightness_sel",
    "17_gallery_selected_112":        "gallery_sel",
    "18_info_selected_112":           "info_sel",
    "19_file_browser_selected_112":   "file_browser_sel",
    "20_volume_selected_112":         "volume_sel",
    "21_white_balance_unselected_64": "white_balance_unsel",
    "22_date_time_unselected_64":     "date_time_unsel",
    "23_resolution_unselected_64":    "resolution_unsel",
    "24_storage_unselected_64":       "storage_unsel",
    "25_frequency_unselected_64":     "frequency_unsel",
    "26_photo_mode_unselected_64":    "photo_mode_unsel",
    "27_language_unselected_64":      "language_unsel",
    "28_video_mode_unselected_64":    "video_mode_unsel",
    "29_print_unselected_64":         "print_unsel",
    "30_brightness_unselected_64":    "brightness_unsel",
    "31_gallery_unselected_64":       "gallery_unsel",
    "32_info_unselected_64":          "info_unsel",
    "33_file_browser_unselected_64":  "file_browser_unsel",
    "34_volume_unselected_64":        "volume_unsel",
    "35_boot_screen_320x240":         "boot_screen",
    "36_insert_sd_320x240":           "insert_sd",
    "37_video_rec_320x240":           "video_rec",
    "38_digit_0_16x32": "digit_0", "39_digit_1_16x32": "digit_1",
    "40_digit_2_16x32": "digit_2", "41_digit_3_16x32": "digit_3",
    "42_digit_4_16x32": "digit_4", "43_digit_5_16x32": "digit_5",
    "44_digit_6_16x32": "digit_6", "45_digit_7_16x32": "digit_7",
    "46_digit_8_16x32": "digit_8", "47_digit_9_16x32": "digit_9",
    "48_blank_16x32": "blank", "49_colon_16x32": "colon",
    "50_slash_16x32": "slash",
    "51_rewind_48x32": "rewind", "52_forward_48x32": "forward",
    "53_bg_1_320x240": "bg_1", "54_bg_2_320x240": "bg_2",
    "55_bg_3_320x240": "bg_3", "56_bg_4_320x240": "bg_4",
    "57_frame_1_1280x720": "frame_1", "58_frame_2_1280x720": "frame_2",
    "59_frame_3_1280x720": "frame_3", "60_frame_4_1280x720": "frame_4",
    "61_frame_5_1280x720": "frame_5", "62_frame_6_1280x720": "frame_6",
    "63_frame_7_1280x720": "frame_7", "64_frame_8_1280x720": "frame_8",
    "65_frame_9_1280x720": "frame_9",
    "66_game_placeholder_120": "game_1", "67_game_placeholder_120": "game_2",
    "68_game_placeholder_120": "game_3", "69_game_placeholder_120": "game_4",
    "70_game_placeholder_120": "game_5",
}


def find_assets(asset_dir):
    """Scan directory for files matching slot names or aliases."""
    slot_lookup = {s[4]: s for s in SLOTS}  # name → slot tuple
    matched = {}

    for f in sorted(Path(asset_dir).iterdir()):
        if f.is_dir():
            continue
        stem = f.stem
        ext = f.suffix.lstrip(".")

        # Try direct slot name match
        if stem in slot_lookup:
            matched[stem] = f
            continue

        # Try alias match
        if stem in ALIASES:
            slot_name = ALIASES[stem]
            matched[slot_name] = f
            continue

        # Try asset_NNN format
        if stem.startswith("asset_"):
            try:
                idx = int(stem.split("_")[1])
                for s in SLOTS:
                    if s[0] == idx:
                        matched[s[4]] = f
                        break
            except ValueError:
                pass

    return matched


def build(args):
    if not DUMP.exists():
        print(f"ERROR: Original dump not found: {DUMP}")
        sys.exit(1)

    fw = bytearray(DUMP.read_bytes())
    assert len(fw) == 4194304, "Dump size wrong"

    asset_dir = Path(args.assets) if args.assets else PROJECT / "themes" / args.theme / "firmware_exports"
    if not asset_dir.exists():
        print(f"ERROR: Asset directory not found: {asset_dir}")
        if not args.assets:
            print(f"  Available themes: {', '.join(d.name for d in (PROJECT / 'themes').iterdir() if d.is_dir())}")
        sys.exit(1)

    matched = find_assets(asset_dir)
    slot_lookup = {s[4]: s for s in SLOTS}

    print(f"Source:  {DUMP}")
    print(f"Assets:  {asset_dir}")
    print(f"Output:  {OUTPUT}")
    print(f"Matched: {len(matched)} / {len(SLOTS)} slots")
    print()

    patched = 0
    errors = 0

    for name, filepath in sorted(matched.items(), key=lambda x: slot_lookup.get(x[0], (999,))[0]):
        if name not in slot_lookup:
            print(f"  [SKIP] {filepath.name} — no matching slot")
            continue

        idx, offset, expected_size, fmt, slot_name, tags = slot_lookup[name]
        data = filepath.read_bytes()

        # Handle size mismatches
        if len(data) == expected_size:
            fw[offset:offset + expected_size] = data
        elif len(data) < expected_size:
            # Pad BMPs with 0x00, JPEGs with 0xFF before EOI
            if fmt == "jpg" and data[-2:] == b'\xff\xd9':
                pad = expected_size - len(data)
                data = data[:-2] + b'\xff' * pad + b'\xff\xd9'
            else:
                data = data + b'\x00' * (expected_size - len(data))
            fw[offset:offset + expected_size] = data
        elif len(data) > expected_size:
            # Truncate (for BMP size variants like game_2)
            fw[offset:offset + expected_size] = data[:expected_size]
            print(f"  [WARN] #{idx:02d} {name}: truncated {len(data)}→{expected_size}")

        if not args.dry_run:
            patched += 1
            print(f"  [OK] #{idx:02d} @ 0x{offset:06X} ({expected_size:,}B) ← {filepath.name}")

    # Also patch boot_screen into slot 2 if only one boot screen provided
    if "boot_screen" in matched and "boot_screen_2" not in matched:
        s = slot_lookup["boot_screen_2"]
        data = matched["boot_screen"].read_bytes()
        if len(data) <= s[2]:
            if len(data) < s[2] and data[-2:] == b'\xff\xd9':
                pad = s[2] - len(data)
                data = data[:-2] + b'\xff' * pad + b'\xff\xd9'
            fw[s[1]:s[1] + s[2]] = data[:s[2]]
            patched += 1
            print(f"  [OK] #{s[0]:02d} @ 0x{s[1]:06X} — boot_screen_2 (auto-copied)")

    # Remove menu text labels
    if args.no_text:
        print(f"\n--- Text Label Removal ---")
        text_count = 0
        for label, offset in MENU_LABEL_OFFSETS.items():
            old = struct.unpack_from('<H', fw, offset)[0]
            if old > 0:
                struct.pack_into('<H', fw, offset, 0)
                text_count += 1
                print(f"  [OK] {label}: num_chars {old}→0 @ 0x{offset:06X}")
        print(f"  Removed {text_count} labels")

    # Menu layout patch
    if args.layout != "3x2":
        layout = MENU_LAYOUT[args.layout]
        print(f"\n--- Menu Layout → {args.layout} ---")

        # Coordinates
        for i, (x, y) in enumerate(layout["coords"]):
            off = MENU_LAYOUT["coord_table"] + i * 4
            struct.pack_into('<HH', fw, off, x, y)
        # Zero out remaining coordinate slots
        for i in range(len(layout["coords"]), 6):
            off = MENU_LAYOUT["coord_table"] + i * 4
            struct.pack_into('<HH', fw, off, 0, 0)
        print(f"  [OK] Coordinates: {layout['coords']}")

        # Asset indices
        for i, asset in enumerate(layout["assets"]):
            off = MENU_LAYOUT["asset_table"] + i * 4
            struct.pack_into('<I', fw, off, asset)
        for i in range(len(layout["assets"]), 6):
            off = MENU_LAYOUT["asset_table"] + i * 4
            struct.pack_into('<I', fw, off, 0)
        print(f"  [OK] Asset indices: {[hex(a) for a in layout['assets']]}")

        # Handlers
        for i, handler in enumerate(layout["handlers"]):
            off = MENU_LAYOUT["handler_table"] + i * 4
            struct.pack_into('<I', fw, off, handler)
        for i in range(len(layout["handlers"]), 6):
            off = MENU_LAYOUT["handler_table"] + i * 4
            struct.pack_into('<I', fw, off, 0)
        print(f"  [OK] Handlers patched")

        # Item count
        for off in MENU_LAYOUT["count_offsets"]:
            fw[off] = layout["count"]
        print(f"  [OK] Count → {layout['count']} at {len(MENU_LAYOUT['count_offsets'])} locations")

        # Max index
        for off in MENU_LAYOUT["max_idx_offsets"]:
            fw[off] = layout["max_idx"]
        print(f"  [OK] Max index → {layout['max_idx']} at {len(MENU_LAYOUT['max_idx_offsets'])} locations")

    # Verify
    print(f"\n--- Verify ---")
    assert len(fw) == 4194304
    assert fw[4:8] == b'BLDR'
    assert fw[0x1FE:0x200] == b'\x55\xAA'
    assert fw[0x304:0x308] == b'SFAT'

    orig_header = DUMP.read_bytes()[:512]
    if fw[0:512] != bytearray(orig_header):
        fw[0x0C] = 0
        hdr_sum = sum(fw[0:512]) % 256
        fw[0x0C] = (256 - hdr_sum) % 256
        print("  [FIXED] Header checksum recalculated")

    hdr_sum = sum(fw[0:512]) % 256
    assert hdr_sum == 0
    print(f"  [PASS] All checks OK — {patched} assets patched")

    if args.dry_run:
        print("\n(Dry run — no files written)")
        return

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(fw)
    md5 = hashlib.md5(fw).hexdigest()
    print(f"\n  Saved: {OUTPUT}")
    print(f"  MD5:   {md5}")
    print(f"  Size:  {len(fw):,} bytes")

    # Copy to SD card
    if args.sd:
        sd_path = Path("/Volumes/NO NAME")
        if not sd_path.exists():
            print("\n  [ERROR] SD card not found at /Volumes/NO NAME")
            return
        dest = sd_path / "DestBin.bin"
        shutil.copy2(OUTPUT, dest)
        sd_md5 = hashlib.md5(dest.read_bytes()).hexdigest()
        assert sd_md5 == md5, "SD card copy hash mismatch!"
        print(f"\n  Copied to SD: {dest}")
        print(f"  Verified: MD5 match")
        os.system(f'diskutil eject "{sd_path}"')
        print(f"  SD card ejected — ready to flash")


def list_slots(args):
    """Show all available slots."""
    print(f"{'#':>3} {'Offset':>10} {'Size':>8} {'Fmt':>4} {'Name':<25} {'Tags'}")
    print("-" * 70)
    for idx, offset, size, fmt, name, tags in SLOTS:
        print(f"{idx:3d} 0x{offset:06X} {size:8,} {fmt:>4} {name:<25} {','.join(tags)}")
    print(f"\nMenu text labels:")
    for label, offset in MENU_LABEL_OFFSETS.items():
        print(f"  {label:<12} @ 0x{offset:06X}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KC02 Firmware Builder")
    sub = parser.add_subparsers(dest="cmd")

    build_p = sub.add_parser("build", help="Build DestBin.bin")
    build_p.add_argument("--theme", default="modern_pixel_light", help="Theme directory name under themes/")
    build_p.add_argument("--assets", default=None, help="Override asset directory path")
    build_p.add_argument("--no-text", action="store_true", default=True, help="Remove menu text labels")
    build_p.add_argument("--keep-text", action="store_true", help="Keep menu text labels")
    build_p.add_argument("--layout", choices=["2x2", "3x2"], default="2x2", help="Menu layout (default: 2x2)")
    build_p.add_argument("--dry-run", action="store_true")
    build_p.add_argument("--sd", action="store_true", help="Copy to SD card after build")

    list_p = sub.add_parser("list", help="List all asset slots")

    args = parser.parse_args()
    if args.cmd == "list":
        list_slots(args)
    elif args.cmd == "build":
        if args.keep_text:
            args.no_text = False
        build(args)
    else:
        parser.print_help()
        print("\nQuick start:")
        print("  python3 build.py build              # build with default assets")
        print("  python3 build.py build --sd          # build + copy to SD card")
        print("  python3 build.py build --keep-text   # keep menu labels")
        print("  python3 build.py list                # show all slots")
