#!/usr/bin/env python3
"""Extract all assets from a KC02 firmware dump using the SFAT table."""

import struct
import sys
import os
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
TABLE_BASE = 0x0D3200

def find_dump():
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    if "KC02_DUMP" in os.environ:
        return Path(os.environ["KC02_DUMP"])
    p = PROJECT / "firmware" / "original.bin"
    if p.exists():
        return p
    print("Usage: extract_assets.py [dump.bin]")
    print("")
    print("  Dump your firmware first:")
    print("    flashrom -p ch341a_spi -r firmware/original.bin")
    print("")
    print("  Or set KC02_DUMP env var, or pass the path as an argument.")
    sys.exit(1)

dump_path = find_dump()
out_dir = PROJECT / "originals"

with open(dump_path, "rb") as f:
    data = f.read()

table_size = struct.unpack_from("<I", data, TABLE_BASE)[0]
num_entries = (table_size - 8) // 8
print(f"Asset table at 0x{TABLE_BASE:06X}, size={table_size}, entries={num_entries}")

entries = []
for i in range(num_entries):
    off_pos = TABLE_BASE + 8 + i * 8
    rel_offset, size = struct.unpack_from("<II", data, off_pos)
    abs_offset = TABLE_BASE + rel_offset
    entries.append((i, abs_offset, size))

for subdir in ["jpeg", "bmp", "wav", "raw"]:
    (out_dir / subdir).mkdir(parents=True, exist_ok=True)

manifest = []
for idx, abs_offset, size in entries:
    chunk = data[abs_offset:abs_offset + size]
    if chunk[:2] == b'\xff\xd8':
        ext, subdir = "jpg", "jpeg"
    elif chunk[:2] == b'BM':
        ext, subdir = "bmp", "bmp"
    elif chunk[:4] == b'RIFF':
        ext, subdir = "wav", "wav"
    else:
        ext, subdir = "bin", "raw"

    filename = f"{idx:03d}_0x{abs_offset:06X}.{ext}"
    (out_dir / subdir / filename).write_bytes(chunk)
    manifest.append(f"{idx:3d}  0x{abs_offset:06X}  {size:8d}  {ext:4s}  {filename}")

print(f"\nExtracted {len(entries)} assets to {out_dir}\n")
print(f"{'#':>3s}  {'Offset':>10s}  {'Size':>8s}  {'Type':>4s}  {'File'}")
print("-" * 70)
for line in manifest:
    print(line)
