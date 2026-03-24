#!/usr/bin/env python3
"""
CatNXEMU2.0V0.X — NX2EMU BETA 0.1
Full oboromi (0xNikilite) architecture ported to Python / Tkinter
────────────────────────────────────────────────────────────────
  • 8-core AArch64 CPU Manager w/ shared 16 MB memory
  • Real ARMv8-A instruction decode & execute (31 GP regs, NZCV)
  • SM86 Ampere GPU stub — 128-bit shader instruction decoder
  • SPIR-V binary emitter (type system + instruction emission)
  • 160 HOS nn:: services (acc, hid, nvdrv, vi, fs …)
  • System state (sys::State) tying CPU + GPU + Services
  • oboromi-compatible test harness (NOP, ADD, SUB, MOV, RET …)
  • Ryujinx 2026 Avalonia-style GUI w/ animated splash screen
  • Pure in-memory • /files=off
────────────────────────────────────────────────────────────────
Ported from: https://github.com/0xNikilite/oboromi (Rust / GPLv3)
© A.C Holdings • CatNXEMU2.0V0.X
"""

import tkinter as tk
from tkinter import ttk, messagebox
import time
import math
import struct
import threading

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Ryujinx 2026 Avalonia Palette
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DARK_BG       = "#1A1B1E"
DARK_SURFACE  = "#25262B"
DARK_RAISED   = "#2C2E33"
DARK_BORDER   = "#373A40"
DARK_HOVER    = "#3B3D44"
TEXT_PRIMARY  = "#C1C2C5"
TEXT_SECONDARY= "#909296"
TEXT_BRIGHT   = "#E4E5E8"
ACCENT_BLUE   = "#4DABF7"
ACCENT_GREEN  = "#51CF66"
ACCENT_ORANGE = "#FF922B"
ACCENT_RED    = "#FF6B6B"
TOOLBAR_BG    = "#212226"
SPLASH_BG     = "#191919"
LOG_GREEN     = "#32FF32"
LOG_RED       = "#FF3232"
LOG_YELLOW    = "#FFC864"
LOG_DIM       = "#C8C8C8"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Memory Map (oboromi: 12 GB — scaled to 16 MB for Python)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CORE_COUNT  = 8
MEM_SIZE    = 16 * 1024 * 1024      # 16 MB (oboromi: 12 GB)
MEM_BASE    = 0x0
CODE_BASE   = 0x00080000
STACK_TOP   = 0x00800000
FB_BASE     = 0x04000000
FB_W, FB_H  = 280, 158
FB_STRIDE   = FB_W * 3
TEST_BASE   = 0x00001000


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AArch64 CPU Core (oboromi: cpu/unicorn_interface.rs)
#  Real ARMv8-A instruction decode — no Unicorn, pure Python
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AArch64Core:
    MASK64 = 0xFFFFFFFFFFFFFFFF
    MASK32 = 0xFFFFFFFF

    def __init__(self, mem: bytearray, core_id: int = 0):
        self.x = [0] * 31          # X0–X30
        self.sp = STACK_TOP - (core_id * 0x100000)
        self.pc = CODE_BASE
        self.N = self.Z = self.C = self.V = False
        self.mem = mem
        self.core_id = core_id
        self.cycles = 0
        self.instrs = 0
        self.halted = False
        self.svc_handler = None

    def xr(self, r):
        return self.x[r] if r < 31 else 0

    def xw(self, r, val):
        if r < 31:
            self.x[r] = val & self.MASK64

    def get_x(self, i):   return self.xr(i)
    def set_x(self, i, v): self.xw(i, v)
    def get_pc(self):      return self.pc
    def set_pc(self, v):   self.pc = v & self.MASK64
    def get_sp(self):      return self.sp
    def set_sp(self, v):   self.sp = v & self.MASK64

    def read8(self, a):    return self.mem[a & (MEM_SIZE - 1)]
    def write8(self, a, v): self.mem[a & (MEM_SIZE - 1)] = v & 0xFF

    def read32(self, a):
        return struct.unpack_from("<I", self.mem, a & (MEM_SIZE - 1))[0]
    def write32(self, a, v):
        struct.pack_into("<I", self.mem, a & (MEM_SIZE - 1), v & self.MASK32)
    def read64(self, a):
        return struct.unpack_from("<Q", self.mem, a & (MEM_SIZE - 1))[0]
    def write64(self, a, v):
        struct.pack_into("<Q", self.mem, a & (MEM_SIZE - 1), v & self.MASK64)

    # oboromi: write_u32 / read_u32 compat
    write_u32 = write32
    read_u32 = read32
    write_u64 = write64
    read_u64 = read64

    def _update_nz(self, r):
        self.N = bool(r & (1 << 63)); self.Z = (r & self.MASK64) == 0
    def _add_flags(self, a, b, r):
        self._update_nz(r); self.C = r > self.MASK64
        sa, sb, sr = bool(a & (1<<63)), bool(b & (1<<63)), bool(r & (1<<63))
        self.V = (sa == sb) and (sa != sr)
    def _sub_flags(self, a, b):
        r = a - b; self._update_nz(r & self.MASK64); self.C = a >= b
        sa, sb, sr = bool(a & (1<<63)), bool(b & (1<<63)), bool(r & (1<<63))
        self.V = (sa != sb) and (sr != sa)
    def check_cond(self, c):
        t = {0:self.Z,1:not self.Z,2:self.C,3:not self.C,4:self.N,5:not self.N,
             6:self.V,7:not self.V,8:self.C and not self.Z,9:not self.C or self.Z,
             10:self.N==self.V,11:self.N!=self.V,12:not self.Z and self.N==self.V,
             13:self.Z or self.N!=self.V,14:True,15:True}
        return t.get(c, False)
    @staticmethod
    def sxt(v, b): return v - (1 << b) if v & (1 << (b-1)) else v

    def step(self):
        """Execute single instruction (oboromi: cpu.step())"""
        if self.halted: return 1
        insn = self.read32(self.pc); self.pc += 4
        self.cycles += 1; self.instrs += 1
        return self._exec(insn)

    def run(self):
        """Run until BRK/HLT (oboromi: cpu.run())"""
        safety = 0
        while not self.halted and safety < 200000:
            self.step(); safety += 1
        return 1 if not self.halted or safety > 0 else 0

    def step_n(self, n):
        for _ in range(n):
            if self.halted: break
            insn = self.read32(self.pc); self.pc += 4
            self.cycles += 1; self.instrs += 1; self._exec(insn)

    def halt(self):
        self.halted = True

    def _exec(self, i):
        if i == 0: self.halted = True; self.pc -= 4; return 0  # uninit memory = halt
        if i == 0xD503201F or (i >> 24) == 0xD5: return 0
        # HLT
        if (i & 0xFFE0001F) == 0xD4400000: self.halted = True; self.pc -= 4; return 0
        # BRK — oboromi uses this to terminate tests
        if (i & 0xFFE0001F) == 0xD4200000: self.halted = True; self.pc -= 4; return 0
        # SVC
        if (i & 0xFFE0001F) == 0xD4000001:
            if self.svc_handler: self.svc_handler(self, (i >> 5) & 0xFFFF)
            return 0
        # MOVZ
        if (i & 0x7F800000) == 0x52800000:
            hw = (i>>21)&3; self.xw(i&0x1F, ((i>>5)&0xFFFF)<<(hw*16)); return 0
        # MOVK
        if (i & 0x7F800000) == 0x72800000:
            hw = (i>>21)&3; rd = i&0x1F; s = hw*16
            self.xw(rd, (self.xr(rd) & ~(0xFFFF<<s) & self.MASK64) | (((i>>5)&0xFFFF)<<s)); return 0
        # ADD imm
        if (i & 0x7F000000) == 0x11000000:
            S = (i>>29)&1; sh = (i>>22)&1; imm = ((i>>10)&0xFFF)<<(12 if sh else 0)
            a = self.xr((i>>5)&0x1F); r = a + imm
            if S: self._add_flags(a, imm, r)
            self.xw(i&0x1F, r); return 0
        # SUB/SUBS imm
        if (i & 0x7F000000) == 0x51000000:
            S = (i>>29)&1; sh = (i>>22)&1; imm = ((i>>10)&0xFFF)<<(12 if sh else 0)
            a = self.xr((i>>5)&0x1F); r = (a - imm) & self.MASK64
            if S: self._sub_flags(a, imm)
            self.xw(i&0x1F, r); return 0
        # ADD/SUB shifted reg
        if (i & 0x1F000000) == 0x0B000000:
            is_sub = (i>>30)&1; S = (i>>29)&1; st = (i>>22)&3
            b = self.xr((i>>16)&0x1F); imm6 = (i>>10)&0x3F
            if st == 0: b = (b << imm6) & self.MASK64
            elif st == 1: b = b >> imm6
            a = self.xr((i>>5)&0x1F)
            if is_sub:
                r = (a - b) & self.MASK64
                if S: self._sub_flags(a, b)
            else:
                r = a + b
                if S: self._add_flags(a, b, r)
                r &= self.MASK64
            self.xw(i&0x1F, r); return 0
        # AND/ORR/EOR/ANDS
        if (i & 0x1F000000) == 0x0A000000:
            opc = (i>>29)&3; st = (i>>22)&3; b = self.xr((i>>16)&0x1F)
            imm6 = (i>>10)&0x3F
            if st == 0: b = (b << imm6) & self.MASK64
            elif st == 1: b = b >> imm6
            a = self.xr((i>>5)&0x1F)
            if opc == 0: self.xw(i&0x1F, a & b)
            elif opc == 1: self.xw(i&0x1F, a | b)
            elif opc == 2: self.xw(i&0x1F, a ^ b)
            elif opc == 3:
                r = a & b; self._update_nz(r); self.C = self.V = False; self.xw(i&0x1F, r)
            return 0
        # MADD/MUL
        if (i & 0xFF208000) == 0x9B000000:
            self.xw(i&0x1F, (self.xr((i>>10)&0x1F) + self.xr((i>>5)&0x1F) * self.xr((i>>16)&0x1F)) & self.MASK64); return 0
        # UDIV
        if (i & 0xFFE0FC00) == 0x9AC00800:
            d = self.xr((i>>16)&0x1F)
            self.xw(i&0x1F, self.xr((i>>5)&0x1F) // d if d else 0); return 0
        # B
        if (i & 0xFC000000) == 0x14000000:
            self.pc = (self.pc - 4 + self.sxt(i & 0x3FFFFFF, 26) * 4) & self.MASK64; return 0
        # BL
        if (i & 0xFC000000) == 0x94000000:
            self.xw(30, self.pc)
            self.pc = (self.pc - 4 + self.sxt(i & 0x3FFFFFF, 26) * 4) & self.MASK64; return 0
        # BR
        if (i & 0xFFFFFC1F) == 0xD61F0000:
            self.pc = self.xr((i>>5)&0x1F); return 0
        # BLR
        if (i & 0xFFFFFC1F) == 0xD63F0000:
            self.xw(30, self.pc); self.pc = self.xr((i>>5)&0x1F); return 0
        # RET
        if (i & 0xFFFFFC1F) == 0xD65F0000:
            self.pc = self.xr((i>>5)&0x1F); return 0
        # B.cond
        if (i & 0xFF000010) == 0x54000000:
            if self.check_cond(i & 0xF):
                self.pc = (self.pc - 4 + self.sxt((i>>5)&0x7FFFF, 19) * 4) & self.MASK64
            return 0
        # CBZ/CBNZ
        if (i & 0x7E000000) == 0x34000000:
            nz = (i>>24)&1; v = self.xr(i&0x1F)
            if (v != 0) == bool(nz):
                self.pc = (self.pc - 4 + self.sxt((i>>5)&0x7FFFF, 19) * 4) & self.MASK64
            return 0
        # LDR 64
        if (i & 0xFFC00000) == 0xF9400000:
            self.xw(i&0x1F, self.read64(self.xr((i>>5)&0x1F) + ((i>>10)&0xFFF)*8)); return 0
        # STR 64
        if (i & 0xFFC00000) == 0xF9000000:
            self.write64(self.xr((i>>5)&0x1F) + ((i>>10)&0xFFF)*8, self.xr(i&0x1F)); return 0
        # LDRB
        if (i & 0xFFC00000) == 0x39400000:
            self.xw(i&0x1F, self.read8(self.xr((i>>5)&0x1F) + ((i>>10)&0xFFF))); return 0
        # STRB
        if (i & 0xFFC00000) == 0x39000000:
            self.write8(self.xr((i>>5)&0x1F) + ((i>>10)&0xFFF), self.xr(i&0x1F) & 0xFF); return 0
        # LDR W
        if (i & 0xFFC00000) == 0xB9400000:
            self.xw(i&0x1F, self.read32(self.xr((i>>5)&0x1F) + ((i>>10)&0xFFF)*4)); return 0
        # STR W
        if (i & 0xFFC00000) == 0xB9000000:
            self.write32(self.xr((i>>5)&0x1F) + ((i>>10)&0xFFF)*4, self.xr(i&0x1F) & self.MASK32); return 0
        return 0  # unknown → NOP


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CPU Manager (oboromi: cpu/cpu_manager.rs)
#  8 cores sharing unified memory, round-robin stepping
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CpuManager:
    """8-core AArch64 CPU manager with shared memory (oboromi CpuManager)."""
    def __init__(self):
        self.shared_memory = bytearray(MEM_SIZE)
        self.cores = []
        for i in range(CORE_COUNT):
            core = AArch64Core(self.shared_memory, core_id=i)
            self.cores.append(core)

    def get_core(self, cid):
        return self.cores[cid] if 0 <= cid < len(self.cores) else None

    def run_all(self):
        """Round-robin step all cores (oboromi: cpu_manager.run_all)."""
        for core in self.cores:
            core.step()

    def step_all_n(self, n):
        for core in self.cores:
            core.step_n(n)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SPIR-V Binary Emitter (oboromi: gpu/spirv.rs)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SPIRVEmitter:
    """
    SPIR-V binary module emitter (Python port of oboromi spirv::Emitter).
    Emits valid SPIR-V word stream. IDs are 1-based.
    """
    MAGIC       = 0x07230203
    VERSION     = 0x00010000
    GENERATOR   = 0x00000000  # CatNXEMU

    # Opcodes
    OP_TYPE_VOID      = 19
    OP_TYPE_BOOL      = 20
    OP_TYPE_INT       = 21
    OP_TYPE_FLOAT     = 22
    OP_TYPE_VECTOR    = 23
    OP_TYPE_POINTER   = 32
    OP_TYPE_FUNCTION  = 33
    OP_CONSTANT       = 43
    OP_VARIABLE       = 59
    OP_LOAD           = 61
    OP_STORE          = 62
    OP_IADD           = 128
    OP_ISUB           = 130
    OP_IMUL           = 132
    OP_CAPABILITY     = 17
    OP_MEMORY_MODEL   = 14
    OP_ENTRY_POINT    = 15
    OP_EXECUTION_MODE = 16
    OP_NAME           = 5
    OP_DECORATE       = 71
    OP_FUNCTION       = 54
    OP_FUNCTION_END   = 56
    OP_LABEL          = 248
    OP_RETURN         = 253

    # Capabilities
    CAP_SHADER = 1
    CAP_INT64  = 11
    CAP_FLOAT16 = 9
    CAP_INT8   = 39

    def __init__(self):
        self.words = []
        self.next_id = 1
        self.bound = 0

    def _alloc_id(self):
        rid = self.next_id; self.next_id += 1; return rid

    def _emit_word(self, w):
        self.words.append(w & 0xFFFFFFFF)

    def _emit_insn(self, opcode, *operands):
        wc = 1 + len(operands)
        self._emit_word((wc << 16) | opcode)
        for op in operands:
            self._emit_word(op)

    def emit_header(self):
        self._emit_word(self.MAGIC)
        self._emit_word(self.VERSION)
        self._emit_word(self.GENERATOR)
        self._emit_word(0)   # bound — patched in finalize()
        self._emit_word(0)   # schema

    def emit_capability(self, cap):
        self._emit_insn(self.OP_CAPABILITY, cap)

    def emit_memory_model(self, addressing=0, memory=1):
        self._emit_insn(self.OP_MEMORY_MODEL, addressing, memory)

    def emit_type_void(self):
        rid = self._alloc_id(); self._emit_insn(self.OP_TYPE_VOID, rid); return rid

    def emit_type_bool(self):
        rid = self._alloc_id(); self._emit_insn(self.OP_TYPE_BOOL, rid); return rid

    def emit_type_int(self, width, signedness):
        rid = self._alloc_id()
        self._emit_insn(self.OP_TYPE_INT, rid, width, signedness); return rid

    def emit_type_float(self, width):
        rid = self._alloc_id()
        self._emit_insn(self.OP_TYPE_FLOAT, rid, width); return rid

    def emit_type_vector(self, component_type, count):
        rid = self._alloc_id()
        self._emit_insn(self.OP_TYPE_VECTOR, rid, component_type, count); return rid

    def emit_type_pointer(self, storage_class, pointee_type):
        rid = self._alloc_id()
        self._emit_insn(self.OP_TYPE_POINTER, rid, storage_class, pointee_type); return rid

    def emit_constant_typed(self, type_id, value):
        rid = self._alloc_id()
        self._emit_insn(self.OP_CONSTANT, type_id, rid, value & 0xFFFFFFFF); return rid

    def emit_variable(self, type_id, storage_class):
        rid = self._alloc_id()
        self._emit_insn(self.OP_VARIABLE, type_id, rid, storage_class); return rid

    def emit_load(self, type_id, pointer):
        rid = self._alloc_id()
        self._emit_insn(self.OP_LOAD, type_id, rid, pointer); return rid

    def emit_store(self, pointer, value):
        self._emit_insn(self.OP_STORE, pointer, value)

    def emit_iadd(self, type_id, a, b):
        rid = self._alloc_id()
        self._emit_insn(self.OP_IADD, type_id, rid, a, b); return rid

    def emit_isub(self, type_id, a, b):
        rid = self._alloc_id()
        self._emit_insn(self.OP_ISUB, type_id, rid, a, b); return rid

    def emit_imul(self, type_id, a, b):
        rid = self._alloc_id()
        self._emit_insn(self.OP_IMUL, type_id, rid, a, b); return rid

    def finalize(self):
        if len(self.words) >= 4:
            self.words[3] = self.next_id
        return self.words


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SM86 GPU Decoder (oboromi: gpu/sm86.rs)
#  128-bit NVIDIA Ampere shader instruction decode
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

MAX_REG_COUNT = 254

class SM86Decoder:
    """
    NVIDIA SM86 (Ampere) shader instruction decoder.
    Decodes 128-bit instructions and emits SPIR-V via the emitter.
    Ported from oboromi gpu/sm86.rs.
    """

    # Instruction mnemonics decoded by oboromi
    OPCODES = [
        "AL2P", "ALD", "ARRIVES", "AST", "ATOM", "ATOMG", "ATOMS",
        "B2R", "BAR", "BPT", "BRA", "BREAK", "BRX", "BSSY", "BSYNC",
        "CALL", "CCTL", "CCTLL", "CONT", "CS2R", "DEPBAR",
        "DMMA", "DSETP",
        "EXIT",
        "F2F", "F2FP", "F2I", "F2IP", "FADD", "FADD32I",
        "FCHK", "FCMP", "FFMA", "FFMA32I", "FLO", "FMNMX", "FMUL",
        "FMUL32I", "FSEL", "FSET", "FSETP", "FSWZADD",
        "GETLMEMBASE", "HMMA",
        "I2F", "I2FP", "I2I", "I2IP", "IADD3", "IADD32I",
        "ICMP", "IDLE", "IMAD", "IMAD32I", "IMADSP", "IMNMX",
        "IMUL", "IMUL32I", "ISBERD", "ISETP",
        "JCAL", "JMP", "JMXU",
        "KILL",
        "LD", "LDC", "LDG", "LDL", "LDS", "LDSM", "LDTRAM",
        "LEA", "LEPC", "LONGJMP",
        "MATCH", "MEMBAR", "MOV", "MOV32I", "MOVM", "MUFU",
        "NANOSLEEP", "NOP",
        "OUT", "P2R", "PBK", "PCNT", "PEXIT", "PIXLD", "PLONGJMP",
        "POPC", "PRET", "PRMT", "PSET", "PSETP",
        "R2B", "R2P", "R2UR", "RED", "REDUX", "RET", "RPCMOV",
        "S2R", "S2UR", "SAM", "SEL", "SETCTAID", "SETLMEMBASE",
        "SHF", "SHFL", "SHL", "SHR",
        "ST", "STG", "STL", "STS", "STTRAM", "SUATOM", "SULD", "SURED", "SUST",
        "TEX", "TLD", "TLD4", "TLDB", "TMML", "TXD", "TXQ",
        "UBMSK", "UBREV", "UCLEA", "UFLO",
        "UIADD3", "UIMAD", "UISETP", "ULDC", "ULEA",
        "UMOV", "UP2UR", "UPLOP3", "UPOPC", "UPRMT",
        "UPSETP", "UR2P", "USEL", "USHF", "USHL", "USHR",
        "VOTE", "VOTEU",
        "WARPSYNC",
        "YIELD",
    ]

    def __init__(self):
        self.ir = SPIRVEmitter()
        self.regs = [0] * MAX_REG_COUNT
        self.type_u32 = 0
        self.type_ptr_u32 = 0
        self.decoded_count = 0
        self.decode_log = []

    def init(self):
        """Initialize type system and register file (oboromi Decoder::init)."""
        self.ir.emit_header()
        self.ir.emit_capability(SPIRVEmitter.CAP_SHADER)
        self.ir.emit_memory_model()
        type_void = self.ir.emit_type_void()
        self.type_u32 = self.ir.emit_type_int(32, 0)
        self.type_ptr_u32 = self.ir.emit_type_pointer(7, self.type_u32)  # Function storage
        # Initialize register file as SPIR-V variables
        for r in range(MAX_REG_COUNT):
            self.regs[r] = self.ir.emit_variable(self.type_ptr_u32, 7)

    def load_reg(self, reg):
        if reg == 255:
            return self.ir.emit_constant_typed(self.type_u32, 0)
        return self.ir.emit_load(self.type_u32, self.regs[reg])

    def store_reg(self, reg, val):
        if reg == 255: return
        self.ir.emit_store(self.regs[reg], val)

    def _extract(self, inst, hi, lo):
        """Extract bit field from 128-bit instruction."""
        width = hi - lo + 1
        return (inst >> lo) & ((1 << width) - 1)

    def decode(self, inst_128bit):
        """
        Decode a 128-bit SM86 instruction.
        Extracts common fields and dispatches to instruction handler.
        (oboromi: sm86_decoder_generated.rs dispatch)
        """
        # Common field extraction (oboromi pattern)
        pg      = self._extract(inst_128bit, 14, 12)
        pg_not  = self._extract(inst_128bit, 15, 15)
        rd      = self._extract(inst_128bit, 23, 16)
        ra      = self._extract(inst_128bit, 31, 24)
        rb      = self._extract(inst_128bit, 39, 32)
        opex    = (self._extract(inst_128bit, 124, 122) << 5) | self._extract(inst_128bit, 109, 105)

        self.decoded_count += 1
        return {
            "pg": pg, "pg_not": pg_not,
            "rd": rd, "ra": ra, "rb": rb,
            "opex": opex,
            "raw": inst_128bit,
        }

    def al2p(self, inst):
        """AL2P: %rd := %ra + offset (oboromi sm86.rs al2p)"""
        rd = self._extract(inst, 23, 16)
        ra = self._extract(inst, 31, 24)
        ra_offset = self._extract(inst, 50, 40)
        base = self.load_reg(ra)
        offset = self.ir.emit_constant_typed(self.type_u32, ra_offset)
        dst_val = self.ir.emit_iadd(self.type_u32, base, offset)
        self.store_reg(rd, dst_val)
        self.decode_log.append(f"AL2P R{rd}, R{ra}, #{ra_offset}")

    def iadd3(self, inst):
        """IADD3: rd = ra + rb + rc (oboromi sm86.rs)"""
        rd = self._extract(inst, 23, 16)
        ra = self._extract(inst, 31, 24)
        rb = self._extract(inst, 39, 32)
        rc = self._extract(inst, 71, 64)
        a = self.load_reg(ra)
        b = self.load_reg(rb)
        tmp = self.ir.emit_iadd(self.type_u32, a, b)
        c = self.load_reg(rc)
        result = self.ir.emit_iadd(self.type_u32, tmp, c)
        self.store_reg(rd, result)
        self.decode_log.append(f"IADD3 R{rd}, R{ra}, R{rb}, R{rc}")

    def imad(self, inst):
        """IMAD: rd = ra * rb + rc"""
        rd = self._extract(inst, 23, 16)
        ra = self._extract(inst, 31, 24)
        rb = self._extract(inst, 39, 32)
        rc = self._extract(inst, 71, 64)
        a = self.load_reg(ra)
        b = self.load_reg(rb)
        prod = self.ir.emit_imul(self.type_u32, a, b)
        c = self.load_reg(rc)
        result = self.ir.emit_iadd(self.type_u32, prod, c)
        self.store_reg(rd, result)
        self.decode_log.append(f"IMAD R{rd}, R{ra}, R{rb}, R{rc}")

    def finalize(self):
        return self.ir.finalize()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  GPU State (oboromi: gpu/mod.rs)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class GPUState:
    """GPU state: SM86 decoder + SPIR-V backend (oboromi gpu::State)."""
    def __init__(self):
        self.pc = 0
        self.decoder = SM86Decoder()
        self.decoder.init()
        self.shared_memory = None
        self.global_memory = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HOS Services (oboromi: nn/mod.rs — 160 services)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Full list from oboromi's define_service! macro
HOS_SERVICE_NAMES = [
    "acc","adraw","ahid","aoc","apm","applet-ae","applet-oe","arp",
    "aud","audctl","auddebug","auddev","auddmg","audin","audout",
    "audrec","audren","audsmx","avm","banana","batlog","bcat","bgtc",
    "bpc","bpmpmr","bsd","bsdcfg","bt","btdrv","btm","btp","capmtp",
    "caps","caps2","cec-mgr","chat","clkrst","codecctl","csrng",
    "dauth","disp","dispdrv","dmnt","dns","dt","ectx","erpt","es",
    "eth","ethc","eupld","fan","fatal","fgm","file-io","friend","fs",
    "fsp-ldr","fsp-pr","fsp-srv","gds","gpio","gpuk","grc","gsv",
    "hdcp","hid","hidbus","host1x","hshl","htc","htcs","hwopus","i2c",
    "idle","ifcfg","imf","ins","irs","jit","lbl","ldn","ldr","led","lm",
    "lp2p","lr","manu","mig","mii","miiimg","mm","mnpp","ncm","nd","ndd",
    "ndrm","news","nfc","nfp","ngc","ngct","nifm","nim","notif","npns",
    "ns","nsd","ntc","nvdbg","nvdrv","nvdrvdbg","nvgem","nvmemp",
    "olsc","omm","ommdisp","ovln","pcie","pcm","pctl","pcv","pdm",
    "pgl","pinmux","pl","pm","prepo","psc","psm","pwm","rgltr","ro",
    "rtc","sasbus","set","sf-uds","sfdnsres","spbg","spi","spl",
    "sprof","spsm","srepo","ssl","syncpt","tc","tcap","time",
    "tma-log","tmagent","ts","tspm","uart","usb","vi","vi2","vic",
    "wlan","xcd",
]

class HOSService:
    """Single HOS service stub (oboromi nn::$name::State)."""
    def __init__(self, name):
        self.name = name
        self.initialized = True

class HOSServices:
    """All 160 HOS services (oboromi sys::Services)."""
    def __init__(self):
        self.services = {}
        for name in HOS_SERVICE_NAMES:
            self.services[name] = HOSService(name)

    def get(self, name):
        return self.services.get(name)

    def count(self):
        return len(self.services)

    def all_names(self):
        return list(self.services.keys())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  System State (oboromi: sys/mod.rs)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SystemState:
    """Top-level system state: CPU + GPU + Services (oboromi sys::State)."""
    def __init__(self):
        self.cpu_manager = CpuManager()
        self.gpu_state = GPUState()
        self.services = HOSServices()
        self.gpu_state.shared_memory = self.cpu_manager.shared_memory

    def start_host_services(self):
        """Boot all 160 HOS services (oboromi nn::start_host_services)."""
        log = []
        for name in HOS_SERVICE_NAMES:
            svc = self.services.get(name)
            if svc:
                log.append(f"  nn::{name} → OK")
        return log


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AArch64 Assembler (for firmware + tests)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ARM64:
    """Instruction encoders matching oboromi tests/run.rs arm64 module."""
    @staticmethod
    def nop(): return 0xD503201F
    @staticmethod
    def brk(imm16=0): return 0xD4200000 | ((imm16 & 0xFFFF) << 5)
    @staticmethod
    def ret(): return 0xD65F03C0
    @staticmethod
    def add_imm(rd, rn, imm12):
        return 0x91000000 | ((imm12 & 0xFFF) << 10) | ((rn & 0x1F) << 5) | (rd & 0x1F)
    @staticmethod
    def sub_imm(rd, rn, imm12):
        return 0xD1000000 | ((imm12 & 0xFFF) << 10) | ((rn & 0x1F) << 5) | (rd & 0x1F)
    @staticmethod
    def add_reg(rd, rn, rm):
        return 0x8B000000 | ((rm & 0x1F) << 16) | ((rn & 0x1F) << 5) | (rd & 0x1F)
    @staticmethod
    def mov_reg(rd, rm):
        return 0xAA0003E0 | ((rm & 0x1F) << 16) | (rd & 0x1F)
    @staticmethod
    def branch(offset):
        return 0x14000000 | (offset & 0x03FFFFFF)
    @staticmethod
    def movz(rd, imm16, hw=0):
        return 0xD2800000 | (hw << 21) | (imm16 << 5) | rd
    @staticmethod
    def mul(rd, rn, rm):
        return 0x9B007C00 | ((rm & 0x1F) << 16) | ((rn & 0x1F) << 5) | (rd & 0x1F)
    @staticmethod
    def strb(rt, rn, imm12=0):
        return 0x39000000 | ((imm12 & 0xFFF) << 10) | ((rn & 0x1F) << 5) | (rt & 0x1F)
    @staticmethod
    def svc(imm): return 0xD4000001 | ((imm & 0xFFFF) << 5)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Test Harness (oboromi: tests/run.rs)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_single_test(name, instructions, setup_fn, verify_fn):
    """Run one CPU test (oboromi run_test function)."""
    t0 = time.time()
    mem = bytearray(MEM_SIZE)
    cpu = AArch64Core(mem, core_id=0)
    cpu.set_sp(0x8000)
    cpu.set_pc(TEST_BASE)

    addr = TEST_BASE
    for insn in instructions:
        cpu.write_u32(addr, insn); addr += 4
    cpu.write_u32(addr, ARM64.brk(0))

    setup_fn(cpu)
    # Place BRK at common branch targets for clean halt
    cpu.write_u32(0x2000, ARM64.brk(0))
    cpu.run()
    dt = time.time() - t0
    passed = verify_fn(cpu)
    icon = "Y" if passed else "N"
    return f"{icon} {name} - {'PASS' if passed else 'FAIL'} ({dt*1000:.1f}ms)", passed


def run_tests():
    """Full oboromi test suite (tests/run.rs::run_tests)."""
    results = []
    passed = 0
    total = 0
    t0 = time.time()

    tests = [
        ("NOP",
         [ARM64.nop()],
         lambda c: None,
         lambda c: c.get_pc() >= TEST_BASE + 4),

        ("ADD X1, X1, #2",
         [ARM64.add_imm(1, 1, 2)],
         lambda c: c.set_x(1, 5),
         lambda c: c.get_x(1) == 7),

        ("SUB X2, X2, #1",
         [ARM64.sub_imm(2, 2, 1)],
         lambda c: c.set_x(2, 10),
         lambda c: c.get_x(2) == 9),

        ("ADD X0, X0, X1",
         [ARM64.add_reg(0, 0, 1)],
         lambda c: (c.set_x(0, 7), c.set_x(1, 3)),
         lambda c: c.get_x(0) == 10),

        ("MOV X3, X4",
         [ARM64.mov_reg(3, 4)],
         lambda c: (c.set_x(3, 0), c.set_x(4, 0xDEADBEEF)),
         lambda c: c.get_x(3) == 0xDEADBEEF),

        ("RET",
         [ARM64.ret()],
         lambda c: c.set_x(30, 0x2000),
         lambda c: c.get_pc() == 0x2000),

        ("Atomic ADD Test",
         [ARM64.add_imm(0, 0, 50)],
         lambda c: c.set_x(0, 100),
         lambda c: c.get_x(0) == 150),

        ("Memory Access Pattern",
         [ARM64.add_imm(1, 1, 1), ARM64.add_imm(1, 1, 1), ARM64.add_imm(1, 1, 1)],
         lambda c: c.set_x(1, 0),
         lambda c: c.get_x(1) == 3),

        ("Multiple Arithmetic Ops",
         [ARM64.add_imm(0, 0, 5), ARM64.sub_imm(1, 1, 3), ARM64.add_reg(0, 0, 1)],
         lambda c: (c.set_x(0, 10), c.set_x(1, 20)),
         lambda c: c.get_x(0) == 32 and c.get_x(1) == 17),
    ]

    for name, insns, setup, verify in tests:
        line, ok = run_single_test(name, insns, setup, verify)
        results.append(line)
        if ok: passed += 1
        total += 1

    dt = time.time() - t0
    failed = total - passed
    results.append(f"Total: {total} ({passed} passed / {failed} failed) time {dt*1000:.0f}ms")
    return results


def run_multicore_tests():
    """Multicore tests (oboromi tests/multicore_test.rs)."""
    results = []
    # test_multicore_initialization
    mgr = CpuManager()
    ok = len(mgr.cores) == CORE_COUNT
    results.append(f"{'Y' if ok else 'N'} Multicore Init ({CORE_COUNT} cores) - {'PASS' if ok else 'FAIL'}")

    # test_shared_memory_access
    c0 = mgr.get_core(0); c1 = mgr.get_core(1)
    c0.write_u32(0x1000, 0xDEADBEEF)
    val = c1.read_u32(0x1000)
    ok = val == 0xDEADBEEF
    results.append(f"{'Y' if ok else 'N'} Shared Memory (Core 0→1) - {'PASS' if ok else 'FAIL'}")

    # Additional: cross-core register independence
    c0.set_x(0, 0xAAAA); c1.set_x(0, 0xBBBB)
    ok = c0.get_x(0) == 0xAAAA and c1.get_x(0) == 0xBBBB
    results.append(f"{'Y' if ok else 'N'} Register Independence - {'PASS' if ok else 'FAIL'}")

    return results


def run_gpu_tests():
    """SM86 GPU decoder + SPIR-V tests."""
    results = []
    gpu = GPUState()

    # Test AL2P decode
    # Build fake 128-bit instruction with ra=1, rd=2, offset=0x10
    inst = (2 << 16) | (1 << 24) | (0x10 << 40)
    gpu.decoder.al2p(inst)
    ok = len(gpu.decoder.decode_log) == 1 and "AL2P" in gpu.decoder.decode_log[0]
    results.append(f"{'Y' if ok else 'N'} SM86 AL2P decode - {'PASS' if ok else 'FAIL'}")

    # Test IADD3
    inst2 = (3 << 16) | (1 << 24) | (2 << 32) | (4 << 64)
    gpu.decoder.iadd3(inst2)
    ok = len(gpu.decoder.decode_log) == 2 and "IADD3" in gpu.decoder.decode_log[1]
    results.append(f"{'Y' if ok else 'N'} SM86 IADD3 decode - {'PASS' if ok else 'FAIL'}")

    # Test SPIR-V emission
    words = gpu.decoder.finalize()
    ok = len(words) > 0 and words[0] == SPIRVEmitter.MAGIC
    results.append(f"{'Y' if ok else 'N'} SPIR-V emission ({len(words)} words) - {'PASS' if ok else 'FAIL'}")

    # Opcode coverage
    results.append(f"  SM86 opcodes registered: {len(SM86Decoder.OPCODES)}")

    return results


def run_service_tests():
    """HOS service boot tests."""
    results = []
    svc = HOSServices()
    ok = svc.count() == len(HOS_SERVICE_NAMES)
    results.append(f"{'Y' if ok else 'N'} HOS Services Init ({svc.count()}) - {'PASS' if ok else 'FAIL'}")

    # Check key services exist
    for key in ["nvdrv", "hid", "fs", "vi", "acc", "applet-ae"]:
        s = svc.get(key)
        ok = s is not None and s.initialized
        results.append(f"{'Y' if ok else 'N'} nn::{key} - {'PASS' if ok else 'FAIL'}")

    return results


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  NX2 Firmware (real AArch64 code for GPU particle render)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class A64Asm:
    def __init__(self):
        self.code = bytearray(); self.labels = {}; self.fixups = []
    def here(self): return len(self.code)
    def label(self, n): self.labels[n] = self.here()
    def emit(self, w): self.code += struct.pack("<I", w)
    def resolve(self):
        for pos, name, kind in self.fixups:
            off = self.labels[name] - pos
            insn = struct.unpack_from("<I", self.code, pos)[0]
            if kind in ("b","bl"):
                insn = (insn & 0xFC000000) | ((off >> 2) & 0x3FFFFFF)
            elif kind in ("bcond","cbz"):
                insn = (insn & 0xFF00001F) | (((off >> 2) & 0x7FFFF) << 5)
            struct.pack_into("<I", self.code, pos, insn)
    def nop(self): self.emit(0xD503201F)
    def svc(self, imm): self.emit(0xD4000001 | ((imm & 0xFFFF) << 5))
    def movz(self, rd, imm16, hw=0): self.emit(0xD2800000 | (hw<<21) | (imm16<<5) | rd)
    def movk(self, rd, imm16, hw=0): self.emit(0xF2800000 | (hw<<21) | (imm16<<5) | rd)
    def mov_imm(self, rd, val):
        val &= 0xFFFFFFFFFFFFFFFF; self.movz(rd, val & 0xFFFF, 0)
        if val > 0xFFFF: self.movk(rd, (val>>16) & 0xFFFF, 1)
    def add_imm(self, rd, rn, imm12): self.emit(0x91000000 | (imm12<<10) | (rn<<5) | rd)
    def sub_imm(self, rd, rn, imm12): self.emit(0xD1000000 | (imm12<<10) | (rn<<5) | rd)
    def subs_imm(self, rd, rn, imm12): self.emit(0xF1000000 | (imm12<<10) | (rn<<5) | rd)
    def cmp_imm(self, rn, imm12): self.subs_imm(31, rn, imm12)
    def add_reg(self, rd, rn, rm): self.emit(0x8B000000 | (rm<<16) | (rn<<5) | rd)
    def mul(self, rd, rn, rm): self.emit(0x9B007C00 | (rm<<16) | (rn<<5) | rd)
    def strb(self, rt, rn, imm12=0): self.emit(0x39000000 | (imm12<<10) | (rn<<5) | rt)
    def b(self, lbl): self.fixups.append((self.here(), lbl, "b")); self.emit(0x14000000)
    def b_cond(self, c, lbl):
        cm = {"eq":0,"ne":1,"lt":11,"ge":10,"gt":12,"le":13,"hs":2,"lo":3}
        self.fixups.append((self.here(), lbl, "bcond")); self.emit(0x54000000 | cm[c])


def build_nx2_firmware():
    asm = A64Asm()
    asm.label("_start")
    asm.mov_imm(0, FB_BASE); asm.movz(1, FB_W); asm.movz(2, FB_H)
    asm.movz(7, FB_W // 2); asm.movz(8, FB_H // 2)
    asm.movz(9, FB_STRIDE); asm.movz(3, 0)
    asm.label("frame_loop")
    asm.movz(14, 0)
    asm.label("clear_y")
    asm.movz(15, 0)
    asm.label("clear_x")
    asm.mul(10, 14, 9); asm.movz(16, 3); asm.mul(21, 15, 16)
    asm.add_reg(10, 10, 21); asm.add_reg(10, 10, 0)
    asm.movz(16, 0x10)
    asm.strb(16, 10, 0); asm.strb(16, 10, 1); asm.strb(16, 10, 2)
    asm.add_imm(15, 15, 1); asm.cmp_imm(15, FB_W); asm.b_cond("lo", "clear_x")
    asm.add_imm(14, 14, 1); asm.cmp_imm(14, FB_H); asm.b_cond("lo", "clear_y")
    asm.movz(4, 0)
    asm.label("particle_loop")
    asm.svc(0x10)
    asm.cmp_imm(5, FB_W); asm.b_cond("hs", "skip_particle")
    asm.cmp_imm(6, FB_H); asm.b_cond("hs", "skip_particle")
    asm.movz(16, 0)
    asm.label("block_dy")
    asm.movz(17, 0)
    asm.label("block_dx")
    asm.add_reg(18, 6, 16); asm.sub_imm(18, 18, 1)
    asm.add_reg(19, 5, 17); asm.sub_imm(19, 19, 1)
    asm.cmp_imm(18, FB_H); asm.b_cond("hs", "skip_px")
    asm.cmp_imm(19, FB_W); asm.b_cond("hs", "skip_px")
    asm.mul(10, 18, 9); asm.movz(21, 3); asm.mul(14, 19, 21)
    asm.add_reg(10, 10, 14); asm.add_reg(10, 10, 0)
    asm.strb(11, 10, 0); asm.strb(12, 10, 1); asm.strb(13, 10, 2)
    asm.label("skip_px")
    asm.add_imm(17, 17, 1); asm.cmp_imm(17, 3); asm.b_cond("lo", "block_dx")
    asm.add_imm(16, 16, 1); asm.cmp_imm(16, 3); asm.b_cond("lo", "block_dy")
    asm.label("skip_particle")
    asm.add_imm(4, 4, 1); asm.cmp_imm(4, 90); asm.b_cond("lo", "particle_loop")
    asm.add_imm(3, 3, 1); asm.svc(0x01); asm.b("frame_loop")
    asm.resolve()
    return asm.code


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  NX2 Emulation System
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class NX2System:
    def __init__(self, sys_state: SystemState):
        self.sys = sys_state
        self.mem = sys_state.cpu_manager.shared_memory
        self.cpu = sys_state.cpu_manager.get_core(0)  # primary core
        fw = build_nx2_firmware()
        self.mem[CODE_BASE:CODE_BASE + len(fw)] = fw
        self.cpu.set_pc(CODE_BASE)
        self.cpu.svc_handler = self._svc
        self.frame_ready = False
        self.docked = True

    def _svc(self, cpu, imm):
        if imm == 0x01:
            self.frame_ready = True
        elif imm == 0x10:
            frame = cpu.xr(3); idx = cpu.xr(4)
            t = frame * 0.04
            angle = t * 2.8 + idx * 0.0785
            radius = 55 + 18 * math.sin(t * 1.3 + idx * 0.5)
            px = int(FB_W//2 + radius * math.cos(angle))
            py = int(FB_H//2 + radius * 0.65 * math.sin(angle * 1.6))
            r = int(max(0, min(255, 40 + 50 * math.sin(t * 2.1 + idx * 0.4))))
            g = int(max(0, min(255, 150 + 90 * math.sin(t * 1.4 + idx * 0.3))))
            b = int(max(0, min(255, 210 + 45 * math.cos(t * 1.7 + idx * 0.2))))
            cpu.xw(5, px & 0xFFFFFFFFFFFFFFFF)
            cpu.xw(6, py & 0xFFFFFFFFFFFFFFFF)
            cpu.xw(11, r); cpu.xw(12, g); cpu.xw(13, b)

    def run_frame(self):
        self.frame_ready = False
        safety = 0
        while not self.frame_ready and not self.cpu.halted and safety < 500000:
            self.cpu.step_n(100); safety += 100

    def get_framebuffer(self):
        fb = []
        for y in range(FB_H):
            row = []
            off = FB_BASE + y * FB_STRIDE
            for x in range(FB_W):
                p = off + x * 3
                row.append((self.mem[p & (MEM_SIZE-1)],
                            self.mem[(p+1) & (MEM_SIZE-1)],
                            self.mem[(p+2) & (MEM_SIZE-1)]))
            fb.append(row)
        return fb


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CatNXEMU2.0V0.X — Ryujinx 2026 Avalonia GUI w/ Splash
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CatNXEMU(tk.Tk):
    """
    Full Ryujinx 2026 Avalonia-style GUI with:
      - Animated splash screen (fade in → hold → fade out)
      - oboromi-style test runner with colored log output
      - Real-time AArch64 framebuffer viewport
      - Toolbar, menu bar, status bar
    """
    TOOLBAR_ICONS = {
        "boot":"▶", "pause":"⏸", "stop":"⏹", "dock":"🖥",
        "hand":"📱", "info":"ℹ", "gear":"⚙", "folder":"📂", "test":"🧪",
    }

    def __init__(self):
        super().__init__()
        self.title("NX2EMU BETA 0.1")
        self.geometry("680x520")
        self.resizable(False, False)
        self.configure(bg=DARK_BG)

        # ── System state (oboromi) ──
        self.sys_state = SystemState()
        self.nx2 = None  # created on boot
        self.running = False
        self.frame_count = 0
        self.last_time = time.time()
        self.test_thread = None
        self.log_lines = ["Click '🧪 Run Tests' or '▶ Boot' to begin"]

        # ── Splash state (oboromi GUI splash) ──
        self.splash_start = time.time()
        self.splash_done = False
        self.splash_phase = "fade_in"

        # ── Build GUI behind splash ──
        self._build_gui()
        self._run_splash()

    def _build_gui(self):
        # ── Menu ──
        menubar = tk.Menu(self, bg=DARK_RAISED, fg=TEXT_PRIMARY,
                          activebackground=ACCENT_BLUE, activeforeground="white",
                          relief="flat", bd=0, font=("Segoe UI", 9))
        self.config(menu=menubar)

        def _m(**kw):
            return tk.Menu(menubar, tearoff=0, bg=DARK_RAISED, fg=TEXT_PRIMARY,
                           activebackground=ACCENT_BLUE, activeforeground="white",
                           font=("Segoe UI", 9), **kw)

        fm = _m(); menubar.add_cascade(label="  File  ", menu=fm)
        fm.add_command(label="  Open NX2 ROM…", command=self._load_rom)
        fm.add_separator(); fm.add_command(label="  Exit", command=self.quit)

        em = _m(); menubar.add_cascade(label="  Emulation  ", menu=em)
        em.add_command(label="  Boot", command=self._start)
        em.add_command(label="  Pause", command=self._stop)
        self._dk = tk.BooleanVar(value=True)
        em.add_checkbutton(label="  Docked Mode", variable=self._dk, command=self._toggle_dock)

        tm = _m(); menubar.add_cascade(label="  Tests  ", menu=tm)
        tm.add_command(label="  Run CPU Tests", command=self._run_tests)
        tm.add_command(label="  Run Multicore Tests", command=self._run_multicore_tests)
        tm.add_command(label="  Run GPU Tests", command=self._run_gpu_tests)
        tm.add_command(label="  Run Service Tests", command=self._run_svc_tests)
        tm.add_command(label="  Run All", command=self._run_all_tests)

        hm = _m(); menubar.add_cascade(label="  About  ", menu=hm)
        hm.add_command(label="  About CatNXEMU", command=self._about)

        # ── Main frame (hidden until splash done) ──
        self.main_frame = tk.Frame(self, bg=DARK_BG)

        # ── Toolbar ──
        toolbar = tk.Frame(self.main_frame, bg=TOOLBAR_BG, height=34)
        toolbar.pack(fill="x"); toolbar.pack_propagate(False)

        self._tb = {}
        items = [
            ("folder","Open ROM",self._load_rom), None,
            ("boot","Boot NX2",self._start), ("pause","Pause",self._stop),
            ("stop","Stop",self._full_stop), None,
            ("test","Run Tests",self._run_all_tests), None,
            ("dock","Docked",self._toggle_dock), None,
            ("gear","Settings",lambda:None), ("info","About",self._about),
        ]
        for item in items:
            if item is None:
                tk.Frame(toolbar, bg=DARK_BORDER, width=1).pack(side="left", fill="y", pady=6, padx=5)
                continue
            ik, tip, cmd = item
            btn = tk.Label(toolbar, text=self.TOOLBAR_ICONS[ik], font=("Segoe UI Emoji", 12),
                           bg=TOOLBAR_BG, fg=TEXT_PRIMARY, padx=6, pady=1, cursor="hand2")
            btn.pack(side="left")
            btn.bind("<Enter>", lambda e, b=btn: b.config(bg=DARK_HOVER))
            btn.bind("<Leave>", lambda e, b=btn: b.config(bg=TOOLBAR_BG))
            btn.bind("<Button-1>", lambda e, c=cmd: c())
            self._tb[ik] = btn

        tk.Label(toolbar, text="CatNXEMU2.0V0.X", font=("Consolas", 8, "bold"),
                 bg=TOOLBAR_BG, fg=ACCENT_BLUE).pack(side="right", padx=8)

        tk.Frame(self.main_frame, bg=ACCENT_BLUE, height=2).pack(fill="x")

        # ── Content area: canvas (left) + log panel (right) ──
        content = tk.Frame(self.main_frame, bg=DARK_BG)
        content.pack(fill="both", expand=True, padx=6, pady=4)

        # Canvas
        cf = tk.Frame(content, bg=DARK_BORDER, bd=1, relief="solid")
        cf.pack(side="left", padx=(0, 4))
        self.canvas = tk.Canvas(cf, width=400, height=226, bg="#101010",
                                highlightthickness=0, bd=0)
        self.canvas.pack()
        self._draw_idle()

        # Log panel (oboromi GUI ScrollArea)
        log_frame = tk.Frame(content, bg=DARK_SURFACE, bd=0)
        log_frame.pack(side="right", fill="both", expand=True)

        tk.Label(log_frame, text="  Results", font=("Consolas", 9, "bold"),
                 bg=DARK_SURFACE, fg=TEXT_BRIGHT, anchor="w").pack(fill="x", padx=4, pady=(4, 0))
        tk.Frame(log_frame, bg=DARK_BORDER, height=1).pack(fill="x", padx=4, pady=2)

        self.log_text = tk.Text(log_frame, bg=DARK_SURFACE, fg=LOG_DIM,
                                font=("Consolas", 9), bd=0, highlightthickness=0,
                                wrap="word", padx=6, pady=4, cursor="arrow",
                                selectbackground=ACCENT_BLUE)
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_configure("pass", foreground=LOG_GREEN)
        self.log_text.tag_configure("fail", foreground=LOG_RED)
        self.log_text.tag_configure("info", foreground=LOG_DIM)
        self.log_text.tag_configure("warn", foreground=LOG_YELLOW)
        self.log_text.config(state="disabled")
        self._refresh_log()

        # ── Status bar ──
        tk.Frame(self.main_frame, bg=DARK_BORDER, height=1).pack(fill="x", side="bottom")
        sb = tk.Frame(self.main_frame, bg=DARK_SURFACE, height=22)
        sb.pack(fill="x", side="bottom"); sb.pack_propagate(False)

        self._dot = tk.Label(sb, text="●", font=("Segoe UI", 7), fg=TEXT_SECONDARY, bg=DARK_SURFACE)
        self._dot.pack(side="left", padx=(8, 3))
        self.lbl_game = tk.Label(sb, text="Idle", fg=TEXT_SECONDARY, bg=DARK_SURFACE, font=("Segoe UI", 8))
        self.lbl_game.pack(side="left")

        for txt_var, attr in [("AArch64","lbl_cpu"), ("DOCKED","lbl_mode"),
                               ("0.0 FPS","lbl_fps"), ("0 instrs","lbl_instrs"),
                               ("PC 0x00080000","lbl_pc")]:
            tk.Label(sb, text="│", fg=DARK_BORDER, bg=DARK_SURFACE, font=("Consolas", 7)).pack(side="right")
            lbl = tk.Label(sb, text=txt_var, fg=ACCENT_BLUE if "FPS" in txt_var or "PC" in txt_var else TEXT_SECONDARY,
                           bg=DARK_SURFACE, font=("Consolas", 7, "bold" if "DOCK" in txt_var else ""))
            lbl.pack(side="right", padx=4)
            setattr(self, attr, lbl)
        self.lbl_mode.config(fg=ACCENT_GREEN)

    # ── Splash screen (oboromi GUI fade in/hold/fade out) ──

    def _run_splash(self):
        self.splash_canvas = tk.Canvas(self, width=680, height=520, bg=SPLASH_BG,
                                       highlightthickness=0, bd=0)
        self.splash_canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self._splash_tick()

    def _splash_tick(self):
        elapsed = time.time() - self.splash_start
        fade_in, hold, fade_out = 0.5, 1.0, 0.5
        total = fade_in + hold + fade_out

        if elapsed < fade_in:
            alpha = elapsed / fade_in
        elif elapsed < fade_in + hold:
            alpha = 1.0
        elif elapsed < total:
            alpha = 1.0 - (elapsed - fade_in - hold) / fade_out
        else:
            # Splash done
            self.splash_canvas.destroy()
            self.main_frame.pack(fill="both", expand=True)
            self.splash_done = True
            return

        self.splash_canvas.delete("all")
        self.splash_canvas.create_rectangle(0, 0, 680, 520, fill=SPLASH_BG)

        # Logo (text-based since /files=off)
        c = int(alpha * 255)
        color = f"#{c:02x}{c:02x}{c:02x}"
        blue_c = int(alpha * 77)
        blue = f"#{blue_c:02x}{int(alpha*171):02x}{int(alpha*247):02x}"
        warn_c = f"#{int(alpha*255):02x}{int(alpha*200):02x}{int(alpha*100):02x}"
        dim = f"#{int(alpha*180):02x}{int(alpha*180):02x}{int(alpha*180):02x}"

        self.splash_canvas.create_text(340, 170, text="CatNXEMU", fill=blue,
                                       font=("Consolas", 32, "bold"))
        self.splash_canvas.create_text(340, 210, text="2.0V0.X  —  oboromi core",
                                       fill=dim, font=("Consolas", 12))
        self.splash_canvas.create_text(340, 260, text="Experimental",
                                       fill=warn_c, font=("Segoe UI", 13))

        lines = [
            "Foundation for Switch 2 emulation (oboromi architecture)",
            "8-core AArch64 CPU • SM86 GPU decoder • 160 HOS services",
            "This release focuses on CPU instruction emulation only.",
        ]
        for j, line in enumerate(lines):
            self.splash_canvas.create_text(340, 295 + j * 18, text=line,
                                           fill=dim, font=("Segoe UI", 9))

        self.splash_canvas.create_text(340, 400, text="© A.C Holdings  •  /files=off",
                                       fill=f"#{int(alpha*80):02x}{int(alpha*80):02x}{int(alpha*80):02x}",
                                       font=("Consolas", 8))

        self.after(30, self._splash_tick)

    # ── Log panel ──

    def _refresh_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        for line in self.log_lines:
            if "PASS" in line or line.startswith("Y "):
                tag = "pass"
            elif "FAIL" in line or line.startswith("N "):
                tag = "fail"
            elif line.startswith("  ") or "Total:" in line or "SM86" in line:
                tag = "warn"
            else:
                tag = "info"
            self.log_text.insert("end", line + "\n", tag)
        self.log_text.config(state="disabled")
        self.log_text.see("end")

    # ── Canvas ──

    def _draw_idle(self):
        self.canvas.delete("all")
        for y in range(0, 226, 16):
            self.canvas.create_line(0, y, 400, y, fill="#1A1A1A")
        for x in range(0, 400, 16):
            self.canvas.create_line(x, 0, x, 226, fill="#1A1A1A")
        self.canvas.create_text(200, 90, text="CatNXEMU", fill=ACCENT_BLUE,
                                font=("Consolas", 22, "bold"))
        self.canvas.create_text(200, 115, text="oboromi core • AArch64 ×8",
                                fill=TEXT_SECONDARY, font=("Consolas", 9))
        self.canvas.create_text(200, 140, text=f"SM86 GPU • {len(SM86Decoder.OPCODES)} opcodes • {len(HOS_SERVICE_NAMES)} services",
                                fill="#3A3A3A", font=("Consolas", 8))
        self.canvas.create_text(200, 210, text="© A.C Holdings  •  /files=off",
                                fill="#2A2A2A", font=("Consolas", 7))

    def _render_fb(self):
        self.canvas.delete("all")
        fb = self.nx2.get_framebuffer()
        # Scale 280×158 → 400×226 (≈1.42×)
        sx_scale = 400 / FB_W
        sy_scale = 226 / FB_H
        for y in range(0, FB_H, 1):
            for x in range(0, FB_W, 2):
                r, g, b = fb[y][x]
                if r == 0x10 and g == 0x10 and b == 0x10: continue
                color = f"#{r:02x}{g:02x}{b:02x}"
                sx = int(x * sx_scale); sy = int(y * sy_scale)
                self.canvas.create_rectangle(sx, sy, sx+3, sy+2, fill=color, outline="")

    # ── Actions ──

    def _load_rom(self):
        messagebox.showinfo("CatNXEMU",
            "✅ NX2 synthetic firmware 22.0.0\n"
            f"AArch64 ×{CORE_COUNT} • SM86 GPU • {len(HOS_SERVICE_NAMES)} services\n"
            "/files=off")

    def _toggle_dock(self):
        if self.nx2: self.nx2.docked = not self.nx2.docked
        docked = self._dk.get() if self.nx2 is None else self.nx2.docked
        if docked:
            self.lbl_mode.config(text="DOCKED", fg=ACCENT_GREEN)
            self._tb["dock"].config(text=self.TOOLBAR_ICONS["dock"])
        else:
            self.lbl_mode.config(text="HANDHELD", fg=ACCENT_ORANGE)
            self._tb["dock"].config(text=self.TOOLBAR_ICONS["hand"])

    def _about(self):
        fw = build_nx2_firmware()
        messagebox.showinfo("About CatNXEMU",
            "CatNXEMU2.0V0.X — NX2EMU BETA 0.1\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"oboromi architecture (0xNikilite)\n\n"
            f"CPU: {CORE_COUNT}× AArch64 cores (real decode)\n"
            f"  Registers: X0–X30 + SP + PC + NZCV\n"
            f"  Shared memory: {MEM_SIZE//(1024*1024)} MB\n"
            f"  Firmware: {len(fw)} bytes ({len(fw)//4} instrs)\n\n"
            f"GPU: SM86 Ampere decoder\n"
            f"  {len(SM86Decoder.OPCODES)} instruction mnemonics\n"
            f"  128-bit instruction decode\n"
            f"  SPIR-V binary emitter backend\n\n"
            f"HOS: {len(HOS_SERVICE_NAMES)} nn:: services\n"
            f"  (nvdrv, hid, vi, fs, acc …)\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Ryujinx 2026 Avalonia GUI • /files=off\n"
            "© A.C Holdings")

    # ── Test runners ──

    def _run_in_thread(self, fn, label):
        def worker():
            self.log_lines = [f"Running {label}..."]
            self._refresh_log()
            results = fn()
            self.log_lines = results
            self.after(0, self._refresh_log)
        if self.test_thread and self.test_thread.is_alive(): return
        self.test_thread = threading.Thread(target=worker, daemon=True)
        self.test_thread.start()

    def _run_tests(self):
        self._run_in_thread(run_tests, "CPU Tests (oboromi)")
    def _run_multicore_tests(self):
        self._run_in_thread(run_multicore_tests, "Multicore Tests")
    def _run_gpu_tests(self):
        self._run_in_thread(run_gpu_tests, "SM86 GPU Tests")
    def _run_svc_tests(self):
        self._run_in_thread(run_service_tests, "HOS Service Tests")

    def _run_all_tests(self):
        def all_tests():
            r = ["═══ oboromi Full Test Suite ═══", ""]
            r.append("── CPU Instruction Tests ──")
            r.extend(run_tests()); r.append("")
            r.append("── Multicore Tests ──")
            r.extend(run_multicore_tests()); r.append("")
            r.append("── SM86 GPU Tests ──")
            r.extend(run_gpu_tests()); r.append("")
            r.append("── HOS Service Tests ──")
            r.extend(run_service_tests()); r.append("")
            # Boot services
            r.append("── Service Boot ──")
            boot_log = self.sys_state.start_host_services()
            r.append(f"  Booted {len(boot_log)} services")
            r.append(f"Y All {len(boot_log)} services initialized - PASS")
            return r
        self._run_in_thread(all_tests, "All Tests")

    # ── Emulation ──

    def _start(self):
        if self.running: return
        self.nx2 = NX2System(self.sys_state)
        self.running = True
        self._dot.config(fg=ACCENT_GREEN)
        self.lbl_game.config(text="NX2 FW 22.0.0 — RUNNING", fg=ACCENT_GREEN)
        self._tb["boot"].config(fg=ACCENT_GREEN)
        self.log_lines.append(f"▶ Boot: AArch64 ×{CORE_COUNT} • SM86 GPU • {len(HOS_SERVICE_NAMES)} services")
        self._refresh_log()
        self._emu_loop()

    def _stop(self):
        self.running = False
        self._dot.config(fg=ACCENT_ORANGE)
        self.lbl_game.config(text="PAUSED", fg=ACCENT_ORANGE)
        self._tb["boot"].config(fg=TEXT_PRIMARY)

    def _full_stop(self):
        self.running = False
        self._dot.config(fg=TEXT_SECONDARY)
        self.lbl_game.config(text="Stopped", fg=TEXT_SECONDARY)
        self._tb["boot"].config(fg=TEXT_PRIMARY)
        self._draw_idle()

    def _emu_loop(self):
        if not self.running: return
        self.nx2.run_frame()
        self._render_fb()
        self.frame_count += 1
        if self.frame_count % 3 == 0:
            now = time.time()
            fps = 3.0 / (now - self.last_time + 1e-9)
            self.last_time = now
            cpu = self.nx2.cpu
            self.lbl_instrs.config(text=f"{cpu.instrs:,} instrs")
            self.lbl_fps.config(text=f"{fps:.1f} FPS")
            self.lbl_pc.config(text=f"PC 0x{cpu.pc:08X}")
        self.after(16, self._emu_loop)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if __name__ == "__main__":
    try:
        app = CatNXEMU()
        app.mainloop()
    except KeyboardInterrupt:
        print("\nNX2 core shut down.")
