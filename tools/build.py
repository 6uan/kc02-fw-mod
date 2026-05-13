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
import re
from pathlib import Path

# ─── Paths ───
PROJECT = Path(__file__).resolve().parent.parent

def _find_dump():
    if "KC02_DUMP" in os.environ:
        return Path(os.environ["KC02_DUMP"])
    for p in [PROJECT / "firmware" / "original.bin", Path.home() / "dump1.bin"]:
        if p.exists():
            return p
    return PROJECT / "firmware" / "original.bin"

DUMP = _find_dump()
BACKUP = Path(os.environ.get("KC02_BACKUP", Path.home() / "original_firmware_backup.bin"))
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
    (63, 0x24E5C2, 37688, "bmp", "auto_power_off_sel",  ["settings", "sel"]),
    (64, 0x2578FA, 37688, "bmp", "date_time_sel",       ["settings", "sel"]),
    (65, 0x260C32, 37688, "bmp", "default_settings_sel", ["settings", "sel"]),
    (66, 0x269F6A, 37688, "bmp", "format_sel",          ["settings", "sel"]),
    (67, 0x2732A2, 37688, "bmp", "frequency_sel",       ["settings", "sel"]),
    (68, 0x27C5DA, 37688, "bmp", "camera_resolution_sel", ["settings", "sel"]),
    (69, 0x285912, 37688, "bmp", "language_sel",         ["settings", "sel"]),
    (70, 0x28EC4A, 37688, "bmp", "cyclic_record_sel",   ["settings", "sel"]),
    (71, 0x297F82, 37688, "bmp", "print_density_sel",   ["settings", "sel"]),
    (72, 0x2A12BA, 37688, "bmp", "print_modes_sel",     ["settings", "sel"]),
    (73, 0x2AA5F2, 37688, "bmp", "screen_savers_sel",   ["settings", "sel"]),
    (74, 0x2B392A, 37688, "bmp", "date_stamp_sel",      ["settings", "sel"]),
    (75, 0x2BCC62, 37688, "bmp", "version_sel",         ["settings", "sel"]),
    (76, 0x2C5F9A, 37688, "bmp", "video_resolution_sel", ["settings", "sel"]),
    (77, 0x2CF2D2, 37688, "bmp", "volume_sel",          ["settings", "sel"]),

    # Settings unselected (64x64 BMP)
    (78, 0x2D860A, 12344, "bmp", "auto_power_off_unsel", ["settings", "unsel"]),
    (79, 0x2DB642, 12344, "bmp", "date_time_unsel",     ["settings", "unsel"]),
    (80, 0x2DE67A, 12344, "bmp", "default_settings_unsel", ["settings", "unsel"]),
    (81, 0x2E16B2, 12344, "bmp", "format_unsel",        ["settings", "unsel"]),
    (82, 0x2E46EA, 12344, "bmp", "frequency_unsel",     ["settings", "unsel"]),
    (83, 0x2E7722, 12344, "bmp", "camera_resolution_unsel", ["settings", "unsel"]),
    (84, 0x2EA75A, 12344, "bmp", "language_unsel",       ["settings", "unsel"]),
    (85, 0x2ED792, 12344, "bmp", "cyclic_record_unsel", ["settings", "unsel"]),
    (86, 0x2F07CA, 12344, "bmp", "print_density_unsel", ["settings", "unsel"]),
    (87, 0x2F3802, 12344, "bmp", "screen_savers_unsel", ["settings", "unsel"]),
    (88, 0x2F683A, 12344, "bmp", "date_stamp_unsel",    ["settings", "unsel"]),
    (89, 0x2F9872, 12344, "bmp", "version_unsel",       ["settings", "unsel"]),
    (90, 0x2FC8AA, 12344, "bmp", "video_resolution_unsel", ["settings", "unsel"]),
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

# ─── Menu layout reference (stock 3x2) ───
# The firmware's native layout is 3x2 with 6 menu items.
# No patching needed — we use the stock layout and replace assets only.
MENU_LAYOUT_REF = {
    "coord_table":  0x07E160,   # 6 × (u16 x, u16 y)
    "asset_table":  0x07E178,   # 6 × u32 asset_index
    "handler_table": 0x07DF08,  # 6 × u32 function_ptr
    "coords": [(12,18),(112,18),(212,18),(12,126),(112,126),(212,126)],
    "assets": [0x22, 0x25, 0x1F, 0x23, 0x21, 0x24],
    "handlers": [0x0200411C, 0x02003E74, 0x02003EF0, 0x02003F90, 0x02004014, 0x02004098],
    "count": 6, "max_idx": 5,
}

# ─── Name alias mapping (generated asset filenames → slot names) ───
ALIASES = {
    "01_photo_96":                "photo",
    "02_video_96":                "video",
    "03_gallery_stack_96":        "playback",
    "04_settings_96":             "settings",
    "05_music_96":                "music",
    "06_games_96":                "games",
    "07_menu_bg_320x240":         "menu_bg",
    "08_auto_power_off_selected_112": "auto_power_off_sel",
    "09_date_time_selected_112":      "date_time_sel",
    "10_default_settings_selected_112": "default_settings_sel",
    "11_format_selected_112":         "format_sel",
    "12_frequency_selected_112":      "frequency_sel",
    "13_camera_resolution_selected_112": "camera_resolution_sel",
    "14_language_selected_112":       "language_sel",
    "15_cyclic_record_selected_112":  "cyclic_record_sel",
    "16_print_density_selected_112":  "print_density_sel",
    "17_print_modes_selected_112":    "print_modes_sel",
    "18_screen_savers_selected_112":  "screen_savers_sel",
    "19_date_stamp_selected_112":     "date_stamp_sel",
    "20_version_selected_112":        "version_sel",
    "21_video_resolution_selected_112": "video_resolution_sel",
    "22_volume_selected_112":         "volume_sel",
    "23_auto_power_off_unselected_64": "auto_power_off_unsel",
    "24_date_time_unselected_64":     "date_time_unsel",
    "25_default_settings_unselected_64": "default_settings_unsel",
    "26_format_unselected_64":        "format_unsel",
    "27_frequency_unselected_64":     "frequency_unsel",
    "28_camera_resolution_unselected_64": "camera_resolution_unsel",
    "29_language_unselected_64":      "language_unsel",
    "30_cyclic_record_unselected_64": "cyclic_record_unsel",
    "31_print_density_unselected_64": "print_density_unsel",
    "32_screen_savers_unselected_64": "screen_savers_unsel",
    "33_date_stamp_unselected_64":    "date_stamp_unsel",
    "34_version_unselected_64":       "version_unsel",
    "35_video_resolution_unselected_64": "video_resolution_unsel",
    "36_volume_unselected_64":        "volume_unsel",
    "37_boot_screen_320x240":         "boot_screen",
    "38_insert_sd_320x240":           "insert_sd",
    "39_video_rec_320x240":           "video_rec",
    "40_digit_0_16x32": "digit_0", "41_digit_1_16x32": "digit_1",
    "42_digit_2_16x32": "digit_2", "43_digit_3_16x32": "digit_3",
    "44_digit_4_16x32": "digit_4", "45_digit_5_16x32": "digit_5",
    "46_digit_6_16x32": "digit_6", "47_digit_7_16x32": "digit_7",
    "48_digit_8_16x32": "digit_8", "49_digit_9_16x32": "digit_9",
    "50_blank_16x32": "blank", "51_colon_16x32": "colon",
    "52_slash_16x32": "slash",
    "53_rewind_48x32": "rewind", "54_forward_48x32": "forward",
    "55_bg_1_320x240": "bg_1", "56_bg_2_320x240": "bg_2",
    "57_bg_3_320x240": "bg_3", "58_bg_4_320x240": "bg_4",
    "59_frame_1_1280x720": "frame_1", "60_frame_2_1280x720": "frame_2",
    "61_frame_3_1280x720": "frame_3", "62_frame_4_1280x720": "frame_4",
    "63_frame_5_1280x720": "frame_5", "64_frame_6_1280x720": "frame_6",
    "65_frame_7_1280x720": "frame_7", "66_frame_8_1280x720": "frame_8",
    "67_frame_9_1280x720": "frame_9",
    "68_game_placeholder_120": "game_1", "69_game_placeholder_120": "game_2",
    "70_game_placeholder_120": "game_3", "71_game_placeholder_120": "game_4",
    "72_game_placeholder_120": "game_5",
}


# ─── Expected dimensions per tag (width, height) ───
EXPECTED_DIMS = {
    "menu_icon": (96, 96),
    "sel":       (112, 112),
    "unsel":     (64, 64),
    "game":      (120, 120),
    "control":   (48, 32),
    "digit":     (16, 32),
    "frame":     (1280, 720),
    "bg":        (320, 240),
    "screen":    (320, 240),
    "menu":      (320, 240),
}


def validate_asset(filepath, slot):
    """Check asset format/dimensions. Returns list of warning strings (empty = all good)."""
    idx, offset, expected_size, fmt, name, tags = slot
    warnings = []
    data = filepath.read_bytes()

    if len(data) > expected_size:
        warnings.append(f"oversized {len(data):,}B > {expected_size:,}B (will truncate)")
    elif len(data) < expected_size and fmt == "bmp":
        warnings.append(f"undersized {len(data):,}B < {expected_size:,}B (will zero-pad {expected_size - len(data)}B)")

    if fmt == "bmp":
        if len(data) < 54:
            warnings.append("BMP too small to have valid header")
            return warnings
        if data[0:2] != b'BM':
            warnings.append("missing BM magic — not a valid BMP")
            return warnings
        bpp = struct.unpack_from('<H', data, 28)[0]
        if bpp != 24:
            warnings.append(f"color depth {bpp}bpp, expected 24bpp")
        compression = struct.unpack_from('<I', data, 30)[0]
        if compression != 0:
            warnings.append(f"compressed (type {compression}), expected uncompressed")
        w = struct.unpack_from('<i', data, 18)[0]
        h = abs(struct.unpack_from('<i', data, 22)[0])
        for tag in reversed(tags):
            if tag in EXPECTED_DIMS:
                ew, eh = EXPECTED_DIMS[tag]
                if (w, h) != (ew, eh):
                    warnings.append(f"dimensions {w}x{h}, expected {ew}x{eh}")
                break

    elif fmt == "jpg":
        if data[0:2] != b'\xff\xd8':
            warnings.append("missing JPEG SOI marker")
        if data[-2:] != b'\xff\xd9':
            warnings.append("missing JPEG EOI marker")

    return warnings


MENU_BG_CONFIG = "menu_bg_offsets.json"
MENU_ICON_NAMES = ["photo", "video", "music", "playback", "games", "settings"]


def load_menu_bg_offsets(asset_dir):
    """Load per-icon position offsets from config. Returns dict of {name: (dx, dy)}."""
    config_path = Path(asset_dir) / MENU_BG_CONFIG
    offsets = {name: (0, 0) for name in MENU_ICON_NAMES}
    if config_path.exists():
        data = json.loads(config_path.read_text())
        for name in MENU_ICON_NAMES:
            if name in data:
                offsets[name] = (data[name].get("dx", 0), data[name].get("dy", 0))
    return offsets


def save_menu_bg_offsets(asset_dir, offsets, effect=None):
    """Save per-icon position offsets and effect to config."""
    config_path = Path(asset_dir) / MENU_BG_CONFIG
    data = {}
    if effect and effect != "none":
        data["effect"] = effect
    for name in MENU_ICON_NAMES:
        dx, dy = offsets.get(name, (0, 0))
        if dx != 0 or dy != 0:
            data[name] = {"dx": dx, "dy": dy}
    config_path.write_text(json.dumps(data, indent=2) + "\n")


MENU_BG_EFFECTS = ["none", "desaturate", "warm_tint", "cool_tint"]


def load_menu_bg_effect(asset_dir):
    config_path = Path(asset_dir) / MENU_BG_CONFIG
    if config_path.exists():
        data = json.loads(config_path.read_text())
        return data.get("effect", "none")
    return "none"


def _apply_bg_effect(rgb_image, effect):
    """Apply a whole-image color treatment to the final menu BG."""
    from PIL import Image, ImageEnhance

    if effect == "none":
        return rgb_image

    if effect == "desaturate":
        return ImageEnhance.Color(rgb_image).enhance(0.35)

    if effect == "warm_tint":
        muted = ImageEnhance.Color(rgb_image).enhance(0.5)
        tint = Image.new('RGB', rgb_image.size, (60, 30, 10))
        return Image.blend(muted, tint, 0.15)

    if effect == "cool_tint":
        muted = ImageEnhance.Color(rgb_image).enhance(0.5)
        tint = Image.new('RGB', rgb_image.size, (10, 20, 50))
        return Image.blend(muted, tint, 0.15)

    return rgb_image


def generate_menu_bg(asset_dir, bg_color=(0, 0, 0), effect=None):
    """Composite menu icons onto a background to create menu_bg JPEG."""
    from PIL import Image

    CHROMA_KEY = (140, 140, 140)  # #8C8C8C
    WIDTH, HEIGHT = 320, 240
    coords = MENU_LAYOUT_REF["coords"]
    offsets = load_menu_bg_offsets(asset_dir)
    if effect is None:
        effect = load_menu_bg_effect(asset_dir)

    matched = find_assets(asset_dir)

    icons_layer = Image.new('RGBA', (WIDTH, HEIGHT), (0, 0, 0, 0))

    placed = 0
    for (x, y), name in zip(coords, MENU_ICON_NAMES):
        if name not in matched:
            print(f"  [SKIP] menu_bg: {name} icon not found")
            continue
        icon = Image.open(matched[name]).convert('RGBA')
        pixels = icon.load()
        for iy in range(icon.height):
            for ix in range(icon.width):
                r, g, b, a = pixels[ix, iy]
                if (r, g, b) == CHROMA_KEY:
                    pixels[ix, iy] = (0, 0, 0, 0)
        dx, dy = offsets[name]
        icons_layer.paste(icon, (x + dx, y + dy), icon)
        placed += 1

    bg = Image.new('RGBA', (WIDTH, HEIGHT), bg_color + (255,))
    bg = Image.alpha_composite(bg, icons_layer)

    output = Path(asset_dir) / "07_menu_bg_320x240.jpg"
    rgb = _apply_bg_effect(bg.convert('RGB'), effect)
    slot = next(s for s in SLOTS if s[4] == "menu_bg")
    max_size = slot[2]

    quality = 95
    while quality > 10:
        rgb.save(output, "JPEG", quality=quality, subsampling="4:2:0")
        if output.stat().st_size <= max_size:
            break
        quality -= 5

    size = output.stat().st_size
    effect_note = f" [{effect}]" if effect != "none" else ""
    print(f"  [OK] menu_bg generated — {placed} icons @ quality={quality}, {size:,}B / {max_size:,}B{effect_note}")
    if any(d != (0, 0) for d in offsets.values()):
        adjusted = [f"{n}({dx:+d},{dy:+d})" for n, (dx, dy) in offsets.items() if (dx, dy) != (0, 0)]
        print(f"  [OK] offsets applied: {', '.join(adjusted)}")
    return output


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

    print(f"\nSource:  {DUMP}")
    print(f"Assets:  {asset_dir}")
    print(f"Output:  {OUTPUT}")
    print(f"Matched: {len(matched)} / {len(SLOTS)} slots")
    print()

    patched = 0
    patched_names = []
    warn_count = 0

    for name, filepath in sorted(matched.items(), key=lambda x: slot_lookup.get(x[0], (999,))[0]):
        if name not in slot_lookup:
            print(f"  [SKIP] {filepath.name} — no matching slot")
            continue

        slot = slot_lookup[name]
        idx, offset, expected_size, fmt, slot_name, tags = slot

        # Validate asset
        warnings = validate_asset(filepath, slot)
        for w in warnings:
            print(f"  [WARN] #{idx:02d} {name}: {w}")
            warn_count += 1

        data = filepath.read_bytes()

        # Handle size mismatches
        if len(data) == expected_size:
            fw[offset:offset + expected_size] = data
        elif len(data) < expected_size:
            if fmt == "jpg" and data[-2:] == b'\xff\xd9':
                pad = expected_size - len(data)
                data = data[:-2] + b'\xff' * pad + b'\xff\xd9'
            else:
                data = data + b'\x00' * (expected_size - len(data))
            fw[offset:offset + expected_size] = data
        elif len(data) > expected_size:
            fw[offset:offset + expected_size] = data[:expected_size]

        if not args.dry_run:
            patched += 1
            patched_names.append(name)
            print(f"  [OK] #{idx:02d} @ 0x{offset:06X} ({expected_size:,}B) ← {filepath.name}")

    if warn_count:
        print(f"\n  {warn_count} warning(s) — assets patched anyway")

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
    build_p.add_argument("--dry-run", action="store_true")
    build_p.add_argument("--sd", action="store_true", help="Copy to SD card after build")

    list_p = sub.add_parser("list", help="List all asset slots")

    menubg_p = sub.add_parser("gen-menu-bg", help="Generate menu background from current icons")
    menubg_p.add_argument("--theme", default="modern_pixel_light", help="Theme directory name")
    menubg_p.add_argument("--assets", default=None, help="Override asset directory path")

    args = parser.parse_args()
    if args.cmd == "list":
        list_slots(args)
    elif args.cmd == "gen-menu-bg":
        asset_dir = Path(args.assets) if args.assets else PROJECT / "themes" / args.theme / "firmware_exports"
        generate_menu_bg(asset_dir)
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
