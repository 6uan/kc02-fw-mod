#!/usr/bin/env python3
"""
Ghidra Import Helper for KC02 Firmware
=======================================
Creates a Ghidra headless import script that sets up the correct
memory map, labels known functions/tables, and defines strings.

Usage:
    # First, import the raw binary into Ghidra:
    ghidra /path/to/project KC02 -import ~/dump1.bin \
        -processor "JieLi:LE:32:default:pi32" \
        -baseAddr 0x02000000 \
        -postScript ghidra_label.py

    # Or just open Ghidra GUI:
    ghidra
    # Then: File > Import > dump1.bin
    #   Language: JieLi pi32 (little endian)
    #   Base Address: 0x02000000
    # Then: Run ghidra_label.py via Script Manager
"""

import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SCRIPT_OUT = PROJECT / "tools" / "ghidra_label.py"

LABELS = {
    # Menu handler functions
    0x0200411C: "handler_camera",
    0x02003E74: "handler_video",
    0x02003EF0: "handler_music",
    0x02003F90: "handler_playback",
    0x02004014: "handler_games",
    0x02004098: "handler_settings",
    0x02006DD4: "handler_null_default",
    0x02006D58: "handler_state_06",
    0x02006CCC: "handler_state_12",
    0x02006D00: "handler_state_13",
    0x02006CE0: "handler_state_14",
    0x02006CBC: "handler_state_17",
    0x02006D10: "handler_state_19",
    0x02006CF8: "handler_state_20",
    0x02006D60: "handler_state_21",
    0x02006D6C: "handler_state_22",
    0x02006DC0: "handler_state_27",
    0x02006D78: "handler_state_32",
    0x02006D90: "handler_state_33",
    0x02006DA8: "handler_state_34",

    # Init callbacks (from pointer table at 0x07E000)
    0x020093AC: "init_callback_0",
    0x020093B4: "init_callback_1",
    0x020093BC: "init_callback_2",
    0x020093C4: "init_callback_3",
    0x020093CC: "init_callback_4",
    0x020093D4: "init_callback_5",
    0x020093DC: "init_callback_6",
    0x020093E4: "init_callback_7",
}

DATA_LABELS = {
    # Data tables
    0x0207DF08: ("ui_state_dispatch_table", "35 x u32 function pointers"),
    0x0207E000: ("init_callback_table", "10 x u32 function pointers"),
    0x0207E160: ("menu_coord_table", "6 x (u16 x, u16 y)"),
    0x0207E178: ("menu_asset_index_table", "6 x u32 SFAT indices"),
    0x0207E19C: ("mode_callback_table", "5 x u32 function pointers"),
    0x020D3200: ("sfat_table", "97 x 8-byte asset entries"),

    # String constants
    0x0207DE1C: ("str_battery", None),
    0x0207DE28: ("str_ad_key", None),
    0x0207DE2F: ("str_sd_card", None),
    0x0207DE42: ("str_cmos_sensor", None),
    0x0207DE52: ("str_video_path", None),
    0x0207DE59: ("str_photo_path", None),
    0x0207DE60: ("str_audio_path", None),
    0x0207DE87: ("str_destbin", None),
    0x0207DE93: ("str_upgrade_failed", None),
    0x0207DFE5: ("str_selftest_bin", None),
    0x0207DF94: ("str_exmend_bin", None),
    0x0207DF9F: ("str_version", None),
    0x0207DFD8: ("str_build_date", None),
    0x0207E13C: ("str_chip_id", None),
    0x0207E143: ("str_firmware_version", None),
    0x0207E153: ("str_main_menu", None),
    0x0207E190: ("str_game_menu", None),
    0x0207E1C4: ("str_audio_player", None),
    0x0207E1E4: ("str_setting_menu", None),

    # Menu label string records
    0x02239C5A: ("menu_label_settings", "num_chars + string ref"),
    0x02239C62: ("menu_label_video", "num_chars + string ref"),
    0x02239C6A: ("menu_label_photo", "num_chars + string ref"),
    0x02239C72: ("menu_label_playback", "num_chars + string ref"),
    0x02239C7A: ("menu_label_mp3", "num_chars + string ref"),
    0x02239C82: ("menu_label_games", "num_chars + string ref"),
}

MEMORY_REGIONS = [
    ("HEADER",     0x02000000, 0x000200, "r"),
    ("SFC_CONFIG", 0x02000200, 0x000100, "r"),
    ("SFAT_HDR",   0x02000300, 0x000100, "r"),
    ("BOOT_CODE",  0x02000400, 0x002200, "rx"),
    ("APP_CODE",   0x02002600, 0x0C6200, "rx"),
    ("FLASH_CFG",  0x020C8800, 0x00AA00, "r"),
    ("SFAT",       0x020D3200, 0x000310, "r"),
    ("ASSET_DATA", 0x020D3510, 0x2336F0, "r"),
    ("FREE",       0x02307200, 0x0F8E00, "r"),
]


def generate_ghidra_script():
    lines = [
        '# Auto-generated Ghidra script for KC02 firmware',
        '# Run via Script Manager after importing dump1.bin',
        '#@category KC02',
        '',
        'from ghidra.program.model.symbol import SourceType',
        'from ghidra.program.model.address import AddressFactory',
        '',
        'fm = currentProgram.getFunctionManager()',
        'st = currentProgram.getSymbolTable()',
        'af = currentProgram.getAddressFactory()',
        'space = af.getDefaultAddressSpace()',
        '',
        'def addr(offset):',
        '    return space.getAddress(offset)',
        '',
        'def label(offset, name):',
        '    st.createLabel(addr(offset), name, SourceType.USER_DEFINED)',
        '',
        'def func(offset, name):',
        '    a = addr(offset)',
        '    f = fm.getFunctionAt(a)',
        '    if f:',
        '        f.setName(name, SourceType.USER_DEFINED)',
        '    else:',
        '        createFunction(a, name)',
        '',
        '# ── Function Labels ──',
    ]

    for offset, name in sorted(LABELS.items()):
        lines.append(f'func(0x{offset:08X}, "{name}")')

    lines.append('')
    lines.append('# ── Data Labels ──')
    for offset, (name, comment) in sorted(DATA_LABELS.items()):
        lines.append(f'label(0x{offset:08X}, "{name}")')
        if comment:
            lines.append(f'setEOLComment(addr(0x{offset:08X}), "{comment}")')

    lines.append('')
    lines.append('println("KC02 labels applied: %d functions, %d data labels" % ({}, {}))'.format(
        len(LABELS), len(DATA_LABELS)
    ))

    SCRIPT_OUT.write_text('\n'.join(lines) + '\n')
    print(f"Ghidra label script written to {SCRIPT_OUT}")


def print_import_instructions():
    print("""
╔══════════════════════════════════════════════════════════╗
║  GHIDRA IMPORT INSTRUCTIONS — KC02 FIRMWARE             ║
╚══════════════════════════════════════════════════════════╝

  1. Launch Ghidra:
       ghidra

  2. Create a new project (File > New Project > Non-Shared)

  3. Import firmware (File > Import File):
       File:      ~/dump1.bin
       Language:  JieLi pi32 (little endian)
       Base Addr: 0x02000000

  4. Open the imported binary, then run the label script:
       Window > Script Manager
       Find/load: tools/ghidra_label.py
       Run it — labels 20 functions + 25 data tables

  5. Start analysis:
       Analysis > Auto Analyze
       (Default settings are fine)

  6. Key locations to explore:
       0x0200411C  handler_camera     — Camera mode entry
       0x02004098  handler_settings   — Settings menu entry
       0x0207DF08  ui_state_dispatch  — 35-entry state machine
       0x0207DE87  str_destbin        — Firmware update code
       0x0207E153  str_main_menu      — Main menu setup
""")


if __name__ == "__main__":
    generate_ghidra_script()
    print_import_instructions()
