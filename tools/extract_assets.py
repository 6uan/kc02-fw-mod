import struct
import sys
import os

DUMP = "/Users/mac/dump1.bin"
TABLE_BASE = 0x0D3200
OUT_DIR = "/Users/mac/Developer/kc02-fw-mod/assets"

with open(DUMP, "rb") as f:
    data = f.read()

# Read table header
table_size = struct.unpack_from("<I", data, TABLE_BASE)[0]
num_entries = (table_size - 8) // 8
print(f"Asset table at 0x{TABLE_BASE:06X}, size={table_size}, entries={num_entries}")

entries = []
for i in range(num_entries):
    off_pos = TABLE_BASE + 8 + i * 8
    rel_offset, size = struct.unpack_from("<II", data, off_pos)
    abs_offset = TABLE_BASE + rel_offset
    entries.append((i, abs_offset, size))

manifest = []
for idx, abs_offset, size in entries:
    chunk = data[abs_offset:abs_offset + size]
    
    # Detect format
    if chunk[:2] == b'\xff\xd8':
        ext = "jpg"
        subdir = "jpeg"
    elif chunk[:2] == b'BM':
        ext = "bmp"
        subdir = "bmp"
    elif chunk[:4] == b'RIFF':
        ext = "wav"
        subdir = "wav"
    else:
        ext = "bin"
        subdir = "raw"
    
    filename = f"{idx:03d}_0x{abs_offset:06X}.{ext}"
    outpath = os.path.join(OUT_DIR, subdir, filename)
    with open(outpath, "wb") as out:
        out.write(chunk)
    
    manifest.append(f"{idx:3d}  0x{abs_offset:06X}  {size:8d}  {ext:4s}  {filename}")
    
print(f"\nExtracted {len(entries)} assets\n")
print(f"{'#':>3s}  {'Offset':>10s}  {'Size':>8s}  {'Type':>4s}  {'File'}")
print("-" * 70)
for line in manifest:
    print(line)
