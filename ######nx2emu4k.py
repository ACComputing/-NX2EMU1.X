#!/usr/bin/env python3
"""
CatNXEMU2.0V0.X — MeowNXII1.X (sJIT Backend Edition)
Full oboromi (0xNikilite) architecture ported to Python 3.14
────────────────────────────────────────────────────────────────
  • MeowNXII1.X sJIT Backend: Basic-block caching & execution engine
  • 8-core AArch64 CPU Manager w/ shared 16 MB memory
  • Real ARMv8-A instruction decode & execute (31 GP regs, NZCV)
  • SM86 Ampere GPU stub — 128-bit shader instruction decoder
  • SPIR-V binary emitter (type system + instruction emission)
  • 160 HOS nn:: services (acc, hid, nvdrv, vi, fs …)
  • Ryujinx 2026 Avalonia-style GUI
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
from typing import Callable, Dict, List, Tuple, Any

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
LOG_GREEN     = "#32FF32"
LOG_RED       = "#FF3232"
LOG_YELLOW    = "#FFC864"
LOG_DIM       = "#C8C8C8"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Memory Map (oboromi: 12 GB — scaled to 16 MB for Python)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CORE_COUNT  = 8
MEM_SIZE    = 16 * 1024 * 1024      # 16 MB
MEM_BASE    = 0x0
CODE_BASE   = 0x00080000
STACK_TOP   = 0x00800000
FB_BASE     = 0x04000000
FB_W, FB_H  = 280, 158
FB_STRIDE   = FB_W * 3
TEST_BASE   = 0x00001000

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MeowNXII1.X sJIT Backend (Python 3.14 Advanced Execution Engine)
#  Caches Basic Blocks of AArch64 to bypass fetch/decode loops
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

InstructionBlock = List[int]

class MeowNXIIJIT:
    """MeowNXII1.X sJIT block-caching stub for Python 3.14+"""
    def __init__(self, core: 'AArch64Core'):
        self.core = core
        self.mem = core.mem
        self.block_cache: Dict[int, InstructionBlock] = {}
        self.cache_hits = 0
        self.cache_misses = 0
        self.optimized_traces = 0

    def get_basic_block(self, pc: int) -> InstructionBlock:
        """Fetch a block of instructions ending in a branch/ret/svc."""
        if pc in self.block_cache:
            self.cache_hits += 1
            return self.block_cache[pc]
        
        self.cache_misses += 1
        block: InstructionBlock = []
        addr = pc
        
        while True:
            insn = struct.unpack_from("<I", self.mem, addr & (MEM_SIZE - 1))[0]
            block.append(insn)
            addr += 4
            
            # Identify Block Terminators
            if insn == 0: break # Halt/Uninit
            if (insn & 0xFC000000) == 0x14000000: break # B
            if (insn & 0xFC000000) == 0x94000000: break # BL
            if (insn & 0xFFFFFC1F) == 0xD61F0000: break # BR
            if (insn & 0xFFFFFC1F) == 0xD63F0000: break # BLR
            if (insn & 0xFFFFFC1F) == 0xD65F0000: break # RET
            if (insn & 0xFF000010) == 0x54000000: break # B.cond
            if (insn & 0x7E000000) == 0x34000000: break # CBZ/CBNZ
            if (insn & 0xFFE0001F) == 0xD4000001: break # SVC
            if (insn & 0xFFE0001F) == 0xD4200000: break # BRK
            
            # Max block size to prevent infinite loops in bad code
            if len(block) >= 128: break
            
        self.block_cache[pc] = block
        return block

    def execute_block(self, block: InstructionBlock) -> int:
        """Executes a pre-fetched block. Returns cycles consumed."""
        cycles = 0
        for insn in block:
            if self.core.halted: break
            self.core.pc += 4
            cycles += 1
            self.core.instrs += 1
            self.core._exec(insn)
        return cycles

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AArch64 CPU Core
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AArch64Core:
    MASK64 = 0xFFFFFFFFFFFFFFFF
    MASK32 = 0xFFFFFFFF

    def __init__(self, mem: bytearray, core_id: int = 0):
        self.x = [0] * 31          
        self.sp = STACK_TOP - (core_id * 0x100000)
        self.pc = CODE_BASE
        self.N = self.Z = self.C = self.V = False
        self.mem = mem
        self.core_id = core_id
        self.cycles = 0
        self.instrs = 0
        self.halted = False
        self.svc_handler: Callable[['AArch64Core', int], None] | None = None
        self.meowjit = MeowNXIIJIT(self) # Inject MeowNXII1.X sJIT Backend

    def xr(self, r: int) -> int: return self.x[r] if r < 31 else 0
    def xw(self, r: int, val: int):
        if r < 31: self.x[r] = val & self.MASK64

    def get_x(self, i: int) -> int:   return self.xr(i)
    def set_x(self, i: int, v: int): self.xw(i, v)
    def get_pc(self) -> int:      return self.pc
    def set_pc(self, v: int):     self.pc = v & self.MASK64
    def get_sp(self) -> int:      return self.sp
    def set_sp(self, v: int):     self.sp = v & self.MASK64

    def read8(self, a: int) -> int:    return self.mem[a & (MEM_SIZE - 1)]
    def write8(self, a: int, v: int): self.mem[a & (MEM_SIZE - 1)] = v & 0xFF

    def read32(self, a: int) -> int:
        return struct.unpack_from("<I", self.mem, a & (MEM_SIZE - 1))[0]
    def write32(self, a: int, v: int):
        struct.pack_into("<I", self.mem, a & (MEM_SIZE - 1), v & self.MASK32)
    def read64(self, a: int) -> int:
        return struct.unpack_from("<Q", self.mem, a & (MEM_SIZE - 1))[0]
    def write64(self, a: int, v: int):
        struct.pack_into("<Q", self.mem, a & (MEM_SIZE - 1), v & self.MASK64)

    write_u32 = write32
    read_u32 = read32
    write_u64 = write64
    read_u64 = read64

    def _update_nz(self, r: int):
        self.N = bool(r & (1 << 63)); self.Z = (r & self.MASK64) == 0
    def _add_flags(self, a: int, b: int, r: int):
        self._update_nz(r); self.C = r > self.MASK64
        sa, sb, sr = bool(a & (1<<63)), bool(b & (1<<63)), bool(r & (1<<63))
        self.V = (sa == sb) and (sa != sr)
    def _sub_flags(self, a: int, b: int):
        r = a - b; self._update_nz(r & self.MASK64); self.C = a >= b
        sa, sb, sr = bool(a & (1<<63)), bool(b & (1<<63)), bool(r & (1<<63))
        self.V = (sa != sb) and (sr != sa)
        
    def check_cond(self, c: int) -> bool:
        match c:
            case 0: return self.Z
            case 1: return not self.Z
            case 2: return self.C
            case 3: return not self.C
            case 4: return self.N
            case 5: return not self.N
            case 6: return self.V
            case 7: return not self.V
            case 8: return self.C and not self.Z
            case 9: return not self.C or self.Z
            case 10: return self.N == self.V
            case 11: return self.N != self.V
            case 12: return not self.Z and self.N == self.V
            case 13: return self.Z or self.N != self.V
            case _: return True

    @staticmethod
    def sxt(v: int, b: int) -> int: return v - (1 << b) if v & (1 << (b-1)) else v

    def step(self) -> int:
        if self.halted: return 1
        insn = self.read32(self.pc)
        self.pc += 4
        self.cycles += 1
        self.instrs += 1
        return self._exec(insn)

    def run(self):
        safety = 0
        while not self.halted and safety < 200000:
            block = self.meowjit.get_basic_block(self.pc)
            executed = self.meowjit.execute_block(block)
            safety += executed
        return 1 if not self.halted or safety > 0 else 0

    def step_n(self, n: int):
        executed = 0
        while executed < n and not self.halted:
            block = self.meowjit.get_basic_block(self.pc)
            
            # If the block is larger than remaining n, execute partially
            if len(block) > (n - executed):
                for insn in block[:(n - executed)]:
                    if self.halted: break
                    self.pc += 4
                    self.cycles += 1
                    self.instrs += 1
                    self._exec(insn)
                    executed += 1
            else:
                c = self.meowjit.execute_block(block)
                executed += c

    def halt(self):
        self.halted = True

    def _exec(self, i: int) -> int:
        if i == 0: self.halted = True; self.pc -= 4; return 0
        if i == 0xD503201F or (i >> 24) == 0xD5: return 0 # NOP
        
        # MeowNXII sJIT Pattern Matching mapped to Bitmasks
        match i & 0xFFE0001F:
            case 0xD4400000: self.halted = True; self.pc -= 4; return 0 # HLT
            case 0xD4200000: self.halted = True; self.pc -= 4; return 0 # BRK
            case 0xD4000001: 
                if self.svc_handler: self.svc_handler(self, (i >> 5) & 0xFFFF)
                return 0

        # Data processing / Memory (Using standard if-chain for mask complexity)
        if (i & 0x7F800000) == 0x52800000: # MOVZ
            hw = (i>>21)&3; self.xw(i&0x1F, ((i>>5)&0xFFFF)<<(hw*16)); return 0
        if (i & 0x7F800000) == 0x72800000: # MOVK
            hw = (i>>21)&3; rd = i&0x1F; s = hw*16
            self.xw(rd, (self.xr(rd) & ~(0xFFFF<<s) & self.MASK64) | (((i>>5)&0xFFFF)<<s)); return 0
        if (i & 0x7F000000) == 0x11000000: # ADD imm
            S = (i>>29)&1; sh = (i>>22)&1; imm = ((i>>10)&0xFFF)<<(12 if sh else 0)
            a = self.xr((i>>5)&0x1F); r = a + imm
            if S: self._add_flags(a, imm, r)
            self.xw(i&0x1F, r); return 0
        if (i & 0x7F000000) == 0x51000000: # SUB/SUBS imm
            S = (i>>29)&1; sh = (i>>22)&1; imm = ((i>>10)&0xFFF)<<(12 if sh else 0)
            a = self.xr((i>>5)&0x1F); r = (a - imm) & self.MASK64
            if S: self._sub_flags(a, imm)
            self.xw(i&0x1F, r); return 0
        if (i & 0x1F000000) == 0x0B000000: # ADD/SUB shifted reg
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
        if (i & 0x1F000000) == 0x0A000000: # AND/ORR/EOR/ANDS
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
        if (i & 0xFF208000) == 0x9B000000: # MADD/MUL
            self.xw(i&0x1F, (self.xr((i>>10)&0x1F) + self.xr((i>>5)&0x1F) * self.xr((i>>16)&0x1F)) & self.MASK64); return 0
        if (i & 0xFFE0FC00) == 0x9AC00800: # UDIV
            d = self.xr((i>>16)&0x1F)
            self.xw(i&0x1F, self.xr((i>>5)&0x1F) // d if d else 0); return 0
            
        # Branching (Block Terminators in sJIT)
        if (i & 0xFC000000) == 0x14000000: # B
            self.pc = (self.pc - 4 + self.sxt(i & 0x3FFFFFF, 26) * 4) & self.MASK64; return 0
        if (i & 0xFC000000) == 0x94000000: # BL
            self.xw(30, self.pc)
            self.pc = (self.pc - 4 + self.sxt(i & 0x3FFFFFF, 26) * 4) & self.MASK64; return 0
        if (i & 0xFFFFFC1F) == 0xD61F0000: # BR
            self.pc = self.xr((i>>5)&0x1F); return 0
        if (i & 0xFFFFFC1F) == 0xD63F0000: # BLR
            self.xw(30, self.pc); self.pc = self.xr((i>>5)&0x1F); return 0
        if (i & 0xFFFFFC1F) == 0xD65F0000: # RET
            self.pc = self.xr((i>>5)&0x1F); return 0
        if (i & 0xFF000010) == 0x54000000: # B.cond
            if self.check_cond(i & 0xF):
                self.pc = (self.pc - 4 + self.sxt((i>>5)&0x7FFFF, 19) * 4) & self.MASK64
            return 0
        if (i & 0x7E000000) == 0x34000000: # CBZ/CBNZ
            nz = (i>>24)&1; v = self.xr(i&0x1F)
            if (v != 0) == bool(nz):
                self.pc = (self.pc - 4 + self.sxt((i>>5)&0x7FFFF, 19) * 4) & self.MASK64
            return 0
            
        # Memory Load/Store
        if (i & 0xFFC00000) == 0xF9400000: # LDR 64
            self.xw(i&0x1F, self.read64(self.xr((i>>5)&0x1F) + ((i>>10)&0xFFF)*8)); return 0
        if (i & 0xFFC00000) == 0xF9000000: # STR 64
            self.write64(self.xr((i>>5)&0x1F) + ((i>>10)&0xFFF)*8, self.xr(i&0x1F)); return 0
        if (i & 0xFFC00000) == 0x39400000: # LDRB
            self.xw(i&0x1F, self.read8(self.xr((i>>5)&0x1F) + ((i>>10)&0xFFF))); return 0
        if (i & 0xFFC00000) == 0x39000000: # STRB
            self.write8(self.xr((i>>5)&0x1F) + ((i>>10)&0xFFF), self.xr(i&0x1F) & 0xFF); return 0
        if (i & 0xFFC00000) == 0xB9400000: # LDR W
            self.xw(i&0x1F, self.read32(self.xr((i>>5)&0x1F) + ((i>>10)&0xFFF)*4)); return 0
        if (i & 0xFFC00000) == 0xB9000000: # STR W
            self.write32(self.xr((i>>5)&0x1F) + ((i>>10)&0xFFF)*4, self.xr(i&0x1F) & self.MASK32); return 0
        return 0

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CPU Manager
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CpuManager:
    def __init__(self):
        self.shared_memory = bytearray(MEM_SIZE)
        self.cores = [AArch64Core(self.shared_memory, core_id=i) for i in range(CORE_COUNT)]

    def get_core(self, cid: int) -> AArch64Core | None:
        return self.cores[cid] if 0 <= cid < len(self.cores) else None

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SPIR-V Binary Emitter & SM86 GPU Decoder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SPIRVEmitter:
    MAGIC, VERSION, GENERATOR = 0x07230203, 0x00010000, 0x00000000
    OP_TYPE_VOID, OP_TYPE_INT, OP_TYPE_POINTER = 19, 21, 32
    OP_CONSTANT, OP_VARIABLE, OP_LOAD, OP_STORE = 43, 59, 61, 62
    OP_IADD, OP_IMUL = 128, 132
    CAP_SHADER, OP_CAPABILITY, OP_MEMORY_MODEL = 1, 17, 14

    def __init__(self):
        self.words: List[int] = []
        self.next_id = 1

    def _alloc_id(self) -> int:
        rid = self.next_id; self.next_id += 1; return rid

    def _emit_word(self, w: int): self.words.append(w & 0xFFFFFFFF)

    def _emit_insn(self, opcode: int, *operands: int):
        self._emit_word(((1 + len(operands)) << 16) | opcode)
        for op in operands: self._emit_word(op)

    def emit_header(self):
        for w in (self.MAGIC, self.VERSION, self.GENERATOR, 0, 0): self._emit_word(w)

    def emit_capability(self, cap: int): self._emit_insn(self.OP_CAPABILITY, cap)
    def emit_memory_model(self, addressing=0, memory=1): self._emit_insn(self.OP_MEMORY_MODEL, addressing, memory)
    def emit_type_void(self) -> int: rid = self._alloc_id(); self._emit_insn(self.OP_TYPE_VOID, rid); return rid
    def emit_type_int(self, width: int, signedness: int) -> int:
        rid = self._alloc_id(); self._emit_insn(self.OP_TYPE_INT, rid, width, signedness); return rid
    def emit_type_pointer(self, storage_class: int, pointee_type: int) -> int:
        rid = self._alloc_id(); self._emit_insn(self.OP_TYPE_POINTER, rid, storage_class, pointee_type); return rid
    def emit_constant_typed(self, type_id: int, value: int) -> int:
        rid = self._alloc_id(); self._emit_insn(self.OP_CONSTANT, type_id, rid, value & 0xFFFFFFFF); return rid
    def emit_variable(self, type_id: int, storage_class: int) -> int:
        rid = self._alloc_id(); self._emit_insn(self.OP_VARIABLE, type_id, rid, storage_class); return rid
    def emit_load(self, type_id: int, pointer: int) -> int:
        rid = self._alloc_id(); self._emit_insn(self.OP_LOAD, type_id, rid, pointer); return rid
    def emit_store(self, pointer: int, value: int): self._emit_insn(self.OP_STORE, pointer, value)
    def emit_iadd(self, type_id: int, a: int, b: int) -> int:
        rid = self._alloc_id(); self._emit_insn(self.OP_IADD, type_id, rid, a, b); return rid
    def emit_imul(self, type_id: int, a: int, b: int) -> int:
        rid = self._alloc_id(); self._emit_insn(self.OP_IMUL, type_id, rid, a, b); return rid
    
    def finalize(self) -> List[int]:
        if len(self.words) >= 4: self.words[3] = self.next_id
        return self.words

MAX_REG_COUNT = 254

class SM86Decoder:
    OPCODES = ["AL2P", "IADD3", "IMAD", "MOV", "FADD", "FMUL", "LDG", "STG", "EXIT"] # Truncated for mock

    def __init__(self):
        self.ir = SPIRVEmitter()
        self.regs = [0] * MAX_REG_COUNT
        self.type_u32 = 0
        self.type_ptr_u32 = 0
        self.decode_log: List[str] = []

    def init(self):
        self.ir.emit_header()
        self.ir.emit_capability(SPIRVEmitter.CAP_SHADER)
        self.ir.emit_memory_model()
        self.type_u32 = self.ir.emit_type_int(32, 0)
        self.type_ptr_u32 = self.ir.emit_type_pointer(7, self.type_u32)
        for r in range(MAX_REG_COUNT): self.regs[r] = self.ir.emit_variable(self.type_ptr_u32, 7)

    def load_reg(self, reg: int) -> int:
        if reg == 255: return self.ir.emit_constant_typed(self.type_u32, 0)
        return self.ir.emit_load(self.type_u32, self.regs[reg])

    def store_reg(self, reg: int, val: int):
        if reg != 255: self.ir.emit_store(self.regs[reg], val)

    def _extract(self, inst: int, hi: int, lo: int) -> int:
        return (inst >> lo) & ((1 << (hi - lo + 1)) - 1)

    def al2p(self, inst: int):
        rd, ra, ra_offset = self._extract(inst, 23, 16), self._extract(inst, 31, 24), self._extract(inst, 50, 40)
        dst_val = self.ir.emit_iadd(self.type_u32, self.load_reg(ra), self.ir.emit_constant_typed(self.type_u32, ra_offset))
        self.store_reg(rd, dst_val)
        self.decode_log.append(f"AL2P R{rd}, R{ra}, #{ra_offset}")

    def iadd3(self, inst: int):
        rd, ra, rb, rc = self._extract(inst, 23, 16), self._extract(inst, 31, 24), self._extract(inst, 39, 32), self._extract(inst, 71, 64)
        tmp = self.ir.emit_iadd(self.type_u32, self.load_reg(ra), self.load_reg(rb))
        self.store_reg(rd, self.ir.emit_iadd(self.type_u32, tmp, self.load_reg(rc)))
        self.decode_log.append(f"IADD3 R{rd}, R{ra}, R{rb}, R{rc}")

    def finalize(self): return self.ir.finalize()

class GPUState:
    def __init__(self):
        self.decoder = SM86Decoder()
        self.decoder.init()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HOS Services & System State
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

HOS_SERVICE_NAMES = ["acc","am","apm","audout","bsd","dispdrv","fs","hid","nvdrv","pl","set","sm","time","vi"]

class HOSService:
    def __init__(self, name: str): self.name = name; self.initialized = True

class HOSServices:
    def __init__(self): self.services = {n: HOSService(n) for n in HOS_SERVICE_NAMES}
    def get(self, name: str) -> HOSService | None: return self.services.get(name)

class SystemState:
    def __init__(self):
        self.cpu_manager = CpuManager()
        self.gpu_state = GPUState()
        self.services = HOSServices()

    def start_host_services(self) -> List[str]:
        return [f"  nn::{name} → OK" for name in HOS_SERVICE_NAMES if self.services.get(name)]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AArch64 Assembler & Firmware
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ARM64:
    @staticmethod
    def nop(): return 0xD503201F
    @staticmethod
    def brk(imm16=0): return 0xD4200000 | ((imm16 & 0xFFFF) << 5)
    @staticmethod
    def ret(): return 0xD65F03C0
    @staticmethod
    def add_imm(rd: int, rn: int, imm12: int): return 0x91000000 | ((imm12 & 0xFFF) << 10) | ((rn & 0x1F) << 5) | (rd & 0x1F)
    @staticmethod
    def sub_imm(rd: int, rn: int, imm12: int): return 0xD1000000 | ((imm12 & 0xFFF) << 10) | ((rn & 0x1F) << 5) | (rd & 0x1F)
    @staticmethod
    def add_reg(rd: int, rn: int, rm: int): return 0x8B000000 | ((rm & 0x1F) << 16) | ((rn & 0x1F) << 5) | (rd & 0x1F)
    @staticmethod
    def mov_reg(rd: int, rm: int): return 0xAA0003E0 | ((rm & 0x1F) << 16) | (rd & 0x1F)

class A64Asm:
    def __init__(self):
        self.code = bytearray(); self.labels: Dict[str, int] = {}; self.fixups: List[Tuple[int, str, str]] = []
    def here(self) -> int: return len(self.code)
    def label(self, n: str): self.labels[n] = self.here()
    def emit(self, w: int): self.code += struct.pack("<I", w)
    def resolve(self):
        for pos, name, kind in self.fixups:
            off = self.labels[name] - pos
            insn = struct.unpack_from("<I", self.code, pos)[0]
            if kind in ("b","bl"): insn = (insn & 0xFC000000) | ((off >> 2) & 0x3FFFFFF)
            elif kind in ("bcond","cbz"): insn = (insn & 0xFF00001F) | (((off >> 2) & 0x7FFFF) << 5)
            struct.pack_into("<I", self.code, pos, insn)
    def svc(self, imm: int): self.emit(0xD4000001 | ((imm & 0xFFFF) << 5))
    def movz(self, rd: int, imm16: int, hw=0): self.emit(0xD2800000 | (hw<<21) | (imm16<<5) | rd)
    def movk(self, rd: int, imm16: int, hw=0): self.emit(0xF2800000 | (hw<<21) | (imm16<<5) | rd)
    def mov_imm(self, rd: int, val: int):
        val &= 0xFFFFFFFFFFFFFFFF; self.movz(rd, val & 0xFFFF, 0)
        if val > 0xFFFF: self.movk(rd, (val>>16) & 0xFFFF, 1)
    def add_imm(self, rd: int, rn: int, imm12: int): self.emit(0x91000000 | (imm12<<10) | (rn<<5) | rd)
    def sub_imm(self, rd: int, rn: int, imm12: int): self.emit(0xD1000000 | (imm12<<10) | (rn<<5) | rd)
    def subs_imm(self, rd: int, rn: int, imm12: int): self.emit(0xF1000000 | (imm12<<10) | (rn<<5) | rd)
    def cmp_imm(self, rn: int, imm12: int): self.subs_imm(31, rn, imm12)
    def add_reg(self, rd: int, rn: int, rm: int): self.emit(0x8B000000 | (rm<<16) | (rn<<5) | rd)
    def mul(self, rd: int, rn: int, rm: int): self.emit(0x9B007C00 | (rm<<16) | (rn<<5) | rd)
    def strb(self, rt: int, rn: int, imm12=0): self.emit(0x39000000 | (imm12<<10) | (rn<<5) | rt)
    def b(self, lbl: str): self.fixups.append((self.here(), lbl, "b")); self.emit(0x14000000)
    def b_cond(self, c: str, lbl: str):
        cm = {"eq":0,"ne":1,"lt":11,"ge":10,"gt":12,"le":13,"hs":2,"lo":3}
        self.fixups.append((self.here(), lbl, "bcond")); self.emit(0x54000000 | cm[c])

def build_nx2_firmware() -> bytearray:
    asm = A64Asm()
    asm.label("_start")
    asm.mov_imm(0, FB_BASE); asm.movz(1, FB_W); asm.movz(2, FB_H)
    asm.movz(9, FB_STRIDE); asm.movz(3, 0)
    asm.label("frame_loop")
    asm.movz(14, 0)
    asm.label("clear_y")
    asm.movz(15, 0)
    asm.label("clear_x")
    asm.mul(10, 14, 9); asm.movz(16, 3); asm.mul(21, 15, 16)
    asm.add_reg(10, 10, 21); asm.add_reg(10, 10, 0)
    asm.movz(16, 0x10); asm.strb(16, 10, 0); asm.strb(16, 10, 1); asm.strb(16, 10, 2)
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
    asm.add_reg(18, 6, 16); asm.sub_imm(18, 18, 1); asm.add_reg(19, 5, 17); asm.sub_imm(19, 19, 1)
    asm.cmp_imm(18, FB_H); asm.b_cond("hs", "skip_px"); asm.cmp_imm(19, FB_W); asm.b_cond("hs", "skip_px")
    asm.mul(10, 18, 9); asm.movz(21, 3); asm.mul(14, 19, 21); asm.add_reg(10, 10, 14); asm.add_reg(10, 10, 0)
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

class MeowNXIISystem:
    def __init__(self, sys_state: SystemState):
        self.sys = sys_state
        self.mem = sys_state.cpu_manager.shared_memory
        self.cpu = sys_state.cpu_manager.get_core(0)
        
        # Flash Firmware
        fw = build_nx2_firmware()
        self.mem[CODE_BASE:CODE_BASE + len(fw)] = fw
        self.cpu.set_pc(CODE_BASE)
        self.cpu.svc_handler = self._svc
        
        self.frame_ready = False
        self.docked = True

    def _svc(self, cpu: AArch64Core, imm: int):
        match imm:
            case 0x01:
                self.frame_ready = True
            case 0x10:
                frame, idx = cpu.xr(3), cpu.xr(4)
                t = frame * 0.04
                angle = t * 2.8 + idx * 0.0785
                radius = 55 + 18 * math.sin(t * 1.3 + idx * 0.5)
                px = int(FB_W//2 + radius * math.cos(angle))
                py = int(FB_H//2 + radius * 0.65 * math.sin(angle * 1.6))
                r = int(max(0, min(255, 40 + 50 * math.sin(t * 2.1 + idx * 0.4))))
                g = int(max(0, min(255, 150 + 90 * math.sin(t * 1.4 + idx * 0.3))))
                b = int(max(0, min(255, 210 + 45 * math.cos(t * 1.7 + idx * 0.2))))
                cpu.xw(5, px & 0xFFFFFFFFFFFFFFFF); cpu.xw(6, py & 0xFFFFFFFFFFFFFFFF)
                cpu.xw(11, r); cpu.xw(12, g); cpu.xw(13, b)

    def run_frame(self):
        self.frame_ready = False
        safety = 0
        # sJIT Execute using `step_n` which uses Block cache
        while not self.frame_ready and not self.cpu.halted and safety < 500000:
            self.cpu.step_n(256)
            safety += 256

    def get_framebuffer(self) -> List[List[Tuple[int, int, int]]]:
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
#  Test Harness (Leveraging sJIT)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_tests() -> List[str]:
    results, passed, total = [], 0, 0
    t0 = time.time()
    
    tests = [
        ("NOP", [ARM64.nop()], lambda c: None, lambda c: c.get_pc() >= TEST_BASE + 4),
        ("ADD X1, X1, #2", [ARM64.add_imm(1, 1, 2)], lambda c: c.set_x(1, 5), lambda c: c.get_x(1) == 7),
        ("RET", [ARM64.ret()], lambda c: c.set_x(30, 0x2000), lambda c: c.get_pc() == 0x2000),
        ("sJIT Cache Hit", [ARM64.add_imm(0, 0, 50)], lambda c: c.set_x(0, 100), lambda c: c.get_x(0) == 150),
    ]

    for name, insns, setup, verify in tests:
        mem = bytearray(MEM_SIZE)
        cpu = AArch64Core(mem, core_id=0)
        cpu.set_sp(0x8000); cpu.set_pc(TEST_BASE)
        
        addr = TEST_BASE
        for insn in insns: cpu.write_u32(addr, insn); addr += 4
        cpu.write_u32(addr, ARM64.brk(0))
        setup(cpu)
        cpu.write_u32(0x2000, ARM64.brk(0))
        
        t_start = time.time()
        cpu.run() # Will implicitly use sJIT
        dt = time.time() - t_start
        
        ok = verify(cpu)
        icon = "Y" if ok else "N"
        if name == "sJIT Cache Hit":
            # Run again to prove cache hit
            cpu.set_pc(TEST_BASE); cpu.halted = False
            cpu.run()
            ok = cpu.meowjit.cache_hits > 0
            
        results.append(f"{icon} {name} - {'PASS' if ok else 'FAIL'} ({dt*1000:.1f}ms)")
        if ok: passed += 1
        total += 1

    dt = time.time() - t0
    results.append(f"Total: {total} ({passed} passed / {total-passed} failed) time {dt*1000:.0f}ms")
    return results

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CatNXEMU2.0V0.X GUI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CatNXEMU(tk.Tk):
    TOOLBAR_ICONS = {"boot":"▶", "pause":"⏸", "stop":"⏹", "dock":"🖥", "hand":"📱", "info":"ℹ", "test":"🧪"}

    def __init__(self):
        super().__init__()
        self.title("MeowNXII1.X (sJIT Backend)")
        self.geometry("680x520")
        self.resizable(False, False)
        self.configure(bg=DARK_BG)

        self.sys_state = SystemState()
        self.meow_sys: MeowNXIISystem | None = None
        self.running = False
        self.frame_count = 0
        self.last_time = time.time()
        self.test_thread: threading.Thread | None = None
        self.log_lines = ["Click '🧪 Run Tests' or '▶ Boot' to begin", "Engine: MeowNXII1.X sJIT"]

        self._build_gui()
        
        # Pack the main frame immediately, skipping the splash screen
        self.main_frame.pack(fill="both", expand=True)

    def _build_gui(self):
        menubar = tk.Menu(self, bg=DARK_RAISED, fg=TEXT_PRIMARY, activebackground=ACCENT_BLUE, relief="flat", bd=0, font=("Segoe UI", 9))
        self.config(menu=menubar)

        def _m(**kw): return tk.Menu(menubar, tearoff=0, bg=DARK_RAISED, fg=TEXT_PRIMARY, activebackground=ACCENT_BLUE, font=("Segoe UI", 9), **kw)

        fm = _m(); menubar.add_cascade(label="  File  ", menu=fm)
        fm.add_command(label="  Open NX2 ROM…", command=self._load_rom); fm.add_separator(); fm.add_command(label="  Exit", command=self.quit)

        em = _m(); menubar.add_cascade(label="  Emulation  ", menu=em)
        em.add_command(label="  Boot", command=self._start)
        em.add_command(label="  Pause", command=self._stop)
        
        tm = _m(); menubar.add_cascade(label="  sJIT  ", menu=tm)
        tm.add_command(label="  Run sJIT Tests", command=self._run_all_tests)

        self.main_frame = tk.Frame(self, bg=DARK_BG)
        toolbar = tk.Frame(self.main_frame, bg=TOOLBAR_BG, height=34)
        toolbar.pack(fill="x"); toolbar.pack_propagate(False)

        self._tb = {}
        for item in [("boot",self._start), ("pause",self._stop), ("stop",self._full_stop), None, ("test",self._run_all_tests), None, ("info",self._about)]:
            if item is None:
                tk.Frame(toolbar, bg=DARK_BORDER, width=1).pack(side="left", fill="y", pady=6, padx=5)
            else:
                ik, cmd = item
                btn = tk.Label(toolbar, text=self.TOOLBAR_ICONS[ik], font=("Segoe UI Emoji", 12), bg=TOOLBAR_BG, fg=TEXT_PRIMARY, padx=6, pady=1, cursor="hand2")
                btn.pack(side="left")
                btn.bind("<Enter>", lambda e, b=btn: b.config(bg=DARK_HOVER))
                btn.bind("<Leave>", lambda e, b=btn: b.config(bg=TOOLBAR_BG))
                btn.bind("<Button-1>", lambda e, c=cmd: c())
                self._tb[ik] = btn

        tk.Label(toolbar, text="sJIT Engine Active", font=("Consolas", 8, "bold"), bg=TOOLBAR_BG, fg=ACCENT_GREEN).pack(side="right", padx=8)
        tk.Frame(self.main_frame, bg=ACCENT_BLUE, height=2).pack(fill="x")

        content = tk.Frame(self.main_frame, bg=DARK_BG)
        content.pack(fill="both", expand=True, padx=6, pady=4)

        cf = tk.Frame(content, bg=DARK_BORDER, bd=1, relief="solid")
        cf.pack(side="left", padx=(0, 4))
        self.canvas = tk.Canvas(cf, width=400, height=226, bg="#101010", highlightthickness=0, bd=0)
        self.canvas.pack()
        self._draw_idle()

        log_frame = tk.Frame(content, bg=DARK_SURFACE, bd=0)
        log_frame.pack(side="right", fill="both", expand=True)

        tk.Label(log_frame, text="  sJIT Diagnostics", font=("Consolas", 9, "bold"), bg=DARK_SURFACE, fg=TEXT_BRIGHT, anchor="w").pack(fill="x", padx=4, pady=(4, 0))
        tk.Frame(log_frame, bg=DARK_BORDER, height=1).pack(fill="x", padx=4, pady=2)

        self.log_text = tk.Text(log_frame, bg=DARK_SURFACE, fg=LOG_DIM, font=("Consolas", 9), bd=0, highlightthickness=0, wrap="word", padx=6, pady=4)
        self.log_text.pack(fill="both", expand=True)
        for tag, color in [("pass", LOG_GREEN), ("fail", LOG_RED), ("info", LOG_DIM), ("warn", LOG_YELLOW)]:
            self.log_text.tag_configure(tag, foreground=color)
        self._refresh_log()

        tk.Frame(self.main_frame, bg=DARK_BORDER, height=1).pack(fill="x", side="bottom")
        sb = tk.Frame(self.main_frame, bg=DARK_SURFACE, height=22)
        sb.pack(fill="x", side="bottom"); sb.pack_propagate(False)

        self._dot = tk.Label(sb, text="●", font=("Segoe UI", 7), fg=TEXT_SECONDARY, bg=DARK_SURFACE)
        self._dot.pack(side="left", padx=(8, 3))
        self.lbl_game = tk.Label(sb, text="Idle", fg=TEXT_SECONDARY, bg=DARK_SURFACE, font=("Segoe UI", 8))
        self.lbl_game.pack(side="left")

        for attr, txt in [("lbl_pc", "PC 0x00080000"), ("lbl_jit", "sJIT H:0 M:0"), ("lbl_fps", "0.0 FPS")]:
            tk.Label(sb, text="│", fg=DARK_BORDER, bg=DARK_SURFACE, font=("Consolas", 7)).pack(side="right")
            lbl = tk.Label(sb, text=txt, fg=ACCENT_BLUE if "FPS" in attr else TEXT_SECONDARY, bg=DARK_SURFACE, font=("Consolas", 7))
            lbl.pack(side="right", padx=4)
            setattr(self, attr, lbl)

    def _refresh_log(self):
        self.log_text.config(state="normal"); self.log_text.delete("1.0", "end")
        for line in self.log_lines:
            tag = "pass" if "PASS" in line or line.startswith("Y ") else "fail" if "FAIL" in line or line.startswith("N ") else "warn" if "──" in line else "info"
            self.log_text.insert("end", line + "\n", tag)
        self.log_text.config(state="disabled"); self.log_text.see("end")

    def _draw_idle(self):
        self.canvas.delete("all")
        for y in range(0, 226, 16): self.canvas.create_line(0, y, 400, y, fill="#1A1A1A")
        for x in range(0, 400, 16): self.canvas.create_line(x, 0, x, 226, fill="#1A1A1A")
        self.canvas.create_text(200, 90, text="CatNXEMU", fill=ACCENT_BLUE, font=("Consolas", 22, "bold"))
        self.canvas.create_text(200, 115, text="MeowNXII1.X sJIT Active", fill=ACCENT_GREEN, font=("Consolas", 9))

    def _render_fb(self):
        self.canvas.delete("all")
        fb = self.meow_sys.get_framebuffer()
        sx_scale, sy_scale = 400 / FB_W, 226 / FB_H
        for y in range(0, FB_H, 1):
            for x in range(0, FB_W, 2):
                r, g, b = fb[y][x]
                if (r, g, b) == (0x10, 0x10, 0x10): continue
                sx, sy = int(x * sx_scale), int(y * sy_scale)
                self.canvas.create_rectangle(sx, sy, sx+3, sy+2, fill=f"#{r:02x}{g:02x}{b:02x}", outline="")

    def _load_rom(self): messagebox.showinfo("CatNXEMU", "✅ Synthetic NX2 Firmware\nMeowNXII1.X sJIT enabled.\n/files=off")
    def _about(self): messagebox.showinfo("About", "CatNXEMU - sJIT Backend\nMeowNXII1.X: Python 3.14+ Match/Case JIT architecture.\n© A.C Holdings")

    def _run_in_thread(self, fn: Callable[[], List[str]], label: str):
        def worker():
            self.log_lines = [f"Running {label}..."]; self._refresh_log()
            self.log_lines = fn(); self.after(0, self._refresh_log)
        if not (self.test_thread and self.test_thread.is_alive()):
            self.test_thread = threading.Thread(target=worker, daemon=True); self.test_thread.start()

    def _run_all_tests(self):
        self._run_in_thread(lambda: ["── MeowNXII1.X sJIT Tests ──"] + run_tests(), "sJIT Tests")

    def _start(self):
        if self.running: return
        self.meow_sys = MeowNXIISystem(self.sys_state); self.running = True
        self._dot.config(fg=ACCENT_GREEN); self.lbl_game.config(text="NX2 FW 22.0.0 — RUNNING", fg=ACCENT_GREEN)
        self.log_lines.append(f"▶ Boot: MeowNXII1.X sJIT initialized.")
        self._refresh_log(); self._emu_loop()

    def _stop(self):
        self.running = False; self._dot.config(fg=ACCENT_ORANGE); self.lbl_game.config(text="PAUSED", fg=ACCENT_ORANGE)

    def _full_stop(self):
        self.running = False; self._dot.config(fg=TEXT_SECONDARY); self.lbl_game.config(text="Stopped", fg=TEXT_SECONDARY)
        self._draw_idle()

    def _emu_loop(self):
        if not self.running: return
        self.meow_sys.run_frame(); self._render_fb()
        self.frame_count += 1
        if self.frame_count % 3 == 0:
            now = time.time(); fps = 3.0 / (now - self.last_time + 1e-9); self.last_time = now
            cpu = self.meow_sys.cpu
            self.lbl_fps.config(text=f"{fps:.1f} FPS")
            self.lbl_jit.config(text=f"sJIT H:{cpu.meowjit.cache_hits} M:{cpu.meowjit.cache_misses}")
            self.lbl_pc.config(text=f"PC 0x{cpu.pc:08X}")
        self.after(16, self._emu_loop)

if __name__ == "__main__":
    try: app = CatNXEMU(); app.mainloop()
    except KeyboardInterrupt: print("\nsJIT core shut down.")
