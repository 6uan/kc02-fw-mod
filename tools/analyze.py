#!/usr/bin/env python3
"""
KC02 Firmware Analyzer
======================
Map every byte of the 4MB firmware binary. Find strings, pointers,
data tables, and unknown regions to enable non-asset firmware mods.

Usage:
    python3 analyze.py scan                  # full analysis report
    python3 analyze.py map                   # memory map with coverage
    python3 analyze.py strings               # find all embedded strings
    python3 analyze.py pointers              # find code/data pointers
    python3 analyze.py hexdump 0x07DF08 96   # hex dump at offset
    python3 analyze.py xrefs 0x07E160        # find references to address
    python3 analyze.py diff file1 file2      # compare two firmware bins
    python3 analyze.py header                # parse BLDR header
    python3 analyze.py sfat                  # parse asset table
    python3 analyze.py entropy               # byte entropy by region
"""

import struct
import sys
import os
import json
import hashlib
import argparse
import math
from pathlib import Path
from collections import defaultdict

PROJECT = Path(__file__).resolve().parent.parent

# ─── Architecture Constants ───
FIRMWARE_SIZE = 0x400000  # 4MB
FLASH_BASE = 0x02000000  # pi32 maps SPI flash here in memory

# ─── Known Memory Regions ───
REGIONS = [
    (0x000000, 0x0001FF, "header",  "BLDR boot header"),
    (0x000200, 0x0002FF, "config",  "SFC timing parameters"),
    (0x000300, 0x0003FF, "config",  "SFAT config / flash header"),
    (0x000400, 0x0025FF, "code",    "Boot init code"),
    (0x002600, 0x0C87FF, "code",    "Application code"),
    (0x0C8800, 0x0D31FF, "config",  "Flash chip driver tables"),
    (0x0D3200, 0x0D3510, "sfat",    "Asset table (SFAT)"),
    (0x0D3510, 0x3071FF, "assets",  "Asset data (BMP/JPEG/WAV/raw)"),
    (0x307200, 0x3FFFFF, "free",    "Erased flash (0xFF)"),
]

# ─── Known Data Tables (embedded in code section) ───
KNOWN_TABLES = {
    "menu_coord_table": {
        "offset": 0x07E160,
        "size": 24,
        "entry_size": 4,
        "description": "Menu icon coordinates (6 x u16 x, u16 y)",
        "format": "<HH",
        "labels": ["Camera", "Video", "Music", "Playback", "Games", "Settings"],
    },
    "menu_asset_table": {
        "offset": 0x07E178,
        "size": 24,
        "entry_size": 4,
        "description": "Menu icon SFAT indices (6 x u32)",
        "format": "<I",
        "labels": ["Camera", "Video", "Music", "Playback", "Games", "Settings"],
    },
    "menu_handler_table": {
        "offset": 0x07DF08,
        "size": 24,
        "entry_size": 4,
        "description": "Menu item handler function pointers (6 x u32)",
        "format": "<I",
        "labels": ["Camera", "Video", "Music", "Playback", "Games", "Settings"],
    },
    "ui_state_dispatch": {
        "offset": 0x07DF08,
        "size": 140,
        "entry_size": 4,
        "description": "Full UI state dispatch table (35 x u32 function pointers)",
        "format": "<I",
        "labels": [
            "Camera", "Video", "Music", "Playback", "Games", "Settings",
            "state_06", "null_07", "null_08", "null_09", "null_10", "null_11",
            "state_12", "state_13", "state_14", "null_15", "null_16",
            "state_17", "null_18", "state_19", "state_20", "state_21",
            "state_22", "null_23", "null_24", "null_25", "null_26",
            "state_27", "null_28", "null_29", "null_30", "null_31",
            "state_32", "state_33", "state_34",
        ],
    },
    "init_callback_table": {
        "offset": 0x07E000,
        "size": 40,
        "entry_size": 4,
        "description": "Initialization callback pointers (10 x u32)",
        "format": "<I",
        "labels": [f"init_{i}" for i in range(10)],
    },
    "mode_callback_table": {
        "offset": 0x07E19C,
        "size": 20,
        "entry_size": 4,
        "description": "Mode transition callback pointers (5 x u32)",
        "format": "<I",
        "labels": [f"mode_{i}" for i in range(5)],
    },
}

MENU_LABEL_OFFSETS = {
    "Settings": 0x239C5A,
    "Video":    0x239C62,
    "Photo":    0x239C6A,
    "Playback": 0x239C72,
    "MP3":      0x239C7A,
    "Games":    0x239C82,
}

# ─── Header Field Definitions ───
HEADER_FIELDS = [
    (0x00, 2, "jump",       "Boot jump instruction"),
    (0x02, 2, "reserved_02", "Reserved"),
    (0x04, 4, "magic",      "Bootloader magic ('BLDR')"),
    (0x08, 4, "reserved_08", "Reserved / flags"),
    (0x0C, 1, "checksum",   "Header checksum byte"),
    (0x0D, 3, "reserved_0D", "Reserved"),
    (0x10, 4, "entry_point", "Code entry point"),
    (0x14, 4, "reserved_14", "Reserved / load address"),
    (0x18, 4, "reserved_18", "Reserved"),
    (0x1C, 4, "reserved_1C", "Reserved"),
    (0x20, 4, "reserved_20", "Reserved"),
    (0x24, 4, "reserved_24", "Reserved"),
    (0x28, 4, "reserved_28", "Reserved"),
    (0x2C, 4, "reserved_2C", "Reserved"),
    (0x30, 4, "validity_1", "Validity marker (0x01234567)"),
    (0x34, 4, "validity_2", "Validity marker (0x01234567)"),
]


def fmt_size(n):
    if n >= 1024 * 1024:
        return f"{n / (1024*1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def hexbytes(data, max_len=16):
    s = data[:max_len].hex()
    return " ".join(s[i:i+2] for i in range(0, len(s), 2))


class FirmwareAnalyzer:
    def __init__(self, path):
        self.path = Path(path)
        self.data = bytearray(self.path.read_bytes())
        self.size = len(self.data)
        self.md5 = hashlib.md5(self.data).hexdigest()

    def region_for(self, offset):
        for start, end, rtype, name in REGIONS:
            if start <= offset <= end:
                return (start, end, rtype, name)
        return None

    # ── Header ──────────────────────────────────────────────

    def parse_header(self):
        print(f"\n{'═' * 60}")
        print(f"  BLDR HEADER (0x000000 - 0x0001FF)")
        print(f"{'═' * 60}")

        hdr = self.data[:512]
        checksum_ok = sum(hdr) % 256 == 0

        for off, size, name, desc in HEADER_FIELDS:
            raw = hdr[off:off + size]
            if size == 1:
                val = raw[0]
                hex_val = f"0x{val:02X}"
            elif size == 2:
                val = struct.unpack_from("<H", hdr, off)[0]
                hex_val = f"0x{val:04X}"
            elif size == 4:
                val = struct.unpack_from("<I", hdr, off)[0]
                hex_val = f"0x{val:08X}"
            else:
                hex_val = hexbytes(raw)
                val = None

            ascii_str = ""
            if size == 4:
                try:
                    t = raw.decode("ascii")
                    if all(32 <= c < 127 for c in raw):
                        ascii_str = f'  "{t}"'
                except (UnicodeDecodeError, ValueError):
                    pass

            extra = ""
            if name == "checksum":
                extra = f"  ({'VALID' if checksum_ok else 'INVALID'}: sum mod 256 = {sum(hdr) % 256})"
            elif name == "entry_point" and val:
                flash_off = val - FLASH_BASE if val >= FLASH_BASE else val
                extra = f"  (flash offset 0x{flash_off:06X})" if val >= FLASH_BASE else ""

            print(f"  0x{off:04X} [{size:2d}B] {desc:<32s} {hex_val}{ascii_str}{extra}")

        # Boot signature
        sig = struct.unpack_from("<H", hdr, 0x1FE)[0]
        print(f"  0x01FE [ 2B] {'Boot signature':<32s} 0x{sig:04X}  {'(VALID 0x55AA)' if sig == 0x55AA else '(INVALID)'}")

        # Scan remaining header for non-zero regions
        print(f"\n  Non-zero regions in header (0x38-0x1FD):")
        in_nonzero = False
        nz_start = 0
        count = 0
        for i in range(0x38, 0x1FE):
            if hdr[i] != 0:
                if not in_nonzero:
                    nz_start = i
                    in_nonzero = True
            else:
                if in_nonzero:
                    length = i - nz_start
                    preview = hexbytes(hdr[nz_start:i], 24)
                    print(f"    0x{nz_start:04X}-0x{i-1:04X} ({length:3d}B): {preview}")
                    count += 1
                    in_nonzero = False
        if in_nonzero:
            length = 0x1FE - nz_start
            preview = hexbytes(hdr[nz_start:0x1FE], 24)
            print(f"    0x{nz_start:04X}-0x{0x1FD:04X} ({length:3d}B): {preview}")
            count += 1
        if count == 0:
            print(f"    (all zeros)")
        else:
            print(f"    {count} non-zero region(s)")

    # ── SFAT ────────────────────────────────────────────────

    def parse_sfat(self):
        print(f"\n{'═' * 60}")
        print(f"  SFAT ASSET TABLE (0x0D3200)")
        print(f"{'═' * 60}")

        base = 0x0D3200
        table_size = struct.unpack_from("<I", self.data, base)[0]
        magic = self.data[base + 4:base + 8]
        num_entries = (table_size - 8) // 8

        print(f"  Table size: {table_size} bytes")
        print(f"  Magic: {magic}  ({hexbytes(magic)})")
        print(f"  Entries: {num_entries}")
        print()
        print(f"  {'#':>3s}  {'Offset':>10s}  {'Size':>8s}  {'Type':>4s}  {'End':>10s}  Notes")
        print(f"  {'─' * 62}")

        entries = []
        for i in range(num_entries):
            off_pos = base + 8 + i * 8
            rel_offset, size = struct.unpack_from("<II", self.data, off_pos)
            abs_offset = base + rel_offset
            chunk = self.data[abs_offset:abs_offset + min(size, 4)]

            if chunk[:2] == b'\xff\xd8':
                fmt = "JPEG"
            elif chunk[:2] == b'BM':
                fmt = "BMP"
            elif chunk[:4] == b'RIFF':
                fmt = "WAV"
            else:
                fmt = "raw"

            notes = ""
            if abs_offset + size > self.size:
                notes = "OVERFLOW!"
            elif size == 0:
                notes = "empty"

            entries.append((i, abs_offset, size, fmt))
            print(f"  {i:3d}  0x{abs_offset:06X}  {size:8,}  {fmt:>4s}  0x{abs_offset + size:06X}  {notes}")

        # Gap analysis
        print(f"\n  Gap analysis (unmapped bytes between assets):")
        sorted_entries = sorted(entries, key=lambda e: e[1])
        total_gaps = 0
        gap_count = 0
        for i in range(len(sorted_entries) - 1):
            _, off1, size1, _ = sorted_entries[i]
            _, off2, _, _ = sorted_entries[i + 1]
            gap = off2 - (off1 + size1)
            if gap > 0:
                print(f"    0x{off1 + size1:06X}-0x{off2 - 1:06X}: {gap:,} bytes gap (after entry #{sorted_entries[i][0]})")
                total_gaps += gap
                gap_count += 1
            elif gap < 0:
                print(f"    OVERLAP: entry #{sorted_entries[i][0]} and #{sorted_entries[i+1][0]} overlap by {-gap} bytes")

        if gap_count:
            print(f"    {gap_count} gaps, {total_gaps:,} bytes total unmapped in asset region")
        else:
            print(f"    No gaps — assets are contiguous")

        return entries

    # ── String Scanner ──────────────────────────────────────

    def find_strings(self, min_len=4, sections=None):
        print(f"\n{'═' * 60}")
        print(f"  STRING SCAN (min length: {min_len})")
        print(f"{'═' * 60}")

        strings = []
        current = []
        start = 0

        for i in range(self.size):
            b = self.data[i]
            if 0x20 <= b <= 0x7E:
                if not current:
                    start = i
                current.append(chr(b))
            else:
                if len(current) >= min_len:
                    text = "".join(current)
                    region = self.region_for(start)
                    section = region[2] if region else "unknown"
                    if sections is None or section in sections:
                        strings.append((start, text, section))
                current = []

        if len(current) >= min_len:
            text = "".join(current)
            region = self.region_for(start)
            section = region[2] if region else "unknown"
            if sections is None or section in sections:
                strings.append((start, text, section))

        by_section = defaultdict(list)
        for offset, text, section in strings:
            by_section[section].append((offset, text))

        for section in ["header", "config", "code", "sfat", "assets", "free"]:
            items = by_section.get(section, [])
            if not items:
                continue
            print(f"\n  [{section.upper()}] — {len(items)} strings")
            print(f"  {'─' * 56}")
            for offset, text in items:
                display = text[:72]
                if len(text) > 72:
                    display += "..."
                print(f"    0x{offset:06X}: \"{display}\" ({len(text)})")

        print(f"\n  Total: {len(strings)} strings found")
        return strings

    # ── Pointer Scanner ─────────────────────────────────────

    def find_pointers(self):
        print(f"\n{'═' * 60}")
        print(f"  POINTER SCAN (flash base: 0x{FLASH_BASE:08X})")
        print(f"{'═' * 60}")

        flash_ptrs = []     # 0x0200XXXX — code/data in flash
        ram_ptrs = []        # 0x0000XXXX — RAM addresses
        periph_ptrs = []     # 0x01XXXXXX — peripheral registers

        code_start = 0x000200
        code_end = 0x0D3200

        for i in range(code_start, code_end - 3, 2):
            val = struct.unpack_from("<I", self.data, i)[0]

            if FLASH_BASE <= val < FLASH_BASE + FIRMWARE_SIZE:
                flash_off = val - FLASH_BASE
                flash_ptrs.append((i, val, flash_off))
            elif 0x01000000 <= val < 0x02000000:
                periph_ptrs.append((i, val))

        # Deduplicate: only keep pointers that look intentional
        # (aligned or in known table regions)
        aligned_flash = [(off, val, tgt) for off, val, tgt in flash_ptrs if off % 4 == 0]

        print(f"\n  Flash pointers (0x0200XXXX → flash offset):")
        print(f"  Found {len(flash_ptrs)} total, {len(aligned_flash)} 4-byte aligned")

        # Group by target region
        target_regions = defaultdict(list)
        for off, val, tgt in aligned_flash:
            region = self.region_for(tgt)
            rname = region[2] if region else "unknown"
            target_regions[rname].append((off, val, tgt))

        for rname, ptrs in sorted(target_regions.items()):
            print(f"\n    → Targets in [{rname}]: {len(ptrs)}")
            for off, val, tgt in ptrs[:30]:
                src_region = self.region_for(off)
                src_name = src_region[3] if src_region else "?"
                print(f"      0x{off:06X}: 0x{val:08X} → flash 0x{tgt:06X}")
            if len(ptrs) > 30:
                print(f"      ... and {len(ptrs) - 30} more")

        # Find pointer tables (consecutive aligned pointers to similar regions)
        print(f"\n  Pointer table detection (3+ consecutive flash pointers):")
        tables = self._find_pointer_tables(aligned_flash)
        for tbl_off, tbl_ptrs in tables:
            targets = [f"0x{tgt:06X}" for _, _, tgt in tbl_ptrs]
            print(f"    0x{tbl_off:06X}: {len(tbl_ptrs)} pointers → {', '.join(targets[:8])}")
            if len(targets) > 8:
                print(f"      ... and {len(targets) - 8} more entries")

        if periph_ptrs:
            print(f"\n  Peripheral register refs (0x01XXXXXX): {len(periph_ptrs)}")
            for off, val in periph_ptrs[:10]:
                print(f"    0x{off:06X}: 0x{val:08X}")
            if len(periph_ptrs) > 10:
                print(f"    ... and {len(periph_ptrs) - 10} more")

        return aligned_flash, tables

    def _find_pointer_tables(self, ptrs):
        if not ptrs:
            return []
        tables = []
        current_table = [ptrs[0]]
        for i in range(1, len(ptrs)):
            off, val, tgt = ptrs[i]
            prev_off = ptrs[i - 1][0]
            if off == prev_off + 4:
                current_table.append(ptrs[i])
            else:
                if len(current_table) >= 3:
                    tables.append((current_table[0][0], current_table))
                current_table = [ptrs[i]]
        if len(current_table) >= 3:
            tables.append((current_table[0][0], current_table))
        return tables

    # ── Known Data Tables ───────────────────────────────────

    def parse_known_tables(self):
        print(f"\n{'═' * 60}")
        print(f"  KNOWN DATA TABLES")
        print(f"{'═' * 60}")

        for name, info in KNOWN_TABLES.items():
            off = info["offset"]
            size = info["size"]
            entry_size = info["entry_size"]
            fmt = info["format"]
            labels = info.get("labels", [])
            n_entries = size // entry_size

            print(f"\n  {info['description']}")
            print(f"  @ 0x{off:06X}, {size} bytes, {n_entries} entries")
            print(f"  {'─' * 50}")

            for i in range(n_entries):
                entry_off = off + i * entry_size
                values = struct.unpack_from(fmt, self.data, entry_off)
                label = labels[i] if i < len(labels) else f"entry_{i}"
                raw = hexbytes(self.data[entry_off:entry_off + entry_size])

                if len(values) == 1:
                    val = values[0]
                    extra = ""
                    if FLASH_BASE <= val < FLASH_BASE + FIRMWARE_SIZE:
                        extra = f" → flash 0x{val - FLASH_BASE:06X}"
                    elif name == "menu_asset_table":
                        extra = f" (SFAT #{val})"
                    print(f"    [{i}] {label:<12s}  0x{val:08X}{extra}  ({raw})")
                elif len(values) == 2:
                    print(f"    [{i}] {label:<12s}  ({values[0]:3d}, {values[1]:3d})  ({raw})")

        # Menu label records
        print(f"\n  Menu label string records")
        print(f"  {'─' * 50}")
        for label, off in MENU_LABEL_OFFSETS.items():
            num_chars = struct.unpack_from("<H", self.data, off)[0]
            # Read a few bytes after for context
            ctx = hexbytes(self.data[off:off + 8])
            print(f"    {label:<12s} @ 0x{off:06X}: num_chars={num_chars}  ({ctx})")

    # ── Cross-References ────────────────────────────────────

    def find_xrefs(self, target, search_range=None):
        print(f"\n{'═' * 60}")
        print(f"  CROSS-REFERENCES TO 0x{target:06X}")
        print(f"{'═' * 60}")

        flash_addr = FLASH_BASE + target
        results = []

        start = search_range[0] if search_range else 0
        end = search_range[1] if search_range else self.size - 3

        # Search for the flash-mapped address (0x0200XXXX)
        target_bytes_flash = struct.pack("<I", flash_addr)
        # Search for the raw offset
        target_bytes_raw = struct.pack("<I", target)
        # Search for 16-bit offset (for near references)
        if target < 0x10000:
            target_bytes_16 = struct.pack("<H", target)
        else:
            target_bytes_16 = None

        for i in range(start, end - 3):
            if self.data[i:i+4] == target_bytes_flash:
                region = self.region_for(i)
                rname = region[2] if region else "?"
                ctx = hexbytes(self.data[max(0,i-4):i+8])
                results.append((i, "flash_addr", f"0x{flash_addr:08X}"))
                print(f"  0x{i:06X} [{rname:6s}]: contains 0x{flash_addr:08X} (flash-mapped)  ctx: {ctx}")

            if self.data[i:i+4] == target_bytes_raw and target != flash_addr:
                region = self.region_for(i)
                rname = region[2] if region else "?"
                ctx = hexbytes(self.data[max(0,i-4):i+8])
                results.append((i, "raw_offset", f"0x{target:08X}"))
                print(f"  0x{i:06X} [{rname:6s}]: contains 0x{target:08X} (raw offset)  ctx: {ctx}")

        if not results:
            print(f"  No references found to 0x{target:06X}")

        return results

    # ── Hex Dump ────────────────────────────────────────────

    def hexdump(self, offset, length=256, width=16):
        print(f"\n{'═' * 60}")
        print(f"  HEX DUMP @ 0x{offset:06X} ({length} bytes)")
        region = self.region_for(offset)
        if region:
            print(f"  Region: {region[3]} ({region[2]})")
        print(f"{'═' * 60}\n")

        end = min(offset + length, self.size)
        for row_start in range(offset, end, width):
            row_end = min(row_start + width, end)
            row_data = self.data[row_start:row_end]

            hex_part = " ".join(f"{b:02x}" for b in row_data)
            hex_part = hex_part.ljust(width * 3 - 1)

            ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in row_data)

            print(f"  {row_start:06X}  {hex_part}  |{ascii_part}|")

    # ── Diff ────────────────────────────────────────────────

    def diff(self, other_path):
        print(f"\n{'═' * 60}")
        print(f"  FIRMWARE DIFF")
        print(f"{'═' * 60}")

        other = bytearray(Path(other_path).read_bytes())
        other_md5 = hashlib.md5(other).hexdigest()

        print(f"  File A: {self.path.name}  ({len(self.data):,} bytes, MD5: {self.md5})")
        print(f"  File B: {Path(other_path).name}  ({len(other):,} bytes, MD5: {other_md5})")

        if len(self.data) != len(other):
            print(f"  WARNING: different sizes ({len(self.data)} vs {len(other)})")

        min_len = min(len(self.data), len(other))

        # Find changed regions
        changes = []
        in_change = False
        change_start = 0

        for i in range(min_len):
            if self.data[i] != other[i]:
                if not in_change:
                    change_start = i
                    in_change = True
            else:
                if in_change:
                    changes.append((change_start, i))
                    in_change = False
        if in_change:
            changes.append((change_start, min_len))

        if not changes:
            print(f"\n  Files are identical")
            return

        # Summarize by region
        region_stats = defaultdict(lambda: {"bytes": 0, "regions": 0})
        for start, end in changes:
            region = self.region_for(start)
            rname = region[2] if region else "unknown"
            region_stats[rname]["bytes"] += end - start
            region_stats[rname]["regions"] += 1

        total_changed = sum(s["bytes"] for s in region_stats.values())
        print(f"\n  Total: {total_changed:,} bytes changed across {len(changes)} regions")
        print(f"\n  By section:")
        for rname, stats in sorted(region_stats.items()):
            print(f"    [{rname:8s}]: {stats['bytes']:>8,} bytes in {stats['regions']:>5,} regions")

        # Show first N changes with context
        print(f"\n  First 20 changes:")
        for start, end in changes[:20]:
            length = end - start
            region = self.region_for(start)
            rname = region[2] if region else "?"
            a_bytes = hexbytes(self.data[start:end], 12)
            b_bytes = hexbytes(other[start:end], 12)
            print(f"    0x{start:06X}-0x{end-1:06X} ({length:>6,}B) [{rname}]")
            if length <= 12:
                print(f"      A: {a_bytes}")
                print(f"      B: {b_bytes}")

        if len(changes) > 20:
            print(f"    ... and {len(changes) - 20} more regions")

    # ── Entropy ─────────────────────────────────────────────

    def entropy(self, block_size=4096):
        print(f"\n{'═' * 60}")
        print(f"  BYTE ENTROPY ANALYSIS (block size: {block_size})")
        print(f"{'═' * 60}")

        print(f"\n  Region entropy summary:")
        for start, end, rtype, name in REGIONS:
            region_data = self.data[start:end + 1]
            ent = self._calc_entropy(region_data)
            bar = "█" * int(ent * 8) + "░" * (8 - int(ent * 8))
            size = end - start + 1
            print(f"    0x{start:06X}-0x{end:06X}  {bar} {ent:.3f}  {name} ({fmt_size(size)})")

        # Find interesting entropy transitions in code section
        print(f"\n  Code section entropy by {fmt_size(block_size)} blocks (look for data embedded in code):")
        code_start = 0x000200
        code_end = 0x0D3200
        prev_ent = None
        for off in range(code_start, code_end, block_size):
            block = self.data[off:off + block_size]
            ent = self._calc_entropy(block)
            marker = ""
            if prev_ent is not None:
                delta = ent - prev_ent
                if abs(delta) > 0.3:
                    marker = f"  ◄ entropy {'jump' if delta > 0 else 'drop'} ({delta:+.2f})"
            if ent < 4.0:
                marker += "  ◄ LOW (likely data/padding)"
            elif ent > 7.5:
                marker += "  ◄ HIGH (compressed/encrypted?)"

            bar = "█" * int(ent) + "░" * (8 - int(ent))
            print(f"    0x{off:06X}  {bar} {ent:.3f}{marker}")
            prev_ent = ent

    def _calc_entropy(self, data):
        if not data:
            return 0.0
        freq = defaultdict(int)
        for b in data:
            freq[b] += 1
        length = len(data)
        entropy = 0.0
        for count in freq.values():
            p = count / length
            if p > 0:
                entropy -= p * math.log2(p)
        return entropy

    # ── Coverage ────────────────────────────────────────────

    def coverage(self):
        print(f"\n{'═' * 60}")
        print(f"  COVERAGE ANALYSIS")
        print(f"{'═' * 60}")

        mapped = bytearray(self.size)  # 0 = unmapped, 1 = mapped

        # Mark known regions
        for start, end, rtype, name in REGIONS:
            if rtype in ("header", "config", "sfat", "free"):
                for i in range(start, min(end + 1, self.size)):
                    mapped[i] = 1

        # Mark asset data from SFAT
        base = 0x0D3200
        table_size = struct.unpack_from("<I", self.data, base)[0]
        num_entries = (table_size - 8) // 8
        for i in range(num_entries):
            off_pos = base + 8 + i * 8
            rel_offset, size = struct.unpack_from("<II", self.data, off_pos)
            abs_offset = base + rel_offset
            for j in range(abs_offset, min(abs_offset + size, self.size)):
                mapped[j] = 1

        # Mark known tables in code section
        for name, info in KNOWN_TABLES.items():
            for j in range(info["offset"], info["offset"] + info["size"]):
                mapped[j] = 1

        # Mark menu labels
        for label, off in MENU_LABEL_OFFSETS.items():
            for j in range(off, off + 8):
                mapped[j] = 1

        # Calculate stats
        total_mapped = sum(mapped)
        total_unmapped = self.size - total_mapped
        pct = (total_mapped / self.size) * 100

        print(f"\n  Total firmware:   {self.size:>10,} bytes ({fmt_size(self.size)})")
        print(f"  Mapped:           {total_mapped:>10,} bytes ({pct:.1f}%)")
        print(f"  Unmapped:         {total_unmapped:>10,} bytes ({100 - pct:.1f}%)")

        print(f"\n  By region:")
        for start, end, rtype, name in REGIONS:
            region_size = end - start + 1
            region_mapped = sum(mapped[start:end + 1])
            region_unmapped = region_size - region_mapped
            rpct = (region_mapped / region_size) * 100 if region_size > 0 else 0
            bar = "█" * int(rpct / 10) + "░" * (10 - int(rpct / 10))
            print(f"    0x{start:06X}-0x{end:06X}  {bar} {rpct:5.1f}%  {name}")
            if region_unmapped > 0 and rtype in ("code",):
                print(f"      → {region_unmapped:,} bytes unmapped (needs disassembly)")

        # Find largest unmapped chunks in code section
        print(f"\n  Largest unmapped regions in code section:")
        code_start = 0x000200
        code_end = 0x0D3200
        in_unmapped = False
        um_start = 0
        unmapped_regions = []
        for i in range(code_start, code_end):
            if mapped[i] == 0:
                if not in_unmapped:
                    um_start = i
                    in_unmapped = True
            else:
                if in_unmapped:
                    unmapped_regions.append((um_start, i - um_start))
                    in_unmapped = False
        if in_unmapped:
            unmapped_regions.append((um_start, code_end - um_start))

        unmapped_regions.sort(key=lambda x: -x[1])
        for off, size in unmapped_regions[:10]:
            print(f"    0x{off:06X}: {size:,} bytes ({fmt_size(size)})")

        return total_mapped, total_unmapped, pct

    # ── Memory Map ──────────────────────────────────────────

    def memory_map(self):
        print(f"\n{'═' * 60}")
        print(f"  MEMORY MAP — {self.path.name}")
        print(f"  {self.size:,} bytes ({fmt_size(self.size)}), MD5: {self.md5}")
        print(f"{'═' * 60}\n")

        for start, end, rtype, name in REGIONS:
            size = end - start + 1
            print(f"  0x{start:06X}-0x{end:06X}  {fmt_size(size):>10s}  [{rtype:6s}]  {name}")

        # Show known tables embedded in code
        print(f"\n  Known data tables in code section:")
        for tbl_name, info in KNOWN_TABLES.items():
            off = info["offset"]
            size = info["size"]
            print(f"    0x{off:06X} ({size:3d}B): {info['description']}")
        for label, off in MENU_LABEL_OFFSETS.items():
            print(f"    0x{off:06X} (  8B): Menu label: {label}")

    # ── Value Scanner ───────────────────────────────────────

    def find_value(self, value, size=4):
        """Find all occurrences of a specific value in the firmware."""
        print(f"\n{'═' * 60}")
        if size == 4:
            print(f"  SEARCHING FOR u32 VALUE 0x{value:08X} ({value})")
            target = struct.pack("<I", value)
        elif size == 2:
            print(f"  SEARCHING FOR u16 VALUE 0x{value:04X} ({value})")
            target = struct.pack("<H", value)
        elif size == 1:
            print(f"  SEARCHING FOR u8 VALUE 0x{value:02X} ({value})")
            target = bytes([value])
        print(f"{'═' * 60}")

        results = []
        for i in range(self.size - len(target) + 1):
            if self.data[i:i + len(target)] == target:
                region = self.region_for(i)
                rname = region[2] if region else "?"
                ctx = hexbytes(self.data[max(0, i-4):i + len(target) + 4])
                results.append((i, rname))
                if len(results) <= 50:
                    print(f"  0x{i:06X} [{rname:6s}]  ctx: {ctx}")

        if len(results) > 50:
            print(f"  ... and {len(results) - 50} more")
        print(f"\n  {len(results)} occurrences found")
        return results

    # ── Scan for embedded data in code section ──────────────

    def find_embedded_data(self):
        """Heuristic scan for non-code data embedded in the code section."""
        print(f"\n{'═' * 60}")
        print(f"  EMBEDDED DATA IN CODE SECTION")
        print(f"{'═' * 60}")

        code_start = 0x000400
        code_end = 0x0C8800
        block_size = 256

        regions = []
        for off in range(code_start, code_end, block_size):
            block = self.data[off:off + block_size]
            ent = self._calc_entropy(block)
            zeros = block.count(0)
            ffs = block.count(0xFF)

            is_data = False
            reason = ""

            if ent < 2.0:
                is_data = True
                reason = "very low entropy"
            elif zeros > 200:
                is_data = True
                reason = f"mostly zeros ({zeros}/256)"
            elif ffs > 200:
                is_data = True
                reason = f"mostly 0xFF ({ffs}/256)"

            if is_data:
                regions.append((off, block_size, ent, reason))

        # Merge adjacent regions
        merged = []
        for off, size, ent, reason in regions:
            if merged and off == merged[-1][0] + merged[-1][1]:
                merged[-1] = (merged[-1][0], merged[-1][1] + size, min(merged[-1][2], ent), reason)
            else:
                merged.append((off, size, ent, reason))

        if merged:
            print(f"\n  Found {len(merged)} regions that look like embedded data (not code):\n")
            for off, size, ent, reason in merged:
                preview = hexbytes(self.data[off:off + 16])
                print(f"    0x{off:06X}-0x{off + size - 1:06X} ({fmt_size(size)}): entropy={ent:.2f} — {reason}")
                print(f"      {preview} ...")
        else:
            print(f"\n  No obvious embedded data regions found")

        return merged

    # ── Full Scan ───────────────────────────────────────────

    def full_scan(self):
        print(f"\n{'╔' + '═' * 58 + '╗'}")
        print(f"{'║'} KC02 FIRMWARE ANALYSIS REPORT{' ' * 29}{'║'}")
        print(f"{'╚' + '═' * 58 + '╝'}")
        print(f"\n  Source:  {self.path}")
        print(f"  Size:    {self.size:,} bytes ({fmt_size(self.size)})")
        print(f"  MD5:     {self.md5}")
        print(f"  Chip:    JieLi AC2546 (pi32 core)")
        print(f"  Flash:   0x{FLASH_BASE:08X} base address")

        self.memory_map()
        self.parse_header()
        self.parse_sfat()
        self.parse_known_tables()
        strings = self.find_strings(min_len=4, sections=["code", "config", "header"])
        self.find_pointers()
        self.find_embedded_data()
        self.entropy()
        self.coverage()

        # Save machine-readable results
        self._save_scan_results(strings)

    def _save_scan_results(self, strings):
        out_path = PROJECT / "docs" / "firmware_map.json"
        results = {
            "firmware": {
                "file": str(self.path),
                "size": self.size,
                "md5": self.md5,
                "chip": "JieLi AC2546",
                "arch": "pi32",
                "flash_base": f"0x{FLASH_BASE:08X}",
            },
            "regions": [
                {
                    "start": f"0x{s:06X}",
                    "end": f"0x{e:06X}",
                    "size": e - s + 1,
                    "type": t,
                    "name": n,
                }
                for s, e, t, n in REGIONS
            ],
            "known_tables": {
                name: {
                    "offset": f"0x{info['offset']:06X}",
                    "size": info["size"],
                    "description": info["description"],
                }
                for name, info in KNOWN_TABLES.items()
            },
            "strings_in_code": [
                {"offset": f"0x{off:06X}", "text": text}
                for off, text, section in strings
                if section in ("code", "config")
            ],
        }
        out_path.write_text(json.dumps(results, indent=2) + "\n")
        print(f"\n  Scan results saved to {out_path}")


# ── CLI ─────────────────────────────────────────────────────

def find_dump():
    if "KC02_DUMP" in os.environ:
        return Path(os.environ["KC02_DUMP"])
    for p in [PROJECT / "firmware" / "original.bin", Path.home() / "dump1.bin"]:
        if p.exists():
            return p
    return None


def main():
    parser = argparse.ArgumentParser(description="KC02 Firmware Analyzer")
    parser.add_argument("--bin", default=None, help="Path to firmware binary")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("scan", help="Full analysis report")
    sub.add_parser("map", help="Memory map")
    sub.add_parser("header", help="Parse BLDR header")
    sub.add_parser("sfat", help="Parse SFAT asset table")
    sub.add_parser("tables", help="Parse known data tables")
    sub.add_parser("entropy", help="Byte entropy analysis")

    str_p = sub.add_parser("strings", help="Find embedded strings")
    str_p.add_argument("--min", type=int, default=4, help="Minimum string length")
    str_p.add_argument("--section", default=None, help="Filter by section (code/config/assets)")

    ptr_p = sub.add_parser("pointers", help="Find code/data pointers")

    hex_p = sub.add_parser("hexdump", help="Hex dump at offset")
    hex_p.add_argument("offset", help="Start offset (hex, e.g. 0x07DF08)")
    hex_p.add_argument("length", nargs="?", type=int, default=256, help="Bytes to show")

    xref_p = sub.add_parser("xrefs", help="Find references to address")
    xref_p.add_argument("address", help="Target address (hex)")

    diff_p = sub.add_parser("diff", help="Compare two firmware binaries")
    diff_p.add_argument("file_a", help="First firmware file")
    diff_p.add_argument("file_b", help="Second firmware file")

    val_p = sub.add_parser("find", help="Find a value in firmware")
    val_p.add_argument("value", help="Value to search (hex)")
    val_p.add_argument("--size", type=int, default=4, choices=[1, 2, 4], help="Value size in bytes")

    cov_p = sub.add_parser("coverage", help="Coverage analysis")
    sub.add_parser("embedded", help="Find embedded data in code section")

    args = parser.parse_args()

    if args.cmd == "diff":
        fw = FirmwareAnalyzer(args.file_a)
        fw.diff(args.file_b)
        return

    bin_path = args.bin
    if not bin_path:
        bin_path = find_dump()
    if not bin_path or not Path(bin_path).exists():
        print("ERROR: No firmware binary found.")
        print("  Place dump at firmware/original.bin, set KC02_DUMP, or use --bin")
        sys.exit(1)

    fw = FirmwareAnalyzer(bin_path)

    if args.cmd == "scan":
        fw.full_scan()
    elif args.cmd == "map":
        fw.memory_map()
    elif args.cmd == "header":
        fw.parse_header()
    elif args.cmd == "sfat":
        fw.parse_sfat()
    elif args.cmd == "tables":
        fw.parse_known_tables()
    elif args.cmd == "strings":
        sections = [args.section] if args.section else None
        fw.find_strings(min_len=args.min, sections=sections)
    elif args.cmd == "pointers":
        fw.find_pointers()
    elif args.cmd == "hexdump":
        offset = int(args.offset, 16) if args.offset.startswith("0x") else int(args.offset)
        fw.hexdump(offset, args.length)
    elif args.cmd == "xrefs":
        addr = int(args.address, 16) if args.address.startswith("0x") else int(args.address)
        fw.find_xrefs(addr)
    elif args.cmd == "find":
        val = int(args.value, 16) if args.value.startswith("0x") else int(args.value)
        fw.find_value(val, args.size)
    elif args.cmd == "coverage":
        fw.coverage()
    elif args.cmd == "embedded":
        fw.find_embedded_data()
    else:
        parser.print_help()
        print("\nQuick start:")
        print("  python3 analyze.py scan              # full report")
        print("  python3 analyze.py strings            # find strings")
        print("  python3 analyze.py hexdump 0x07DF08   # hex dump")
        print("  python3 analyze.py xrefs 0x07E160     # cross-references")
        print("  python3 analyze.py diff a.bin b.bin    # compare binaries")


if __name__ == "__main__":
    main()
