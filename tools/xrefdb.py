#!/usr/bin/env python3
"""
KC02 Cross-Reference Database
==============================
Builds a full cross-reference graph for the firmware binary.
Answers: "What references this address?" and "Is this safe to modify?"

Usage:
    python3 xrefdb.py build                  # Build/rebuild database
    python3 xrefdb.py query 0x07E160         # What references this address?
    python3 xrefdb.py safe 0x07E160          # Safety assessment for modification
    python3 xrefdb.py function 0x003E74      # Function details + callers/callees
    python3 xrefdb.py deps 0x07E160          # Reverse dependency chain
    python3 xrefdb.py imm 6                  # Find all uses of immediate value (live)
    python3 xrefdb.py stats                  # Database statistics
"""

import struct
import sys
import os
import json
import hashlib
import time
import bisect
import argparse
from pathlib import Path
from collections import defaultdict

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))

from pi32_disasm import (
    Pi32Decoder, KNOWN_FUNCTIONS, FLASH_BASE,
    CODE_START, APP_START, CODE_END, DATA_IN_CODE,
)
from analyze import REGIONS, KNOWN_TABLES, MENU_LABEL_OFFSETS, FIRMWARE_SIZE

DB_PATH = PROJECT / "docs" / "xref_db.json"


def find_dump():
    if "KC02_DUMP" in os.environ:
        return Path(os.environ["KC02_DUMP"])
    for p in [PROJECT / "firmware" / "original.bin", Path.home() / "dump1.bin"]:
        if p.exists():
            return p
    return None


class XRefBuilder:
    def __init__(self, data):
        self.data = data
        self.decoder = Pi32Decoder()
        self.size = len(data)
        self.md5 = hashlib.md5(data).hexdigest()

        self.all_instructions = {}
        self.functions = {}
        self.call_graph_out = defaultdict(set)
        self.call_graph_in = defaultdict(set)
        self.pointer_refs = defaultdict(list)
        self.strings = {}
        self._func_entries = []

    def in_data_region(self, offset):
        for start, end in DATA_IN_CODE:
            if start <= offset < end:
                return True
        return False

    def region_for(self, offset):
        for start, end, rtype, name in REGIONS:
            if start <= offset <= end:
                return rtype
        return "unknown"

    def build(self):
        print("Building cross-reference database...")
        print(f"  Firmware: {self.size:,} bytes, MD5: {self.md5}")
        t0 = time.time()

        self._disassemble_all()
        t1 = time.time()
        print(f"  [1/5] Disassembly: {t1-t0:.1f}s — {len(self.all_instructions):,} instructions")

        self._discover_functions()
        t2 = time.time()
        print(f"  [2/5] Functions: {t2-t1:.1f}s — {len(self.functions):,} functions")

        self._build_call_graph()
        t3 = time.time()
        total_edges = sum(len(v) for v in self.call_graph_out.values())
        print(f"  [3/5] Call graph: {t3-t2:.1f}s — {total_edges:,} call edges")

        self._scan_data_pointers()
        t4 = time.time()
        total_ptrs = sum(len(v) for v in self.pointer_refs.values())
        print(f"  [4/5] Data pointers: {t4-t3:.1f}s — {total_ptrs:,} pointer refs")

        self._scan_strings()
        t5 = time.time()
        print(f"  [5/5] Strings: {t5-t4:.1f}s — {len(self.strings):,} strings")

        print(f"  Total: {t5-t0:.1f}s")

    def _disassemble_all(self):
        offset = CODE_START
        while offset < CODE_END:
            if self.in_data_region(offset):
                offset += 2
                continue
            inst = self.decoder.decode(self.data, offset)
            if inst is None:
                offset += 2
                continue
            self.all_instructions[offset] = inst
            offset += inst.size

    def _discover_functions(self):
        for off, name in KNOWN_FUNCTIONS.items():
            self.functions[off] = {"name": name, "source": "known"}

        for off, inst in self.all_instructions.items():
            if inst.is_call and inst.target is not None:
                target = inst.target - FLASH_BASE
                if CODE_START <= target < CODE_END and not self.in_data_region(target):
                    if target not in self.functions:
                        self.functions[target] = {"name": f"sub_{target:06X}", "source": "call_target"}

        for offset in range(CODE_START, CODE_END, 2):
            if self.in_data_region(offset):
                continue
            if offset + 2 > self.size:
                break
            w = struct.unpack_from('<H', self.data, offset)[0]
            if (w >> 4) & 0x1FF == 0x021:
                if offset not in self.functions:
                    self.functions[offset] = {"name": f"sub_{offset:06X}", "source": "prologue"}

        # Compute sizes from sorted entries
        self._func_entries = sorted(self.functions.keys())
        for i, entry in enumerate(self._func_entries):
            if i + 1 < len(self._func_entries):
                next_entry = self._func_entries[i + 1]
                max_end = next_entry
                for ds, de in DATA_IN_CODE:
                    if entry < ds < max_end:
                        max_end = ds
                        break
                self.functions[entry]["size"] = max_end - entry
            else:
                self.functions[entry]["size"] = min(CODE_END - entry, 4096)

            end = entry + self.functions[entry]["size"]
            self.functions[entry]["insn_count"] = sum(
                1 for o in self.all_instructions if entry <= o < end
            )

    def _containing_function(self, offset):
        idx = bisect.bisect_right(self._func_entries, offset) - 1
        if idx >= 0:
            entry = self._func_entries[idx]
            if offset < entry + self.functions[entry]["size"]:
                return entry
        return None

    def _build_call_graph(self):
        for off, inst in self.all_instructions.items():
            if inst.is_call and inst.target is not None:
                target = inst.target - FLASH_BASE
                if target in self.functions:
                    caller = self._containing_function(off)
                    if caller is not None:
                        self.call_graph_out[caller].add(target)
                        self.call_graph_in[target].add(caller)

    def _scan_data_pointers(self):
        for ds, de in DATA_IN_CODE:
            for i in range(ds, min(de, self.size - 3), 4):
                val = struct.unpack_from("<I", self.data, i)[0]
                if FLASH_BASE <= val < FLASH_BASE + FIRMWARE_SIZE:
                    target = val - FLASH_BASE
                    self.pointer_refs[target].append(i)

        for rstart, rend, rtype, _ in REGIONS:
            if rtype in ("config", "sfat"):
                for i in range(rstart, min(rend + 1, self.size - 3), 4):
                    val = struct.unpack_from("<I", self.data, i)[0]
                    if FLASH_BASE <= val < FLASH_BASE + FIRMWARE_SIZE:
                        target = val - FLASH_BASE
                        self.pointer_refs[target].append(i)

    def _scan_strings(self):
        current = []
        start = 0
        for i in range(self.size):
            b = self.data[i]
            if 0x20 <= b <= 0x7E:
                if not current:
                    start = i
                current.append(chr(b))
            else:
                if len(current) >= 4:
                    region = self.region_for(start)
                    if region in ("code", "config"):
                        self.strings[start] = "".join(current)
                current = []

    def save(self):
        db = {
            "meta": {
                "firmware_md5": self.md5,
                "firmware_size": self.size,
                "built": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "stats": {
                    "total_instructions": len(self.all_instructions),
                    "decoded_pct": round(
                        100 * sum(1 for i in self.all_instructions.values() if i.decoded)
                        / max(1, len(self.all_instructions)), 1
                    ),
                    "total_functions": len(self.functions),
                    "known_functions": sum(1 for f in self.functions.values() if f["source"] == "known"),
                    "discovered_functions": sum(1 for f in self.functions.values() if f["source"] != "known"),
                    "call_edges": sum(len(v) for v in self.call_graph_out.values()),
                    "pointer_targets": len(self.pointer_refs),
                    "total_strings": len(self.strings),
                },
            },
            "functions": {
                f"0x{off:06X}": {
                    "name": f["name"],
                    "size": f["size"],
                    "insn_count": f["insn_count"],
                    "source": f["source"],
                    "callers": sorted(f"0x{c:06X}" for c in self.call_graph_in.get(off, set())),
                    "callees": sorted(f"0x{c:06X}" for c in self.call_graph_out.get(off, set())),
                }
                for off, f in sorted(self.functions.items())
            },
            "pointer_refs": {
                f"0x{target:06X}": {
                    "label": self._label_for(target),
                    "region": self.region_for(target),
                    "sources": [f"0x{s:06X}" for s in sorted(sources)],
                }
                for target, sources in sorted(self.pointer_refs.items())
                if sources
            },
            "strings": {
                f"0x{off:06X}": text
                for off, text in sorted(self.strings.items())
            },
            "known_tables": {
                name: {
                    "offset": f"0x{info['offset']:06X}",
                    "size": info["size"],
                    "description": info["description"],
                    "pointer_refs_to": len(self.pointer_refs.get(info["offset"], [])),
                    "call_refs_to": len(self.call_graph_in.get(info["offset"], set())),
                }
                for name, info in KNOWN_TABLES.items()
            },
        }

        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        DB_PATH.write_text(json.dumps(db, indent=2) + "\n")
        print(f"\n  Saved: {DB_PATH}")
        print(f"  Size:  {DB_PATH.stat().st_size:,} bytes")


    def _label_for(self, offset):
        if offset in self.functions:
            return self.functions[offset]["name"]
        for name, info in KNOWN_TABLES.items():
            if info["offset"] == offset:
                return name
        for label, off in MENU_LABEL_OFFSETS.items():
            if off == offset:
                return f"label_{label}"
        if offset in self.strings:
            return f'"{self.strings[offset][:30]}"'
        return None


class XRefQuery:
    def __init__(self):
        if not DB_PATH.exists():
            print(f"ERROR: Database not found at {DB_PATH}")
            print("  Run: python3 xrefdb.py build")
            sys.exit(1)
        self.db = json.loads(DB_PATH.read_text())

    def query(self, offset):
        key = f"0x{offset:06X}"
        found = False

        if key in self.db["functions"]:
            found = True
            func = self.db["functions"][key]
            print(f"\n  FUNCTION: {func['name']}")
            print(f"  Flash addr: 0x{FLASH_BASE + offset:08X}")
            print(f"  Size: {func['size']}B, {func['insn_count']} instructions ({func['source']})")
            print(f"\n  Callers ({len(func['callers'])}):")
            for c in func["callers"]:
                cname = self.db["functions"].get(c, {}).get("name", c)
                print(f"    <- {cname} ({c})")
            if not func["callers"]:
                print(f"    (none — entry point or table-dispatched)")
            print(f"\n  Callees ({len(func['callees'])}):")
            for c in func["callees"]:
                cname = self.db["functions"].get(c, {}).get("name", c)
                print(f"    -> {cname} ({c})")
            if not func["callees"]:
                print(f"    (leaf function)")

        if key in self.db["pointer_refs"]:
            found = True
            entry = self.db["pointer_refs"][key]
            print(f"\n  POINTER REFERENCES TO: {key}")
            if entry.get("label"):
                print(f"  Label:  {entry['label']}")
            print(f"  Region: {entry['region']}")
            print(f"  Referenced from {len(entry['sources'])} data locations:")
            for src in entry["sources"][:30]:
                src_off = int(src, 16)
                src_region = "unknown"
                for s, e, rt, _ in REGIONS:
                    if s <= src_off <= e:
                        src_region = rt
                        break
                in_data = any(ds <= src_off < de for ds, de in DATA_IN_CODE)
                ctx = f"[data-in-code]" if in_data else f"[{src_region}]"
                print(f"    {src} {ctx}")
            if len(entry["sources"]) > 30:
                print(f"    ... and {len(entry['sources']) - 30} more")

        # Check if address is referenced as a call target (even if not a function entry)
        # by scanning all function callees
        call_refs = []
        for fkey, func in self.db["functions"].items():
            if key in func["callees"]:
                call_refs.append((fkey, func["name"]))
        if call_refs and key not in self.db["functions"]:
            found = True
            print(f"\n  CALL TARGET: {key}")
            print(f"  Called from:")
            for fkey, fname in call_refs:
                print(f"    <- {fname} ({fkey})")

        if key in self.db["strings"]:
            found = True
            print(f"\n  STRING @ {key}: \"{self.db['strings'][key]}\"")

        for name, info in self.db["known_tables"].items():
            if info["offset"] == key:
                found = True
                print(f"\n  KNOWN TABLE: {name}")
                print(f"  {info['description']}")
                print(f"  Size: {info['size']} bytes")
                print(f"  Pointer refs: {info['pointer_refs_to']}, Call refs: {info['call_refs_to']}")

        if not found:
            print(f"\n  No references found for {key}")
            region = "unknown"
            for s, e, rt, rn in REGIONS:
                if s <= offset <= e:
                    region = f"{rn} ({rt})"
                    break
            print(f"  Region: {region}")

    def safe(self, offset):
        key = f"0x{offset:06X}"
        print(f"\n{'=' * 60}")
        print(f"  SAFETY ASSESSMENT: {key}")
        print(f"{'=' * 60}")

        region = "unknown"
        region_name = "unknown"
        for s, e, rt, rn in REGIONS:
            if s <= offset <= e:
                region = rt
                region_name = rn
                break

        print(f"\n  Region: {region_name} ({region})")

        # Count all references
        ref_count = 0
        ref_detail = []

        if key in self.db["functions"]:
            func = self.db["functions"][key]
            n = len(func["callers"])
            ref_count += n
            ref_detail.append(f"{n} function callers")

        if key in self.db["pointer_refs"]:
            n = len(self.db["pointer_refs"][key]["sources"])
            ref_count += n
            ref_detail.append(f"{n} data pointer refs")

        # Is it inside a known table?
        in_table = None
        for name, info in self.db["known_tables"].items():
            toff = int(info["offset"], 16)
            if toff <= offset < toff + info["size"]:
                in_table = name
                break

        print(f"  References: {ref_count} total")
        for d in ref_detail:
            print(f"    {d}")
        if in_table:
            print(f"  Inside known table: {in_table}")

        # Verdict
        print(f"\n  Verdict:")
        if region == "assets":
            print(f"  [SAFE] Asset data — use build.py for replacement")
        elif region == "free":
            print(f"  [SAFE] Erased flash — no active data")
        elif in_table and ref_count <= 10:
            print(f"  [MODERATE] Known data table, format documented")
            print(f"  Modify if you preserve the expected format/value range")
        elif region == "code" and key in self.db["functions"]:
            func = self.db["functions"][key]
            print(f"  [DANGEROUS] Function entry point: {func['name']}")
            print(f"  Called by {len(func['callers'])} functions")
            print(f"  DO NOT patch code bytes without disassembly confirmation")
        elif region == "code":
            print(f"  [DANGEROUS] Code region")
            if ref_count > 0:
                print(f"  {ref_count} things reference this address")
            print(f"  See 0x002F6C incident: code values may be shared")
        elif region in ("header", "config", "sfat"):
            print(f"  [CRITICAL] Firmware infrastructure ({region})")
            print(f"  Changes may prevent boot")
        else:
            print(f"  [UNKNOWN] No specific info — investigate before modifying")

    def function(self, offset):
        key = f"0x{offset:06X}"
        if key not in self.db["functions"]:
            nearest = max(
                (int(k, 16) for k in self.db["functions"] if int(k, 16) <= offset),
                default=None
            )
            if nearest:
                key = f"0x{nearest:06X}"
                print(f"  (nearest function: {key})")
            else:
                print(f"  No function at or before {key}")
                return

        func = self.db["functions"][key]
        print(f"\n  {func['name']}  @  {key}  (0x{FLASH_BASE + int(key,16):08X})")
        print(f"  Size: {func['size']}B | Instructions: {func['insn_count']} | Source: {func['source']}")

        print(f"\n  Callers ({len(func['callers'])}):")
        for c in func["callers"]:
            cname = self.db["functions"].get(c, {}).get("name", c)
            print(f"    <- {cname}")
        if not func["callers"]:
            print(f"    (root / table-dispatched)")

        print(f"\n  Callees ({len(func['callees'])}):")
        for c in func["callees"]:
            cname = self.db["functions"].get(c, {}).get("name", c)
            csize = self.db["functions"].get(c, {}).get("size", "?")
            print(f"    -> {cname} ({csize}B)")
        if not func["callees"]:
            print(f"    (leaf)")

    def deps(self, offset):
        key = f"0x{offset:06X}"
        print(f"\n  REVERSE DEPENDENCIES: what uses {key}?")
        print(f"  (Showing what breaks if you modify this address)\n")

        visited = set()
        self._deps_recurse(offset, 0, visited, max_depth=4)

    def _deps_recurse(self, offset, depth, visited, max_depth):
        key = f"0x{offset:06X}"
        if key in visited or depth > max_depth:
            return
        visited.add(key)

        indent = "  " + "  " * depth
        label = ""
        if key in self.db["functions"]:
            label = self.db["functions"][key]["name"]
        elif key in self.db["pointer_refs"] and self.db["pointer_refs"][key].get("label"):
            label = self.db["pointer_refs"][key]["label"]
        for name, info in self.db["known_tables"].items():
            if info["offset"] == key:
                label = name
        print(f"{indent}{key}  {label}")

        # Who calls this function?
        if key in self.db["functions"]:
            for caller in self.db["functions"][key]["callers"]:
                caller_off = int(caller, 16)
                cname = self.db["functions"].get(caller, {}).get("name", "")
                if caller not in visited:
                    print(f"{indent}  <- {caller} {cname}")
                    self._deps_recurse(caller_off, depth + 1, visited, max_depth)

        # What data locations point here?
        if key in self.db["pointer_refs"]:
            for src in self.db["pointer_refs"][key]["sources"][:10]:
                src_off = int(src, 16)
                if f"0x{src_off:06X}" not in visited:
                    # Find what function/table contains this source
                    container = None
                    for name, info in self.db["known_tables"].items():
                        toff = int(info["offset"], 16)
                        if toff <= src_off < toff + info["size"]:
                            container = name
                            break
                    ctx = f"(in {container})" if container else "(in data)"
                    print(f"{indent}  <- ptr @ {src} {ctx}")

    def imm(self, value):
        print(f"\n  Scanning firmware for immediate value {value} (0x{value:X})...")
        dump = find_dump()
        if not dump:
            print("  ERROR: firmware binary not found")
            return
        data = dump.read_bytes()
        decoder = Pi32Decoder()

        hits = []
        offset = CODE_START
        while offset < CODE_END:
            skip = False
            for ds, de in DATA_IN_CODE:
                if ds <= offset < de:
                    offset = de
                    skip = True
                    break
            if skip:
                continue
            inst = decoder.decode(data, offset)
            if inst is None:
                offset += 2
                continue
            if inst.decoded and inst.mnemonic in ('movs', 'movz', 'movl', 'movh', 'cmp', 'add', 'sub'):
                for part in inst.operands.split(','):
                    part = part.strip()
                    try:
                        if part.startswith('0x'):
                            v = int(part, 16)
                        elif part.lstrip('-').isdigit():
                            v = int(part)
                        else:
                            continue
                        if v == value:
                            func = self._find_func_for(offset)
                            hits.append((offset, inst, func))
                    except ValueError:
                        pass
            offset += inst.size

        print(f"  Found {len(hits)} instruction(s) using value {value}:\n")
        for off, inst, func_name in hits:
            fn = f"  in {func_name}" if func_name else ""
            print(f"    0x{off:06X}: {inst.mnemonic} {inst.operands}{fn}")

        if len(hits) > 1:
            print(f"\n  WARNING: Value {value} appears in {len(hits)} locations.")
            print(f"  Changing it in one place may not change it everywhere.")
            print(f"  Verify each location before patching (see 0x002F6C incident).")
        elif len(hits) == 1:
            print(f"\n  Value appears in only 1 location — lower risk of shared-value issues.")

    def _find_func_for(self, offset):
        entries = sorted(int(k, 16) for k in self.db["functions"])
        idx = bisect.bisect_right(entries, offset) - 1
        if idx >= 0:
            entry = entries[idx]
            func = self.db["functions"][f"0x{entry:06X}"]
            if offset < entry + func["size"]:
                return func["name"]
        return None

    def stats(self):
        meta = self.db["meta"]
        s = meta["stats"]
        print(f"\n{'=' * 60}")
        print(f"  XREF DATABASE")
        print(f"{'=' * 60}")
        print(f"  Firmware:     {meta['firmware_md5']}")
        print(f"  Built:        {meta['built']}")
        print(f"  DB file:      {DB_PATH.stat().st_size:,} bytes")
        print(f"\n  Instructions: {s['total_instructions']:,}  ({s['decoded_pct']}% decoded)")
        print(f"  Functions:    {s['total_functions']:,}  ({s['known_functions']} known, {s['discovered_functions']} discovered)")
        print(f"  Call edges:   {s['call_edges']:,}")
        print(f"  Ptr targets:  {s['pointer_targets']:,}")
        print(f"  Strings:      {s['total_strings']:,}")

        # Most-called functions
        funcs = [(k, v) for k, v in self.db["functions"].items()]
        by_callers = sorted(funcs, key=lambda x: len(x[1]["callers"]), reverse=True)
        print(f"\n  Top 10 most-called functions:")
        for key, func in by_callers[:10]:
            print(f"    {key}  {len(func['callers']):>3} callers  {func['name']}")

        # Most-referenced pointer targets
        ptrs = [(k, v) for k, v in self.db["pointer_refs"].items()]
        by_refs = sorted(ptrs, key=lambda x: len(x[1]["sources"]), reverse=True)
        print(f"\n  Top 10 most-referenced data addresses:")
        for key, entry in by_refs[:10]:
            label = entry.get("label") or ""
            print(f"    {key}  {len(entry['sources']):>3} ptr refs  {label}")

        # Orphan functions
        orphans = sum(1 for f in self.db["functions"].values() if not f["callers"])
        print(f"\n  Functions with no callers: {orphans}")
        print(f"  (entry points, ISRs, or dispatched via table)")


def main():
    parser = argparse.ArgumentParser(description="KC02 Cross-Reference Database")
    parser.add_argument("--bin", default=None, help="Path to firmware binary")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("build", help="Build/rebuild xref database")
    q = sub.add_parser("query", help="Query all references to an address")
    q.add_argument("address", help="Target address (hex, e.g. 0x07E160)")
    s = sub.add_parser("safe", help="Safety assessment for modification")
    s.add_argument("address", help="Target address (hex)")
    f = sub.add_parser("function", help="Function details + call graph")
    f.add_argument("address", help="Function address (hex)")
    d = sub.add_parser("deps", help="Reverse dependency chain")
    d.add_argument("address", help="Target address (hex)")
    i = sub.add_parser("imm", help="Find all uses of an immediate value (live scan)")
    i.add_argument("value", help="Value to search (decimal or 0xHEX)")
    sub.add_parser("stats", help="Database statistics")

    args = parser.parse_args()

    if args.cmd == "build":
        bin_path = args.bin or find_dump()
        if not bin_path or not Path(bin_path).exists():
            print("ERROR: No firmware binary found.")
            print("  Place dump at firmware/original.bin, set KC02_DUMP, or use --bin")
            sys.exit(1)
        builder = XRefBuilder(Path(bin_path).read_bytes())
        builder.build()
        builder.save()

    elif args.cmd == "stats":
        XRefQuery().stats()

    elif args.cmd == "imm":
        val_str = args.value
        val = int(val_str, 16) if val_str.startswith("0x") else int(val_str)
        XRefQuery().imm(val)

    elif args.cmd in ("query", "safe", "function", "deps"):
        addr_str = args.address
        addr = int(addr_str, 16) if addr_str.startswith("0x") else int(addr_str)
        if addr >= FLASH_BASE:
            addr -= FLASH_BASE
        getattr(XRefQuery(), args.cmd)(addr)

    else:
        parser.print_help()
        print("\nQuick start:")
        print("  python3 xrefdb.py build              # build database (~15s)")
        print("  python3 xrefdb.py query 0x07E160     # what references this?")
        print("  python3 xrefdb.py safe 0x07E160      # safe to modify?")
        print("  python3 xrefdb.py function 0x003E74  # function details")
        print("  python3 xrefdb.py imm 6              # find uses of value 6")
        print("  python3 xrefdb.py stats              # overview")


if __name__ == "__main__":
    main()
