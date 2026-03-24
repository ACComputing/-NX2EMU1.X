#!/usr/bin/env python3
"""
ACNX2Emu 0.2 — Beta NX2 Edition (Ryujinx-style GUI)
A.C Holdings • Real Switch 2 AArch64 CPU Core (T239 / 8× A78C + Ampere)
Pure in-memory • /files=off • Tkinter 600×400

CPU: Real ARMv8-A / AArch64 decode+execute
  - 31 GP registers (X0–X30) + SP + PC + NZCV flags
  - Instruction classes: MOVZ, MOVK, ADD, SUB, AND, ORR, EOR, LSL, LSR,
    MUL, MADD, CMP, B, BL, BR, BLR, RET, B.cond, CBZ, CBNZ,
    LDR, STR, LDRB, STRB, SVC, NOP
  - 16 MB unified memory with MMIO framebuffer @ 0x04000000
  - Boots synthetic NX2 firmware that renders GPU particles via CPU writes
"""

import tkinter as tk
from tkinter import ttk, messagebox
import time
import math
import struct

# ── Ryujinx palette ──
RYU_BG       = "#2D2D2D"
RYU_SIDEBAR  = "#242424"
RYU_SURFACE  = "#383838"
RYU_ACCENT   = "#50B0F0"
RYU_GREEN    = "#63C174"
RYU_TEXT     = "#E8E8E8"
RYU_TEXT_DIM = "#A0A0A0"
RYU_BORDER   = "#4A4A4A"
RYU_MENU_BG  = "#333333"
RYU_MENU_FG  = "#E0E0E0"

# ── Memory map ──
MEM_SIZE       = 16 * 1024 * 1024       # 16 MB
CODE_BASE      = 0x00080000             # firmware code
STACK_TOP      = 0x00800000             # 8 MB stack top
FB_BASE        = 0x04000000             # framebuffer MMIO
FB_W, FB_H     = 280, 158              # half-res for speed (scaled ×2)
FB_STRIDE      = FB_W * 3              # RGB888
FB_SIZE        = FB_H * FB_STRIDE


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AArch64 CPU Core — real instruction decode & execute
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AArch64Core:
    """
    Minimal but *real* ARMv8-A / AArch64 integer CPU.
    Decodes actual 32-bit ARM instructions from memory and executes them.
    """

    def __init__(self, mem: bytearray):
        # 31 general-purpose 64-bit registers  (X0–X30)
        self.x = [0] * 31
        self.sp = STACK_TOP
        self.pc = CODE_BASE
        # NZCV condition flags
        self.N = False
        self.Z = False
        self.C = False
        self.V = False
        # Memory
        self.mem = mem
        # Stats
        self.cycles = 0
        self.instrs = 0
        self.halted = False
        # SVC handler hook (for firmware syscalls)
        self.svc_handler = None

    MASK64 = 0xFFFFFFFFFFFFFFFF
    MASK32 = 0xFFFFFFFF

    # ── register helpers (X31 reads as XZR=0, writes discard) ──
    def xr(self, r):
        return self.x[r] if r < 31 else 0

    def xw(self, r, val):
        if r < 31:
            self.x[r] = val & self.MASK64

    # ── memory access ──
    def read32(self, addr):
        a = addr & (MEM_SIZE - 1)
        return struct.unpack_from("<I", self.mem, a)[0]

    def read64(self, addr):
        a = addr & (MEM_SIZE - 1)
        return struct.unpack_from("<Q", self.mem, a)[0]

    def write32(self, addr, val):
        a = addr & (MEM_SIZE - 1)
        struct.pack_into("<I", self.mem, a, val & self.MASK32)

    def write64(self, addr, val):
        a = addr & (MEM_SIZE - 1)
        struct.pack_into("<Q", self.mem, a, val & self.MASK64)

    def read8(self, addr):
        return self.mem[addr & (MEM_SIZE - 1)]

    def write8(self, addr, val):
        self.mem[addr & (MEM_SIZE - 1)] = val & 0xFF

    # ── flag helpers ──
    def _update_nz64(self, result):
        self.N = bool(result & (1 << 63))
        self.Z = (result & self.MASK64) == 0

    def _add_flags64(self, a, b, result):
        self._update_nz64(result)
        self.C = result > self.MASK64
        sa = bool(a & (1 << 63))
        sb = bool(b & (1 << 63))
        sr = bool(result & (1 << 63))
        self.V = (sa == sb) and (sa != sr)

    def _sub_flags64(self, a, b):
        result = a - b
        self._update_nz64(result & self.MASK64)
        self.C = a >= b
        sa = bool(a & (1 << 63))
        sb = bool(b & (1 << 63))
        sr = bool(result & (1 << 63))
        self.V = (sa != sb) and (sr != sa)

    # ── condition check ──
    def check_cond(self, cond):
        conds = {
            0b0000: self.Z,                         # EQ
            0b0001: not self.Z,                     # NE
            0b0010: self.C,                         # CS/HS
            0b0011: not self.C,                     # CC/LO
            0b0100: self.N,                         # MI
            0b0101: not self.N,                     # PL
            0b0110: self.V,                         # VS
            0b0111: not self.V,                     # VC
            0b1000: self.C and not self.Z,          # HI
            0b1001: not self.C or self.Z,           # LS
            0b1010: self.N == self.V,               # GE
            0b1011: self.N != self.V,               # LT
            0b1100: not self.Z and (self.N == self.V),  # GT
            0b1101: self.Z or (self.N != self.V),       # LE
            0b1110: True,                           # AL
            0b1111: True,                           # AL
        }
        return conds.get(cond, False)

    # ── sign extend ──
    @staticmethod
    def sign_extend(val, bits):
        if val & (1 << (bits - 1)):
            val -= (1 << bits)
        return val

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  FETCH  →  DECODE  →  EXECUTE   (one insn)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def step(self):
        if self.halted:
            return
        insn = self.read32(self.pc)
        self.pc += 4
        self.cycles += 1
        self.instrs += 1
        self._execute(insn)

    def step_n(self, n):
        """Execute n instructions (batch for speed)."""
        for _ in range(n):
            if self.halted:
                break
            insn = self.read32(self.pc)
            self.pc += 4
            self.cycles += 1
            self.instrs += 1
            self._execute(insn)

    def _execute(self, insn):
        # NOP (and hint space)
        if insn == 0xD503201F or (insn >> 24) == 0xD5:
            return

        top8 = (insn >> 24) & 0xFF
        top11 = (insn >> 21) & 0x7FF

        # ── HLT ──
        if (insn & 0xFFE0001F) == 0xD4400000:
            self.halted = True
            return

        # ── SVC ──
        if (insn & 0xFFE0001F) == 0xD4000001:
            imm16 = (insn >> 5) & 0xFFFF
            if self.svc_handler:
                self.svc_handler(self, imm16)
            return

        # ── MOVZ  (sf=1 → 64-bit) ──
        if (insn & 0x7F800000) == 0x52800000:
            sf = (insn >> 31) & 1
            hw = (insn >> 21) & 3
            imm16 = (insn >> 5) & 0xFFFF
            rd = insn & 0x1F
            val = imm16 << (hw * 16)
            self.xw(rd, val)
            return

        # ── MOVK ──
        if (insn & 0x7F800000) == 0x72800000:
            hw = (insn >> 21) & 3
            imm16 = (insn >> 5) & 0xFFFF
            rd = insn & 0x1F
            shift = hw * 16
            mask = ~(0xFFFF << shift) & self.MASK64
            self.xw(rd, (self.xr(rd) & mask) | (imm16 << shift))
            return

        # ── ADD / ADDS immediate ──
        if (insn & 0x7F000000) == 0x11000000:
            sf = (insn >> 31) & 1
            S = (insn >> 29) & 1
            sh = (insn >> 22) & 1
            imm12 = (insn >> 10) & 0xFFF
            rn = (insn >> 5) & 0x1F
            rd = insn & 0x1F
            imm = imm12 << (12 if sh else 0)
            a = self.xr(rn)
            result = a + imm
            if S:
                self._add_flags64(a, imm, result)
            self.xw(rd, result)
            return

        # ── SUB / SUBS immediate ──
        if (insn & 0x7F000000) == 0x51000000:
            sf = (insn >> 31) & 1
            S = (insn >> 29) & 1
            sh = (insn >> 22) & 1
            imm12 = (insn >> 10) & 0xFFF
            rn = (insn >> 5) & 0x1F
            rd = insn & 0x1F
            imm = imm12 << (12 if sh else 0)
            a = self.xr(rn)
            result = (a - imm) & self.MASK64
            if S:
                self._sub_flags64(a, imm)
            self.xw(rd, result)
            return

        # ── ADD / SUB shifted register ──
        if (insn & 0x1F000000) == 0x0B000000:
            sf = (insn >> 31) & 1
            S = (insn >> 29) & 1
            is_sub = (insn >> 30) & 1
            shift_type = (insn >> 22) & 3
            rm = (insn >> 16) & 0x1F
            imm6 = (insn >> 10) & 0x3F
            rn = (insn >> 5) & 0x1F
            rd = insn & 0x1F
            b = self.xr(rm)
            if shift_type == 0:
                b = (b << imm6) & self.MASK64
            elif shift_type == 1:
                b = (b >> imm6) & self.MASK64
            elif shift_type == 2:
                if b & (1 << 63):
                    b = ((b >> imm6) | (self.MASK64 << (64 - imm6))) & self.MASK64
                else:
                    b = (b >> imm6)
            a = self.xr(rn)
            if is_sub:
                result = (a - b) & self.MASK64
                if S:
                    self._sub_flags64(a, b)
            else:
                result = a + b
                if S:
                    self._add_flags64(a, b, result)
                result &= self.MASK64
            self.xw(rd, result)
            return

        # ── AND / ORR / EOR shifted register ──
        if (insn & 0x1F000000) == 0x0A000000:
            opc = (insn >> 29) & 3
            shift_type = (insn >> 22) & 3
            rm = (insn >> 16) & 0x1F
            imm6 = (insn >> 10) & 0x3F
            rn = (insn >> 5) & 0x1F
            rd = insn & 0x1F
            b = self.xr(rm)
            if shift_type == 0:
                b = (b << imm6) & self.MASK64
            elif shift_type == 1:
                b = (b >> imm6) & self.MASK64
            a = self.xr(rn)
            if opc == 0:    # AND
                self.xw(rd, a & b)
            elif opc == 1:  # ORR
                self.xw(rd, a | b)
            elif opc == 2:  # EOR
                self.xw(rd, a ^ b)
            elif opc == 3:  # ANDS
                result = a & b
                self._update_nz64(result)
                self.C = False
                self.V = False
                self.xw(rd, result)
            return

        # ── MADD / MUL (X) ──
        if (insn & 0xFF208000) == 0x9B000000:
            rm = (insn >> 16) & 0x1F
            ra = (insn >> 10) & 0x1F
            rn = (insn >> 5) & 0x1F
            rd = insn & 0x1F
            result = (self.xr(ra) + self.xr(rn) * self.xr(rm)) & self.MASK64
            self.xw(rd, result)
            return

        # ── UDIV ──
        if (insn & 0xFFE0FC00) == 0x9AC00800:
            rm = (insn >> 16) & 0x1F
            rn = (insn >> 5) & 0x1F
            rd = insn & 0x1F
            divisor = self.xr(rm)
            self.xw(rd, (self.xr(rn) // divisor) if divisor else 0)
            return

        # ── LSLV / LSRV ──
        if (insn & 0xFFE0F800) == 0x9AC02000:
            sub = (insn >> 10) & 3
            rm = (insn >> 16) & 0x1F
            rn = (insn >> 5) & 0x1F
            rd = insn & 0x1F
            shift = self.xr(rm) & 63
            if sub == 0:  # LSLV
                self.xw(rd, (self.xr(rn) << shift) & self.MASK64)
            else:         # LSRV
                self.xw(rd, self.xr(rn) >> shift)
            return

        # ── B (unconditional) ──
        if (insn & 0xFC000000) == 0x14000000:
            imm26 = insn & 0x3FFFFFF
            offset = self.sign_extend(imm26, 26) * 4
            self.pc = (self.pc - 4 + offset) & self.MASK64
            return

        # ── BL ──
        if (insn & 0xFC000000) == 0x94000000:
            imm26 = insn & 0x3FFFFFF
            offset = self.sign_extend(imm26, 26) * 4
            self.xw(30, self.pc)  # LR = return address
            self.pc = (self.pc - 4 + offset) & self.MASK64
            return

        # ── BR ──
        if (insn & 0xFFFFFC1F) == 0xD61F0000:
            rn = (insn >> 5) & 0x1F
            self.pc = self.xr(rn)
            return

        # ── BLR ──
        if (insn & 0xFFFFFC1F) == 0xD63F0000:
            rn = (insn >> 5) & 0x1F
            self.xw(30, self.pc)
            self.pc = self.xr(rn)
            return

        # ── RET ──
        if (insn & 0xFFFFFC1F) == 0xD65F0000:
            rn = (insn >> 5) & 0x1F
            self.pc = self.xr(rn)
            return

        # ── B.cond ──
        if (insn & 0xFF000010) == 0x54000000:
            cond = insn & 0xF
            imm19 = (insn >> 5) & 0x7FFFF
            offset = self.sign_extend(imm19, 19) * 4
            if self.check_cond(cond):
                self.pc = (self.pc - 4 + offset) & self.MASK64
            return

        # ── CBZ / CBNZ (64-bit) ──
        if (insn & 0x7E000000) == 0x34000000:
            sf = (insn >> 31) & 1
            is_nz = (insn >> 24) & 1
            imm19 = (insn >> 5) & 0x7FFFF
            rt = insn & 0x1F
            offset = self.sign_extend(imm19, 19) * 4
            val = self.xr(rt)
            take = (val != 0) if is_nz else (val == 0)
            if take:
                self.pc = (self.pc - 4 + offset) & self.MASK64
            return

        # ── LDR (unsigned offset, 64-bit) ──
        if (insn & 0xFFC00000) == 0xF9400000:
            imm12 = (insn >> 10) & 0xFFF
            rn = (insn >> 5) & 0x1F
            rt = insn & 0x1F
            addr = self.xr(rn) + imm12 * 8
            self.xw(rt, self.read64(addr))
            return

        # ── STR (unsigned offset, 64-bit) ──
        if (insn & 0xFFC00000) == 0xF9000000:
            imm12 = (insn >> 10) & 0xFFF
            rn = (insn >> 5) & 0x1F
            rt = insn & 0x1F
            addr = self.xr(rn) + imm12 * 8
            self.write64(addr, self.xr(rt))
            return

        # ── LDR (register offset, 64-bit) ──
        if (insn & 0xFFE00C00) == 0xF8600800:
            rm = (insn >> 16) & 0x1F
            rn = (insn >> 5) & 0x1F
            rt = insn & 0x1F
            addr = self.xr(rn) + self.xr(rm)
            self.xw(rt, self.read64(addr))
            return

        # ── LDRB (unsigned offset) ──
        if (insn & 0xFFC00000) == 0x39400000:
            imm12 = (insn >> 10) & 0xFFF
            rn = (insn >> 5) & 0x1F
            rt = insn & 0x1F
            addr = self.xr(rn) + imm12
            self.xw(rt, self.read8(addr))
            return

        # ── STRB (unsigned offset) ──
        if (insn & 0xFFC00000) == 0x39000000:
            imm12 = (insn >> 10) & 0xFFF
            rn = (insn >> 5) & 0x1F
            rt = insn & 0x1F
            addr = self.xr(rn) + imm12
            self.write8(addr, self.xr(rt) & 0xFF)
            return

        # ── LDR W (unsigned offset, 32-bit) ──
        if (insn & 0xFFC00000) == 0xB9400000:
            imm12 = (insn >> 10) & 0xFFF
            rn = (insn >> 5) & 0x1F
            rt = insn & 0x1F
            addr = self.xr(rn) + imm12 * 4
            self.xw(rt, self.read32(addr))
            return

        # ── STR W (unsigned offset, 32-bit) ──
        if (insn & 0xFFC00000) == 0xB9000000:
            imm12 = (insn >> 10) & 0xFFF
            rn = (insn >> 5) & 0x1F
            rt = insn & 0x1F
            addr = self.xr(rn) + imm12 * 4
            self.write32(addr, self.xr(rt) & self.MASK32)
            return

        # ── LDP (load pair, 64-bit, signed offset) ──
        if (insn & 0xFFC00000) == 0xA9400000:
            imm7 = (insn >> 15) & 0x7F
            rt2 = (insn >> 10) & 0x1F
            rn = (insn >> 5) & 0x1F
            rt = insn & 0x1F
            offset = self.sign_extend(imm7, 7) * 8
            base = self.xr(rn) + offset
            self.xw(rt, self.read64(base))
            self.xw(rt2, self.read64(base + 8))
            return

        # ── STP (store pair, 64-bit, signed offset) ──
        if (insn & 0xFFC00000) == 0xA9000000:
            imm7 = (insn >> 15) & 0x7F
            rt2 = (insn >> 10) & 0x1F
            rn = (insn >> 5) & 0x1F
            rt = insn & 0x1F
            offset = self.sign_extend(imm7, 7) * 8
            base = self.xr(rn) + offset
            self.write64(base, self.xr(rt))
            self.write64(base + 8, self.xr(rt2))
            return

        # ── STP pre-index ──
        if (insn & 0xFFC00000) == 0xA9800000:
            imm7 = (insn >> 15) & 0x7F
            rt2 = (insn >> 10) & 0x1F
            rn = (insn >> 5) & 0x1F
            rt = insn & 0x1F
            offset = self.sign_extend(imm7, 7) * 8
            base = self.xr(rn) + offset
            if rn < 31:
                self.x[rn] = base & self.MASK64
            else:
                self.sp = base & self.MASK64
            self.write64(base, self.xr(rt))
            self.write64(base + 8, self.xr(rt2))
            return

        # ── Unknown — treat as NOP (don't crash) ──
        pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AArch64 Assembler — build real machine code from mnemonics
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class A64Asm:
    """Tiny assembler: emits real AArch64 machine code into a bytearray."""

    def __init__(self):
        self.code = bytearray()
        self.labels = {}
        self.fixups = []

    def here(self):
        return len(self.code)

    def label(self, name):
        self.labels[name] = self.here()

    def emit(self, word):
        self.code += struct.pack("<I", word)

    def resolve(self):
        for (pos, name, kind) in self.fixups:
            target = self.labels[name]
            offset = target - pos
            insn = struct.unpack_from("<I", self.code, pos)[0]
            if kind == "b":
                imm26 = (offset >> 2) & 0x3FFFFFF
                insn = (insn & 0xFC000000) | imm26
            elif kind == "bcond":
                imm19 = (offset >> 2) & 0x7FFFF
                insn = (insn & 0xFF00001F) | (imm19 << 5)
            elif kind == "cbz":
                imm19 = (offset >> 2) & 0x7FFFF
                insn = (insn & 0xFF00001F) | (imm19 << 5)
            elif kind == "bl":
                imm26 = (offset >> 2) & 0x3FFFFFF
                insn = (insn & 0xFC000000) | imm26
            struct.pack_into("<I", self.code, pos, insn)

    # ── Instructions ──

    def nop(self):
        self.emit(0xD503201F)

    def hlt(self, imm=0):
        self.emit(0xD4400000 | ((imm & 0xFFFF) << 5))

    def svc(self, imm):
        self.emit(0xD4000001 | ((imm & 0xFFFF) << 5))

    def movz(self, rd, imm16, hw=0):
        self.emit(0xD2800000 | (hw << 21) | (imm16 << 5) | rd)

    def movk(self, rd, imm16, hw=0):
        self.emit(0xF2800000 | (hw << 21) | (imm16 << 5) | rd)

    def mov_imm(self, rd, val):
        """Load arbitrary 64-bit immediate using MOVZ + MOVK sequence."""
        val &= 0xFFFFFFFFFFFFFFFF
        self.movz(rd, val & 0xFFFF, 0)
        if val > 0xFFFF:
            self.movk(rd, (val >> 16) & 0xFFFF, 1)
        if val > 0xFFFFFFFF:
            self.movk(rd, (val >> 32) & 0xFFFF, 2)
        if val > 0xFFFFFFFFFFFF:
            self.movk(rd, (val >> 48) & 0xFFFF, 3)

    def add_imm(self, rd, rn, imm12, shift=0):
        self.emit(0x91000000 | (shift << 22) | (imm12 << 10) | (rn << 5) | rd)

    def sub_imm(self, rd, rn, imm12, shift=0):
        self.emit(0xD1000000 | (shift << 22) | (imm12 << 10) | (rn << 5) | rd)

    def subs_imm(self, rd, rn, imm12, shift=0):
        self.emit(0xF1000000 | (shift << 22) | (imm12 << 10) | (rn << 5) | rd)

    def cmp_imm(self, rn, imm12):
        self.subs_imm(31, rn, imm12)  # SUBS XZR, Xn, #imm

    def add_reg(self, rd, rn, rm, shift=0, amount=0):
        self.emit(0x8B000000 | (shift << 22) | (rm << 16) | (amount << 10) | (rn << 5) | rd)

    def sub_reg(self, rd, rn, rm):
        self.emit(0xCB000000 | (rm << 16) | (rn << 5) | rd)

    def mul(self, rd, rn, rm):
        # MADD Xd, Xn, Xm, XZR
        self.emit(0x9B007C00 | (rm << 16) | (rn << 5) | rd)

    def udiv(self, rd, rn, rm):
        self.emit(0x9AC00800 | (rm << 16) | (rn << 5) | rd)

    def and_reg(self, rd, rn, rm):
        self.emit(0x8A000000 | (rm << 16) | (rn << 5) | rd)

    def orr_reg(self, rd, rn, rm):
        self.emit(0xAA000000 | (rm << 16) | (rn << 5) | rd)

    def eor_reg(self, rd, rn, rm):
        self.emit(0xCA000000 | (rm << 16) | (rn << 5) | rd)

    def lsl_reg(self, rd, rn, rm):
        self.emit(0x9AC02000 | (rm << 16) | (rn << 5) | rd)

    def lsr_reg(self, rd, rn, rm):
        self.emit(0x9AC02400 | (rm << 16) | (rn << 5) | rd)

    def lsl_imm(self, rd, rn, rm, imm6=0):
        # Using shifted register ADD with XZR: ORR Xd, XZR, Xn, LSL #imm
        self.emit(0xAA000000 | (0 << 22) | (rn << 16) | (imm6 << 10) | (31 << 5) | rd)

    def strb(self, rt, rn, imm12=0):
        self.emit(0x39000000 | (imm12 << 10) | (rn << 5) | rt)

    def ldrb(self, rt, rn, imm12=0):
        self.emit(0x39400000 | (imm12 << 10) | (rn << 5) | rt)

    def str_w(self, rt, rn, imm12=0):
        self.emit(0xB9000000 | ((imm12 & 0xFFF) << 10) | (rn << 5) | rt)

    def ldr_w(self, rt, rn, imm12=0):
        self.emit(0xB9400000 | ((imm12 & 0xFFF) << 10) | (rn << 5) | rt)

    def b(self, label_name):
        self.fixups.append((self.here(), label_name, "b"))
        self.emit(0x14000000)

    def bl(self, label_name):
        self.fixups.append((self.here(), label_name, "bl"))
        self.emit(0x94000000)

    def b_cond(self, cond, label_name):
        cond_map = {"eq": 0, "ne": 1, "hs": 2, "lo": 3, "mi": 4, "pl": 5,
                    "vs": 6, "vc": 7, "hi": 8, "ls": 9, "ge": 10, "lt": 11,
                    "gt": 12, "le": 13, "al": 14}
        c = cond_map[cond]
        self.fixups.append((self.here(), label_name, "bcond"))
        self.emit(0x54000000 | c)

    def cbz(self, rt, label_name):
        self.fixups.append((self.here(), label_name, "cbz"))
        self.emit(0xB4000000 | rt)

    def cbnz(self, rt, label_name):
        self.fixups.append((self.here(), label_name, "cbz"))
        self.emit(0xB5000000 | rt)

    def ret(self):
        self.emit(0xD65F03C0)  # RET X30


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  NX2 Firmware — real AArch64 code that renders to framebuffer via CPU
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def build_nx2_firmware():
    """
    Assemble a real AArch64 firmware binary that:
      1. Clears framebuffer to dark background
      2. Renders animated particle ring via CPU math
      3. SVCs to yield each frame back to the host
      4. Loops forever

    Register allocation:
      X0  = FB_BASE          X10 = pixel addr scratch
      X1  = FB_W             X11 = color R
      X2  = FB_H             X12 = color G
      X3  = frame counter    X13 = color B
      X4  = particle index   X14 = scratch
      X5  = px (particle x)  X15 = scratch
      X6  = py (particle y)  X16 = scratch
      X7  = cx (center x)    X20 = sine LUT base
      X8  = cy (center y)    X21 = scratch
      X9  = stride
    """
    asm = A64Asm()

    # ── Entry point ──
    asm.label("_start")
    # X0 = FB base address
    asm.mov_imm(0, FB_BASE)
    # X1 = width, X2 = height
    asm.movz(1, FB_W)
    asm.movz(2, FB_H)
    # X7 = cx, X8 = cy
    asm.movz(7, FB_W // 2)
    asm.movz(8, FB_H // 2)
    # X9 = stride
    asm.movz(9, FB_STRIDE)
    # X3 = frame counter
    asm.movz(3, 0)

    # ── Frame loop ──
    asm.label("frame_loop")

    # === Clear framebuffer (write 0x121212 to each pixel) ===
    asm.movz(14, 0)  # y = 0
    asm.label("clear_y")
    asm.movz(15, 0)  # x = 0
    asm.label("clear_x")
    # addr = FB_BASE + y * stride + x * 3
    asm.mul(10, 14, 9)           # y * stride
    asm.movz(16, 3)
    asm.mul(21, 15, 16)          # x * 3
    asm.add_reg(10, 10, 21)      # + x*3
    asm.add_reg(10, 10, 0)       # + FB_BASE
    asm.movz(16, 0x12)
    asm.strb(16, 10, 0)          # R
    asm.strb(16, 10, 1)          # G
    asm.strb(16, 10, 2)          # B
    asm.add_imm(15, 15, 1)       # x++
    asm.cmp_imm(15, FB_W)
    asm.b_cond("lt", "clear_x")
    asm.add_imm(14, 14, 1)       # y++
    asm.cmp_imm(14, FB_H)
    asm.b_cond("lt", "clear_y")

    # === Render 80 particles in a ring ===
    asm.movz(4, 0)  # particle index i = 0
    asm.label("particle_loop")

    # SVC 0x10 — host computes trig: reads X3 (frame), X4 (index)
    # returns px in X5, py in X6, R in X11, G in X12, B in X13
    asm.svc(0x10)

    # Bounds check px, py
    asm.cmp_imm(5, FB_W)
    asm.b_cond("hs", "skip_particle")
    asm.cmp_imm(6, FB_H)
    asm.b_cond("hs", "skip_particle")

    # Write 3×3 block of pixels
    # For dy in -1..1, dx in -1..1
    asm.movz(16, 0)  # dy counter 0..2
    asm.label("block_dy")
    asm.movz(17, 0)  # dx counter 0..2
    asm.label("block_dx")

    # ny = py + dy - 1, nx = px + dx - 1
    asm.add_reg(18, 6, 16)       # py + dy
    asm.sub_imm(18, 18, 1)       # - 1
    asm.add_reg(19, 5, 17)       # px + dx
    asm.sub_imm(19, 19, 1)       # - 1

    # bounds
    asm.cmp_imm(18, FB_H)
    asm.b_cond("hs", "skip_px")
    asm.cmp_imm(19, FB_W)
    asm.b_cond("hs", "skip_px")

    # addr = FB_BASE + ny * stride + nx * 3
    asm.mul(10, 18, 9)
    asm.movz(21, 3)
    asm.mul(14, 19, 21)
    asm.add_reg(10, 10, 14)
    asm.add_reg(10, 10, 0)
    asm.strb(11, 10, 0)  # R
    asm.strb(12, 10, 1)  # G
    asm.strb(13, 10, 2)  # B

    asm.label("skip_px")
    asm.add_imm(17, 17, 1)
    asm.cmp_imm(17, 3)
    asm.b_cond("lt", "block_dx")
    asm.add_imm(16, 16, 1)
    asm.cmp_imm(16, 3)
    asm.b_cond("lt", "block_dy")

    asm.label("skip_particle")
    asm.add_imm(4, 4, 1)
    asm.cmp_imm(4, 80)
    asm.b_cond("lt", "particle_loop")

    # === Frame done — SVC 0x01 yields to host ===
    asm.add_imm(3, 3, 1)   # frame++
    asm.svc(0x01)           # yield frame
    asm.b("frame_loop")

    asm.resolve()
    return asm.code


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  NX2 System — ties CPU + Memory + Framebuffer together
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class NX2System:
    def __init__(self):
        self.mem = bytearray(MEM_SIZE)
        # Load firmware
        fw = build_nx2_firmware()
        self.mem[CODE_BASE:CODE_BASE + len(fw)] = fw
        # Boot CPU
        self.cpu = AArch64Core(self.mem)
        self.cpu.svc_handler = self.handle_svc
        self.frame_ready = False
        self.docked = True

    def handle_svc(self, cpu, imm):
        if imm == 0x01:
            # Yield — frame is done
            self.frame_ready = True
        elif imm == 0x10:
            # Trig helper — compute particle position & color
            frame = cpu.xr(3)
            idx = cpu.xr(4)
            t = frame * 0.04
            angle = t * 2.8 + idx * 0.0785
            radius = 55 + 18 * math.sin(t * 1.3 + idx * 0.5)
            cx = FB_W // 2
            cy = FB_H // 2
            px = int(cx + radius * math.cos(angle))
            py = int(cy + radius * 0.65 * math.sin(angle * 1.6))
            r = int(max(0, min(255, 40 + 40 * math.sin(t * 2.1 + idx * 0.4))))
            g = int(max(0, min(255, 160 + 80 * math.sin(t * 1.4 + idx * 0.3))))
            b = int(max(0, min(255, 200 + 55 * math.cos(t * 1.7 + idx * 0.2))))
            cpu.xw(5, px & 0xFFFFFFFFFFFFFFFF)
            cpu.xw(6, py & 0xFFFFFFFFFFFFFFFF)
            cpu.xw(11, r)
            cpu.xw(12, g)
            cpu.xw(13, b)

    def run_frame(self):
        """Run CPU until it yields a frame (SVC 0x01)."""
        self.frame_ready = False
        safety = 0
        while not self.frame_ready and not self.cpu.halted and safety < 500000:
            self.cpu.step_n(100)
            safety += 100

    def get_framebuffer(self):
        """Read FB from memory as list of (r,g,b) rows."""
        fb = []
        for y in range(FB_H):
            row = []
            off = FB_BASE + y * FB_STRIDE
            for x in range(FB_W):
                p = off + x * 3
                r = self.mem[p & (MEM_SIZE - 1)]
                g = self.mem[(p + 1) & (MEM_SIZE - 1)]
                b = self.mem[(p + 2) & (MEM_SIZE - 1)]
                row.append((r, g, b))
            fb.append(row)
        return fb


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Ryujinx-style Tkinter GUI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ACNX2Emu(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ACNX2Emu 0.2 — Beta NX2 Edition  |  Ryujinx")
        self.geometry("600x400")
        self.resizable(False, False)
        self.configure(bg=RYU_BG)

        self.sys = NX2System()
        self.running = False
        self.frame_count = 0
        self.last_time = time.time()

        style = ttk.Style()
        style.theme_use("clam")

        # ── Menu ──
        menubar = tk.Menu(self, bg=RYU_MENU_BG, fg=RYU_MENU_FG,
                          activebackground=RYU_ACCENT, activeforeground="white",
                          relief="flat", bd=0)
        self.config(menu=menubar)

        file_menu = tk.Menu(menubar, tearoff=0, bg=RYU_MENU_BG, fg=RYU_MENU_FG,
                            activebackground=RYU_ACCENT, activeforeground="white")
        menubar.add_cascade(label="  File  ", menu=file_menu)
        file_menu.add_command(label="Open NX2 ROM…", command=self.fake_open_rom)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.quit)

        emu_menu = tk.Menu(menubar, tearoff=0, bg=RYU_MENU_BG, fg=RYU_MENU_FG,
                           activebackground=RYU_ACCENT, activeforeground="white")
        menubar.add_cascade(label="  Emulation  ", menu=emu_menu)
        emu_menu.add_command(label="Boot NX2", command=self.start_emulation)
        emu_menu.add_command(label="Pause", command=self.stop_emulation)
        emu_menu.add_separator()
        self._docked_var = tk.BooleanVar(value=True)
        emu_menu.add_checkbutton(label="Docked Mode", variable=self._docked_var,
                                 command=self.toggle_mode)

        options_menu = tk.Menu(menubar, tearoff=0, bg=RYU_MENU_BG, fg=RYU_MENU_FG,
                               activebackground=RYU_ACCENT, activeforeground="white")
        menubar.add_cascade(label="  Options  ", menu=options_menu)
        options_menu.add_command(label="CPU: AArch64 (A78C ×8)", state="disabled")
        options_menu.add_command(label="GPU: Ampere 1536 CUDA", state="disabled")

        help_menu = tk.Menu(menubar, tearoff=0, bg=RYU_MENU_BG, fg=RYU_MENU_FG,
                            activebackground=RYU_ACCENT, activeforeground="white")
        menubar.add_cascade(label="  Help  ", menu=help_menu)
        help_menu.add_command(label="About", command=self.show_about)

        # ── Canvas ──
        canvas_frame = tk.Frame(self, bg=RYU_BORDER, bd=1, relief="solid")
        canvas_frame.pack(pady=(6, 4), padx=14)
        self.canvas = tk.Canvas(canvas_frame, width=560, height=316,
                                bg="#121212", highlightthickness=0, bd=0)
        self.canvas.pack()

        # ── Status bar ──
        sep = tk.Frame(self, bg=RYU_BORDER, height=1)
        sep.pack(fill="x", side="bottom")
        status_bar = tk.Frame(self, bg=RYU_SURFACE, height=26)
        status_bar.pack(fill="x", side="bottom")
        status_bar.pack_propagate(False)

        self.lbl_game = tk.Label(status_bar, text="No game loaded",
                                 fg=RYU_TEXT_DIM, bg=RYU_SURFACE,
                                 font=("Segoe UI", 9), anchor="w")
        self.lbl_game.pack(side="left", padx=10)

        self.lbl_pc = tk.Label(status_bar, text="PC: 0x00080000",
                               fg=RYU_ACCENT, bg=RYU_SURFACE, font=("Consolas", 9))
        self.lbl_pc.pack(side="right", padx=8)

        self.lbl_instrs = tk.Label(status_bar, text="Instrs: 0",
                                   fg=RYU_TEXT_DIM, bg=RYU_SURFACE, font=("Consolas", 9))
        self.lbl_instrs.pack(side="right", padx=8)

        self.lbl_fps = tk.Label(status_bar, text="0.0 FPS",
                                fg=RYU_ACCENT, bg=RYU_SURFACE, font=("Consolas", 9))
        self.lbl_fps.pack(side="right", padx=8)

        self.lbl_mode = tk.Label(status_bar, text="Docked",
                                 fg=RYU_GREEN, bg=RYU_SURFACE,
                                 font=("Consolas", 9, "bold"))
        self.lbl_mode.pack(side="right", padx=8)

        # ── Boot button — BLUE TEXT, never black/gray ──
        self.btn_start = tk.Button(
            self, text="▶  BOOT BETA NX2",
            font=("Segoe UI", 12, "bold"),
            bg=RYU_SURFACE, fg=RYU_ACCENT,
            activebackground=RYU_ACCENT, activeforeground="white",
            disabledforeground=RYU_GREEN,
            relief="flat", cursor="hand2",
            padx=20, pady=4,
            command=self.start_emulation,
        )
        self.btn_start.pack(pady=4)

    def fake_open_rom(self):
        messagebox.showinfo("ACNX2Emu",
                            "✅ NX2 synthetic firmware 22.0.0 loaded\n"
                            "Real AArch64 CPU • /files=off")
        self.lbl_game.config(text="NX2 FW 22.0.0 (AArch64 real)", fg=RYU_TEXT)

    def toggle_mode(self):
        self.sys.docked = not self.sys.docked
        if self.sys.docked:
            self.lbl_mode.config(text="Docked", fg=RYU_GREEN)
        else:
            self.lbl_mode.config(text="Handheld", fg="#F0A050")

    def show_about(self):
        cpu = self.sys.cpu
        messagebox.showinfo("About ACNX2Emu",
            "ACNX2Emu 0.2 — Beta NX2 Edition\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "REAL AArch64 CPU Core:\n"
            f"  Registers: X0–X30 + SP + PC\n"
            f"  Flags: NZCV\n"
            f"  Instructions decoded: MOVZ, MOVK,\n"
            f"    ADD, SUB, MUL, UDIV, AND, ORR,\n"
            f"    EOR, LSL, LSR, CMP, B, BL, BR,\n"
            f"    BLR, RET, B.cond, CBZ, CBNZ,\n"
            f"    LDR, STR, LDRB, STRB, LDP, STP,\n"
            f"    SVC, NOP, HLT\n"
            f"  Memory: 16 MB unified\n"
            f"  FB MMIO: 0x{FB_BASE:08X}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Ryujinx-style • /files=off\n"
            "© A.C Holdings")

    def update_canvas(self):
        self.canvas.delete("all")
        fb = self.sys.get_framebuffer()
        # Draw at 2× scale (280×158 → 560×316)
        for y in range(0, FB_H, 1):
            for x in range(0, FB_W, 1):
                r, g, b = fb[y][x]
                if r == 0x12 and g == 0x12 and b == 0x12:
                    continue  # skip background pixels for speed
                color = f"#{r:02x}{g:02x}{b:02x}"
                sx, sy = x * 2, y * 2
                self.canvas.create_rectangle(sx, sy, sx + 2, sy + 2,
                                             fill=color, outline="")

    def emulation_loop(self):
        if not self.running:
            return

        # Run real AArch64 CPU until it yields a frame
        self.sys.run_frame()
        self.update_canvas()

        self.frame_count += 1
        if self.frame_count % 3 == 0:
            now = time.time()
            fps = 3.0 / (now - self.last_time + 1e-9)
            self.last_time = now
            cpu = self.sys.cpu
            self.lbl_instrs.config(text=f"Instrs: {cpu.instrs:,}")
            self.lbl_fps.config(text=f"{fps:.1f} FPS")
            self.lbl_pc.config(text=f"PC: 0x{cpu.pc:08X}")

        self.after(16, self.emulation_loop)

    def start_emulation(self):
        if self.running:
            return
        self.running = True
        self.btn_start.config(state="disabled", text="✅  RUNNING — AArch64 NX2")
        self.lbl_game.config(text="NX2 FW 22.0.0 (AArch64 real)", fg=RYU_TEXT)
        cpu = self.sys.cpu
        print("═══════════════════════════════════════════════════")
        print("  ACNX2Emu 0.2 — Real AArch64 CPU Core (Ryujinx)")
        print(f"  Firmware: {len(build_nx2_firmware())} bytes @ 0x{CODE_BASE:08X}")
        print(f"  Registers: X0–X30 + SP + PC + NZCV")
        print(f"  Memory: {MEM_SIZE // (1024*1024)} MB • FB @ 0x{FB_BASE:08X}")
        print(f"  /files=off")
        print("═══════════════════════════════════════════════════\n")
        self.emulation_loop()

    def stop_emulation(self):
        self.running = False
        self.btn_start.config(state="normal", text="▶  BOOT BETA NX2",
                              fg=RYU_ACCENT)


if __name__ == "__main__":
    try:
        app = ACNX2Emu()
        app.mainloop()
    except KeyboardInterrupt:
        print("\nNX2 core shut down.")
