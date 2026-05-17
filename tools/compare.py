#!/usr/bin/env python3
"""
KC02 Asset Comparison Server
Serves a side-by-side comparison of original vs theme assets at localhost.
Supports opening assets in Aseprite and normalizing BMPs to firmware spec.

Usage: python3 compare.py [--theme NAME] [--port PORT]
"""

import sys
import json
import struct
import subprocess
import argparse
import re
import tempfile
import shutil
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))
from build import SLOTS, ALIASES, PROJECT, MENU_LAYOUT_REF, MENU_ICON_NAMES, \
    MENU_BG_EFFECTS, load_menu_bg_offsets, load_menu_bg_effect, \
    save_menu_bg_offsets, generate_menu_bg, find_menu_bg_base

MIME = {"bmp": "image/bmp", "jpg": "image/jpeg", "jpeg": "image/jpeg"}

CATEGORIES = [
    ("menu_icon",      "Menu Icons"),
    ("menu_bg",        "Menu Background"),
    ("settings_sel",   "Settings (Selected)"),
    ("settings_unsel", "Settings (Unselected)"),
    ("screen",         "Screens"),
    ("digit",          "Digits"),
    ("control",        "Playback Controls"),
    ("bg",             "Backgrounds"),
    ("frame",          "Photo Frames"),
    ("game",           "Game Icons"),
]

SLOT_BY_NAME = {s[4]: s for s in SLOTS}

REVERSE_ALIASES = {}
for _alias, _slot_name in ALIASES.items():
    if _slot_name not in REVERSE_ALIASES:
        REVERSE_ALIASES[_slot_name] = _alias

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

CHROMA_KEYS = {
    "menu_icon": (140, 140, 140),
    "sel":       (140, 140, 140),
    "unsel":     (140, 140, 140),
    "game":      (140, 140, 140),
    "control":   (140, 140, 140),
    "digit":     (128, 128, 128),
}

ORIGINAL_FILES = {}
THEME_FILES = {}
MANIFEST_JSON = b""
CURRENT_THEME = "modern_pixel_light"


def categorize(tags):
    if "menu_icon" in tags:
        return "menu_icon"
    if "sel" in tags:
        return "settings_sel"
    if "unsel" in tags:
        return "settings_unsel"
    if "menu" in tags:
        return "menu_bg"
    if "screen" in tags:
        return "screen"
    if "digit" in tags:
        return "digit"
    if "control" in tags:
        return "control"
    if "bg" in tags:
        return "bg"
    if "frame" in tags:
        return "frame"
    if "game" in tags:
        return "game"
    return "other"


def normalize_bmp(filepath, max_size):
    """Read a BMP (24 or 32-bit), rewrite as clean 24-bit BGR, pad to max_size."""
    data = bytearray(filepath.read_bytes())

    if len(data) < 54 or data[:2] != b'BM':
        return False, "Not a valid BMP file"

    pixel_offset = struct.unpack_from('<I', data, 10)[0]
    width = struct.unpack_from('<i', data, 18)[0]
    height = struct.unpack_from('<i', data, 22)[0]
    bpp = struct.unpack_from('<H', data, 28)[0]
    compression = struct.unpack_from('<I', data, 30)[0]

    abs_height = abs(height)
    bottom_up = height > 0

    if bpp not in (24, 32):
        return False, f"Unsupported bit depth: {bpp}bpp (need 24 or 32)"
    if compression not in (0, 3):
        return False, f"Compressed BMP not supported (type {compression})"

    in_stride = ((width * (bpp // 8) + 3) // 4) * 4
    rows = []
    for y in range(abs_height):
        row_start = pixel_offset + y * in_stride
        if bpp == 24:
            rows.append(bytes(data[row_start:row_start + width * 3]))
        else:
            row_bgr = bytearray()
            for x in range(width):
                px = row_start + x * 4
                row_bgr.extend(data[px:px + 3])
            rows.append(bytes(row_bgr))

    if not bottom_up:
        rows.reverse()

    out_stride = ((width * 3 + 3) // 4) * 4
    pixel_data_size = abs_height * out_stride
    file_size = 54 + pixel_data_size

    if file_size > max_size:
        return False, f"BMP {file_size:,}B exceeds slot budget {max_size:,}B — check canvas dimensions"

    out = bytearray()
    out.extend(b'BM')
    out.extend(struct.pack('<I', file_size))
    out.extend(struct.pack('<HH', 0, 0))
    out.extend(struct.pack('<I', 54))
    out.extend(struct.pack('<I', 40))
    out.extend(struct.pack('<i', width))
    out.extend(struct.pack('<i', abs_height))
    out.extend(struct.pack('<HH', 1, 24))
    out.extend(struct.pack('<I', 0))
    out.extend(struct.pack('<I', pixel_data_size))
    out.extend(struct.pack('<ii', 0, 0))
    out.extend(struct.pack('<II', 0, 0))

    for row in rows:
        out.extend(row)
        pad = out_stride - width * 3
        if pad > 0:
            out.extend(b'\x00' * pad)

    if len(out) < max_size:
        out.extend(b'\x00' * (max_size - len(out)))

    filepath.write_bytes(bytes(out))
    return True, f"{width}x{abs_height} 24bpp, {len(out):,} bytes"


def build_manifest(theme_name):
    originals_dir = PROJECT / "originals"
    theme_dir = PROJECT / "themes" / theme_name / "firmware_exports"

    slot_by_index = {s[0]: s for s in SLOTS}
    alias_to_slot = dict(ALIASES)

    originals_by_index = {}
    for subdir in ["bmp", "jpeg"]:
        d = originals_dir / subdir
        if not d.exists():
            continue
        for f in d.iterdir():
            if f.is_dir():
                continue
            try:
                idx = int(f.stem.split("_")[0])
                if idx in slot_by_index:
                    originals_by_index[idx] = f
            except ValueError:
                pass

    slot_names = {s[4] for s in SLOTS}
    theme_by_slot = {}
    if theme_dir.exists():
        for f in sorted(theme_dir.iterdir()):
            if f.is_dir():
                continue
            stem = f.stem
            if stem in alias_to_slot:
                theme_by_slot[alias_to_slot[stem]] = f
            elif stem in slot_names:
                theme_by_slot[stem] = f

    base_entry = {
        "slot": "menu_bg_base",
        "index": -1,
        "offset": "",
        "format": "png",
        "size": 0,
        "category": "menu_bg",
        "tags": ["menu"],
        "original": None,
        "theme": None,
    }
    base_path = find_menu_bg_base(theme_dir)
    if base_path:
        base_entry["theme"] = {
            "url": f"/theme/{base_path.name}",
            "filename": base_path.name,
            "filesize": base_path.stat().st_size,
        }

    entries = [base_entry]
    for idx, offset, size, fmt, name, tags in SLOTS:
        cat = categorize(tags)
        entry = {
            "slot": name,
            "index": idx,
            "offset": f"0x{offset:06X}",
            "format": fmt,
            "size": size,
            "category": cat,
            "tags": tags,
            "original": None,
            "theme": None,
        }
        if idx in originals_by_index:
            f = originals_by_index[idx]
            entry["original"] = {
                "url": f"/originals/{idx:03d}.{fmt}",
                "filename": f.name,
                "filesize": f.stat().st_size,
            }
        if name in theme_by_slot:
            f = theme_by_slot[name]
            entry["theme"] = {
                "url": f"/theme/{f.name}",
                "filename": f.name,
                "filesize": f.stat().st_size,
            }
        entries.append(entry)

    return {
        "theme": theme_name,
        "total": len(entries),
        "replaced": sum(1 for e in entries if e["theme"]),
        "categories": [{"key": k, "label": l} for k, l in CATEGORIES],
        "entries": entries,
    }


def init_data(theme_name):
    global ORIGINAL_FILES, THEME_FILES, MANIFEST_JSON, CURRENT_THEME
    CURRENT_THEME = theme_name

    originals_dir = PROJECT / "originals"
    theme_dir = PROJECT / "themes" / theme_name / "firmware_exports"
    slot_by_index = {s[0]: s for s in SLOTS}

    for subdir in ["bmp", "jpeg"]:
        d = originals_dir / subdir
        if not d.exists():
            continue
        for f in d.iterdir():
            if f.is_dir():
                continue
            try:
                idx = int(f.stem.split("_")[0])
                if idx in slot_by_index:
                    fmt = slot_by_index[idx][3]
                    ORIGINAL_FILES[f"{idx:03d}.{fmt}"] = f
            except ValueError:
                pass

    if theme_dir.exists():
        for f in theme_dir.iterdir():
            if not f.is_dir():
                THEME_FILES[f.name] = f

    MANIFEST_JSON = json.dumps(build_manifest(theme_name)).encode()


def refresh_manifest():
    global MANIFEST_JSON
    MANIFEST_JSON = json.dumps(build_manifest(CURRENT_THEME)).encode()


def find_theme_file(slot_name):
    """Find the theme file path for a given slot name."""
    theme_dir = PROJECT / "themes" / CURRENT_THEME / "firmware_exports"
    for ext in ["bmp", "jpg"]:
        p = theme_dir / f"{slot_name}.{ext}"
        if p.exists():
            return p
    for alias, sname in ALIASES.items():
        if sname == slot_name:
            for ext in ["bmp", "jpg"]:
                p = theme_dir / f"{alias}.{ext}"
                if p.exists():
                    return p
    return None


def get_slot_dims(slot):
    tags = slot[5]
    for tag in reversed(tags):
        if tag in EXPECTED_DIMS:
            return EXPECTED_DIMS[tag]
    return None


def get_theme_filename(slot_name, fmt):
    if slot_name in REVERSE_ALIASES:
        return f"{REVERSE_ALIASES[slot_name]}.{fmt}"
    return f"{slot_name}.{fmt}"


def apply_chroma_key(filepath, slot):
    """Detect background color from corners and replace with correct chroma key."""
    tags = slot[5]
    chroma_rgb = None
    for tag in reversed(tags):
        if tag in CHROMA_KEYS:
            chroma_rgb = CHROMA_KEYS[tag]
            break
    if not chroma_rgb:
        return

    data = bytearray(filepath.read_bytes())
    if len(data) < 54 or data[:2] != b'BM':
        return

    width = struct.unpack_from('<i', data, 18)[0]
    height = abs(struct.unpack_from('<i', data, 22)[0])
    stride = ((width * 3 + 3) // 4) * 4

    def get_pixel(x, y):
        off = 54 + y * stride + x * 3
        return (data[off], data[off + 1], data[off + 2])

    corners = [
        get_pixel(0, 0), get_pixel(width - 1, 0),
        get_pixel(0, height - 1), get_pixel(width - 1, height - 1),
    ]
    from collections import Counter
    bg_bgr = Counter(corners).most_common(1)[0][0]

    chroma_bgr = (chroma_rgb[2], chroma_rgb[1], chroma_rgb[0])
    if bg_bgr == chroma_bgr:
        return

    tolerance = 30
    replaced = 0
    for y in range(height):
        for x in range(width):
            off = 54 + y * stride + x * 3
            b, g, r = data[off], data[off + 1], data[off + 2]
            if (abs(b - bg_bgr[0]) <= tolerance and
                abs(g - bg_bgr[1]) <= tolerance and
                abs(r - bg_bgr[2]) <= tolerance):
                data[off] = chroma_bgr[0]
                data[off + 1] = chroma_bgr[1]
                data[off + 2] = chroma_bgr[2]
                replaced += 1

    filepath.write_bytes(bytes(data))
    return replaced


def reposition_bmp(filepath, dx, dy, slot):
    """Shift icon pixels by (dx, dy) in screen space. Fill exposed areas with chroma."""
    data = bytearray(filepath.read_bytes())
    if len(data) < 54 or data[:2] != b'BM':
        return False, "Not a valid BMP"

    pixel_offset = struct.unpack_from('<I', data, 10)[0]
    width = struct.unpack_from('<i', data, 18)[0]
    height_raw = struct.unpack_from('<i', data, 22)[0]
    height = abs(height_raw)
    bpp = struct.unpack_from('<H', data, 28)[0]

    if bpp != 24:
        return False, f"Expected 24bpp BMP, got {bpp}bpp — run Fix BMP first"

    stride = ((width * 3 + 3) // 4) * 4

    chroma_rgb = (140, 140, 140)
    for tag in reversed(slot[5]):
        if tag in CHROMA_KEYS:
            chroma_rgb = CHROMA_KEYS[tag]
            break
    cb, cg, cr = chroma_rgb[2], chroma_rgb[1], chroma_rgb[0]

    orig = bytes(data[pixel_offset:pixel_offset + height * stride])

    # bottom-up (height > 0): +dy in file rows = down on screen
    # top-down (height < 0): -dy in file rows = down on screen
    flip = 1 if height_raw > 0 else -1

    for y in range(height):
        for x in range(width):
            dest_off = pixel_offset + y * stride + x * 3
            sx = x - dx
            sy = y + dy * flip
            if 0 <= sx < width and 0 <= sy < height:
                src_off = sy * stride + sx * 3
                data[dest_off] = orig[src_off]
                data[dest_off + 1] = orig[src_off + 1]
                data[dest_off + 2] = orig[src_off + 2]
            else:
                data[dest_off] = cb
                data[dest_off + 1] = cg
                data[dest_off + 2] = cr

    filepath.write_bytes(bytes(data))
    return True, f"Repositioned by ({dx}, {dy})"


def convert_asset(input_path, slot_name):
    """Convert any image to correct format/size for a slot using sips."""
    if slot_name not in SLOT_BY_NAME:
        return False, f"Unknown slot: {slot_name}", None

    slot = SLOT_BY_NAME[slot_name]
    idx, offset, max_size, fmt, name, tags = slot
    dims = get_slot_dims(slot)
    if not dims:
        return False, f"No expected dimensions for slot {slot_name}", None

    w, h = dims
    theme_dir = PROJECT / "themes" / CURRENT_THEME / "firmware_exports"
    theme_dir.mkdir(parents=True, exist_ok=True)
    out_filename = get_theme_filename(slot_name, fmt)
    out_path = theme_dir / out_filename

    tmp_dir = Path(tempfile.mkdtemp())
    try:
        tmp_input = tmp_dir / f"input{Path(input_path).suffix}"
        shutil.copy2(input_path, tmp_input)

        if fmt == "bmp":
            tmp_bmp = tmp_dir / "output.bmp"
            r = subprocess.run(
                ["sips", "-z", str(h), str(w), "-s", "format", "bmp",
                 str(tmp_input), "--out", str(tmp_bmp)],
                capture_output=True, text=True
            )
            if r.returncode != 0:
                return False, f"sips failed: {r.stderr.strip()}", None
            shutil.copy2(tmp_bmp, out_path)
            ok, detail = normalize_bmp(out_path, max_size)
            if not ok:
                return False, f"BMP normalize failed: {detail}", None
            replaced = apply_chroma_key(out_path, slot)
            chroma_note = f", chroma fixed ({replaced}px)" if replaced else ""
            THEME_FILES[out_filename] = out_path
            return True, f"{w}x{h} 24bpp BMP ({max_size:,}B){chroma_note} → {out_filename}", out_path

        elif fmt == "jpg":
            try:
                from PIL import Image as PILImage
                pil_img = PILImage.open(str(tmp_input)).convert("RGB").resize((w, h), PILImage.LANCZOS)
                tmp_jpg = tmp_dir / "output.jpg"
                for quality in range(92, 8, -2):
                    pil_img.save(str(tmp_jpg), format="JPEG", quality=quality,
                                 subsampling="4:2:0", optimize=True)
                    size = tmp_jpg.stat().st_size
                    if size <= max_size:
                        data = tmp_jpg.read_bytes()
                        if len(data) < max_size and data[-2:] == b'\xff\xd9':
                            pad = max_size - len(data)
                            data = data[:-2] + b'\xff' * pad + b'\xff\xd9'
                        out_path.write_bytes(data[:max_size])
                        THEME_FILES[out_filename] = out_path
                        return True, f"{w}x{h} JPEG 4:2:0 q={quality} ({max_size:,}B) → {out_filename}", out_path
                return False, f"Cannot compress to {max_size:,}B (smallest: {size:,}B)", None
            except ImportError:
                return False, "Pillow required for JPEG conversion (pip install Pillow)", None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)



HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KC02 Asset Compare</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#111;color:#ccc;font-family:-apple-system,system-ui,sans-serif;padding:20px}
h1{font-size:1.4rem;font-weight:600;color:#fff}
.header{display:flex;align-items:baseline;gap:16px;margin-bottom:8px}
.stats{color:#888;font-size:.85rem}
.stats b{color:#6bf}
.filters{display:flex;flex-wrap:wrap;gap:6px;margin:16px 0 24px}
.filter-btn{
  background:#222;border:1px solid #333;color:#999;padding:5px 12px;
  border-radius:4px;cursor:pointer;font-size:.8rem;transition:all .15s;
}
.filter-btn:hover{border-color:#555;color:#ddd}
.filter-btn.active{background:#1a3a5c;border-color:#2a6ab0;color:#6bf}
.badge{background:#333;color:#888;padding:1px 6px;border-radius:8px;font-size:.7rem;margin-left:4px}
.filter-btn.active .badge{background:#2a6ab0;color:#adf}
.cat-section{margin-bottom:32px}
.cat-title{font-size:1rem;color:#999;border-bottom:1px solid #222;padding-bottom:6px;margin-bottom:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px}
.card{background:#1a1a1a;border:1px solid #262626;border-radius:6px;overflow:hidden}
.card-head{padding:8px 12px;background:#151515;border-bottom:1px solid #222;display:flex;justify-content:space-between;align-items:center;gap:6px;flex-wrap:wrap}
.slot-name{color:#eee;font-weight:600;font-size:.85rem;white-space:nowrap}
.card-right{display:flex;align-items:center;gap:4px;flex-wrap:wrap}
.slot-meta{color:#555;font-size:.75rem;font-family:monospace}
.btn-edit,.btn-fix{
  padding:3px 8px;border-radius:3px;border:1px solid #333;
  font-size:.7rem;cursor:pointer;transition:all .15s;background:#222;color:#999;
}
.btn-edit:hover{border-color:#4a9;color:#4a9}
.btn-fix:hover{border-color:#e90;color:#e90}
.btn-edit:disabled,.btn-fix:disabled,.btn-remove:disabled{opacity:.3;cursor:default}
.btn-edit.loading,.btn-fix.loading{opacity:.5;pointer-events:none}
.btn-remove{
  padding:3px 8px;border-radius:3px;border:1px solid #333;
  font-size:.7rem;cursor:pointer;transition:all .15s;background:#222;color:#999;
}
.btn-remove:hover{border-color:#e55;color:#e55}
.btn-remove.confirming{background:#6a1a1a;border-color:#e55;color:#fff;animation:pulse 1s infinite}
.comparison{display:grid;grid-template-columns:1fr auto 1fr;align-items:center}
.side{padding:10px;text-align:center}
.side-label{font-size:.7rem;color:#555;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}
.img-wrap{
  display:inline-flex;align-items:center;justify-content:center;
  min-height:64px;min-width:64px;padding:4px;border-radius:4px;
  background-color:#0a0a0a;
  background-image:linear-gradient(45deg,#1a1a1a 25%,transparent 25%),
    linear-gradient(-45deg,#1a1a1a 25%,transparent 25%),
    linear-gradient(45deg,transparent 75%,#1a1a1a 75%),
    linear-gradient(-45deg,transparent 75%,#1a1a1a 75%);
  background-size:12px 12px;
  background-position:0 0,0 6px,6px -6px,-6px 0;
}
.img-wrap img{
  display:block;max-width:100%;height:auto;
  image-rendering:pixelated;image-rendering:-moz-crisp-edges;
}
.img-wrap img.scale-lg{min-height:128px;min-width:64px}
.img-wrap img.scale-md{min-height:96px}
.img-wrap img.scale-sm{min-height:80px}
.placeholder{
  display:flex;align-items:center;justify-content:center;
  min-height:64px;min-width:64px;padding:16px;
  border:1px dashed #333;border-radius:4px;color:#444;font-size:.75rem;
}
.file-info{color:#444;font-size:.7rem;margin-top:4px;font-family:monospace;word-break:break-all}
.arrow{color:#333;font-size:1.2rem;padding:0 4px}
.frames-grid{grid-template-columns:1fr}
.frames-grid .comparison{grid-template-columns:1fr auto 1fr}
.frames-grid .img-wrap img{min-height:auto;max-width:100%}
.sd-badge{padding:4px 12px;border-radius:12px;font-size:.75rem;font-weight:600;transition:all .3s}
.sd-badge.mounted{background:#143a24;color:#4a9;border:1px solid #2a6a4a}
.sd-badge.ejected{background:#2a1a1a;color:#666;border:1px solid #333}
.build-btn{
  padding:8px 20px;background:#2a6ab0;color:#fff;border:none;border-radius:6px;
  font-size:.85rem;font-weight:600;cursor:pointer;transition:all .15s;margin-left:auto;
}
.build-btn:hover{background:#3a7ac0}
.build-btn:disabled{opacity:.4;cursor:default}
.build-btn.building{background:#6a4a1a;animation:pulse 1.5s infinite}
.build-btn.success{background:#2a6a4a}
@keyframes pulse{0%,100%{opacity:.7}50%{opacity:1}}
.modal-overlay{
  position:fixed;inset:0;background:rgba(0,0,0,.75);
  display:flex;align-items:center;justify-content:center;z-index:50;
}
.modal{background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:20px;max-width:90vw}
.modal-head{display:flex;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap}
.modal-head b{color:#fff}
.modal-offset{font-family:monospace;color:#6bf;font-size:.85rem;margin-left:auto}
.modal-actions{display:flex;gap:6px}
.modal-canvas{border:1px solid #333;cursor:grab;display:block;margin:0 auto}
.modal-canvas:active{cursor:grabbing}
.modal-hint{text-align:center;color:#555;font-size:.75rem;margin-top:8px}
.build-output{
  margin-top:12px;padding:12px 16px;background:#0a0a0a;border:1px solid #262626;
  border-radius:6px;font-family:monospace;font-size:.75rem;color:#888;
  white-space:pre-wrap;max-height:300px;overflow-y:auto;display:none;
}
.tabs{display:flex;gap:2px;margin:0 0 20px}
.tab{
  padding:8px 20px;background:#1a1a1a;border:1px solid #262626;
  color:#888;cursor:pointer;font-size:.85rem;transition:all .15s;
}
.tab:first-child{border-radius:6px 0 0 6px}
.tab:last-child{border-radius:0 6px 6px 0}
.tab.active{background:#1a3a5c;border-color:#2a6ab0;color:#6bf}
.tab:hover:not(.active){color:#ddd;border-color:#444}
.panel{display:none}.panel.active{display:block}
.side.drop-target{transition:all .15s;border:2px solid transparent;border-radius:4px;margin:-2px}
.side.drop-target.dragover{background:#0a1a2a;border-color:#2a6ab0}
.side.drop-target .side-label .drop-hint{display:none;color:#2a6ab0;font-size:.6rem}
.side.drop-target.dragover .side-label .drop-hint{display:inline}
.fbtn{padding:5px 12px;border-radius:4px;border:none;cursor:pointer;font-size:.75rem;font-weight:600;transition:all .15s}
.fbtn-ok{background:#2a6a4a;color:#fff}.fbtn-ok:hover{background:#3a7a5a}
.fbtn-warn{background:#6a4a1a;color:#fff}.fbtn-warn:hover{background:#7a5a2a}
.fbtn-bad{background:#6a1a1a;color:#fff}.fbtn-bad:hover{background:#8a2a2a}
#toast{
  position:fixed;bottom:24px;left:50%;transform:translateX(-50%);
  padding:10px 20px;border-radius:6px;font-size:.85rem;
  pointer-events:none;opacity:0;transition:opacity .3s;z-index:100;
  max-width:500px;text-align:center;
}
#toast.show{opacity:1}
#toast.ok{background:#143a24;color:#4a9;border:1px solid #2a6a4a}
#toast.err{background:#3a1414;color:#e66;border:1px solid #6a2a2a}
</style>
</head>
<body>
<div class="header">
  <h1>KC02 Asset Compare</h1>
  <div class="stats" id="stats"></div>
  <span class="sd-badge" id="sdBadge">SD: checking...</span>
  <button class="build-btn" id="buildBtn" onclick="runBuild()">Build & Flash to SD</button>
</div>
<div class="tabs">
  <div class="tab active" data-tab="compare">Assets</div>
</div>
<div id="panel-compare" class="panel active">
  <div class="filters" id="filters"></div>
  <div id="content"></div>
</div>
<div class="modal-overlay" id="moveModal" style="display:none" onclick="if(event.target===this)closeMove()"></div>
<div id="toast"></div>
<script>
function checkSd(){
  fetch('/api/sd-status').then(r=>r.json()).then(d=>{
    const b=document.getElementById('sdBadge');
    if(d.mounted){b.textContent='SD: mounted';b.className='sd-badge mounted';}
    else{b.textContent='SD: not found';b.className='sd-badge ejected';}
  }).catch(()=>{});
}
checkSd();setInterval(checkSd,3000);

let DATA=null;
fetch('/api/manifest').then(r=>r.json()).then(data=>{
  DATA=data;
  document.getElementById('stats').innerHTML=
    `Theme: <b>${data.theme}</b> &middot; <b>${data.replaced}</b> / ${data.total} slots replaced`;

  const frag=document.getElementById('filters');
  frag.appendChild(mkBtn('All','all',true,data.total));
  const catCounts={};
  data.entries.forEach(e=>{catCounts[e.category]=(catCounts[e.category]||0)+1});
  data.categories.forEach(c=>{
    if(catCounts[c.key])frag.appendChild(mkBtn(c.label,c.key,false,catCounts[c.key]));
  });

  renderAll(data);

  document.querySelectorAll('.filter-btn').forEach(btn=>{
    btn.addEventListener('click',()=>{
      document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));
      btn.classList.add('active');
      const cat=btn.dataset.category;
      document.querySelectorAll('.cat-section').forEach(s=>{
        s.style.display=(cat==='all'||s.dataset.category===cat)?'':'none';
      });
    });
  });
});

function renderAll(data){
  const content=document.getElementById('content');
  content.innerHTML='';
  const grouped={};
  data.entries.forEach(e=>{
    if(!grouped[e.category])grouped[e.category]=[];
    grouped[e.category].push(e);
  });
  data.categories.forEach(cat=>{
    const items=grouped[cat.key];
    if(!items)return;
    const sec=document.createElement('div');
    sec.className='cat-section';
    sec.dataset.category=cat.key;
    const isFrame=cat.key==='frame';
    sec.innerHTML=`<div class="cat-title">${cat.label} (${items.length})</div>`+
      `<div class="grid${isFrame?' frames-grid':''}">` +
      items.map(e=>renderCard(e,cat.key)).join('')+'</div>';
    content.appendChild(sec);
  });
}

function mkBtn(label,key,active,count){
  const b=document.createElement('button');
  b.className='filter-btn'+(active?' active':'');
  b.dataset.category=key;
  b.innerHTML=label+'<span class="badge">'+count+'</span>';
  return b;
}

function scaleClass(cat){
  if(cat==='digit'||cat==='control')return' scale-lg';
  if(cat==='settings_unsel')return' scale-sm';
  return'';
}

function renderCard(e,cat){
  const sc=scaleClass(cat);
  const isBmp=e.format==='bmp';
  const hasTheme=!!e.theme;
  const t=Date.now();

  if(e.index<0){
    const editBtn=hasTheme?`<button class="btn-edit" onclick="openEdit('${e.slot}',this)">Edit</button>`:'';
    const removeBtn=hasTheme?`<button class="btn-remove" onclick="removeAsset('${e.slot}',this)">Remove</button>`:'';
    const inner=hasTheme
      ? `<div class="img-wrap"><img id="img-${e.slot}" src="${e.theme.url}?t=${t}" class="${sc}" loading="lazy"></div>
         <div class="file-info" id="info-${e.slot}">${e.theme.filename}<br>${fmtSize(e.theme.filesize)}</div>`
      : '<div class="placeholder">Drop image to set background</div>';
    return `<div class="card" id="card-${e.slot}">
      <div class="card-head">
        <span class="slot-name">${e.slot.replace(/_/g,' ')}</span>
        <div class="card-right"><span class="slot-meta">source</span>${editBtn}${removeBtn}</div>
      </div>
      <div class="comparison">
        <div class="side drop-target" data-slot="${e.slot}" style="flex:1"
             ondragover="event.preventDefault();this.classList.add('dragover')"
             ondragleave="this.classList.remove('dragover')"
             ondrop="handleDrop(event,'${e.slot}')">
          ${inner}</div>
      </div></div>`;
  }

  const editBtn=isBmp&&hasTheme
    ? `<button class="btn-edit" onclick="openEdit('${e.slot}',this)">Edit</button>`
    : '';
  const fixBtn=isBmp&&hasTheme
    ? `<button class="btn-fix" onclick="normalizeBmp('${e.slot}',this)">Fix BMP</button>`
    : '';
  const moveBtn=isBmp&&hasTheme
    ? `<button class="btn-edit" onclick="openMove('${e.slot}')">Move</button>`
    : '';
  const removeBtn=hasTheme
    ? `<button class="btn-remove" onclick="removeAsset('${e.slot}',this)">Remove</button>`
    : '';
  const layerBtn=e.slot==='menu_bg'
    ? `<button class="btn-edit" onclick="openMenuBgEditor()">Edit Layers</button>`
    : '';
  const orig=e.original
    ? `<div class="img-wrap"><img src="${e.original.url}" class="${sc}" loading="lazy"></div>
       <div class="file-info">${e.original.filename}<br>${fmtSize(e.original.filesize)}</div>`
    : '<div class="placeholder">No original</div>';
  const theme=e.theme
    ? `<div class="img-wrap"><img id="img-${e.slot}" src="${e.theme.url}?t=${t}" class="${sc}" loading="lazy"></div>
       <div class="file-info" id="info-${e.slot}">${e.theme.filename}<br>${fmtSize(e.theme.filesize)}</div>`
    : '<div class="placeholder">Drop image to add</div>';
  return `<div class="card" id="card-${e.slot}">
    <div class="card-head">
      <span class="slot-name">${e.slot.replace(/_/g,' ')}</span>
      <div class="card-right">
        <span class="slot-meta">#${e.index} ${e.format}</span>
        ${editBtn}${fixBtn}${moveBtn}${layerBtn}${removeBtn}
      </div>
    </div>
    <div class="comparison">
      <div class="side"><div class="side-label">Original</div>${orig}</div>
      <div class="arrow">&rarr;</div>
      <div class="side drop-target" data-slot="${e.slot}"
           ondragover="event.preventDefault();this.classList.add('dragover')"
           ondragleave="this.classList.remove('dragover')"
           ondrop="handleDrop(event,'${e.slot}')">
        <div class="side-label">Theme <span class="drop-hint">— drop to replace</span></div>${theme}</div>
    </div></div>`;
}

function fmtSize(b){
  if(b<1024)return b+' B';
  return (b/1024).toFixed(1)+' KB';
}

function toast(msg,ok){
  const el=document.getElementById('toast');
  el.textContent=msg;
  el.className=ok?'show ok':'show err';
  clearTimeout(el._t);
  el._t=setTimeout(()=>{el.className=''},3000);
}

function openEdit(slot,btn){
  btn.classList.add('loading');
  fetch('/api/open/'+slot,{method:'POST'}).then(r=>r.json()).then(d=>{
    btn.classList.remove('loading');
    if(d.ok) toast('Opened '+slot+' in Aseprite',true);
    else toast(d.error,false);
  }).catch(()=>{btn.classList.remove('loading');toast('Request failed',false)});
}

function normalizeBmp(slot,btn){
  btn.classList.add('loading');
  fetch('/api/normalize/'+slot,{method:'POST'}).then(r=>r.json()).then(d=>{
    btn.classList.remove('loading');
    if(d.ok){
      toast('Normalized: '+d.detail,true);
      reloadImg(slot);
    } else {
      toast(d.error,false);
    }
  }).catch(()=>{btn.classList.remove('loading');toast('Request failed',false)});
}

function reloadCard(slot){
  fetch('/api/manifest').then(r=>r.json()).then(data=>{
    DATA=data;
    document.getElementById('stats').innerHTML=
      `Theme: <b>${data.theme}</b> &middot; <b>${data.replaced}</b> / ${data.total} slots replaced`;
    const e=data.entries.find(x=>x.slot===slot);
    if(!e)return;
    const card=document.getElementById('card-'+slot);
    if(!card)return;
    const cat=e.category;
    card.outerHTML=renderCard(e,cat);
  });
}

function reloadImg(slot){
  const img=document.getElementById('img-'+slot);
  if(img){
    const base=img.src.split('?')[0];
    img.src=base+'?t='+Date.now();
  }
  fetch('/api/manifest').then(r=>r.json()).then(data=>{
    const e=data.entries.find(x=>x.slot===slot);
    if(e&&e.theme){
      const info=document.getElementById('info-'+slot);
      if(info) info.innerHTML=e.theme.filename+'<br>'+fmtSize(e.theme.filesize);
    }
  });
}

function handleDrop(ev,slot){
  ev.preventDefault();
  ev.currentTarget.classList.remove('dragover');
  const files=ev.dataTransfer.files;
  if(!files.length)return;
  uploadAsset(files[0],slot);
}

function uploadAsset(file,slot){
  const card=document.getElementById('card-'+slot);
  if(card)card.style.opacity='0.5';
  const form=new FormData();
  form.append('file',file);
  fetch('/api/convert/'+slot,{method:'POST',body:form}).then(r=>r.json()).then(d=>{
    if(card)card.style.opacity='1';
    if(d.ok){
      toast(d.message,true);
      reloadCard(slot);
      if(slot==='menu_bg_base')reloadCard('menu_bg');
    } else {
      toast(d.error,false);
    }
  }).catch(()=>{
    if(card)card.style.opacity='1';
    toast('Upload failed',false);
  });
}

function removeAsset(slot,btn){
  if(!btn.dataset.confirming){
    btn.dataset.confirming='1';
    btn.textContent='Confirm?';
    btn.classList.add('confirming');
    btn._timeout=setTimeout(()=>{
      delete btn.dataset.confirming;
      btn.textContent='Remove';
      btn.classList.remove('confirming');
    },3000);
    return;
  }
  clearTimeout(btn._timeout);
  btn.disabled=true;
  btn.textContent='Removing...';
  fetch('/api/remove/'+slot,{method:'POST'}).then(r=>r.json()).then(d=>{
    if(d.ok){
      toast('Removed '+slot.replace(/_/g,' '),true);
      reloadCard(slot);
      if(slot==='menu_bg_base')reloadCard('menu_bg');
    }else{
      toast(d.error,false);
      btn.disabled=false;btn.textContent='Remove';
      btn.classList.remove('confirming');delete btn.dataset.confirming;
    }
  }).catch(()=>{
    toast('Remove failed',false);
    btn.disabled=false;btn.textContent='Remove';
    btn.classList.remove('confirming');delete btn.dataset.confirming;
  });
}

// Tabs
document.querySelectorAll('.tab').forEach(tab=>{
  tab.addEventListener('click',()=>{
    document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('panel-'+tab.dataset.tab).classList.add('active');
  });
});

// Move tool
const CHROMA_HEX={menu_icon:'#8C8C8C',sel:'#8C8C8C',unsel:'#8C8C8C',game:'#8C8C8C',control:'#8C8C8C',digit:'#808080'};
function getChroma(tags){for(let i=tags.length-1;i>=0;i--)if(CHROMA_HEX[tags[i]])return CHROMA_HEX[tags[i]];return'#8C8C8C';}
let moveState={};

function openMove(slot){
  const entry=DATA.entries.find(e=>e.slot===slot);
  if(!entry||!entry.theme)return;
  const modal=document.getElementById('moveModal');
  modal.innerHTML=`<div class="modal">
    <div class="modal-head">
      <span>Move: <b>${slot.replace(/_/g,' ')}</b></span>
      <span class="modal-offset" id="moveOffset">x: 0  y: 0</span>
      <div class="modal-actions">
        <button class="fbtn fbtn-warn" onclick="moveState.dx=0;moveState.dy=0;redrawMove()">Reset</button>
        <button class="fbtn fbtn-ok" id="moveSaveBtn" onclick="saveMove()">Save</button>
        <button class="fbtn" style="background:#333;color:#999" onclick="closeMove()">&#x2715;</button>
      </div>
    </div>
    <canvas id="moveCanvas" class="modal-canvas"></canvas>
    <div class="modal-controls" style="display:flex;align-items:center;gap:12px;margin-top:10px;justify-content:center">
      <label style="font-size:.75rem;color:#888;cursor:pointer;display:flex;align-items:center;gap:4px">
        <input type="checkbox" id="guideToggle" checked onchange="showGuides=this.checked;redrawMove()"> Guides
      </label>
      <label style="font-size:.75rem;color:#888;display:flex;align-items:center;gap:4px">
        Padding: <input type="range" id="guidePadRange" min="2" max="30" value="10" style="width:100px;accent-color:#6bf"
          oninput="guidePad=+this.value;redrawMove()">
        <span id="guidePadVal" style="font-family:monospace;color:#6bf;min-width:32px">10px</span>
      </label>
    </div>
    <div class="modal-hint">Drag to move &middot; Arrow &#177;1px &middot; Shift+Arrow &#177;5px &middot; Esc to cancel</div>
  </div>`;
  const img=new Image();
  img.onload=()=>{
    const maxD=Math.max(img.naturalWidth,img.naturalHeight);
    const scale=Math.max(1,Math.floor(400/maxD));
    moveState={slot,dx:0,dy:0,img,scale,dragging:false,lastX:0,lastY:0,tags:entry.tags};
    const c=document.getElementById('moveCanvas');
    c.width=img.naturalWidth*scale;
    c.height=img.naturalHeight*scale;
    c.onmousedown=e=>{moveState.dragging=true;moveState.lastX=e.clientX;moveState.lastY=e.clientY;};
    c.onmousemove=e=>{
      if(!moveState.dragging)return;
      moveState.dx+=(e.clientX-moveState.lastX)/moveState.scale;
      moveState.dy+=(e.clientY-moveState.lastY)/moveState.scale;
      moveState.lastX=e.clientX;moveState.lastY=e.clientY;
      redrawMove();
    };
    c.onmouseup=()=>{moveState.dragging=false;moveState.dx=Math.round(moveState.dx);moveState.dy=Math.round(moveState.dy);redrawMove();};
    c.onmouseleave=()=>{if(moveState.dragging){moveState.dragging=false;moveState.dx=Math.round(moveState.dx);moveState.dy=Math.round(moveState.dy);redrawMove();}};
    redrawMove();
    modal.style.display='flex';
  };
  img.src=entry.theme.url+'?t='+Date.now();
}

function closeMove(){document.getElementById('moveModal').style.display='none';mbgActive=false;}

let showGuides=true;
let guidePad=10;

function redrawMove(){
  const{img,dx,dy,scale,tags}=moveState;
  const c=document.getElementById('moveCanvas');
  const ctx=c.getContext('2d');
  const w=c.width,h=c.height;

  ctx.fillStyle=getChroma(tags);
  ctx.fillRect(0,0,w,h);
  ctx.imageSmoothingEnabled=false;
  ctx.drawImage(img,Math.round(dx)*scale,Math.round(dy)*scale,img.naturalWidth*scale,img.naturalHeight*scale);

  if(showGuides){
    const pad=guidePad*scale;
    // Crosshair
    ctx.strokeStyle='rgba(0,180,255,0.4)';
    ctx.lineWidth=1;
    ctx.setLineDash([4,4]);
    ctx.beginPath();
    ctx.moveTo(w/2,0);ctx.lineTo(w/2,h);
    ctx.moveTo(0,h/2);ctx.lineTo(w,h/2);
    ctx.stroke();
    // Bounding box
    ctx.strokeStyle='rgba(255,180,0,0.5)';
    ctx.setLineDash([6,3]);
    ctx.strokeRect(pad,pad,w-pad*2,h-pad*2);
    ctx.setLineDash([]);
  }

  document.getElementById('moveOffset').textContent='x: '+Math.round(dx)+'  y: '+Math.round(dy);
  const gp=document.getElementById('guidePadVal');
  if(gp)gp.textContent=guidePad+'px';
}

function saveMove(){
  const{slot,dx,dy}=moveState;
  const rdx=Math.round(dx),rdy=Math.round(dy);
  if(rdx===0&&rdy===0){closeMove();return;}
  const btn=document.getElementById('moveSaveBtn');
  btn.disabled=true;btn.textContent='Saving...';
  fetch('/api/reposition/'+slot,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dx:rdx,dy:rdy})})
    .then(r=>r.json()).then(d=>{
      btn.disabled=false;btn.textContent='Save';
      if(d.ok){toast(d.message,true);closeMove();reloadCard(slot);}
      else toast(d.error,false);
    }).catch(()=>{btn.disabled=false;btn.textContent='Save';toast('Save failed',false);});
}

document.addEventListener('keydown',e=>{
  const modal=document.getElementById('moveModal');
  if(modal.style.display!=='flex')return;
  let step=e.shiftKey?5:1;
  if(mbgActive){
    if(mbg.selected<0)return;
    const name=mbg.NAMES[mbg.selected];
    switch(e.key){
      case'ArrowLeft':mbg.offsets[name].dx-=step;break;
      case'ArrowRight':mbg.offsets[name].dx+=step;break;
      case'ArrowUp':mbg.offsets[name].dy-=step;break;
      case'ArrowDown':mbg.offsets[name].dy+=step;break;
      case'Escape':closeMove();return;
      default:return;
    }
    e.preventDefault();
    redrawMbg();
    return;
  }
  switch(e.key){
    case'ArrowLeft':moveState.dx-=step;break;
    case'ArrowRight':moveState.dx+=step;break;
    case'ArrowUp':moveState.dy-=step;break;
    case'ArrowDown':moveState.dy+=step;break;
    case'Escape':closeMove();return;
    default:return;
  }
  e.preventDefault();
  redrawMove();
});

// Menu BG Layer Editor
let mbgActive=false;
let mbg={icons:[],offsets:{},selected:-1,dragging:false,lastX:0,lastY:0,SCALE:2,COORDS:[],NAMES:[]};
const MBG_CHROMA=[140,140,140];

function openMenuBgEditor(){
  mbgActive=true;
  const modal=document.getElementById('moveModal');
  const S=2,W=320,H=240;
  Promise.all([
    fetch('/api/menu-bg-coords').then(r=>r.json()),
    fetch('/api/menu-bg-offsets').then(r=>r.json())
  ]).then(([layout,offsets])=>{
    const COORDS=layout.coords, NAMES=layout.names;
    const availEffects=offsets._effects||['none','glow_warm','glow_cool','outline'];
    const curEffect=offsets._effect||'none';
    let loaded=0;
    const icons=new Array(NAMES.length).fill(null);
    NAMES.forEach((name,i)=>{
      const entry=DATA.entries.find(e=>e.slot===name);
      if(!entry||!entry.theme){loaded++;if(loaded===NAMES.length)initMbg();return;}
      const img=new Image();
      img.onload=()=>{
        const oc=document.createElement('canvas');
        oc.width=img.naturalWidth;oc.height=img.naturalHeight;
        const octx=oc.getContext('2d');
        octx.drawImage(img,0,0);
        const id=octx.getImageData(0,0,oc.width,oc.height);
        for(let p=0;p<id.data.length;p+=4){
          if(id.data[p]===MBG_CHROMA[0]&&id.data[p+1]===MBG_CHROMA[1]&&id.data[p+2]===MBG_CHROMA[2])id.data[p+3]=0;
        }
        icons[i]={imageData:id,w:oc.width,h:oc.height};
        loaded++;if(loaded===NAMES.length)initMbg();
      };
      img.src=entry.theme.url+'?t='+Date.now();
    });
    function initMbg(){
      mbg={icons,offsets:{},selected:-1,dragging:false,lastX:0,lastY:0,SCALE:S,COORDS,NAMES,effect:curEffect,baseImg:null};
      const baseEntry=DATA.entries.find(e=>e.slot==='menu_bg_base');
      if(baseEntry&&baseEntry.theme){const bi=new Image();bi.onload=()=>{mbg.baseImg=bi;redrawMbg();};bi.src=baseEntry.theme.url+'?t='+Date.now();}
      NAMES.forEach(n=>{mbg.offsets[n]={dx:(offsets[n]&&offsets[n].dx)||0,dy:(offsets[n]&&offsets[n].dy)||0};});
      const effectLabels={none:'None',desaturate:'Desaturate',warm_tint:'Warm Tint',cool_tint:'Cool Tint'};
      modal.innerHTML=`<div class="modal">
        <div class="modal-head">
          <span><b>Menu BG — Edit Layers</b></span>
          <span class="modal-offset" id="mbgOff">Click an icon</span>
          <div class="modal-actions">
            <button class="fbtn fbtn-warn" onclick="resetMbg()">Reset All</button>
            <button class="fbtn fbtn-ok" id="mbgSave" onclick="saveMbg()">Save</button>
            <button class="fbtn" style="background:#333;color:#999" onclick="closeMove()">&#x2715;</button>
          </div>
        </div>
        <canvas id="mbgCanvas" class="modal-canvas" width="${W*S}" height="${H*S}"></canvas>
        <div style="display:flex;gap:6px;margin-top:10px;justify-content:center;flex-wrap:wrap">
          <span style="color:#555;font-size:.7rem;line-height:26px">Effect:</span>
          ${availEffects.map(e=>`<button class="fbtn mbg-fx" data-fx="${e}"
            style="font-size:.7rem;padding:4px 10px;background:#222;color:#888;border:1px solid #333"
            onclick="mbg.effect='${e}';document.querySelectorAll('.mbg-fx').forEach(b=>{b.style.background='#222';b.style.color='#888';b.style.borderColor='#333'});this.style.background='#1a3a5c';this.style.color='#6bf';this.style.borderColor='#2a6ab0';redrawMbg()">${effectLabels[e]||e}</button>`).join('')}
        </div>
        <div style="display:flex;gap:6px;margin-top:8px;justify-content:center;flex-wrap:wrap" id="mbgChips"></div>
        <div class="modal-hint">Click icon to select &middot; Drag to move &middot; Arrow &plusmn;1px &middot; Shift+Arrow &plusmn;5px</div>
      </div>`;
      document.querySelectorAll('.mbg-fx').forEach(b=>{
        if(b.dataset.fx===mbg.effect){b.style.background='#1a3a5c';b.style.color='#6bf';b.style.borderColor='#2a6ab0';}
      });
      const chips=document.getElementById('mbgChips');
      NAMES.forEach((name,i)=>{
        if(!icons[i])return;
        const b=document.createElement('button');
        b.className='fbtn';b.id='mbgc-'+i;
        b.style.cssText='background:#222;color:#888;font-size:.7rem;border:1px solid #333;padding:4px 10px';
        b.textContent=name;
        b.onclick=()=>{mbg.selected=i;redrawMbg();};
        chips.appendChild(b);
      });
      const c=document.getElementById('mbgCanvas');
      c.onmousedown=e=>{
        const r=c.getBoundingClientRect();
        const mx=(e.clientX-r.left)/S,my=(e.clientY-r.top)/S;
        for(let i=NAMES.length-1;i>=0;i--){
          if(!icons[i])continue;
          const ox=COORDS[i][0]+mbg.offsets[NAMES[i]].dx;
          const oy=COORDS[i][1]+mbg.offsets[NAMES[i]].dy;
          if(mx>=ox&&mx<ox+icons[i].w&&my>=oy&&my<oy+icons[i].h){
            mbg.selected=i;mbg.dragging=true;mbg.lastX=e.clientX;mbg.lastY=e.clientY;
            redrawMbg();return;
          }
        }
      };
      c.onmousemove=e=>{
        if(!mbg.dragging||mbg.selected<0)return;
        const n=NAMES[mbg.selected];
        mbg.offsets[n].dx+=(e.clientX-mbg.lastX)/S;
        mbg.offsets[n].dy+=(e.clientY-mbg.lastY)/S;
        mbg.lastX=e.clientX;mbg.lastY=e.clientY;
        redrawMbg();
      };
      c.onmouseup=c.onmouseleave=()=>{
        if(mbg.dragging&&mbg.selected>=0){
          const n=NAMES[mbg.selected];
          mbg.offsets[n].dx=Math.round(mbg.offsets[n].dx);
          mbg.offsets[n].dy=Math.round(mbg.offsets[n].dy);
        }
        mbg.dragging=false;redrawMbg();
      };
      redrawMbg();
      modal.style.display='flex';
    }
  });
}

function redrawMbg(){
  const{icons,offsets,selected,SCALE:S,COORDS,NAMES,effect}=mbg;
  const c=document.getElementById('mbgCanvas');
  if(!c)return;
  const ctx=c.getContext('2d');
  ctx.filter='none';
  ctx.fillStyle='#000';ctx.fillRect(0,0,c.width,c.height);
  if(mbg.baseImg)ctx.drawImage(mbg.baseImg,0,0,c.width,c.height);
  ctx.imageSmoothingEnabled=false;
  NAMES.forEach((name,i)=>{
    if(!icons[i])return;
    const dx=Math.round(offsets[name].dx),dy=Math.round(offsets[name].dy);
    const x=(COORDS[i][0]+dx)*S,y=(COORDS[i][1]+dy)*S;
    const oc=document.createElement('canvas');
    oc.width=icons[i].w;oc.height=icons[i].h;
    oc.getContext('2d').putImageData(icons[i].imageData,0,0);
    ctx.drawImage(oc,x,y,icons[i].w*S,icons[i].h*S);
    if(i===selected){
      ctx.strokeStyle='#2a6ab0';ctx.lineWidth=2;ctx.setLineDash([6,3]);
      ctx.strokeRect(x-1,y-1,icons[i].w*S+2,icons[i].h*S+2);
      ctx.setLineDash([]);
    }
  });
  const fxFilter={none:'none',desaturate:'saturate(0.35)',warm_tint:'saturate(0.5) sepia(0.2)',cool_tint:'saturate(0.5) hue-rotate(200deg) brightness(0.9)'};
  c.style.filter=fxFilter[effect]||'none';
  NAMES.forEach((name,i)=>{
    const ch=document.getElementById('mbgc-'+i);
    if(!ch)return;
    const dx=Math.round(offsets[name].dx),dy=Math.round(offsets[name].dy);
    ch.style.background=i===selected?'#1a3a5c':'#222';
    ch.style.color=i===selected?'#6bf':'#888';
    ch.style.borderColor=i===selected?'#2a6ab0':'#333';
    ch.textContent=(dx||dy)?`${name} (${dx},${dy})`:name;
  });
  const lbl=document.getElementById('mbgOff');
  if(lbl){
    if(selected>=0&&icons[selected]){
      const n=NAMES[selected],dx=Math.round(offsets[n].dx),dy=Math.round(offsets[n].dy);
      lbl.textContent=`${n}: x=${dx}  y=${dy}`;
    }else lbl.textContent='Click an icon';
  }
}

function resetMbg(){mbg.NAMES.forEach(n=>{mbg.offsets[n]={dx:0,dy:0};});redrawMbg();}

function saveMbg(){
  const btn=document.getElementById('mbgSave');
  btn.disabled=true;btn.textContent='Saving...';
  const out={_effect:mbg.effect||'none'};
  mbg.NAMES.forEach(n=>{out[n]={dx:Math.round(mbg.offsets[n].dx),dy:Math.round(mbg.offsets[n].dy)};});
  fetch('/api/menu-bg-save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(out)})
    .then(r=>r.json()).then(d=>{
      btn.disabled=false;btn.textContent='Save';
      if(d.ok){toast(d.message,true);closeMove();reloadCard('menu_bg');}
      else toast(d.error,false);
    }).catch(()=>{btn.disabled=false;btn.textContent='Save';toast('Save failed',false);});
}

function runBuild(){
  const btn=document.getElementById('buildBtn');
  btn.disabled=true;
  btn.textContent='Building...';
  btn.className='build-btn building';
  fetch('/api/build',{method:'POST'}).then(r=>r.json()).then(d=>{
    btn.disabled=false;
    if(d.ok){
      btn.textContent='Built & Flashed!';
      btn.className='build-btn success';
      toast(d.message,true);
    } else {
      btn.textContent='Build Failed';
      btn.className='build-btn';
      toast(d.error,false);
    }
    if(d.output){
      let out=document.getElementById('buildOutput');
      if(!out){
        out=document.createElement('div');
        out.id='buildOutput';
        out.className='build-output';
        document.querySelector('.header').after(out);
      }
      out.style.display='block';
      out.textContent=d.output;
      out.scrollTop=out.scrollHeight;
    }
    setTimeout(()=>{btn.textContent='Build & Flash to SD';btn.className='build-btn'},4000);
  }).catch(()=>{
    btn.disabled=false;
    btn.textContent='Build & Flash to SD';
    btn.className='build-btn';
    toast('Build request failed',false);
  });
}
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("", "/"):
            self.respond(200, "text/html", HTML_PAGE.encode())
        elif path == "/api/manifest":
            refresh_manifest()
            self.respond(200, "application/json", MANIFEST_JSON)
        elif path.startswith("/originals/"):
            key = path[len("/originals/"):]
            self.serve_file(ORIGINAL_FILES.get(key))
        elif path.startswith("/theme/"):
            key = path[len("/theme/"):]
            filepath = THEME_FILES.get(key)
            if not filepath:
                theme_dir = PROJECT / "themes" / CURRENT_THEME / "firmware_exports"
                p = theme_dir / key
                if p.exists():
                    filepath = p
            self.serve_file(filepath)
        elif path == "/api/menu-bg-offsets":
            theme_dir = PROJECT / "themes" / CURRENT_THEME / "firmware_exports"
            offsets = load_menu_bg_offsets(theme_dir)
            effect = load_menu_bg_effect(theme_dir)
            data = {n: {"dx": dx, "dy": dy} for n, (dx, dy) in offsets.items()}
            data["_effect"] = effect
            data["_effects"] = MENU_BG_EFFECTS
            self.respond(200, "application/json", json.dumps(data).encode())
        elif path == "/api/menu-bg-coords":
            self.respond(200, "application/json", json.dumps({
                "coords": MENU_LAYOUT_REF["coords"],
                "names": MENU_ICON_NAMES,
            }).encode())
        elif path == "/api/sd-status":
            mounted = Path("/Volumes/NO NAME").exists()
            self.respond(200, "application/json", json.dumps({"mounted": mounted}).encode())
        else:
            self.respond(404, "text/plain", b"Not found")

    def do_POST(self):
        path = urlparse(self.path).path

        if path.startswith("/api/open/"):
            slot = path[len("/api/open/"):]
            self.handle_open(slot)
        elif path.startswith("/api/normalize/"):
            slot = path[len("/api/normalize/"):]
            self.handle_normalize(slot)
        elif path.startswith("/api/convert/"):
            slot = path[len("/api/convert/"):]
            self.handle_convert(slot)
        elif path.startswith("/api/remove/"):
            slot = path[len("/api/remove/"):]
            self.handle_remove(slot)
        elif path.startswith("/api/reposition/"):
            slot = path[len("/api/reposition/"):]
            self.handle_reposition(slot)
        elif path == "/api/menu-bg-save":
            self.handle_menu_bg_save()
        elif path == "/api/build":
            self.handle_build()
        else:
            self.respond(404, "application/json", json.dumps({"ok": False, "error": "Not found"}).encode())

    def handle_open(self, slot):
        if slot == "menu_bg_base":
            theme_dir = PROJECT / "themes" / CURRENT_THEME / "firmware_exports"
            filepath = find_menu_bg_base(theme_dir)
        else:
            filepath = find_theme_file(slot)
        if not filepath:
            return self.json_resp({"ok": False, "error": f"No theme file for slot '{slot}'"})
        try:
            subprocess.Popen(["open", "-a", "Aseprite", str(filepath)])
            self.json_resp({"ok": True})
        except FileNotFoundError:
            self.json_resp({"ok": False, "error": "Aseprite not found. Install it or check the app name."})
        except Exception as e:
            self.json_resp({"ok": False, "error": str(e)})

    def handle_normalize(self, slot):
        if slot not in SLOT_BY_NAME:
            return self.json_resp({"ok": False, "error": f"Unknown slot '{slot}'"})

        info = SLOT_BY_NAME[slot]
        fmt = info[3]
        max_size = info[2]

        if fmt != "bmp":
            return self.json_resp({"ok": False, "error": f"Normalize only supports BMP (this slot is {fmt})"})

        filepath = find_theme_file(slot)
        if not filepath:
            return self.json_resp({"ok": False, "error": f"No theme file for slot '{slot}'"})

        ok, detail = normalize_bmp(filepath, max_size)
        if ok:
            THEME_FILES[filepath.name] = filepath
            self.json_resp({"ok": True, "detail": detail})
        else:
            self.json_resp({"ok": False, "error": detail})

    def handle_reposition(self, slot):
        if slot not in SLOT_BY_NAME:
            return self.json_resp({"ok": False, "error": f"Unknown slot '{slot}'"})
        info = SLOT_BY_NAME[slot]
        if info[3] != "bmp":
            return self.json_resp({"ok": False, "error": "Reposition only works on BMP slots"})
        filepath = find_theme_file(slot)
        if not filepath:
            return self.json_resp({"ok": False, "error": f"No theme file for '{slot}'"})
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        dx = body.get("dx", 0)
        dy = body.get("dy", 0)
        ok, msg = reposition_bmp(filepath, dx, dy, info)
        self.json_resp({"ok": ok, "message": msg} if ok else {"ok": False, "error": msg})

    def handle_remove(self, slot):
        if slot == "menu_bg_base":
            theme_dir = PROJECT / "themes" / CURRENT_THEME / "firmware_exports"
            removed = False
            for ext in ["png", "jpg", "jpeg", "bmp"]:
                p = theme_dir / f"menu_bg_base.{ext}"
                if p.exists():
                    p.unlink()
                    if p.name in THEME_FILES:
                        del THEME_FILES[p.name]
                    removed = True
            if removed:
                try:
                    effect = load_menu_bg_effect(theme_dir)
                    generate_menu_bg(theme_dir, effect=effect)
                    THEME_FILES["07_menu_bg_320x240.jpg"] = theme_dir / "07_menu_bg_320x240.jpg"
                except Exception:
                    pass
                return self.json_resp({"ok": True, "message": "Base image removed, menu BG regenerated"})
            return self.json_resp({"ok": False, "error": "No base image to remove"})

        if slot not in SLOT_BY_NAME:
            return self.json_resp({"ok": False, "error": f"Unknown slot '{slot}'"})
        filepath = find_theme_file(slot)
        if not filepath:
            return self.json_resp({"ok": False, "error": f"No theme file for '{slot}'"})
        try:
            name = filepath.name
            subprocess.run(
                ["osascript", "-e",
                 f'tell app "Finder" to move POSIX file "{filepath.resolve()}" to trash'],
                capture_output=True, timeout=5,
            )
            if name in THEME_FILES:
                del THEME_FILES[name]
            self.json_resp({"ok": True, "message": f"Removed {name}"})
        except Exception as e:
            self.json_resp({"ok": False, "error": str(e)})

    def handle_menu_bg_save(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        theme_dir = PROJECT / "themes" / CURRENT_THEME / "firmware_exports"
        effect = body.get("_effect", "none")
        offsets = {}
        for name in MENU_ICON_NAMES:
            if name in body:
                offsets[name] = (body[name].get("dx", 0), body[name].get("dy", 0))
            else:
                offsets[name] = (0, 0)
        save_menu_bg_offsets(theme_dir, offsets, effect=effect)
        try:
            generate_menu_bg(theme_dir, effect=effect)
            THEME_FILES["07_menu_bg_320x240.jpg"] = theme_dir / "07_menu_bg_320x240.jpg"
            self.json_resp({"ok": True, "message": f"Menu BG regenerated [{effect}]"})
        except Exception as e:
            self.json_resp({"ok": False, "error": str(e)})

    def handle_build(self):
        build_script = TOOLS_DIR / "build.py"
        cmd = [sys.executable, str(build_script), "build", "--theme", CURRENT_THEME, "--sd"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=str(PROJECT))
            output = r.stdout + r.stderr
            if r.returncode == 0:
                self.json_resp({"ok": True, "message": "Firmware built and flashed to SD", "output": output})
            else:
                self.json_resp({"ok": False, "error": "Build failed", "output": output})
        except subprocess.TimeoutExpired:
            self.json_resp({"ok": False, "error": "Build timed out"})
        except Exception as e:
            self.json_resp({"ok": False, "error": str(e)})

    def handle_convert(self, slot):
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            return self.json_resp({"ok": False, "error": "Expected multipart form data"})

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        boundary = None
        for part in content_type.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[len("boundary="):]
                break
        if not boundary:
            return self.json_resp({"ok": False, "error": "No boundary"})

        file_data = None
        file_ext = ".png"
        for part in body.split(f"--{boundary}".encode()):
            if b"Content-Disposition" not in part:
                continue
            header_end = part.find(b"\r\n\r\n")
            if header_end < 0:
                continue
            header_section = part[:header_end].decode("utf-8", errors="replace")
            payload = part[header_end + 4:]
            if payload.endswith(b"\r\n"):
                payload = payload[:-2]
            if 'name="file"' in header_section:
                file_data = payload
                fn_match = re.search(r'filename="([^"]+)"', header_section)
                if fn_match:
                    file_ext = Path(fn_match.group(1)).suffix or ".png"

        if not file_data:
            return self.json_resp({"ok": False, "error": "No file uploaded"})

        tmp = Path(tempfile.mktemp(suffix=file_ext))
        try:
            tmp.write_bytes(file_data)

            if slot == "menu_bg_base":
                try:
                    from PIL import Image as PILImage
                    theme_dir = PROJECT / "themes" / CURRENT_THEME / "firmware_exports"
                    theme_dir.mkdir(parents=True, exist_ok=True)
                    for ext in ["png", "jpg", "jpeg", "bmp"]:
                        old = theme_dir / f"menu_bg_base.{ext}"
                        if old.exists():
                            old.unlink()
                    img = PILImage.open(str(tmp)).convert("RGBA").resize((320, 240), PILImage.LANCZOS)
                    out_path = theme_dir / "menu_bg_base.png"
                    img.save(str(out_path), "PNG")
                    THEME_FILES["menu_bg_base.png"] = out_path
                    effect = load_menu_bg_effect(theme_dir)
                    generate_menu_bg(theme_dir, effect=effect)
                    THEME_FILES["07_menu_bg_320x240.jpg"] = theme_dir / "07_menu_bg_320x240.jpg"
                    self.json_resp({"ok": True, "message": "Base image set, menu BG regenerated",
                                    "preview_url": "/theme/menu_bg_base.png"})
                except Exception as e:
                    self.json_resp({"ok": False, "error": str(e)})
                return

            ok, msg, out_path = convert_asset(str(tmp), slot)
            if ok:
                resp = {"ok": True, "message": msg}
                if out_path and out_path.exists():
                    resp["preview_url"] = f"/theme/{out_path.name}"
                self.json_resp(resp)
            else:
                self.json_resp({"ok": False, "error": msg})
        finally:
            tmp.unlink(missing_ok=True)

    def serve_file(self, filepath):
        if filepath and filepath.exists():
            ext = filepath.suffix.lstrip(".").lower()
            mime = MIME.get(ext, "application/octet-stream")
            self.respond(200, mime, filepath.read_bytes())
        else:
            self.respond(404, "text/plain", b"Not found")

    def respond(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def json_resp(self, obj):
        self.respond(200, "application/json", json.dumps(obj).encode())

    def log_message(self, fmt, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description="KC02 Asset Comparison Server")
    parser.add_argument("--theme", default="modern_pixel_light")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    theme_dir = PROJECT / "themes" / args.theme / "firmware_exports"
    if not theme_dir.exists():
        available = [d.name for d in (PROJECT / "themes").iterdir() if d.is_dir()]
        print(f"Theme '{args.theme}' not found. Available: {', '.join(available)}")
        sys.exit(1)

    init_data(args.theme)

    manifest = json.loads(MANIFEST_JSON)
    server = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"KC02 Asset Compare")
    print(f"  Theme:  {args.theme}")
    print(f"  Slots:  {manifest['replaced']}/{manifest['total']} replaced")
    print(f"  URL:    http://localhost:{args.port}")
    print(f"  Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
