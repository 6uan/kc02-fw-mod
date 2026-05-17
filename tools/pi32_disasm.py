#!/usr/bin/env python3
"""
Pi32 Disassembler & Firmware Analyzer for KC02
Partial pi32 ISA decoder focused on control flow analysis,
function mapping, and identifying modifiable firmware parameters.
"""

import struct
import sys
import json
from pathlib import Path
from collections import defaultdict

PROJECT = Path(__file__).resolve().parent.parent
FLASH_BASE = 0x02000000

CODE_START = 0x000400
APP_START = 0x002600
CODE_END = 0x0C8800
DATA_IN_CODE = [(0x07DE00, 0x099200), (0x0CA600, 0x0CB000)]

KNOWN_FUNCTIONS = {
    0x003E74: "handler_video",
    0x003EF0: "handler_music",
    0x003F90: "handler_playback",
    0x004014: "handler_games",
    0x004098: "handler_settings",
    0x00411C: "handler_camera",
    0x006CBC: "handler_state_17",
    0x006CCC: "handler_state_12",
    0x006CE0: "handler_state_14",
    0x006CF8: "handler_state_20",
    0x006D00: "handler_state_13",
    0x006D10: "handler_state_19",
    0x006D58: "handler_state_06",
    0x006D60: "handler_state_21",
    0x006D6C: "handler_state_22",
    0x006D78: "handler_state_32",
    0x006D90: "handler_state_33",
    0x006DA8: "handler_state_34",
    0x006DC0: "handler_state_27",
    0x006DD4: "handler_null_default",
    0x0093AC: "init_callback_0",
    0x0093B4: "init_callback_1",
    0x0093BC: "init_callback_2",
    0x0093C4: "init_callback_3",
    0x0093CC: "init_callback_4",
    0x0093D4: "init_callback_5",
    0x0093DC: "init_callback_6",
    0x0093E4: "init_callback_7",
    0x002E0C: "adc_init",
    0x003A60: "adc_handler",
    0x004CC8: "adc_key_scan",
}

BUTTON_ADC = {
    "power": (22, 23),
    "left": (251, 252),
    "ok": (387, 390),
    "down": (509, 512),
    "up": (655, 660),
    "right": (783, 785),
}

GPR = ['r0','r1','r2','r3','r4','r5','r6','r7',
       'r8','r9','r10','r11','r12','r13','r14','sp']
SFR = ['reti','rete','sfr2','sfr3','maccl','macch','rets','psr',
       'sfr8','sfr9','ie1','ssp','ie0','icfg','pc','usp']
COND = ['eq','ne','cs','cc','mi','pl','vs','vc',
        'hi','ls','ge','lt','gt','le','','?']


def sign_extend(val, bits):
    if val & (1 << (bits - 1)):
        val -= (1 << bits)
    return val


def _find_dump():
    import os
    if "KC02_DUMP" in os.environ:
        return Path(os.environ["KC02_DUMP"])
    for p in [PROJECT / "firmware" / "original.bin", Path.home() / "dump1.bin"]:
        if p.exists():
            return p
    return PROJECT / "firmware" / "original.bin"


def regAf(w):
    return (w & 7) + (8 if (w >> 6) & 1 else 0)

def regBf(w):
    return ((w >> 3) & 7) + (8 if (w >> 7) & 1 else 0)

def regE(w2):
    return w2 & 0xF

def regF(w2):
    return (w2 >> 4) & 0xF

def regG(w2):
    return (w2 >> 8) & 0xF


class Instruction:
    __slots__ = ['offset', 'size', 'mnemonic', 'operands', 'group',
                 'is_call', 'is_jump', 'is_return', 'is_cond',
                 'target', 'decoded', 'raw_w1', 'raw_w2']

    def __init__(self):
        self.offset = 0
        self.size = 2
        self.mnemonic = "???"
        self.operands = ""
        self.group = 0
        self.is_call = False
        self.is_jump = False
        self.is_return = False
        self.is_cond = False
        self.target = None
        self.decoded = False
        self.raw_w1 = 0
        self.raw_w2 = 0

    @property
    def addr(self):
        return FLASH_BASE + self.offset

    def __str__(self):
        addr = f"0x{self.addr:08X}"
        if self.size == 4:
            raw = f"{self.raw_w1:04x} {self.raw_w2:04x}"
        else:
            raw = f"{self.raw_w1:04x}     "
        mark = ""
        if self.is_call:
            mark = " -> CALL"
        elif self.is_return:
            mark = " <- RET"
        elif self.is_jump and not self.is_cond:
            mark = " -> JMP"
        elif self.is_jump and self.is_cond:
            mark = " -> BR"
        tgt = ""
        if self.target is not None:
            tgt = f"  ; target=0x{self.target:08X}"
        mn = self.mnemonic
        if not self.decoded:
            mn = f"({mn})"
        op = f" {self.operands}" if self.operands else ""
        return f"  {addr}  {raw}  {mn}{op}{tgt}{mark}"


class Pi32Decoder:
    def decode(self, data, offset):
        if offset + 2 > len(data):
            return None

        w1 = struct.unpack_from('<H', data, offset)[0]
        group = (w1 >> 13) & 7
        size = 4 if group == 7 else 2

        if size == 4 and offset + 4 > len(data):
            return None

        w2 = struct.unpack_from('<H', data, offset + 2)[0] if size == 4 else 0

        inst = Instruction()
        inst.offset = offset
        inst.size = size
        inst.group = group
        inst.raw_w1 = w1
        inst.raw_w2 = w2

        inst_next = FLASH_BASE + offset + size
        self._dispatch(inst, w1, w2, inst_next)
        return inst

    def _dispatch(self, inst, w1, w2, inst_next):
        g = inst.group
        if g == 0:
            self._g0(inst, w1, inst_next)
        elif g == 1:
            self._g1(inst, w1, inst_next)
        elif g == 2:
            self._g2(inst, w1)
        elif g == 3:
            self._g3(inst, w1)
        elif g == 4:
            self._g4(inst, w1)
        elif g == 5:
            self._g5(inst, w1)
        elif g == 6:
            self._g6(inst, w1)
        elif g == 7:
            self._g7(inst, w1, w2, inst_next)

    def _g0(self, inst, w, next_addr):
        lo13 = w & 0x1FFF

        # --- exact lo13 matches (most specific) ---
        if lo13 == 0x0000:
            inst.mnemonic = "nop"
            inst.decoded = True
        elif lo13 == 0x0002:
            inst.mnemonic = "bkpt"
            inst.decoded = True
        elif lo13 == 0x0003:
            inst.mnemonic = "hbkpt"
            inst.decoded = True
        elif lo13 == 0x0004:
            inst.mnemonic = "clrmacc"
            inst.decoded = True
        elif lo13 == 0x0005:
            inst.mnemonic = "satmacc"
            inst.decoded = True
        elif lo13 == 0x0006:
            inst.mnemonic = "abstmacc"
            inst.decoded = True
        elif lo13 == 0x0008:
            inst.mnemonic = "csync"
            inst.decoded = True
        elif lo13 == 0x0009:
            inst.mnemonic = "ssync"
            inst.decoded = True
        elif lo13 == 0x000A:
            inst.mnemonic = "illeg"
            inst.decoded = True
        elif lo13 == 0x000C:
            inst.mnemonic = "lockset"
            inst.decoded = True
        elif lo13 == 0x000D:
            inst.mnemonic = "lockclr"
            inst.decoded = True
        elif lo13 == 0x0010:
            inst.mnemonic = "idle"
            inst.decoded = True
        elif lo13 == 0x0020:
            inst.mnemonic = "rts"
            inst.is_return = True
            inst.decoded = True
        elif lo13 == 0x0030:
            inst.mnemonic = "rti"
            inst.is_return = True
            inst.decoded = True
        elif lo13 == 0x0038:
            inst.mnemonic = "rte"
            inst.is_return = True
            inst.decoded = True
        elif lo13 == 0x0040:
            inst.mnemonic = "cli"
            inst.decoded = True
        elif lo13 == 0x0060:
            inst.mnemonic = "sti"
            inst.decoded = True

        # --- 9-bit field (ins0412) ---
        elif (w >> 4) & 0x1FF == 0x005:
            inst.mnemonic = "cli"
            inst.operands = GPR[w & 0xF]
            inst.decoded = True
        elif (w >> 4) & 0x1FF == 0x007:
            inst.mnemonic = "sti"
            inst.operands = GPR[w & 0xF]
            inst.decoded = True

        # --- 7-bit field (ins0612) ---
        elif (w >> 6) & 0x7F == 0x02:
            inst.mnemonic = "swi"
            inst.operands = str(w & 0x3F)
            inst.decoded = True
        elif (w >> 6) & 0x7F == 0x03:
            inst.mnemonic = "excpt"
            inst.operands = str(w & 0x3F)
            inst.decoded = True
        elif (w >> 6) & 0x7F == 0x18:
            inst.mnemonic = "mul"
            inst.operands = f"{GPR[w & 7]}, {GPR[(w >> 3) & 7]}"
            inst.decoded = True

        # --- 6-bit field (ins0712) ---
        elif (w >> 7) & 0x3F == 0x03:
            sub = (w >> 3) & 7
            ra = regAf(w)
            G0_SYS = {0: "flush", 1: "flushinv", 2: "iflush", 3: "pfetch"}
            if sub in G0_SYS:
                inst.mnemonic = G0_SYS[sub]
                inst.operands = f"[{GPR[ra]}]"
                inst.decoded = True
            elif sub == 4:
                inst.mnemonic = "j"
                inst.operands = GPR[ra]
                inst.is_jump = True
                inst.decoded = True
            elif sub == 5:
                inst.mnemonic = "call"
                inst.operands = GPR[ra]
                inst.is_call = True
                inst.decoded = True
            elif sub == 6:
                inst.mnemonic = "tbb"
                inst.operands = f"[{GPR[ra]}]"
                inst.is_jump = True
                inst.decoded = True
            elif sub == 7:
                inst.mnemonic = "tbh"
                inst.operands = f"[{GPR[ra]}]"
                inst.is_jump = True
                inst.decoded = True
        elif (w >> 7) & 0x3F == 0x02:
            ra = regAf(w)
            blk = (w >> 3) & 7
            inst.mnemonic = "rep"
            inst.operands = f"{blk}, {GPR[ra]}"
            inst.decoded = True

        # --- undocumented ins0712=0x04 family (from missing logicops.sinc) ---
        elif (w >> 7) & 0x3F == 0x04:
            ra = regAf(w)
            sub = (w >> 3) & 7
            G0_04_MN = {0: "ld.x", 1: "cmpn", 2: "st.x", 3: "ldm.x",
                        4: "stm.x", 5: "ldp.x", 6: "stp.x", 7: "xop7"}
            inst.mnemonic = G0_04_MN.get(sub, f"g0_04_{sub}")
            inst.operands = GPR[ra]
            inst.decoded = True

        # --- undocumented ins0712=0x00 family (logic/system ops) ---
        elif (w >> 7) & 0x3F == 0x00 and w != 0:
            ra = regAf(w)
            sub = (w >> 3) & 7
            G0_00_MN = {0: "sys0", 1: "sys1", 2: "sys2", 3: "sys3",
                        4: "sys4", 5: "sys5", 6: "sys6", 7: "sys7"}
            inst.mnemonic = G0_00_MN.get(sub, f"g0_00_{sub}")
            inst.operands = GPR[ra]
            inst.decoded = True

        # --- undocumented ins0712=0x0F family (mostly sub=7 at func boundaries) ---
        elif (w >> 7) & 0x3F == 0x0F:
            ra = regAf(w)
            sub = (w >> 3) & 7
            inst.mnemonic = f"frm{sub}" if sub == 7 else f"g0_0F_{sub}"
            inst.operands = GPR[ra]
            inst.decoded = True

        # --- other undocumented ins0712 values (0x05, 0x0A-0x0E) ---
        elif (w >> 7) & 0x3F in (0x05, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E):
            ra = regAf(w)
            sub = (w >> 3) & 7
            f = (w >> 7) & 0x3F
            inst.mnemonic = f"g0x{f:02X}"
            inst.operands = f"sub={sub}, {GPR[ra]}"
            inst.decoded = True

        # --- 5-bit field (ins0812) ---
        elif (w >> 8) & 0x1F == 0x03:
            cond_idx = (w >> 4) & 0xF
            c = COND[cond_idx] if cond_idx < 16 else "?"
            else_sz = (w >> 2) & 3
            true_sz = w & 3
            inst.mnemonic = f"if{c}"
            inst.operands = f"else={else_sz}, true={true_sz}"
            inst.decoded = True
        elif (w >> 8) & 0x1F == 0x04:
            off = sign_extend(w & 0xFF, 8)
            target = next_addr + (off << 1)
            inst.mnemonic = "call"
            inst.operands = f"0x{target:08X}"
            inst.is_call = True
            inst.target = target
            inst.decoded = True
        elif (w >> 8) & 0x1F == 0x12:
            blk = (w >> 5) & 7
            imm = w & 0x1F
            inst.mnemonic = "rep"
            inst.operands = f"{blk}, {imm}"
            inst.decoded = True

        # --- 2-bit field: j reladdr11 (ins1112=1) ---
        elif (w >> 11) & 3 == 1:
            off = sign_extend(w & 0x7FF, 11)
            target = next_addr + (off << 1)
            inst.mnemonic = "j"
            inst.operands = f"0x{target:08X}"
            inst.is_jump = True
            inst.target = target
            inst.decoded = True

        # --- 1-bit field: j<cond> reladdr8 (bit12=1) — must be last ---
        elif (w >> 12) & 1 == 1:
            cond_idx = (w >> 8) & 0xF
            off = sign_extend(w & 0xFF, 8)
            target = next_addr + (off << 1)
            c = COND[cond_idx] if cond_idx < 16 else "?"
            inst.mnemonic = f"j{c}"
            inst.operands = f"0x{target:08X}"
            inst.is_jump = True
            inst.is_cond = True
            inst.target = target
            inst.decoded = True

        else:
            inst.mnemonic = f"g0_{(w >> 8) & 0x1F:02x}"

    def _g1(self, inst, w, next_addr):
        sub = (w >> 11) & 3
        if sub == 0:
            ra = w & 7
            zflag = (w >> 3) & 1
            off = sign_extend((w >> 4) & 0x7F, 7)
            target = next_addr + (off << 1)
            inst.mnemonic = "jnz" if zflag else "jz"
            inst.operands = f"{GPR[ra]}, 0x{target:08X}"
            inst.is_jump = True
            inst.is_cond = True
            inst.target = target
            inst.decoded = True
        elif sub == 1:
            ra = w & 7
            off_hi = sign_extend((w >> 3) & 7, 3)
            off_lo = (w >> 6) & 0x1F
            raddr = (off_hi << 7) | (off_lo << 2)
            addr = (next_addr & ~3) + raddr
            inst.mnemonic = "lw"
            inst.operands = f"{GPR[ra]}, [0x{addr & 0xFFFFFFFF:08X}]"
            inst.decoded = True
        elif sub == 2:
            mask = w & 0xFF
            rd = (w >> 8) & 7
            regs = [GPR[i] for i in range(8) if mask & (1 << i)]
            reglist = ", ".join(regs) if regs else "none"
            inst.mnemonic = "lm"
            inst.operands = f"{{{reglist}}}, [{GPR[rd]}++]"
            inst.decoded = True
        elif sub == 3:
            mask = w & 0xFF
            rd = (w >> 8) & 7
            regs = [GPR[i] for i in range(8) if mask & (1 << i)]
            reglist = ", ".join(regs) if regs else "none"
            inst.mnemonic = "sm"
            inst.operands = f"[{GPR[rd]}++], {{{reglist}}}"
            inst.decoded = True

    def _g2(self, inst, w):
        sub = (w >> 11) & 3
        ra = w & 7
        rb = (w >> 3) & 7
        if sub == 0:
            off_hi = sign_extend((w >> 3) & 7, 3)
            off_lo = (w >> 6) & 0x1F
            off = (off_hi << 7) | (off_lo << 2)
            inst.mnemonic = "lw"
            inst.operands = f"{GPR[ra]}, [sp+{off}]"
            inst.decoded = True
        elif sub == 1:
            off = sign_extend((w >> 6) & 0x1F, 5) << 2
            inst.mnemonic = "lw"
            inst.operands = f"{GPR[ra]}, [{GPR[rb]}+{off}]"
            inst.decoded = True
        elif sub == 2:
            off_hi = sign_extend((w >> 6) & 0xF, 4)
            off_lo = (w >> 10) & 1
            off = (off_hi << 2) | (off_lo << 1)
            inst.mnemonic = "lhz"
            inst.operands = f"{GPR[ra]}, [{GPR[rb]}+{off}]"
            inst.decoded = True
        elif sub == 3:
            off_hi = sign_extend((w >> 6) & 7, 3)
            off_lo = (w >> 9) & 3
            off = (off_hi << 2) | off_lo
            inst.mnemonic = "lbz"
            inst.operands = f"{GPR[ra]}, [{GPR[rb]}+{off}]"
            inst.decoded = True

    def _g3(self, inst, w):
        sub = (w >> 11) & 3
        ra = w & 7
        rb = (w >> 3) & 7
        if sub == 0:
            off_hi = sign_extend((w >> 3) & 7, 3)
            off_lo = (w >> 6) & 0x1F
            off = (off_hi << 7) | (off_lo << 2)
            inst.mnemonic = "sw"
            inst.operands = f"{GPR[ra]}, [sp+{off}]"
            inst.decoded = True
        elif sub == 1:
            off = sign_extend((w >> 6) & 0x1F, 5) << 2
            inst.mnemonic = "sw"
            inst.operands = f"{GPR[ra]}, [{GPR[rb]}+{off}]"
            inst.decoded = True
        elif sub == 2:
            off_hi = sign_extend((w >> 6) & 0xF, 4)
            off_lo = (w >> 10) & 1
            off = (off_hi << 2) | (off_lo << 1)
            inst.mnemonic = "sh"
            inst.operands = f"{GPR[ra]}, [{GPR[rb]}+{off}]"
            inst.decoded = True
        elif sub == 3:
            off_hi = sign_extend((w >> 6) & 7, 3)
            off_lo = (w >> 9) & 3
            off = (off_hi << 2) | off_lo
            inst.mnemonic = "sb"
            inst.operands = f"{GPR[ra]}, [{GPR[rb]}+{off}]"
            inst.decoded = True

    def _g4(self, inst, w):
        sub = (w >> 11) & 3
        ra = w & 7
        imm = sign_extend((w >> 3) & 0xFF, 8)
        if sub == 0:
            inst.mnemonic = "movs"
            inst.operands = f"{GPR[ra]}, {imm}"
            inst.decoded = True
        elif sub == 1:
            inst.mnemonic = "add"
            inst.operands = f"{GPR[ra]}, {imm}"
            inst.decoded = True
        elif sub == 2:
            inst.mnemonic = "add"
            inst.operands = f"{GPR[ra]}, sp, {imm}"
            inst.decoded = True
        elif sub == 3:
            inst.mnemonic = "cmp"
            inst.operands = f"{GPR[ra]}, {imm}"
            inst.decoded = True
        else:
            inst.mnemonic = f"g4_{sub}"

    def _g5(self, inst, w):
        ra = w & 7
        rb = (w >> 3) & 7
        amt = (w >> 8) & 0x1F
        op = (w >> 6) & 3
        names = {0: "lsl", 1: "lsr", 2: "qasl", 3: "qasr"}
        inst.mnemonic = names[op]
        inst.operands = f"{GPR[ra]}, {GPR[rb]}, {amt}"
        inst.decoded = True

    def _g6(self, inst, w):
        lo = w & 0x1FFF
        top9 = (w >> 4) & 0x1FF
        top5 = (w >> 8) & 0x1F
        top7 = (w >> 6) & 0x7F

        if top9 == 0x000:
            bitmask = w & 0xF
            inst.mnemonic = "pop"
            inst.operands = f"{{mask=0x{bitmask:X}}}"
            inst.decoded = True
        elif top9 == 0x001:
            bitmask = w & 0xF
            inst.mnemonic = "pop"
            inst.operands = f"{{pc, mask=0x{bitmask:X}}}"
            inst.is_return = True
            inst.decoded = True
        elif top9 == 0x020:
            bitmask = w & 0xF
            inst.mnemonic = "push"
            inst.operands = f"{{mask=0x{bitmask:X}}}"
            inst.decoded = True
        elif top9 == 0x021:
            bitmask = w & 0xF
            inst.mnemonic = "push"
            inst.operands = f"{{rets, mask=0x{bitmask:X}}}"
            inst.decoded = True
        elif top5 == 0x06:
            inst.mnemonic = "mov"
            inst.operands = f"{GPR[regAf(w)]}, {GPR[regBf(w)]}"
            inst.decoded = True
        elif top5 == 0x07:
            ra = regAf(w)
            rb = regBf(w)
            si = ra  # sfr index
            inst.mnemonic = "mov"
            inst.operands = f"{SFR[si] if si < 16 else f'sfr{si}'}, {GPR[rb]}"
            inst.decoded = True
        elif top5 == 0x08:
            ra = regAf(w)
            sb = regBf(w)
            inst.mnemonic = "mov"
            inst.operands = f"{GPR[ra]}, {SFR[sb] if sb < 16 else f'sfr{sb}'}"
            inst.decoded = True
        elif top5 == 0x0B:
            inst.mnemonic = "add"
            inst.operands = f"{GPR[regAf(w)]}, {GPR[regBf(w)]}"
            inst.decoded = True
        elif top5 == 0x0D:
            imm = sign_extend(w & 0xFF, 8)
            inst.mnemonic = "add"
            inst.operands = f"sp, {imm}"
            inst.decoded = True
        elif top7 == 0x24:
            inst.mnemonic = "uxtb"
            inst.operands = f"{GPR[w & 7]}, {GPR[(w >> 3) & 7]}"
            inst.decoded = True
        elif top7 == 0x25:
            inst.mnemonic = "uxth"
            inst.operands = f"{GPR[w & 7]}, {GPR[(w >> 3) & 7]}"
            inst.decoded = True
        elif top7 == 0x26:
            inst.mnemonic = "sxtb"
            inst.operands = f"{GPR[w & 7]}, {GPR[(w >> 3) & 7]}"
            inst.decoded = True
        elif top7 == 0x27:
            inst.mnemonic = "sxth"
            inst.operands = f"{GPR[w & 7]}, {GPR[(w >> 3) & 7]}"
            inst.decoded = True
        elif top7 == 0x70:
            inst.mnemonic = "addc"
            inst.operands = f"{GPR[w & 7]}, {GPR[(w >> 3) & 7]}"
            inst.decoded = True
        elif top7 == 0x71:
            inst.mnemonic = "subc"
            inst.operands = f"{GPR[w & 7]}, {GPR[(w >> 3) & 7]}"
            inst.decoded = True
        elif top7 == 0x72:
            inst.mnemonic = "neg"
            inst.operands = f"{GPR[w & 7]}, {GPR[(w >> 3) & 7]}"
            inst.decoded = True
        elif (w >> 10) & 7 == 4:
            ra = w & 7
            rb = (w >> 3) & 7
            imm = sign_extend((w >> 6) & 0xF, 4)
            inst.mnemonic = "add"
            inst.operands = f"{GPR[ra]}, {GPR[rb]}, {imm}"
            inst.decoded = True
        elif (w >> 9) & 0xF == 0xA:
            ra = w & 7
            rb = (w >> 3) & 7
            rc = (w >> 6) & 7
            inst.mnemonic = "add"
            inst.operands = f"{GPR[ra]}, {GPR[rb]}, {GPR[rc]}"
            inst.decoded = True
        elif (w >> 9) & 0xF == 0xB:
            ra = w & 7
            rb = (w >> 3) & 7
            rc = (w >> 6) & 7
            inst.mnemonic = "sub"
            inst.operands = f"{GPR[ra]}, {GPR[rb]}, {GPR[rc]}"
            inst.decoded = True
        elif top5 == 0x0E:
            inst.mnemonic = "cmp"
            inst.operands = f"{GPR[regBf(w)]}, {GPR[regAf(w)]}"
            inst.decoded = True
        elif top7 == 0x68:
            inst.mnemonic = "or"
            inst.operands = f"{GPR[w & 7]}, {GPR[(w >> 3) & 7]}"
            inst.decoded = True
        elif top7 == 0x69:
            inst.mnemonic = "xor"
            inst.operands = f"{GPR[w & 7]}, {GPR[(w >> 3) & 7]}"
            inst.decoded = True
        elif top7 == 0x6A:
            inst.mnemonic = "and"
            inst.operands = f"{GPR[w & 7]}, {GPR[(w >> 3) & 7]}"
            inst.decoded = True
        elif top7 == 0x6B:
            inst.mnemonic = "not"
            inst.operands = f"{GPR[w & 7]}, {GPR[(w >> 3) & 7]}"
            inst.decoded = True
        elif top5 == 0x18:
            inst.mnemonic = "bitset"
            inst.operands = f"{GPR[w & 7]}, {(w >> 3) & 0x1F}"
            inst.decoded = True
        elif top5 == 0x19:
            inst.mnemonic = "bittgl"
            inst.operands = f"{GPR[w & 7]}, {(w >> 3) & 0x1F}"
            inst.decoded = True
        elif top5 == 0x1B:
            inst.mnemonic = "bitclr"
            inst.operands = f"{GPR[w & 7]}, {(w >> 3) & 0x1F}"
            inst.decoded = True
        elif top7 == 0x3C:
            inst.mnemonic = "lsl"
            inst.operands = f"{GPR[w & 7]}, {GPR[(w >> 3) & 7]}"
            inst.decoded = True
        elif top7 == 0x3D:
            inst.mnemonic = "lsr"
            inst.operands = f"{GPR[w & 7]}, {GPR[(w >> 3) & 7]}"
            inst.decoded = True
        elif top7 == 0x3E:
            inst.mnemonic = "qasl"
            inst.operands = f"{GPR[w & 7]}, {GPR[(w >> 3) & 7]}"
            inst.decoded = True
        elif top7 == 0x3F:
            inst.mnemonic = "qasr"
            inst.operands = f"{GPR[w & 7]}, {GPR[(w >> 3) & 7]}"
            inst.decoded = True
        elif top7 == 0x74:
            inst.mnemonic = "rotr"
            inst.operands = f"{GPR[w & 7]}, {GPR[(w >> 3) & 7]}"
            inst.decoded = True
        elif top7 == 0x75:
            inst.mnemonic = "rotrc"
            inst.operands = f"{GPR[w & 7]}, {GPR[(w >> 3) & 7]}"
            inst.decoded = True
        elif top5 == 0x03:
            inst.mnemonic = "testset"
            inst.operands = f"b[{GPR[w & 0xF]}]"
            inst.decoded = True
        elif top5 == 0x04:
            mask = w & 0xFF
            regs = [SFR[i] for i in range(8) if mask & (1 << i)]
            inst.mnemonic = "pops"
            inst.operands = "{%s}" % ", ".join(regs) if regs else "{}"
            inst.decoded = True
        elif top5 == 0x05:
            mask = w & 0xFF
            regs = [SFR[i] for i in range(8) if mask & (1 << i)]
            inst.mnemonic = "pushs"
            inst.operands = "{%s}" % ", ".join(regs) if regs else "{}"
            inst.decoded = True
        elif top5 == 0x0A:
            inst.mnemonic = "addrev"
            inst.operands = f"{GPR[regAf(w)]}, {GPR[regBf(w)]}"
            inst.decoded = True
        elif top5 == 0x0C:
            inst.mnemonic = "cmn"
            inst.operands = f"{GPR[regBf(w)]}, {GPR[regAf(w)]}"
            inst.decoded = True
        elif top7 == 0x73:
            imm = w & 0x3F
            inst.mnemonic = "lslmacc"
            inst.operands = f"{imm}"
            inst.decoded = True
        elif top7 == 0x7C:
            imm = w & 0x3F
            inst.mnemonic = "lsrmacc"
            inst.operands = f"{imm}"
            inst.decoded = True
        elif top7 == 0x7E:
            imm = w & 0x3F
            inst.mnemonic = "asrmacc"
            inst.operands = f"{imm}"
            inst.decoded = True
        else:
            inst.mnemonic = f"g6_{top5:02x}"

    def _g7(self, inst, w1, w2, next_addr):
        lo13 = w1 & 0x1FFF
        top5 = (w1 >> 8) & 0x1F
        rE = regE(w2)
        rF = regF(w2)
        rG = regG(w2)

        if top5 == 0x01:
            off_hi = sign_extend((w2 >> 0) & 0xF, 4)
            off_lo_hi = (w1 >> 0) & 0xFF
            off_lo_lo = (w2 >> 4) & 0xFFF
            target = next_addr + ((off_hi << 21) | (off_lo_hi << 13) | (off_lo_lo << 1))
            inst.mnemonic = "call"
            inst.operands = f"0x{target & 0xFFFFFFFF:08X}"
            inst.is_call = True
            inst.target = target & 0xFFFFFFFF
            inst.decoded = True
        elif top5 == 0x1A:
            off_hi = sign_extend((w2 >> 0) & 0xF, 4)
            off_lo_hi = (w1 >> 0) & 0xFF
            off_lo_lo = (w2 >> 4) & 0xFFF
            target = next_addr + ((off_hi << 21) | (off_lo_hi << 13) | (off_lo_lo << 1))
            inst.mnemonic = "j"
            inst.operands = f"0x{target & 0xFFFFFFFF:08X}"
            inst.is_jump = True
            inst.target = target & 0xFFFFFFFF
            inst.decoded = True
        elif top5 == 0x1B:
            cond = w2 & 0xF
            off_lo_hi = w1 & 0xFF
            off_lo_lo = (w2 >> 4) & 0xFFF
            off = sign_extend((off_lo_hi << 13) | (off_lo_lo << 1), 21)
            target = next_addr + off
            c = COND[cond] if cond < 16 else "?"
            inst.mnemonic = f"j{c}"
            inst.operands = f"0x{target & 0xFFFFFFFF:08X}"
            inst.is_jump = True
            inst.is_cond = True
            inst.target = target & 0xFFFFFFFF
            inst.decoded = True
        elif lo13 == 0x0080:
            inst.mnemonic = "trigger"
            inst.decoded = True
        elif lo13 == 0x0000:
            cond = w2 & 0xF
            rf = rF
            c = COND[cond] if cond < 16 else "?"
            inst.mnemonic = f"j{c}"
            inst.operands = GPR[rf]
            inst.is_jump = True
            inst.is_cond = True
            inst.decoded = True
        elif top5 == 0x00 and lo13 != 0 and lo13 != 0x0080:
            lo8 = w1 & 0xFF
            rBase = (lo8 >> 5) & 7
            mode = (lo8 >> 3) & 3
            rData = lo8 & 7
            soff = sign_extend(w2, 16)
            G7_00_MN = {0: "lw", 1: "sw", 2: "lbs", 3: "sbs"}
            inst.mnemonic = G7_00_MN[mode]
            if soff == 0:
                inst.operands = f"{GPR[rData]}, [{GPR[rBase]}]"
            elif soff > 0:
                inst.operands = f"{GPR[rData]}, [{GPR[rBase]}+0x{soff:X}]"
            else:
                inst.operands = f"{GPR[rData]}, [{GPR[rBase]}-0x{-soff:X}]"
            inst.decoded = True
        elif (w1 >> 7) & 0x3F == 0x15:
            sub = (w1 >> 3) & 7
            ra = regAf(w1)
            imm16 = w2
            simm16 = sign_extend(w2, 16)
            if sub == 0:
                inst.mnemonic = "movh"
                inst.operands = f"{GPR[ra]}, 0x{imm16:04X}"
                inst.decoded = True
            elif sub == 1:
                inst.mnemonic = "movh"
                inst.operands = f"{SFR[ra] if ra < 16 else f'sfr{ra}'}, 0x{imm16:04X}"
                inst.decoded = True
            elif sub == 2:
                inst.mnemonic = "movs"
                inst.operands = f"{GPR[ra]}, {simm16}"
                inst.decoded = True
            elif sub == 3:
                inst.mnemonic = "movs"
                inst.operands = f"{SFR[ra] if ra < 16 else f'sfr{ra}'}, {simm16}"
                inst.decoded = True
            else:
                inst.mnemonic = f"g7_15_{sub}"
        elif (w1 >> 7) & 0x3F == 0x14:
            sub = (w1 >> 3) & 7
            ra = regAf(w1)
            imm16 = w2
            if sub == 0:
                inst.mnemonic = "movl"
                inst.operands = f"{GPR[ra]}, 0x{imm16:04X}"
                inst.decoded = True
            elif sub == 1:
                inst.mnemonic = "movl"
                inst.operands = f"{SFR[ra] if ra < 16 else f'sfr{ra}'}, 0x{imm16:04X}"
                inst.decoded = True
            elif sub == 2:
                inst.mnemonic = "movz"
                inst.operands = f"{GPR[ra]}, 0x{imm16:04X}"
                inst.decoded = True
            elif sub == 3:
                inst.mnemonic = "movz"
                inst.operands = f"{SFR[ra] if ra < 16 else f'sfr{ra}'}, 0x{imm16:04X}"
                inst.decoded = True
            else:
                inst.mnemonic = f"g7_14_{sub}"
        elif lo13 == 0x0E00:
            inst.mnemonic = "add"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {GPR[rG]}"
            inst.decoded = True
        elif lo13 == 0x0E40:
            inst.mnemonic = "sub"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {GPR[rG]}"
            inst.decoded = True
        elif (w1 >> 5) & 0xFF == 0x68:
            imm_lo = sign_extend(w1 & 0x1F, 5)
            imm_hi = w2 >> 8
            imm = (imm_lo << 8) | imm_hi
            inst.mnemonic = "add"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {imm}"
            inst.decoded = True
        elif lo13 == 0x0420:
            off_hi = sign_extend((w2 >> 4) & 0xF, 4)
            off_lo = (w2 >> 8) & 0xFF
            raddr = (off_hi << 10) | (off_lo << 2)
            inst.mnemonic = "lw"
            inst.operands = f"{GPR[rE]}, [pc+{raddr}]"
            inst.decoded = True
        elif lo13 == 0x0720:
            inst.mnemonic = "lw"
            inst.operands = f"{GPR[rE]}, [{GPR[rF]}+{GPR[rG]}]"
            inst.decoded = True
        elif lo13 == 0x0820:
            off = sign_extend((w2 >> 8) & 0xFF, 8)
            inst.mnemonic = "lw"
            inst.operands = f"{GPR[rE]}, [{GPR[rF]}+{off<<2}]"
            inst.decoded = True
        elif lo13 == 0x0760:
            inst.mnemonic = "sw"
            inst.operands = f"{GPR[rE]}, [{GPR[rF]}+{GPR[rG]}]"
            inst.decoded = True
        elif lo13 == 0x0860:
            off = sign_extend((w2 >> 8) & 0xFF, 8)
            inst.mnemonic = "sw"
            inst.operands = f"{GPR[rE]}, [{GPR[rF]}+{off<<2}]"
            inst.decoded = True
        elif lo13 == 0x1C80:
            inst.mnemonic = "mul"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {GPR[rG]}"
            inst.decoded = True
        elif lo13 == 0x1C40:
            inst.mnemonic = "udiv"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {GPR[rG]}"
            inst.decoded = True
        elif lo13 == 0x1CC0:
            inst.mnemonic = "sdiv"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {GPR[rG]}"
            inst.decoded = True
        elif lo13 == 0x1000:
            inst.mnemonic = "or"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {GPR[rG]}"
            inst.decoded = True
        elif lo13 == 0x1040:
            inst.mnemonic = "xor"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {GPR[rG]}"
            inst.decoded = True
        elif lo13 == 0x1080:
            inst.mnemonic = "and"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {GPR[rG]}"
            inst.decoded = True
        elif lo13 == 0x10C0:
            inst.mnemonic = "andnot"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {GPR[rG]}"
            inst.decoded = True
        elif lo13 == 0x1200:
            inst.mnemonic = "tst"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}"
            inst.decoded = True
        elif lo13 == 0x1240:
            inst.mnemonic = "not"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}"
            inst.decoded = True
        elif lo13 == 0x1100:
            inst.mnemonic = "bitset"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {GPR[rG]}"
            inst.decoded = True
        elif lo13 == 0x11C0:
            inst.mnemonic = "bitclr"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {GPR[rG]}"
            inst.decoded = True
        elif lo13 == 0x1700:
            inst.mnemonic = "lsl"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {GPR[rG]}"
            inst.decoded = True
        elif lo13 == 0x1720:
            shamt = (w2 >> 8) & 0x1F
            inst.mnemonic = "lsl"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {shamt}"
            inst.decoded = True
        elif (w1 >> 4) & 0x1FF == 0x0F0:
            inst.mnemonic = "cmp"
            inst.operands = f"{GPR[rF]}, weirdimm"
            inst.decoded = True
        elif (w1 >> 4) & 0x1FF == 0x0F8:
            imm_lo = sign_extend(w1 & 0xF, 4)
            imm_hi = (w2 >> 8) & 0xFF
            imm = (imm_lo << 8) | imm_hi
            inst.mnemonic = "cmp"
            inst.operands = f"{GPR[rF]}, {imm}"
            inst.decoded = True
        elif lo13 == 0x0C00:
            inst.mnemonic = "uxtb"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}"
            inst.decoded = True
        elif lo13 == 0x0C40:
            inst.mnemonic = "uxth"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}"
            inst.decoded = True
        elif lo13 == 0x0C80:
            inst.mnemonic = "sxtb"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}"
            inst.decoded = True
        elif lo13 == 0x0CC0:
            inst.mnemonic = "sxth"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}"
            inst.decoded = True

        # ── Additional decodings (from complete ISA map) ──

        # g7_0E extras: addc, subc
        elif lo13 == 0x0E80:
            inst.mnemonic = "addc"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {GPR[rG]}"
            inst.decoded = True
        elif lo13 == 0x0EC0:
            inst.mnemonic = "subc"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {GPR[rG]}"
            inst.decoded = True

        # g7_0B: rev8
        elif lo13 == 0x0B00:
            inst.mnemonic = "rev8"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}"
            inst.decoded = True

        # g7_11 extras: bittgl, and1
        elif lo13 == 0x1140:
            inst.mnemonic = "bittgl"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {GPR[rG]}"
            inst.decoded = True
        elif lo13 == 0x1180:
            inst.mnemonic = "and1"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {GPR[rG]}"
            inst.decoded = True

        # g7_17: lsr, qasl, qasr (register and immediate variants)
        elif lo13 == 0x1740:
            inst.mnemonic = "lsr"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {GPR[rG]}"
            inst.decoded = True
        elif lo13 == 0x1760:
            shamt = (w2 >> 8) & 0x1F
            inst.mnemonic = "lsr"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {shamt}"
            inst.decoded = True
        elif lo13 == 0x1780:
            inst.mnemonic = "qasl"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {GPR[rG]}"
            inst.decoded = True
        elif lo13 == 0x17A0:
            shamt = (w2 >> 8) & 0x1F
            inst.mnemonic = "qasl"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {shamt}"
            inst.decoded = True
        elif lo13 == 0x17C0:
            inst.mnemonic = "qasr"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {GPR[rG]}"
            inst.decoded = True
        elif lo13 == 0x17E0:
            shamt = (w2 >> 8) & 0x1F
            inst.mnemonic = "qasr"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {shamt}"
            inst.decoded = True

        # g7_15: clz, cls, zcmp
        elif lo13 == 0x1500:
            inst.mnemonic = "clz"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}"
            inst.decoded = True
        elif lo13 == 0x1540:
            inst.mnemonic = "cls"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}"
            inst.decoded = True
        elif lo13 == 0x1580:
            inst.mnemonic = "zcmp"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}"
            inst.decoded = True

        # g7_18: rotate
        elif lo13 == 0x1800:
            inst.mnemonic = "rotr"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {GPR[rG]}"
            inst.decoded = True
        elif lo13 == 0x1820:
            shamt = (w2 >> 8) & 0x1F
            inst.mnemonic = "rotr"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {shamt}"
            inst.decoded = True

        # ── Top5-based catchalls for whole sub-opcodes ──

        # g7_02: memory OR
        elif top5 == 0x02:
            inst.mnemonic = "memor"
            inst.operands = f"[{GPR[rE]}+off], weirdimm"
            inst.decoded = True

        # g7_03: memory AND-NOT
        elif top5 == 0x03:
            inst.mnemonic = "memandnot"
            inst.operands = f"[{GPR[rE]}+off], weirdimm"
            inst.decoded = True

        # g7_04: PC-relative load (byte/half variants)
        elif top5 == 0x04:
            sub8 = w1 & 0xFF
            pc_ops = {0x00: "lbz", 0x10: "lhz", 0x20: "lw", 0x80: "lbs", 0x90: "lhs"}
            base = sub8 & 0xF0
            mn = pc_ops.get(base, "ld")
            inst.mnemonic = mn
            inst.operands = f"{GPR[rE]}, [pc+off]"
            inst.decoded = True

        # g7_05: double-word load/store
        elif top5 == 0x05:
            sub4 = (w1 >> 4) & 0xF
            if sub4 < 4:
                inst.mnemonic = "ld.d"
            else:
                inst.mnemonic = "st.d"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}"
            inst.decoded = True

        # g7_06: multi-register load/store (LDM/STM)
        elif top5 == 0x06:
            sub4 = (w1 >> 4) & 0xF
            base_reg = w1 & 0xF
            rets_flag = (w2 >> 15) & 1
            bitmask = w2 & 0x7FFF
            regs = [GPR[i] for i in range(15) if bitmask & (1 << i)]
            if rets_flag:
                regs.append("rets" if sub4 >= 0xE else "pc")
            reglist = ", ".join(regs) if regs else "none"
            if sub4 >= 0xE:
                inst.mnemonic = "stm"
                inst.operands = f"[--{GPR[base_reg]}], {{{reglist}}}"
            elif sub4 <= 0x9:
                inst.mnemonic = "ldm"
                inst.operands = f"{{{reglist}}}, [{GPR[base_reg]}++]"
                if rets_flag:
                    inst.is_return = True
            else:
                inst.mnemonic = "ldstm"
                inst.operands = f"{GPR[base_reg]}, {{{reglist}}}"
            inst.decoded = True

        # g7_07: 3-register load/store (remaining variants)
        elif top5 == 0x07:
            sub8 = w1 & 0xFF
            ops_3r = {0x00: "lbz", 0x10: "lhz", 0x20: "lw",
                      0x40: "sb", 0x50: "sh", 0x60: "sw",
                      0x80: "lbs", 0x90: "lhs"}
            base = sub8 & 0xF0
            mn = ops_3r.get(base, "ld/st")
            mode = (sub8 >> 2) & 3
            if mode == 0:
                inst.operands = f"{GPR[rE]}, [{GPR[rF]}+{GPR[rG]}]"
            elif mode == 1:
                inst.operands = f"{GPR[rE]}, [++{GPR[rF]}={GPR[rG]}]"
            else:
                inst.operands = f"{GPR[rE]}, [{GPR[rF]}++={GPR[rG]}]"
            inst.mnemonic = mn
            inst.decoded = True

        # g7_08: reg+offset load/store (remaining variants)
        elif top5 == 0x08:
            sub8 = w1 & 0xFF
            off = sign_extend((w2 >> 8) & 0xFF, 8)
            ops_8 = {0x00: ("lbz", off), 0x10: ("lhz", off << 1),
                     0x20: ("lw", off << 2), 0x40: ("sb", off),
                     0x50: ("sh", off << 1), 0x60: ("sw", off << 2),
                     0x80: ("lbs", off), 0x90: ("lhs", off << 1)}
            base = sub8 & 0xF0
            if base in ops_8:
                mn, final_off = ops_8[base]
                inst.mnemonic = mn
                inst.operands = f"{GPR[rE]}, [{GPR[rF]}+{final_off}]"
            else:
                inst.mnemonic = "ld/st"
                inst.operands = f"{GPR[rE]}, [{GPR[rF]}+off]"
            inst.decoded = True

        # g7_09: SP-relative load/store
        elif top5 == 0x09:
            sub8 = w1 & 0xFF
            ops_sp = {0x00: "lbz", 0x10: "lhz", 0x20: "lw",
                      0x40: "sb", 0x50: "sh", 0x60: "sw",
                      0x80: "lbs", 0x90: "lhs"}
            base = sub8 & 0xF0
            mn = ops_sp.get(base, "ld/st")
            inst.mnemonic = mn
            inst.operands = f"{GPR[rE]}, [sp+off]"
            inst.decoded = True

        # g7_0D: add with 13-bit immediate (catchall)
        elif top5 == 0x0D:
            inst.mnemonic = "add"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, imm13"
            inst.decoded = True

        # g7_0E: add/sub variants (catchall)
        elif top5 == 0x0E:
            sub8 = w1 & 0xFF
            if sub8 & 0xC0 == 0x00:
                inst.mnemonic = "add"
            elif sub8 & 0xC0 == 0x40:
                inst.mnemonic = "sub"
            elif sub8 & 0xC0 == 0x80:
                inst.mnemonic = "addc"
            else:
                inst.mnemonic = "subc"
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, weirdimm"
            inst.decoded = True

        # g7_10: logic with register (catchall)
        elif top5 == 0x10:
            sub8 = w1 & 0xFF
            logic_ops = {0x00: "or", 0x40: "xor", 0x80: "and", 0xC0: "andnot"}
            mn = logic_ops.get(sub8 & 0xC0, "logic")
            inst.mnemonic = mn
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {GPR[rG]}"
            inst.decoded = True

        # g7_11: bit operations (catchall)
        elif top5 == 0x11:
            sub8 = w1 & 0xFF
            bit_ops = {0x00: "bitset", 0x40: "bittgl", 0x80: "and1", 0xC0: "bitclr"}
            mn = bit_ops.get(sub8 & 0xC0, "bitop")
            inst.mnemonic = mn
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {GPR[rG]}"
            inst.decoded = True

        # g7_13: logic with immediate
        elif top5 == 0x13:
            sub8 = w1 & 0xFF
            logic_imm = {0x00: "or", 0x40: "xor", 0x80: "and", 0xC0: "andnot"}
            mn = logic_imm.get(sub8 & 0xC0, "logic")
            inst.mnemonic = mn
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, weirdimm"
            inst.decoded = True

        # g7_14: tst with immediate
        elif top5 == 0x14:
            inst.mnemonic = "tst"
            inst.operands = f"{GPR[rE]}, weirdimm"
            inst.decoded = True

        # g7_16: bit extract/insert with position and length
        elif top5 == 0x16:
            sub8 = w1 & 0xFF
            extr_ops = {0x00: "uextra", 0x20: "uextra", 0x40: "insert",
                        0x60: "insert", 0x80: "sextra", 0xA0: "sextra"}
            mn = extr_ops.get(sub8 & 0xE0, "bfld")
            pos5 = ((w1 & 3) << 3) | ((w2 >> 13) & 7)
            length = (w2 >> 8) & 0x1F
            inst.mnemonic = mn
            inst.operands = f"{GPR[rE]}, {GPR[rF]}, {pos5}, {length}"
            inst.decoded = True

        # g7_17: shift/rotate (catchall for remaining)
        elif top5 == 0x17:
            sub8 = w1 & 0xFF
            shift_ops = {0x00: "lsl", 0x20: "lsl", 0x40: "lsr", 0x60: "lsr",
                         0x80: "qasl", 0xA0: "qasl", 0xC0: "qasr", 0xE0: "qasr"}
            mn = shift_ops.get(sub8 & 0xE0, "shift")
            inst.mnemonic = mn
            inst.operands = f"{GPR[rE]}, {GPR[rF]}"
            inst.decoded = True

        # g7_19: macc extraction (movz64/movs64)
        elif top5 == 0x19:
            inst.mnemonic = "macc_ext"
            inst.operands = f"{GPR[rE]}"
            inst.decoded = True

        # g7_1D: macc register multiply
        elif top5 == 0x1D:
            sub8 = w1 & 0xFF
            if sub8 & 0x20:
                inst.mnemonic = "macc+="
            elif sub8 & 0x30 == 0x30:
                inst.mnemonic = "macc-="
            else:
                inst.mnemonic = "macc="
            inst.operands = f"{GPR[rF]} * {GPR[rG]}"
            inst.decoded = True

        # g7_1E: macc memory multiply (register increment)
        elif top5 == 0x1E:
            sub8 = w1 & 0xFF
            rI = (w2 >> 12) & 0xF
            if sub8 & 0x20 and not (sub8 & 0x10):
                op = "+="
            elif sub8 & 0x30 == 0x30:
                op = "-="
            else:
                op = "="
            hw = "h" if sub8 & 0x40 else ""
            inst.mnemonic = f"macc{op}"
            inst.operands = f"{hw}[{GPR[rF]}++={GPR[rI]}] * {hw}[{GPR[rE]}++={GPR[rG]}]"
            inst.decoded = True

        # g7_1F: stack frame operations — register save/restore
        # w2 is a fixed opcode per register for STORE (prologue save).
        # w1 bits[7:0] = signed sp-relative offset.
        elif top5 == 0x1F:
            lo8 = w1 & 0xFF
            soff = sign_extend(lo8, 8)
            STK_SAVE = {
                0x8421: 'r4',  0x8441: 'r5',  0x8521: 'rets',
                0x85c1: 'r6',  0x8641: 'r7',  0x8681: 'r8',
                0x86c1: 'r9',  0x8701: 'r10', 0x8741: 'r11',
                0x8781: 'r12', 0x87c1: 'r13',
            }
            if w2 in STK_SAVE:
                reg = STK_SAVE[w2]
                inst.mnemonic = "push"
                inst.operands = f"{reg}, [sp{soff:+d}]"
            elif (w2 >> 12) & 0xF == 9:
                b10_6 = (w2 >> 6) & 0x1F
                STK_REST = {16:'r4', 17:'r5', 20:'rets', 23:'r6',
                            25:'r7', 26:'r8', 27:'r9', 28:'r10',
                            29:'r11', 30:'r12', 31:'r13'}
                reg = STK_REST.get(b10_6, f'?{b10_6}')
                inst.mnemonic = "pop"
                inst.operands = f"{reg}, [sp{soff:+d}]"
            else:
                inst.mnemonic = "stk_op"
                inst.operands = f"0x{w2:04X}, [sp{soff:+d}]"
            inst.decoded = True

        else:
            inst.mnemonic = f"g7_{top5:02x}"


class FirmwareAnalyzer:
    def __init__(self, data):
        self.data = data
        self.decoder = Pi32Decoder()
        self.functions = {}
        self.call_graph = defaultdict(set)
        self.callers = defaultdict(set)
        self.discovered_calls = set()
        self.all_instructions = {}

    def in_data_region(self, offset):
        for start, end in DATA_IN_CODE:
            if start <= offset < end:
                return True
        return False

    def decode_range(self, start, end):
        offset = start
        instructions = []
        while offset < end:
            if self.in_data_region(offset):
                offset += 2
                continue
            inst = self.decoder.decode(self.data, offset)
            if inst is None:
                break
            instructions.append(inst)
            self.all_instructions[offset] = inst
            offset += inst.size
        return instructions

    def trace_function(self, entry, name=None, max_insns=2000):
        if entry in self.functions:
            return
        func = {
            'entry': entry,
            'name': name or f"sub_{entry:06X}",
            'instructions': [],
            'calls': set(),
            'end': entry,
            'size': 0,
        }
        self.functions[entry] = func

        offset = entry
        seen = set()
        branch_targets = set()
        count = 0
        furthest = entry

        while offset < CODE_END and count < max_insns:
            if offset in seen:
                break
            if self.in_data_region(offset):
                break
            seen.add(offset)

            inst = self.decoder.decode(self.data, offset)
            if inst is None:
                break

            func['instructions'].append(inst)
            self.all_instructions[offset] = inst
            count += 1

            if offset + inst.size > furthest:
                furthest = offset + inst.size

            if inst.is_call and inst.target is not None:
                target_off = inst.target - FLASH_BASE
                if CODE_START <= target_off < CODE_END:
                    func['calls'].add(target_off)
                    self.discovered_calls.add(target_off)
                    self.call_graph[entry].add(target_off)
                    self.callers[target_off].add(entry)

            if inst.is_jump and inst.target is not None:
                target_off = inst.target - FLASH_BASE
                if entry <= target_off < CODE_END:
                    branch_targets.add(target_off)

            if inst.is_return:
                remaining = branch_targets - seen
                if remaining:
                    offset = min(remaining)
                    continue
                break

            if inst.is_jump and not inst.is_cond and inst.target is not None:
                target_off = inst.target - FLASH_BASE
                if target_off in seen or target_off < entry:
                    remaining = branch_targets - seen
                    if remaining:
                        offset = min(remaining)
                        continue
                    break
                offset = target_off
                continue

            offset += inst.size

        func['end'] = furthest
        func['size'] = furthest - entry

    def find_all_functions(self):
        for off, name in sorted(KNOWN_FUNCTIONS.items()):
            self.trace_function(off, name)

        new_found = True
        while new_found:
            new_found = False
            pending = self.discovered_calls - set(self.functions.keys())
            for off in sorted(pending):
                if CODE_START <= off < CODE_END and not self.in_data_region(off):
                    self.trace_function(off)
                    new_found = True

    def scan_for_prologue(self):
        """Scan code section for push {rets, ...} patterns to find functions"""
        extra = []
        for offset in range(CODE_START, CODE_END, 2):
            if self.in_data_region(offset):
                continue
            if offset + 2 > len(self.data):
                break
            w = struct.unpack_from('<H', self.data, offset)[0]
            top9 = (w >> 4) & 0x1FF
            if top9 == 0x021:
                if offset not in self.functions:
                    extra.append(offset)
        return extra

    def find_adc_values(self):
        results = []
        for name, (lo, hi) in BUTTON_ADC.items():
            for val in range(lo, hi + 1):
                le16 = struct.pack('<H', val)
                le32 = struct.pack('<I', val)
                pos = 0
                while True:
                    idx = self.data.find(le16, pos, CODE_END)
                    if idx == -1:
                        break
                    if not self.in_data_region(idx):
                        results.append((name, val, idx, 16))
                    pos = idx + 1
                pos = 0
                while True:
                    idx = self.data.find(le32, pos, CODE_END)
                    if idx == -1:
                        break
                    if not self.in_data_region(idx):
                        results.append((name, val, idx, 32))
                    pos = idx + 1
        return results

    def analyze_table(self, offset, count, entry_size=4):
        entries = []
        for i in range(count):
            pos = offset + i * entry_size
            if pos + entry_size > len(self.data):
                break
            if entry_size == 4:
                val = struct.unpack_from('<I', self.data, pos)[0]
            elif entry_size == 2:
                val = struct.unpack_from('<H', self.data, pos)[0]
            else:
                val = self.data[pos]
            entries.append(val)

        is_ptrs = True
        ptr_range = (FLASH_BASE + CODE_START, FLASH_BASE + CODE_END)
        ptr_count = 0
        for v in entries:
            if ptr_range[0] <= v <= ptr_range[1]:
                ptr_count += 1
        if ptr_count < len(entries) * 0.3:
            is_ptrs = False

        return {
            'offset': offset,
            'count': len(entries),
            'entries': entries,
            'is_pointer_table': is_ptrs,
            'ptr_ratio': ptr_count / max(1, len(entries)),
        }

    def find_immediate_values(self, target_vals):
        """Find where specific values appear as immediate operands in decoded instructions"""
        hits = []
        for off, inst in sorted(self.all_instructions.items()):
            if not inst.decoded:
                continue
            if inst.mnemonic in ('movs', 'movz', 'movl', 'movh', 'add'):
                for part in inst.operands.split(','):
                    part = part.strip()
                    try:
                        if part.startswith('0x'):
                            v = int(part, 16)
                        elif part.lstrip('-').isdigit():
                            v = int(part)
                        else:
                            continue
                        if v in target_vals:
                            hits.append((off, inst, v))
                    except ValueError:
                        continue
        return hits


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Pi32 Firmware Disassembler")
    parser.add_argument("command", choices=[
        "functions", "disasm", "adc", "tables", "callgraph",
        "prologues", "cmp-scan", "report", "all"
    ])
    parser.add_argument("--addr", type=lambda x: int(x, 0), help="Address for disasm")
    parser.add_argument("--count", type=int, default=50, help="Instruction count")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    dump = _find_dump()
    if not dump.exists():
        print(f"Firmware not found: {dump}")
        print("Set KC02_DUMP env var or place original.bin in firmware/")
        sys.exit(1)
    data = dump.read_bytes()
    print(f"Loaded {dump} ({len(data)} bytes)")

    analyzer = FirmwareAnalyzer(data)

    if args.command == "disasm":
        addr = args.addr or (FLASH_BASE + APP_START)
        off = addr - FLASH_BASE if addr >= FLASH_BASE else addr
        print(f"\nDisassembly at 0x{FLASH_BASE + off:08X}:")
        instructions = []
        pos = off
        for _ in range(args.count):
            inst = analyzer.decoder.decode(data, pos)
            if inst is None:
                break
            instructions.append(inst)
            pos += inst.size
        decoded = sum(1 for i in instructions if i.decoded)
        for inst in instructions:
            print(inst)
        print(f"\n{decoded}/{len(instructions)} instructions decoded "
              f"({100*decoded/max(1,len(instructions)):.1f}%)")

    elif args.command == "functions":
        analyzer.find_all_functions()
        print(f"\n{'='*70}")
        print(f"FUNCTIONS FOUND: {len(analyzer.functions)}")
        print(f"{'='*70}")
        for off in sorted(analyzer.functions):
            f = analyzer.functions[off]
            ncalls = len(f['calls'])
            ninsns = len(f['instructions'])
            decoded = sum(1 for i in f['instructions'] if i.decoded)
            pct = 100 * decoded / max(1, ninsns)
            callercount = len(analyzer.callers.get(off, set()))
            print(f"  0x{FLASH_BASE+off:08X}  {f['name']:<30s}  "
                  f"{f['size']:5d}B  {ninsns:3d} insns  "
                  f"{pct:5.1f}% decoded  {ncalls} calls  {callercount} callers")

    elif args.command == "prologues":
        analyzer.find_all_functions()
        extra = analyzer.scan_for_prologue()
        known = set(analyzer.functions.keys())
        new = [e for e in extra if e not in known]
        print(f"\nPush {{rets}} prologue scan: {len(extra)} total, {len(new)} undiscovered")
        for off in sorted(new)[:100]:
            print(f"  0x{FLASH_BASE+off:08X}  (new)")
        if len(new) > 100:
            print(f"  ... and {len(new)-100} more")

        for off in sorted(new):
            analyzer.trace_function(off)
        total = len(analyzer.functions)
        print(f"\nTotal functions after prologue scan: {total}")

    elif args.command == "adc":
        print("\nSearching for button ADC threshold values...")
        results = analyzer.find_adc_values()
        by_button = defaultdict(list)
        for name, val, off, bits in results:
            by_button[name].append((val, off, bits))
        for name in BUTTON_ADC:
            hits = by_button.get(name, [])
            print(f"\n  {name.upper()} (ADC {BUTTON_ADC[name][0]}-{BUTTON_ADC[name][1]}):")
            if not hits:
                print(f"    No direct matches in code section")
            for val, off, bits in sorted(hits, key=lambda x: x[1]):
                region = "CODE" if not analyzer.in_data_region(off) else "DATA"
                print(f"    0x{off:06X}: value={val} ({bits}-bit) [{region}]")

    elif args.command == "tables":
        tables = [
            (0x082A4C, 148, "dispatch_148"),
            (0x0844E4, 191, "dispatch_191"),
            (0x0CA660, 399, "lookup_399"),
            (0x07E19C, 5, "mode_callback_table"),
        ]
        for off, count, name in tables:
            print(f"\n{'='*60}")
            print(f"TABLE: {name} at 0x{off:06X} ({count} entries)")
            print(f"{'='*60}")
            result = analyzer.analyze_table(off, count)
            if result['is_pointer_table']:
                print(f"  Type: FUNCTION POINTER TABLE ({result['ptr_ratio']*100:.0f}% valid ptrs)")
                for i, v in enumerate(result['entries'][:20]):
                    code_off = v - FLASH_BASE
                    fname = KNOWN_FUNCTIONS.get(code_off, "")
                    print(f"    [{i:3d}] 0x{v:08X}  {fname}")
                if count > 20:
                    print(f"    ... ({count-20} more)")
            else:
                print(f"  Type: DATA TABLE (only {result['ptr_ratio']*100:.0f}% look like ptrs)")
                for i, v in enumerate(result['entries'][:20]):
                    print(f"    [{i:3d}] 0x{v:08X} ({v})")
                if count > 20:
                    print(f"    ... ({count-20} more)")

    elif args.command == "callgraph":
        analyzer.find_all_functions()
        print(f"\nCALL GRAPH ({len(analyzer.functions)} functions):")
        for off in sorted(analyzer.functions):
            f = analyzer.functions[off]
            if f['calls']:
                callees = []
                for c in sorted(f['calls']):
                    cn = analyzer.functions.get(c, {}).get('name', f'sub_{c:06X}')
                    callees.append(cn)
                print(f"  {f['name']} -> {', '.join(callees)}")

    elif args.command == "cmp-scan":
        print("\nScanning all code for CMP instructions...")
        adc_vals = set()
        for name, (lo, hi) in BUTTON_ADC.items():
            for v in range(lo, hi + 1):
                adc_vals.add(v)

        offset = CODE_START
        cmp_hits = []
        while offset < CODE_END:
            if analyzer.in_data_region(offset):
                offset += 2
                continue
            inst = analyzer.decoder.decode(data, offset)
            if inst is None:
                break
            if inst.decoded and inst.mnemonic == "cmp":
                for part in inst.operands.split(','):
                    part = part.strip()
                    try:
                        if part.lstrip('-').isdigit():
                            v = int(part)
                            if v in adc_vals:
                                cmp_hits.append((offset, inst, v))
                            elif 10 <= v <= 1023:
                                cmp_hits.append((offset, inst, v))
                    except ValueError:
                        pass
            offset += inst.size

        if cmp_hits:
            print(f"\nCMP instructions with ADC-range values (10-1023):")
            for off, inst, val in sorted(cmp_hits):
                match = ""
                for name, (lo, hi) in BUTTON_ADC.items():
                    if lo <= val <= hi:
                        match = f"  *** {name.upper()} BUTTON ***"
                        break
                print(f"  0x{off:06X}: {inst.mnemonic} {inst.operands}  (val={val}){match}")
        else:
            print("  No CMP instructions with ADC-range values found")

    elif args.command in ("report", "all"):
        print("\n" + "=" * 70)
        print("KC02 FIRMWARE ANALYSIS REPORT")
        print("=" * 70)

        # Functions
        analyzer.find_all_functions()
        extra_prologues = analyzer.scan_for_prologue()
        for off in extra_prologues:
            if off not in analyzer.functions:
                analyzer.trace_function(off)

        total_funcs = len(analyzer.functions)
        total_insns = sum(len(f['instructions']) for f in analyzer.functions.values())
        total_decoded = sum(
            sum(1 for i in f['instructions'] if i.decoded)
            for f in analyzer.functions.values()
        )
        decode_pct = 100 * total_decoded / max(1, total_insns)

        print(f"\n── FUNCTION DISCOVERY ──")
        print(f"  Known entry points: {len(KNOWN_FUNCTIONS)}")
        print(f"  Discovered via calls: {len(analyzer.discovered_calls)}")
        print(f"  Discovered via prologue scan: {len(extra_prologues)}")
        print(f"  Total functions: {total_funcs}")
        print(f"  Total instructions traced: {total_insns}")
        print(f"  Instructions decoded: {total_decoded} ({decode_pct:.1f}%)")

        # Named functions
        print(f"\n── NAMED FUNCTIONS ──")
        for off in sorted(KNOWN_FUNCTIONS):
            f = analyzer.functions.get(off)
            if f:
                ninsns = len(f['instructions'])
                ncalls = len(f['calls'])
                print(f"  0x{FLASH_BASE+off:08X}  {f['name']:<30s}  "
                      f"{f['size']:4d}B  {ninsns:3d}i  calls:{ncalls}")

        # Largest discovered functions
        print(f"\n── LARGEST DISCOVERED FUNCTIONS ──")
        by_size = sorted(analyzer.functions.values(), key=lambda f: f['size'], reverse=True)
        for f in by_size[:20]:
            if f['entry'] not in KNOWN_FUNCTIONS:
                print(f"  0x{FLASH_BASE+f['entry']:08X}  {f['name']:<30s}  "
                      f"{f['size']:5d}B  {len(f['instructions']):4d}i")

        # Most-called functions
        print(f"\n── MOST-CALLED FUNCTIONS ──")
        by_callers = sorted(analyzer.callers.items(), key=lambda x: len(x[1]), reverse=True)
        for off, callers_set in by_callers[:20]:
            name = analyzer.functions.get(off, {}).get('name', f'sub_{off:06X}')
            print(f"  0x{FLASH_BASE+off:08X}  {name:<30s}  called by {len(callers_set)} functions")

        # ADC
        print(f"\n── BUTTON ADC VALUES ──")
        adc_results = analyzer.find_adc_values()
        by_button = defaultdict(list)
        for name, val, off, bits in adc_results:
            by_button[name].append((val, off, bits))
        for name in BUTTON_ADC:
            hits = by_button.get(name, [])
            code_hits = [(v, o, b) for v, o, b in hits if not analyzer.in_data_region(o)]
            print(f"  {name.upper()} ({BUTTON_ADC[name][0]}-{BUTTON_ADC[name][1]}): "
                  f"{len(code_hits)} matches in code")
            for val, off, bits in sorted(code_hits)[:5]:
                print(f"    0x{off:06X}: {val} ({bits}-bit)")

        # Tables
        print(f"\n── UNKNOWN DATA TABLES ──")
        tables = [
            (0x082A4C, 148, "dispatch_148"),
            (0x0844E4, 191, "dispatch_191"),
            (0x0CA660, 399, "lookup_399"),
        ]
        for toff, count, name in tables:
            result = analyzer.analyze_table(toff, count)
            ttype = "FUNCTION PTRS" if result['is_pointer_table'] else "DATA"
            print(f"  0x{toff:06X}  {name:<20s}  {count} entries  {ttype}")

        # Modification opportunities
        print(f"\n── MODIFICATION OPPORTUNITIES ──")
        print(f"  [SAFE] Dispatch table swaps (0x07DF08): redirect UI states")
        print(f"  [SAFE] Menu coordinates (0x07E160): rearrange icon positions")
        print(f"  [SAFE] Menu asset indices (0x07E178): swap icon graphics")
        print(f"  [SAFE] String modifications (0x07DE1C+): change displayed text")
        print(f"  [SAFE] Init callback order (0x07E000): reorder boot sequence")
        print(f"  [SAFE] Button remapping (0x0C4C80): swap key_ids in ADC table")
        print(f"  [TEST] Mode callback table (0x07E19C): change mode transitions")
        print(f"  [RISK] Code patches: REQUIRES disassembly confirmation")
        print(f"         (confirmed: 0x002F6C patch breaks SD update routine)")

        if args.json:
            out = {
                'total_functions': total_funcs,
                'total_instructions': total_insns,
                'decode_rate': decode_pct,
                'functions': {
                    hex(FLASH_BASE + off): {
                        'name': f['name'],
                        'size': f['size'],
                        'calls': [hex(FLASH_BASE + c) for c in sorted(f['calls'])],
                    }
                    for off, f in sorted(analyzer.functions.items())
                },
            }
            outpath = PROJECT / "docs" / "disasm_report.json"
            outpath.write_text(json.dumps(out, indent=2))
            print(f"\nJSON report saved to {outpath}")


if __name__ == "__main__":
    main()
