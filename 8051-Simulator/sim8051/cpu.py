from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import os
from typing import Callable

from .base_cpu import BaseCPU
from .exceptions import DecodeError, ExecutionError
from .memory import BIT_ALIASES, MemoryMap, SFR_ADDRESSES
from .model import Breakpoint, ProgramImage, ReverseDelta, RunResult, TraceEntry, Watchpoint

INTERRUPT_ORDER = [
    ("EX0", 0x0003, "IE0", "EX0", "PX0"),
    ("T0", 0x000B, "TF0", "ET0", "PT0"),
    ("EX1", 0x0013, "IE1", "EX1", "PX1"),
    ("T1", 0x001B, "TF1", "ET1", "PT1"),
    ("SER", 0x0023, ("RI", "TI"), "ES", "PS"),
    ("T2", 0x002B, ("TF2", "EXF2"), "ET2", "PT2"),
]
_INTERRUPT_ENTRY_MACHINE_CYCLES = 2
AJMP_OPCODES = {0x01, 0x21, 0x41, 0x61, 0x81, 0xA1, 0xC1, 0xE1}
ACALL_OPCODES = {0x11, 0x31, 0x51, 0x71, 0x91, 0xB1, 0xD1, 0xF1}
PAGE_BRANCH_OPCODES = AJMP_OPCODES | ACALL_OPCODES
_DEBUG_TIMING = os.environ.get("HEXLOGIC_DEBUG_TIMING", "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class SerialPort:
    tx_log: list[int] = field(default_factory=list)
    rx_queue: deque[int] = field(default_factory=deque)
    pending_tx_cycles: int = 0
    pending_rx_cycles: int = 0
    tx_byte: int | None = None


InstructionHandler = Callable[[int], tuple[str, int, list[int]]]


class CPU8051(BaseCPU):
    def __init__(self, *, code_size: int = 0x1000, xram_size: int = 0x10000, upper_iram: bool = False) -> None:
        super().__init__()
        self.memory = MemoryMap(code_size=code_size, xram_size=xram_size, upper_iram=upper_iram)
        self.serial = SerialPort()
        self.io_reads: deque[dict[str, int | float | str]] = deque(maxlen=128)
        self._pending_io_reads: deque[dict[str, int | float | str]] = deque()
        self.active_interrupt_priorities: list[int] = []
        self._previous_int_pins = {0: 1, 1: 1}
        self._previous_timer_pins = {0: 1, 1: 1}
        self._pending_timer_edges = {0: 0, 1: 0}
        self._previous_t2_pins = {0: 1, 1: 1}
        self._pending_t2_edges = {0: 0, 1: 0}
        self._instruction_active = False
        self._last_hardware_tick_signature = None
        self._listing_text_by_address: dict[int, str] = {}
        self._dispatch: tuple[InstructionHandler, ...] = self._build_dispatch_table()
        self.reset(hard=True)

    def reset(self, *, hard: bool = False) -> None:
        self.memory.reset()
        self.pc = 0
        self.cycles = 0
        self.halted = self.program is None
        self.last_error = None
        self.last_interrupt = None
        self.active_interrupt_priorities.clear()
        self.debugger.call_stack.clear()
        self.serial = SerialPort()
        self.io_reads.clear()
        self._pending_io_reads.clear()
        self._previous_int_pins = {0: 1, 1: 1}
        self._previous_timer_pins = {0: 1, 1: 1}
        self._pending_timer_edges = {0: 0, 1: 0}
        self._previous_t2_pins = {0: 1, 1: 1}
        self._pending_t2_edges = {0: 0, 1: 0}
        self._instruction_active = False
        self._last_hardware_tick_signature = None
        self._set_port_defaults()
        if self.program and not hard:
            self.load_program(self.program)

    def load_program(self, program: ProgramImage) -> None:
        self.program = program
        self._listing_text_by_address = {item.address: item.text for item in program.listing}
        self.memory.reset()
        self.memory.load_rom(0, program.rom)
        self.pc = program.origin
        self.cycles = 0
        self.halted = False
        self.last_error = None
        self.last_interrupt = None
        self.active_interrupt_priorities.clear()
        self.debugger.call_stack.clear()
        self.serial = SerialPort()
        self.io_reads.clear()
        self._pending_io_reads.clear()
        self._previous_int_pins = {0: 1, 1: 1}
        self._previous_timer_pins = {0: 1, 1: 1}
        self._pending_timer_edges = {0: 0, 1: 0}
        self._previous_t2_pins = {0: 1, 1: 1}
        self._pending_t2_edges = {0: 0, 1: 0}
        self._instruction_active = False
        self._last_hardware_tick_signature = None
        self._set_port_defaults()
        self.memory.consume_changes()

    def effective_clock_hz(self) -> float:
        return super().effective_clock_hz() / 12.0

    def _current_time_ms(self) -> float:
        cycles = float(self.cycles)
        computed_seconds = cycles / max(1.0, self.effective_clock_hz())
        computed_ms = computed_seconds * 1000.0
        if _DEBUG_TIMING:
            print(
                "[DEBUG_TIMING][cpu8051]",
                {
                    "cycles": cycles,
                    "computed_seconds": round(computed_seconds, 9),
                    "computed_ms": round(computed_ms, 6),
                },
            )
        return computed_ms

    def _record_port_read(self, address: int, value: int) -> None:
        port_name = {
            SFR_ADDRESSES["P0"]: "P0",
            SFR_ADDRESSES["P1"]: "P1",
            SFR_ADDRESSES["P2"]: "P2",
            SFR_ADDRESSES["P3"]: "P3",
        }.get(address & 0xFF)
        if not port_name:
            return
        time_ms = round(self._current_time_ms(), 6)
        for bit in range(8):
            event = {
                "signal": f"{port_name}.{bit}",
                "value": (value >> bit) & 0x01,
                "time_ms": time_ms,
                "source": "cpu",
            }
            self.io_reads.append(event)
            self._pending_io_reads.append(dict(event))

    def _serial_mode(self) -> int:
        return (self.memory.read_sfr("SCON") >> 6) & 0x03

    def _timer2_reload_span(self) -> int:
        reload_value = ((self.memory.read_sfr("RCAP2H") << 8) | self.memory.read_sfr("RCAP2L")) & 0xFFFF
        span = (0x10000 - reload_value) & 0xFFFF
        return span or 0x10000

    def _serial_frame_cycles(self, *, transmit: bool) -> int:
        mode = self._serial_mode()
        flag_name = "TCLK" if transmit else "RCLK"
        if mode in {1, 3} and self._get_flag(flag_name):
            frame_bits = 11 if mode == 3 else 10
            return max(1, frame_bits * 16 * self._timer2_reload_span())
        return 1

    def _timer2_baud_generator_active(self) -> bool:
        return bool(self._get_flag("TCLK") or self._get_flag("RCLK"))

    def _timer2_dcen_enabled(self) -> bool:
        return bool(self.memory.read_sfr("T2MOD") & 0x01)

    def _timer2_up_down_mode_active(self) -> bool:
        return self._timer2_dcen_enabled() and not self._timer2_baud_generator_active() and not self._get_flag("CP/RL2")

    def _set_port_defaults(self) -> None:
        for port in ("P0", "P1", "P2", "P3"):
            self.memory.write_sfr(port, 0xFF)
        self.memory.write_sfr("SP", 0x07)
        self.memory.write_sfr("PSW", 0x00)
        self.memory.write_sfr("IE", 0x00)
        self.memory.write_sfr("IP", 0x00)
        self.memory.write_sfr("TMOD", 0x00)
        self.memory.write_sfr("TCON", 0x00)
        self.memory.write_sfr("SCON", 0x00)
        self.memory.write_sfr("SBUF", 0x00)
        self.memory.write_sfr("TH0", 0x00)
        self.memory.write_sfr("TL0", 0x00)
        self.memory.write_sfr("TH1", 0x00)
        self.memory.write_sfr("TL1", 0x00)
        self.memory.write_sfr("RCAP2L", 0x00)
        self.memory.write_sfr("RCAP2H", 0x00)
        self.memory.write_sfr("TL2", 0x00)
        self.memory.write_sfr("TH2", 0x00)
        self.memory.write_sfr("T2CON", 0x00)
        self.memory.write_sfr("T2MOD", 0x00)
        self.memory.write_sfr("PCON", 0x00)
        self.memory.write_sfr("DPL", 0x00)
        self.memory.write_sfr("DPH", 0x00)
        self.memory.write_sfr("ACC", 0x00)
        self.memory.write_sfr("B", 0x00)

    def set_pin(self, port_index: int, bit: int, level: int | bool | None) -> None:
        previous_level = self._read_port_pin_level(port_index, bit)
        self.memory.set_pin_input(port_index, bit, level)
        current_level = self._read_port_pin_level(port_index, bit)
        if port_index == 3 and bit in {2, 3}:
            self._update_external_interrupt_latch(bit - 2)
        if port_index == 3 and bit in {4, 5}:
            timer_id = bit - 4
            if previous_level == 1 and current_level == 0:
                self._pending_timer_edges[timer_id] = int(self._pending_timer_edges.get(timer_id, 0)) + 1
            self._previous_timer_pins[timer_id] = current_level
        if port_index == 1 and bit in {0, 1}:
            t2_pin = bit
            if previous_level == 1 and current_level == 0:
                self._pending_t2_edges[t2_pin] = int(self._pending_t2_edges.get(t2_pin, 0)) + 1
            self._previous_t2_pins[t2_pin] = current_level

    def _read_port_pin_level(self, port_index: int, bit: int) -> int:
        return (self.memory.get_port(port_index).read_pin() >> bit) & 0x01

    def _timer_gate_open(self, timer_id: int) -> bool:
        shift = 0 if timer_id == 0 else 4
        gate_enabled = (self.memory.read_sfr("TMOD") >> (shift + 3)) & 0x01
        if not gate_enabled:
            return True
        interrupt_bit = 2 if timer_id == 0 else 3
        return bool(self._read_port_pin_level(3, interrupt_bit))

    def _timer_increment_count(self, timer_id: int, machine_cycles: int, *, counter_mode: bool) -> int:
        current_level = self._read_port_pin_level(3, 4 + timer_id)
        previous_level = self._previous_timer_pins[timer_id]
        self._previous_timer_pins[timer_id] = current_level
        if not counter_mode:
            return machine_cycles
        queued_edges = int(self._pending_timer_edges.get(timer_id, 0))
        self._pending_timer_edges[timer_id] = 0
        boundary_edge = 1 if previous_level == 1 and current_level == 0 else 0
        return queued_edges + boundary_edge

    def inject_serial_rx(self, data: bytes | bytearray | list[int]) -> None:
        for byte in data:
            self.serial.rx_queue.append(int(byte) & 0xFF)
        self._ensure_serial_rx_progress()

    @property
    def sp(self) -> int:
        return self.memory.read_sfr("SP")

    @sp.setter
    def sp(self, value: int) -> None:
        self.memory.write_sfr("SP", value)

    @property
    def a(self) -> int:
        return self.memory.read_sfr("A")

    @a.setter
    def a(self, value: int) -> None:
        self.memory.write_sfr("A", value & 0xFF)
        self._update_parity()

    @property
    def b(self) -> int:
        return self.memory.read_sfr("B")

    @b.setter
    def b(self, value: int) -> None:
        self.memory.write_sfr("B", value & 0xFF)

    @property
    def dptr(self) -> int:
        return (self.memory.read_sfr("DPH") << 8) | self.memory.read_sfr("DPL")

    @dptr.setter
    def dptr(self, value: int) -> None:
        value &= 0xFFFF
        self.memory.write_sfr("DPH", (value >> 8) & 0xFF)
        self.memory.write_sfr("DPL", value & 0xFF)

    def _get_flag(self, name: str) -> int:
        return self.memory.read_named_bit(name)

    def _set_flag(self, name: str, value: int | bool) -> None:
        self.memory.write_named_bit(name, 1 if value else 0)

    def _update_parity(self) -> None:
        self._set_flag("P", bin(self.a & 0xFF).count("1") % 2)

    def _bank_base(self) -> int:
        psw = self.memory.read_sfr("PSW")
        return ((psw >> 3) & 0x03) * 8

    def _read_r(self, index: int) -> int:
        return self.memory.read_direct(self._bank_base() + index)

    def _write_r(self, index: int, value: int) -> None:
        self.memory.write_direct(self._bank_base() + index, value & 0xFF)

    def _push_byte(self, value: int) -> None:
        next_sp = self.sp + 1
        if not self.memory.upper_iram_enabled and next_sp > 0x7F:
            raise ExecutionError("stack overflow", pc=self.pc)
        self.sp = next_sp & 0xFF
        self.memory.write_indirect(self.sp, value & 0xFF)

    def _pop_byte(self) -> int:
        if self.sp <= 0x07:
            raise ExecutionError("stack underflow", pc=self.pc)
        value = self.memory.read_indirect(self.sp)
        self.sp = (self.sp - 1) & 0xFF
        return value

    def _push_word(self, value: int) -> None:
        self._push_byte(value & 0xFF)
        self._push_byte((value >> 8) & 0xFF)

    def _pop_word(self) -> int:
        high = self._pop_byte()
        low = self._pop_byte()
        return ((high << 8) | low) & 0xFFFF

    def _fetch8(self) -> int:
        byte = self.memory.read_code(self.pc)
        self.pc = (self.pc + 1) & 0xFFFF
        return byte

    def _fetch16(self) -> int:
        high = self._fetch8()
        low = self._fetch8()
        return ((high << 8) | low) & 0xFFFF

    def _read_direct(self, address: int, *, rmw: bool = False) -> int:
        value = self.memory.read_direct(address, rmw=rmw)
        if not rmw:
            self._record_port_read(address, value)
        return value

    def _write_direct(self, address: int, value: int) -> None:
        value &= 0xFF
        self.memory.write_direct(address, value)
        if self._instruction_active and (address & 0xFF) == SFR_ADDRESSES["SBUF"]:
            self.serial.tx_byte = value
            self.serial.pending_tx_cycles = max(self._serial_frame_cycles(transmit=True), self.serial.pending_tx_cycles)
        if (address & 0xFF) == SFR_ADDRESSES["SCON"]:
            self._ensure_serial_rx_progress()
        if address == SFR_ADDRESSES["A"]:
            self._update_parity()

    def _read_direct_or_r(self, opcode_low: int) -> int:
        if opcode_low == 4:
            return self._fetch8()
        if opcode_low == 5:
            return self._read_direct(self._fetch8())
        if opcode_low == 6:
            return self.memory.read_indirect(self._read_r(0))
        if opcode_low == 7:
            return self.memory.read_indirect(self._read_r(1))
        return self._read_r(opcode_low - 8)

    def _carry(self) -> int:
        return 1 if self._get_flag("CY") else 0

    def _debug_registers(self) -> dict[str, int]:
        return {
            "A": self.a,
            "B": self.b,
            "SP": self.sp,
            "PC": self.pc & 0xFFFF,
            "DPTR": self.dptr,
            "PSW": self.memory.read_sfr("PSW"),
            **{f"R{i}": self._read_r(i) for i in range(8)},
        }

    def _register_diff(self, before: dict[str, int], after: dict[str, int]) -> dict[str, dict[str, int]]:
        return {
            name: {"before": before[name], "after": after[name]}
            for name in after
            if before.get(name) != after[name]
        }

    def _set_add_flags(self, left: int, right: int, carry_in: int, result: int) -> None:
        total = left + right + carry_in
        self._set_flag("CY", total > 0xFF)
        self._set_flag("AC", ((left & 0x0F) + (right & 0x0F) + carry_in) > 0x0F)
        self._set_flag("OV", (~(left ^ right) & (left ^ result) & 0x80) != 0)

    def _set_sub_flags(self, left: int, right: int, borrow: int, result: int) -> None:
        total = left - right - borrow
        self._set_flag("CY", total < 0)
        self._set_flag("AC", (left & 0x0F) < ((right & 0x0F) + borrow))
        self._set_flag("OV", ((left ^ right) & (left ^ result) & 0x80) != 0)

    def _bit_address_to_byte_bit(self, bit_addr: int) -> tuple[int, int]:
        if bit_addr < 0x80:
            return 0x20 + (bit_addr // 8), bit_addr % 8
        return bit_addr & 0xF8, bit_addr & 0x07

    def _decode_ajmp_target(self, opcode: int, low: int, next_pc: int) -> int:
        return ((next_pc & 0xF800) | ((opcode & 0xE0) << 3) | low) & 0xFFFF

    def _relative_target(self, offset: int) -> int:
        signed = offset if offset < 0x80 else offset - 0x100
        return (self.pc + signed) & 0xFFFF

    def _set_pc(self, address: int) -> None:
        self.pc = address & 0xFFFF

    def _update_external_interrupt_latch(self, interrupt_index: int) -> None:
        level = self._read_port_pin_level(3, 2 if interrupt_index == 0 else 3)
        previous = self._previous_int_pins[interrupt_index]
        it_bit = "IT0" if interrupt_index == 0 else "IT1"
        ie_flag = "IE0" if interrupt_index == 0 else "IE1"
        if self._get_flag(it_bit):
            if previous == 1 and level == 0:
                self._set_flag(ie_flag, 1)
        else:
            self._set_flag(ie_flag, 1 if level == 0 else 0)
        self._previous_int_pins[interrupt_index] = level

    def _interrupt_pending(self, interrupt_name: str, flag_name, enable_name: str) -> bool:
        if not self._get_flag("EA") or not self._get_flag(enable_name):
            return False
        if interrupt_name == "T2":
            if self._timer2_up_down_mode_active():
                return bool(self._get_flag("TF2"))
            return any(self._get_flag(name) for name in ("TF2", "EXF2"))
        if isinstance(flag_name, tuple):
            return any(self._get_flag(name) for name in flag_name)
        if interrupt_name in {"EX0", "EX1"}:
            self._update_external_interrupt_latch(0 if interrupt_name == "EX0" else 1)
        return bool(self._get_flag(flag_name))

    def _clear_interrupt_source(self, interrupt_name: str) -> None:
        if interrupt_name == "EX0" and self._get_flag("IT0"):
            self._set_flag("IE0", 0)
        elif interrupt_name == "EX1" and self._get_flag("IT1"):
            self._set_flag("IE1", 0)
        elif interrupt_name == "T0":
            self._set_flag("TF0", 0)
        elif interrupt_name == "T1":
            self._set_flag("TF1", 0)
        elif interrupt_name == "T2":
            self._set_flag("TF2", 0)
            self._set_flag("EXF2", 0)

    def _maybe_take_interrupt(self) -> str | None:
        if self.halted:
            return None
        if not self._get_flag("EA"):
            return None
        current_priority = self.active_interrupt_priorities[-1] if self.active_interrupt_priorities else -1
        pending: list[tuple[int, int, str, int]] = []
        for order, (name, vector, flag_name, enable_name, priority_name) in enumerate(INTERRUPT_ORDER):
            if self._interrupt_pending(name, flag_name, enable_name):
                priority = 1 if self._get_flag(priority_name) else 0
                if priority > current_priority:
                    pending.append((-priority, order, name, vector))
        if not pending:
            return None
        pending.sort()
        _, _, name, vector = pending[0]
        self._push_word(self.pc)
        self.debugger.call_stack.append(self.pc)
        self.active_interrupt_priorities.append(1 if self._get_flag(next(entry[4] for entry in INTERRUPT_ORDER if entry[0] == name)) else 0)
        self._clear_interrupt_source(name)
        self.pc = vector
        self.cycles += _INTERRUPT_ENTRY_MACHINE_CYCLES
        self._tick_peripherals(_INTERRUPT_ENTRY_MACHINE_CYCLES)
        self.last_interrupt = name
        return name

    def _ensure_serial_rx_progress(self) -> None:
        if self.serial.rx_queue and self.serial.pending_rx_cycles == 0 and self._get_flag("REN"):
            self.serial.pending_rx_cycles = self._serial_frame_cycles(transmit=False)

    def _tick_serial(self, machine_cycles: int) -> None:
        if self.serial.pending_tx_cycles > 0:
            self.serial.pending_tx_cycles = max(0, self.serial.pending_tx_cycles - machine_cycles)
            if self.serial.pending_tx_cycles == 0 and self.serial.tx_byte is not None:
                self.serial.tx_log.append(self.serial.tx_byte)
                self.serial.tx_byte = None
                self._set_flag("TI", 1)
        self._ensure_serial_rx_progress()
        if self.serial.pending_rx_cycles > 0:
            self.serial.pending_rx_cycles = max(0, self.serial.pending_rx_cycles - machine_cycles)
            if self.serial.pending_rx_cycles == 0 and self.serial.rx_queue and self._get_flag("REN"):
                byte = self.serial.rx_queue.popleft()
                self.memory.write_sfr("SBUF", byte)
                self._set_flag("RI", 1)
                self._ensure_serial_rx_progress()

    def _tick_timer_mode(self, timer_id: int, machine_cycles: int) -> None:
        shift = 0 if timer_id == 0 else 4
        mode = (self.memory.read_sfr("TMOD") >> shift) & 0x03
        if timer_id == 0 and mode == 3:
            self._tick_split_timer0(machine_cycles)
            return
        counter_mode = (self.memory.read_sfr("TMOD") >> (shift + 2)) & 0x01
        run_flag = self._get_flag("TR0" if timer_id == 0 else "TR1")
        if not run_flag or not self._timer_gate_open(timer_id):
            self._previous_timer_pins[timer_id] = self._read_port_pin_level(3, 4 + timer_id)
            return
        increments = self._timer_increment_count(timer_id, machine_cycles, counter_mode=bool(counter_mode))
        if increments <= 0:
            return
        th_name = "TH0" if timer_id == 0 else "TH1"
        tl_name = "TL0" if timer_id == 0 else "TL1"
        flag_name = "TF0" if timer_id == 0 else "TF1"
        if mode == 0:
            value = ((self.memory.read_sfr(th_name) << 5) | (self.memory.read_sfr(tl_name) & 0x1F)) + increments
            overflow = value > 0x1FFF
            value &= 0x1FFF
            self.memory.write_sfr(th_name, (value >> 5) & 0xFF)
            tl = self.memory.read_sfr(tl_name) & 0xE0
            self.memory.write_sfr(tl_name, tl | (value & 0x1F))
            if overflow:
                self._set_flag(flag_name, 1)
            return
        if mode == 1:
            value = (((self.memory.read_sfr(th_name) << 8) | self.memory.read_sfr(tl_name)) + increments) & 0x1FFFF
            overflow = value > 0xFFFF
            value &= 0xFFFF
            self.memory.write_sfr(th_name, (value >> 8) & 0xFF)
            self.memory.write_sfr(tl_name, value & 0xFF)
            if overflow:
                self._set_flag(flag_name, 1)
            return
        if mode == 2:
            tl = self.memory.read_sfr(tl_name)
            reload = self.memory.read_sfr(th_name)
            for _ in range(increments):
                tl += 1
                if tl > 0xFF:
                    tl = reload
                    self._set_flag(flag_name, 1)
            self.memory.write_sfr(tl_name, tl & 0xFF)
            return
        # Timer1 mode 3 is not defined for classic 8051 operation.
        return

    def _tick_split_timer0(self, machine_cycles: int) -> None:
        if self._get_flag("TR0") and self._timer_gate_open(0):
            increments = self._timer_increment_count(0, machine_cycles, counter_mode=bool((self.memory.read_sfr("TMOD") >> 2) & 0x01))
            tl0 = self.memory.read_sfr("TL0")
            for _ in range(increments):
                tl0 += 1
                if tl0 > 0xFF:
                    tl0 = 0
                    self._set_flag("TF0", 1)
            self.memory.write_sfr("TL0", tl0 & 0xFF)
        else:
            self._previous_timer_pins[0] = self._read_port_pin_level(3, 4)
        if self._get_flag("TR1") and self._timer_gate_open(1):
            increments = self._timer_increment_count(1, machine_cycles, counter_mode=bool((self.memory.read_sfr("TMOD") >> 6) & 0x01))
            th0 = self.memory.read_sfr("TH0")
            for _ in range(increments):
                th0 += 1
                if th0 > 0xFF:
                    th0 = 0
                    self._set_flag("TF1", 1)
            self.memory.write_sfr("TH0", th0 & 0xFF)
        else:
            self._previous_timer_pins[1] = self._read_port_pin_level(3, 5)

    def _tick_timer2(self, machine_cycles: int) -> None:
        if not self._get_flag("TR2"):
            self._previous_t2_pins[0] = self._read_port_pin_level(1, 0)
            self._previous_t2_pins[1] = self._read_port_pin_level(1, 1)
            return
        counter_mode = bool(self._get_flag("C/T2"))
        baud_mode = self._timer2_baud_generator_active()
        up_down_mode = self._timer2_up_down_mode_active() and not counter_mode
        t2ex_edges = int(self._pending_t2_edges.get(1, 0))
        self._pending_t2_edges[1] = 0
        if counter_mode:
            increments = int(self._pending_t2_edges.get(0, 0))
            self._pending_t2_edges[0] = 0
            if increments <= 0 and not t2ex_edges:
                return
        else:
            increments = int(machine_cycles)
        reload_value = ((self.memory.read_sfr("RCAP2H") << 8) | self.memory.read_sfr("RCAP2L")) & 0xFFFF
        value = ((self.memory.read_sfr("TH2") << 8) | self.memory.read_sfr("TL2")) & 0xFFFF
        exen2 = bool(self._get_flag("EXEN2"))
        cp_rl2 = bool(self._get_flag("CP/RL2"))
        if up_down_mode:
            count_up = bool(self._read_port_pin_level(1, 1))
            for _ in range(max(0, increments)):
                if count_up:
                    value = (value + 1) & 0xFFFF
                    if value == 0x0000:
                        value = reload_value
                        self._set_flag("TF2", 1)
                        self._set_flag("EXF2", 0 if self._get_flag("EXF2") else 1)
                else:
                    if value == reload_value:
                        value = 0xFFFF
                        self._set_flag("TF2", 1)
                        self._set_flag("EXF2", 0 if self._get_flag("EXF2") else 1)
                    else:
                        value = (value - 1) & 0xFFFF
        else:
            if exen2 and t2ex_edges:
                if cp_rl2 and not baud_mode:
                    current = value & 0xFFFF
                    self.memory.write_sfr("RCAP2H", (current >> 8) & 0xFF)
                    self.memory.write_sfr("RCAP2L", current & 0xFF)
                else:
                    value = reload_value
                self._set_flag("EXF2", 1)
            if increments > 0:
                total = value + increments
                overflow = total > 0xFFFF
                value = total & 0xFFFF
                if overflow:
                    if not cp_rl2 or baud_mode:
                        value = reload_value
                    if not baud_mode:
                        self._set_flag("TF2", 1)
        self.memory.write_sfr("TH2", (value >> 8) & 0xFF)
        self.memory.write_sfr("TL2", value & 0xFF)

    def _peripherals_active(self) -> bool:
        return bool(
            self._get_flag("TR0")
            or self._get_flag("TR1")
            or self._get_flag("TR2")
            or self.serial.pending_tx_cycles > 0
            or self.serial.pending_rx_cycles > 0
            or self.serial.rx_queue
        )

    def _tick_peripherals(self, machine_cycles: int) -> None:
        if not self._peripherals_active():
            return
        self._tick_timer_mode(0, machine_cycles)
        self._tick_timer_mode(1, machine_cycles)
        self._tick_timer2(machine_cycles)
        self._tick_serial(machine_cycles)

    def _tight_loop_fast_path_allowed(self) -> bool:
        return (
            self.compact_execution_allowed()
            and not self.debugger.breakpoints
            and not self._get_flag("EA")
            and not self._peripherals_active()
        )

    def _fast_djnz_iterations(self, value: int) -> int:
        return int(value) if int(value) > 0 else 256

    def _try_fast_sjmp_self(self, *, max_steps: int, max_cycles: int) -> dict | None:
        if self.memory.read_code(self.pc) != 0x80:
            return None
        rel = self.memory.read_code((self.pc + 1) & 0xFFFF)
        signed = rel if rel < 0x80 else rel - 0x100
        target = (self.pc + 2 + signed) & 0xFFFF
        if target != self.pc:
            return None
        steps = min(int(max_steps), int(max_cycles) // 2)
        if steps <= 0:
            return None
        self.cycles += steps * 2
        return {"steps": steps, "cycles": steps * 2}

    def _try_fast_djnz_loop(self, *, max_steps: int, max_cycles: int) -> dict | None:
        opcode = self.memory.read_code(self.pc)
        if opcode == 0xD5:
            direct = self.memory.read_code((self.pc + 1) & 0xFFFF)
            rel = self.memory.read_code((self.pc + 2) & 0xFFFF)
            if direct >= 0x80:
                return None
            signed = rel if rel < 0x80 else rel - 0x100
            if ((self.pc + 3 + signed) & 0xFFFF) != self.pc:
                return None
            old_value = self._read_direct(direct, rmw=True)
            remaining = self._fast_djnz_iterations(old_value)
            steps = min(int(max_steps), int(max_cycles) // 2, remaining)
            if steps <= 0:
                return None
            new_value = (old_value - steps) & 0xFF
            self._write_direct(direct, new_value)
            changes = self.memory.consume_changes()
            self.cycles += steps * 2
            self._set_pc((self.pc + 3) & 0xFFFF if steps >= remaining else self.pc)
            return {"steps": steps, "cycles": steps * 2, "memory_changes": changes}
        if 0xD8 <= opcode <= 0xDF:
            reg = opcode & 0x07
            rel = self.memory.read_code((self.pc + 1) & 0xFFFF)
            signed = rel if rel < 0x80 else rel - 0x100
            if ((self.pc + 2 + signed) & 0xFFFF) != self.pc:
                return None
            old_value = self._read_r(reg)
            remaining = self._fast_djnz_iterations(old_value)
            steps = min(int(max_steps), int(max_cycles) // 2, remaining)
            if steps <= 0:
                return None
            new_value = (old_value - steps) & 0xFF
            self._write_r(reg, new_value)
            changes = self.memory.consume_changes()
            self.cycles += steps * 2
            self._set_pc((self.pc + 2) & 0xFFFF if steps >= remaining else self.pc)
            return {"steps": steps, "cycles": steps * 2, "memory_changes": changes}
        return None

    def try_fast_realtime_slice(self, *, max_steps: int, max_cycles: int) -> dict | None:
        if not self._tight_loop_fast_path_allowed() or max_steps <= 0 or max_cycles <= 0 or self.halted:
            return None
        return self._try_fast_djnz_loop(max_steps=max_steps, max_cycles=max_cycles) or self._try_fast_sjmp_self(
            max_steps=max_steps,
            max_cycles=max_cycles,
        )

    def _check_watchpoints(self, trace: TraceEntry) -> bool:
        return super()._check_watchpoints(trace)

    def _record_trace(self, trace: TraceEntry) -> None:
        super()._record_trace(trace)

    def run(self, *, max_steps: int = 1000, after_step=None) -> RunResult:
        return super().run(max_steps=max_steps, after_step=after_step)

    def step_over(self, *, after_step=None) -> RunResult:
        return super().step_over(after_step=after_step)

    def step_out(self, *, after_step=None) -> RunResult:
        return super().step_out(after_step=after_step)

    def _instruction_length_preview(self, opcode: int) -> int:
        if opcode in {0x02, 0x12, 0x90}:
            return 3
        if opcode in {0x10, 0x20, 0x30, 0x43, 0x53, 0x75, 0x85, 0xB4, 0xB5, 0xB6, 0xB7, 0xB8, 0xB9, 0xBA, 0xBB, 0xBC, 0xBD, 0xBE, 0xBF, 0xC0, 0xD0, 0xD5}:
            return 3
        if opcode in {0x24, 0x25, 0x26, 0x27, 0x34, 0x35, 0x36, 0x37, 0x40, 0x50, 0x60, 0x70, 0x80, 0x82, 0x92, 0x94, 0x95, 0x96, 0x97, 0xA0, 0xA2, 0xB0, 0xC2, 0xD2}:
            return 2
        if (opcode & 0x1F) in {0x01, 0x11}:
            return 2
        if 0x28 <= opcode <= 0x2F or 0x38 <= opcode <= 0x3F or 0x48 <= opcode <= 0x4F or 0x58 <= opcode <= 0x5F or 0x68 <= opcode <= 0x6F or 0x78 <= opcode <= 0x7F or 0x98 <= opcode <= 0x9F or 0xD8 <= opcode <= 0xDF:
            return 2 if opcode >= 0x78 or opcode >= 0xD8 else 1
        return 1

    def step(self) -> TraceEntry:
        return super().step()

    def _step_impl(self) -> TraceEntry:
        if self.halted:
            raise ExecutionError("CPU halted", pc=self.pc)
        self.last_interrupt = self._maybe_take_interrupt()
        start_pc = self.pc
        registers_before = self._debug_registers()
        opcode = self._fetch8()
        bytes_ = [opcode]
        mnemonic = f"DB 0x{opcode:02X}"
        machine_cycles = 1

        self._instruction_active = True
        try:
            mnemonic, machine_cycles, extra_bytes = self._execute_opcode(opcode)
            bytes_.extend(extra_bytes)
        except Exception as exc:
            self.halted = True
            self.last_error = str(exc)
            if isinstance(exc, ExecutionError):
                raise
            raise ExecutionError(str(exc), pc=start_pc) from exc
        finally:
            self._instruction_active = False

        self.cycles += machine_cycles
        self._tick_peripherals(machine_cycles)
        changes = self.memory.consume_changes()
        registers_after = self._debug_registers()
        line = self.program.address_to_line.get(start_pc) if self.program else None
        text = self._listing_text_by_address.get(start_pc) if self.program else None
        trace = TraceEntry(
            pc=start_pc,
            opcode=opcode,
            mnemonic=mnemonic,
            bytes_=bytes_,
            cycles=machine_cycles,
            line=line,
            text=text,
            changes=changes,
            register_diff=self._register_diff(registers_before, registers_after),
            interrupt=self.last_interrupt,
        )
        self._record_trace(trace)
        if self.program and self.pc >= (self.program.origin + len(self.program.binary)):
            self.halted = True
        return trace

    def _step_impl_compact(self) -> TraceEntry:
        if self.halted:
            raise ExecutionError("CPU halted", pc=self.pc)
        self.last_interrupt = self._maybe_take_interrupt()
        start_pc = self.pc
        opcode = self._fetch8()
        bytes_ = [opcode]
        mnemonic = f"DB 0x{opcode:02X}"
        machine_cycles = 1
        self._instruction_active = True
        try:
            mnemonic, machine_cycles, extra_bytes = self._execute_opcode(opcode)
            bytes_.extend(extra_bytes)
        except Exception as exc:
            self.halted = True
            self.last_error = str(exc)
            if isinstance(exc, ExecutionError):
                raise
            raise ExecutionError(str(exc), pc=start_pc) from exc
        finally:
            self._instruction_active = False
        self.cycles += machine_cycles
        self._tick_peripherals(machine_cycles)
        changes = self.memory.consume_changes()
        if self.program and self.pc >= (self.program.origin + len(self.program.binary)):
            self.halted = True
        return TraceEntry(
            pc=start_pc,
            opcode=opcode,
            mnemonic=mnemonic,
            bytes_=bytes_,
            cycles=machine_cycles,
            line=self.program.address_to_line.get(start_pc) if self.program else None,
            text=self._listing_text_by_address.get(start_pc) if self.program else None,
            changes=changes,
            register_diff={},
            interrupt=self.last_interrupt,
        )

    def step_compact_payload(self) -> dict:
        if self.halted:
            raise ExecutionError("CPU halted", pc=self.pc)
        self.last_interrupt = self._maybe_take_interrupt()
        start_pc = self.pc
        opcode = self._fetch8()
        bytes_ = [opcode]
        mnemonic = f"DB 0x{opcode:02X}"
        machine_cycles = 1
        self._instruction_active = True
        try:
            mnemonic, machine_cycles, extra_bytes = self._execute_opcode(opcode)
            bytes_.extend(extra_bytes)
        except Exception as exc:
            self.halted = True
            self.last_error = str(exc)
            if isinstance(exc, ExecutionError):
                raise
            raise ExecutionError(str(exc), pc=start_pc) from exc
        finally:
            self._instruction_active = False
        self.cycles += machine_cycles
        self._tick_peripherals(machine_cycles)
        changes = self.memory.consume_changes()
        if self.program and self.pc >= (self.program.origin + len(self.program.binary)):
            self.halted = True
        return {
            "pc": start_pc,
            "opcode": opcode,
            "mnemonic": mnemonic,
            "bytes": bytes_,
            "cycles": machine_cycles,
            "line": self.program.address_to_line.get(start_pc) if self.program else None,
            "text": self._listing_text_by_address.get(start_pc) if self.program else None,
            "changes": changes,
            "interrupt": self.last_interrupt,
        }

    def _build_dispatch_table(self) -> tuple[InstructionHandler, ...]:
        groups: tuple[InstructionHandler, ...] = (
            self._group_0x0,
            self._group_0x1,
            self._group_0x2,
            self._group_0x3,
            self._group_0x4,
            self._group_0x5,
            self._group_0x6,
            self._group_0x7,
            self._group_0x8,
            self._group_0x9,
            self._group_0xA,
            self._group_0xB,
            self._group_0xC,
            self._group_0xD,
            self._group_0xE,
            self._group_0xF,
        )
        table = [groups[opcode >> 4] for opcode in range(256)]
        for opcode in PAGE_BRANCH_OPCODES:
            table[opcode] = self._op_page_branch
        return tuple(table)

    def _current_opcode(self) -> int:
        return self.memory.read_code(self.pc)

    def _is_call_opcode(self, opcode: int) -> bool:
        return opcode in {0x12} or opcode in ACALL_OPCODES

    def _execute_opcode(self, opcode: int) -> tuple[str, int, list[int]]:
        return self._dispatch[opcode](opcode)

    def _undefined_opcode(self, opcode: int) -> tuple[str, int, list[int]]:
        raise DecodeError(f"unsupported opcode 0x{opcode:02X}", pc=self.pc - 1)

    def _op_page_branch(self, opcode: int) -> tuple[str, int, list[int]]:
        low = self._fetch8()
        target = self._decode_ajmp_target(opcode, low, self.pc)
        mnemonic = "ACALL" if opcode in ACALL_OPCODES else "AJMP"
        if mnemonic == "ACALL":
            self._push_word(self.pc)
            self.debugger.call_stack.append(self.pc)
        self._set_pc(target)
        return f"{mnemonic} 0x{target:04X}", 2, [low]

    def _group_0x0(self, opcode: int) -> tuple[str, int, list[int]]:
        extra: list[int] = []
        if opcode == 0x00:
            return "NOP", 1, extra
        if opcode == 0x02:
            target = self._fetch16(); extra.extend([(target >> 8) & 0xFF, target & 0xFF]); self._set_pc(target); return f"LJMP 0x{target:04X}", 2, extra
        if opcode == 0x03:
            self.a = ((self.a >> 1) | ((self.a & 0x01) << 7)) & 0xFF; return "RR A", 1, extra
        if opcode == 0x04:
            self.a = (self.a + 1) & 0xFF; return "INC A", 1, extra
        if opcode == 0x05:
            direct = self._fetch8(); extra.append(direct); self._write_direct(direct, (self._read_direct(direct, rmw=True) + 1) & 0xFF); return f"INC 0x{direct:02X}", 1, extra
        if opcode == 0x06:
            addr = self._read_r(0); self.memory.write_indirect(addr, (self.memory.read_indirect(addr) + 1) & 0xFF); return "INC @R0", 1, extra
        if opcode == 0x07:
            addr = self._read_r(1); self.memory.write_indirect(addr, (self.memory.read_indirect(addr) + 1) & 0xFF); return "INC @R1", 1, extra
        if 0x08 <= opcode <= 0x0F:
            reg = opcode & 0x07; self._write_r(reg, (self._read_r(reg) + 1) & 0xFF); return f"INC R{reg}", 1, extra
        return self._undefined_opcode(opcode)

    def _group_0x1(self, opcode: int) -> tuple[str, int, list[int]]:
        extra: list[int] = []
        if opcode == 0x10:
            bit_addr = self._fetch8(); rel = self._fetch8(); extra.extend([bit_addr, rel]); target = self._relative_target(rel)
            if self.memory.read_bit(bit_addr):
                self.memory.write_bit(bit_addr, 0); self._set_pc(target)
            return f"JBC 0x{bit_addr:02X},0x{target:04X}", 2, extra
        if opcode == 0x12:
            target = self._fetch16(); extra.extend([(target >> 8) & 0xFF, target & 0xFF]); self._push_word(self.pc); self.debugger.call_stack.append(self.pc); self._set_pc(target); return f"LCALL 0x{target:04X}", 2, extra
        if opcode == 0x13:
            carry = self._carry(); new_cy = self.a & 0x01; self.a = ((carry << 7) | (self.a >> 1)) & 0xFF; self._set_flag("CY", new_cy); return "RRC A", 1, extra
        if opcode == 0x14:
            self.a = (self.a - 1) & 0xFF; return "DEC A", 1, extra
        if opcode == 0x15:
            direct = self._fetch8(); extra.append(direct); self._write_direct(direct, (self._read_direct(direct, rmw=True) - 1) & 0xFF); return f"DEC 0x{direct:02X}", 1, extra
        if opcode == 0x16:
            addr = self._read_r(0); self.memory.write_indirect(addr, (self.memory.read_indirect(addr) - 1) & 0xFF); return "DEC @R0", 1, extra
        if opcode == 0x17:
            addr = self._read_r(1); self.memory.write_indirect(addr, (self.memory.read_indirect(addr) - 1) & 0xFF); return "DEC @R1", 1, extra
        if 0x18 <= opcode <= 0x1F:
            reg = opcode & 0x07; self._write_r(reg, (self._read_r(reg) - 1) & 0xFF); return f"DEC R{reg}", 1, extra
        return self._undefined_opcode(opcode)

    def _group_0x2(self, opcode: int) -> tuple[str, int, list[int]]:
        extra: list[int] = []
        if opcode == 0x20:
            bit_addr = self._fetch8(); rel = self._fetch8(); extra.extend([bit_addr, rel]); target = self._relative_target(rel)
            if self.memory.read_bit(bit_addr):
                self._set_pc(target)
            return f"JB 0x{bit_addr:02X},0x{target:04X}", 2, extra
        if opcode == 0x22:
            self._set_pc(self._pop_word())
            if self.debugger.call_stack:
                self.debugger.call_stack.pop()
            return "RET", 2, extra
        if opcode == 0x23:
            self.a = ((self.a << 1) | (self.a >> 7)) & 0xFF; return "RL A", 1, extra
        if 0x24 <= opcode <= 0x2F:
            low = opcode & 0x0F; right = self._read_accumulator_group_operand(low, extra); left = self.a; result = (left + right) & 0xFF
            self._set_add_flags(left, right, 0, result); self.a = result; return "ADD A,<src>", 1, extra
        return self._undefined_opcode(opcode)

    def _group_0x3(self, opcode: int) -> tuple[str, int, list[int]]:
        extra: list[int] = []
        if opcode == 0x30:
            bit_addr = self._fetch8(); rel = self._fetch8(); extra.extend([bit_addr, rel]); target = self._relative_target(rel)
            if not self.memory.read_bit(bit_addr):
                self._set_pc(target)
            return f"JNB 0x{bit_addr:02X},0x{target:04X}", 2, extra
        if opcode == 0x32:
            self._set_pc(self._pop_word())
            if self.active_interrupt_priorities:
                self.active_interrupt_priorities.pop()
            if self.debugger.call_stack:
                self.debugger.call_stack.pop()
            return "RETI", 2, extra
        if opcode == 0x33:
            carry = self._carry(); new_cy = 1 if (self.a & 0x80) else 0; self.a = ((self.a << 1) | carry) & 0xFF; self._set_flag("CY", new_cy); return "RLC A", 1, extra
        if 0x34 <= opcode <= 0x3F:
            low = opcode & 0x0F; right = self._read_accumulator_group_operand(low, extra); carry = self._carry(); left = self.a; result = (left + right + carry) & 0xFF
            self._set_add_flags(left, right, carry, result); self.a = result; return "ADDC A,<src>", 1, extra
        return self._undefined_opcode(opcode)

    def _group_0x4(self, opcode: int) -> tuple[str, int, list[int]]:
        extra: list[int] = []
        if opcode == 0x40:
            rel = self._fetch8(); extra.append(rel); target = self._relative_target(rel)
            if self._get_flag("CY"):
                self._set_pc(target)
            return f"JC 0x{target:04X}", 2, extra
        if opcode == 0x42:
            direct = self._fetch8(); extra.append(direct); self._write_direct(direct, self._read_direct(direct, rmw=True) | self.a); return f"ORL 0x{direct:02X},A", 1, extra
        if opcode == 0x43:
            direct = self._fetch8(); imm = self._fetch8(); extra.extend([direct, imm]); self._write_direct(direct, self._read_direct(direct, rmw=True) | imm); return f"ORL 0x{direct:02X},#0x{imm:02X}", 2, extra
        if 0x44 <= opcode <= 0x4F:
            low = opcode & 0x0F; self.a = self.a | self._read_accumulator_group_operand(low, extra); return "ORL A,<src>", 1, extra
        return self._undefined_opcode(opcode)

    def _group_0x5(self, opcode: int) -> tuple[str, int, list[int]]:
        extra: list[int] = []
        if opcode == 0x50:
            rel = self._fetch8(); extra.append(rel); target = self._relative_target(rel)
            if not self._get_flag("CY"):
                self._set_pc(target)
            return f"JNC 0x{target:04X}", 2, extra
        if opcode == 0x52:
            direct = self._fetch8(); extra.append(direct); self._write_direct(direct, self._read_direct(direct, rmw=True) & self.a); return f"ANL 0x{direct:02X},A", 1, extra
        if opcode == 0x53:
            direct = self._fetch8(); imm = self._fetch8(); extra.extend([direct, imm]); self._write_direct(direct, self._read_direct(direct, rmw=True) & imm); return f"ANL 0x{direct:02X},#0x{imm:02X}", 2, extra
        if 0x54 <= opcode <= 0x5F:
            low = opcode & 0x0F; self.a = self.a & self._read_accumulator_group_operand(low, extra); return "ANL A,<src>", 1, extra
        return self._undefined_opcode(opcode)

    def _group_0x6(self, opcode: int) -> tuple[str, int, list[int]]:
        extra: list[int] = []
        if opcode == 0x60:
            rel = self._fetch8(); extra.append(rel); target = self._relative_target(rel)
            if self.a == 0:
                self._set_pc(target)
            return f"JZ 0x{target:04X}", 2, extra
        if opcode == 0x62:
            direct = self._fetch8(); extra.append(direct); self._write_direct(direct, self._read_direct(direct, rmw=True) ^ self.a); return f"XRL 0x{direct:02X},A", 1, extra
        if opcode == 0x63:
            direct = self._fetch8(); imm = self._fetch8(); extra.extend([direct, imm]); self._write_direct(direct, self._read_direct(direct, rmw=True) ^ imm); return f"XRL 0x{direct:02X},#0x{imm:02X}", 2, extra
        if 0x64 <= opcode <= 0x6F:
            low = opcode & 0x0F; self.a = self.a ^ self._read_accumulator_group_operand(low, extra); return "XRL A,<src>", 1, extra
        return self._undefined_opcode(opcode)

    def _group_0x7(self, opcode: int) -> tuple[str, int, list[int]]:
        extra: list[int] = []
        if opcode == 0x70:
            rel = self._fetch8(); extra.append(rel); target = self._relative_target(rel)
            if self.a != 0:
                self._set_pc(target)
            return f"JNZ 0x{target:04X}", 2, extra
        if opcode == 0x72:
            bit_addr = self._fetch8(); extra.append(bit_addr); self._set_flag("CY", self._carry() | self.memory.read_bit(bit_addr)); return f"ORL C,0x{bit_addr:02X}", 2, extra
        if opcode == 0x73:
            self._set_pc((self.dptr + self.a) & 0xFFFF); return "JMP @A+DPTR", 2, extra
        if opcode == 0x74:
            imm = self._fetch8(); extra.append(imm); self.a = imm; return f"MOV A,#0x{imm:02X}", 1, extra
        if opcode == 0x75:
            direct = self._fetch8(); imm = self._fetch8(); extra.extend([direct, imm]); self._write_direct(direct, imm); return f"MOV 0x{direct:02X},#0x{imm:02X}", 2, extra
        if opcode == 0x76:
            imm = self._fetch8(); extra.append(imm); self.memory.write_indirect(self._read_r(0), imm); return "MOV @R0,#data", 1, extra
        if opcode == 0x77:
            imm = self._fetch8(); extra.append(imm); self.memory.write_indirect(self._read_r(1), imm); return "MOV @R1,#data", 1, extra
        if 0x78 <= opcode <= 0x7F:
            reg = opcode & 0x07; imm = self._fetch8(); extra.append(imm); self._write_r(reg, imm); return f"MOV R{reg},#0x{imm:02X}", 1, extra
        return self._undefined_opcode(opcode)

    def _group_0x8(self, opcode: int) -> tuple[str, int, list[int]]:
        extra: list[int] = []
        if opcode == 0x80:
            rel = self._fetch8(); extra.append(rel); target = self._relative_target(rel); self._set_pc(target); return f"SJMP 0x{target:04X}", 2, extra
        if opcode == 0x82:
            bit_addr = self._fetch8(); extra.append(bit_addr); self._set_flag("CY", self._carry() & self.memory.read_bit(bit_addr)); return f"ANL C,0x{bit_addr:02X}", 2, extra
        if opcode == 0x83:
            self.a = self.memory.read_code((self.pc + self.a) & 0xFFFF); return "MOVC A,@A+PC", 2, extra
        if opcode == 0x84:
            if self.b == 0:
                self._set_flag("OV", 1)
            else:
                quotient = self.a // self.b; remainder = self.a % self.b; self.a = quotient; self.b = remainder; self._set_flag("OV", 0)
            self._set_flag("CY", 0); return "DIV AB", 4, extra
        if opcode == 0x85:
            src = self._fetch8(); dst = self._fetch8(); extra.extend([src, dst]); self._write_direct(dst, self._read_direct(src)); return f"MOV 0x{dst:02X},0x{src:02X}", 2, extra
        if opcode == 0x86:
            dst = self._fetch8(); extra.append(dst); self._write_direct(dst, self.memory.read_indirect(self._read_r(0))); return f"MOV 0x{dst:02X},@R0", 2, extra
        if opcode == 0x87:
            dst = self._fetch8(); extra.append(dst); self._write_direct(dst, self.memory.read_indirect(self._read_r(1))); return f"MOV 0x{dst:02X},@R1", 2, extra
        if 0x88 <= opcode <= 0x8F:
            dst = self._fetch8(); extra.append(dst); reg = opcode & 0x07; self._write_direct(dst, self._read_r(reg)); return f"MOV 0x{dst:02X},R{reg}", 2, extra
        return self._undefined_opcode(opcode)

    def _group_0x9(self, opcode: int) -> tuple[str, int, list[int]]:
        extra: list[int] = []
        if opcode == 0x90:
            value = self._fetch16(); extra.extend([(value >> 8) & 0xFF, value & 0xFF]); self.dptr = value; return f"MOV DPTR,#0x{value:04X}", 2, extra
        if opcode == 0x92:
            bit_addr = self._fetch8(); extra.append(bit_addr); self.memory.write_bit(bit_addr, self._carry()); return f"MOV 0x{bit_addr:02X},C", 2, extra
        if opcode == 0x93:
            self.a = self.memory.read_code((self.dptr + self.a) & 0xFFFF); return "MOVC A,@A+DPTR", 2, extra
        if 0x94 <= opcode <= 0x9F:
            low = opcode & 0x0F; right = self._read_accumulator_group_operand(low, extra); left = self.a; borrow = self._carry(); result = (left - right - borrow) & 0xFF
            self._set_sub_flags(left, right, borrow, result); self.a = result; return "SUBB A,<src>", 1, extra
        return self._undefined_opcode(opcode)

    def _group_0xA(self, opcode: int) -> tuple[str, int, list[int]]:
        extra: list[int] = []
        if opcode == 0xA0:
            bit_addr = self._fetch8(); extra.append(bit_addr); self._set_flag("CY", self._carry() | (1 - self.memory.read_bit(bit_addr))); return f"ORL C,/0x{bit_addr:02X}", 2, extra
        if opcode == 0xA2:
            bit_addr = self._fetch8(); extra.append(bit_addr); self._set_flag("CY", self.memory.read_bit(bit_addr)); return f"MOV C,0x{bit_addr:02X}", 2, extra
        if opcode == 0xA3:
            self.dptr = (self.dptr + 1) & 0xFFFF; return "INC DPTR", 2, extra
        if opcode == 0xA4:
            product = self.a * self.b; self.a = product & 0xFF; self.b = (product >> 8) & 0xFF; self._set_flag("CY", 0); self._set_flag("OV", 1 if product > 0xFF else 0); return "MUL AB", 4, extra
        if opcode == 0xA5:
            raise DecodeError("undefined opcode 0xA5", pc=self.pc - 1)
        if opcode == 0xA6:
            src = self._fetch8(); extra.append(src); self.memory.write_indirect(self._read_r(0), self._read_direct(src)); return "MOV @R0,direct", 2, extra
        if opcode == 0xA7:
            src = self._fetch8(); extra.append(src); self.memory.write_indirect(self._read_r(1), self._read_direct(src)); return "MOV @R1,direct", 2, extra
        if 0xA8 <= opcode <= 0xAF:
            src = self._fetch8(); extra.append(src); self._write_r(opcode & 0x07, self._read_direct(src)); return f"MOV R{opcode & 0x07},direct", 2, extra
        return self._undefined_opcode(opcode)

    def _group_0xB(self, opcode: int) -> tuple[str, int, list[int]]:
        extra: list[int] = []
        if opcode == 0xB0:
            bit_addr = self._fetch8(); extra.append(bit_addr); self._set_flag("CY", self._carry() & (1 - self.memory.read_bit(bit_addr))); return f"ANL C,/0x{bit_addr:02X}", 2, extra
        if opcode == 0xB2:
            bit_addr = self._fetch8(); extra.append(bit_addr); self.memory.write_bit(bit_addr, 0 if self.memory.read_bit(bit_addr) else 1); return f"CPL 0x{bit_addr:02X}", 2, extra
        if opcode == 0xB3:
            self._set_flag("CY", 0 if self._carry() else 1); return "CPL C", 1, extra
        if 0xB4 <= opcode <= 0xBF:
            mnemonic = self._execute_cjne(opcode, extra); return mnemonic, 2, extra
        return self._undefined_opcode(opcode)

    def _group_0xC(self, opcode: int) -> tuple[str, int, list[int]]:
        extra: list[int] = []
        if opcode == 0xC0:
            direct = self._fetch8(); extra.append(direct); self._push_byte(self._read_direct(direct)); return f"PUSH 0x{direct:02X}", 2, extra
        if opcode == 0xC2:
            bit_addr = self._fetch8(); extra.append(bit_addr); self.memory.write_bit(bit_addr, 0); return f"CLR 0x{bit_addr:02X}", 1, extra
        if opcode == 0xC3:
            self._set_flag("CY", 0); return "CLR C", 1, extra
        if opcode == 0xC4:
            self.a = ((self.a & 0x0F) << 4) | ((self.a & 0xF0) >> 4); return "SWAP A", 1, extra
        if 0xC5 <= opcode <= 0xCF:
            mnemonic = self._execute_xch(opcode, extra); return mnemonic, 1, extra
        return self._undefined_opcode(opcode)

    def _group_0xD(self, opcode: int) -> tuple[str, int, list[int]]:
        extra: list[int] = []
        if opcode == 0xD0:
            direct = self._fetch8(); extra.append(direct); self._write_direct(direct, self._pop_byte()); return f"POP 0x{direct:02X}", 2, extra
        if opcode == 0xD2:
            bit_addr = self._fetch8(); extra.append(bit_addr); self.memory.write_bit(bit_addr, 1); return f"SETB 0x{bit_addr:02X}", 1, extra
        if opcode == 0xD3:
            self._set_flag("CY", 1); return "SETB C", 1, extra
        if opcode == 0xD4:
            self._execute_da(); return "DA A", 1, extra
        if opcode == 0xD5:
            direct = self._fetch8(); rel = self._fetch8(); extra.extend([direct, rel]); value = (self._read_direct(direct, rmw=True) - 1) & 0xFF; self._write_direct(direct, value)
            if value != 0:
                self._set_pc(self._relative_target(rel))
            return f"DJNZ 0x{direct:02X},rel", 2, extra
        if opcode == 0xD6:
            addr = self._read_r(0); value = self.memory.read_indirect(addr); new_a = (self.a & 0xF0) | (value & 0x0F); new_mem = (value & 0xF0) | (self.a & 0x0F); self.a = new_a; self.memory.write_indirect(addr, new_mem); return "XCHD A,@R0", 1, extra
        if opcode == 0xD7:
            addr = self._read_r(1); value = self.memory.read_indirect(addr); new_a = (self.a & 0xF0) | (value & 0x0F); new_mem = (value & 0xF0) | (self.a & 0x0F); self.a = new_a; self.memory.write_indirect(addr, new_mem); return "XCHD A,@R1", 1, extra
        if 0xD8 <= opcode <= 0xDF:
            reg = opcode & 0x07; rel = self._fetch8(); extra.append(rel); value = (self._read_r(reg) - 1) & 0xFF; self._write_r(reg, value)
            if value != 0:
                self._set_pc(self._relative_target(rel))
            return f"DJNZ R{reg},rel", 2, extra
        return self._undefined_opcode(opcode)

    def _group_0xE(self, opcode: int) -> tuple[str, int, list[int]]:
        extra: list[int] = []
        if opcode == 0xE0:
            self.a = self.memory.read_xram(self.dptr); return "MOVX A,@DPTR", 2, extra
        if opcode == 0xE2:
            self.a = self.memory.read_xram(self._read_r(0)); return "MOVX A,@R0", 2, extra
        if opcode == 0xE3:
            self.a = self.memory.read_xram(self._read_r(1)); return "MOVX A,@R1", 2, extra
        if opcode == 0xE4:
            self.a = 0; return "CLR A", 1, extra
        if opcode == 0xE5:
            direct = self._fetch8(); extra.append(direct); self.a = self._read_direct(direct); return f"MOV A,0x{direct:02X}", 1, extra
        if opcode == 0xE6:
            self.a = self.memory.read_indirect(self._read_r(0)); return "MOV A,@R0", 1, extra
        if opcode == 0xE7:
            self.a = self.memory.read_indirect(self._read_r(1)); return "MOV A,@R1", 1, extra
        if 0xE8 <= opcode <= 0xEF:
            self.a = self._read_r(opcode & 0x07); return f"MOV A,R{opcode & 0x07}", 1, extra
        return self._undefined_opcode(opcode)

    def _group_0xF(self, opcode: int) -> tuple[str, int, list[int]]:
        extra: list[int] = []
        if opcode == 0xF0:
            self.memory.write_xram(self.dptr, self.a); return "MOVX @DPTR,A", 2, extra
        if opcode == 0xF2:
            self.memory.write_xram(self._read_r(0), self.a); return "MOVX @R0,A", 2, extra
        if opcode == 0xF3:
            self.memory.write_xram(self._read_r(1), self.a); return "MOVX @R1,A", 2, extra
        if opcode == 0xF4:
            self.a = (~self.a) & 0xFF; return "CPL A", 1, extra
        if opcode == 0xF5:
            direct = self._fetch8(); extra.append(direct); self._write_direct(direct, self.a); return f"MOV 0x{direct:02X},A", 1, extra
        if opcode == 0xF6:
            self.memory.write_indirect(self._read_r(0), self.a); return "MOV @R0,A", 1, extra
        if opcode == 0xF7:
            self.memory.write_indirect(self._read_r(1), self.a); return "MOV @R1,A", 1, extra
        if 0xF8 <= opcode <= 0xFF:
            self._write_r(opcode & 0x07, self.a); return f"MOV R{opcode & 0x07},A", 1, extra
        return self._undefined_opcode(opcode)

    def _read_accumulator_group_operand(self, low: int, extra: list[int]) -> int:
        if low == 4:
            value = self._fetch8(); extra.append(value); return value
        if low == 5:
            direct = self._fetch8(); extra.append(direct); return self._read_direct(direct)
        if low == 6:
            return self.memory.read_indirect(self._read_r(0))
        if low == 7:
            return self.memory.read_indirect(self._read_r(1))
        return self._read_r(low - 8)

    def _execute_xch(self, opcode: int, extra: list[int]) -> str:
        if opcode == 0xC5:
            direct = self._fetch8(); extra.append(direct)
            value = self._read_direct(direct, rmw=True)
            self._write_direct(direct, self.a)
            self.a = value
            return f"XCH A,0x{direct:02X}"
        if opcode == 0xC6:
            addr = self._read_r(0)
            value = self.memory.read_indirect(addr)
            self.memory.write_indirect(addr, self.a)
            self.a = value
            return "XCH A,@R0"
        if opcode == 0xC7:
            addr = self._read_r(1)
            value = self.memory.read_indirect(addr)
            self.memory.write_indirect(addr, self.a)
            self.a = value
            return "XCH A,@R1"
        reg = opcode & 0x07
        value = self._read_r(reg)
        self._write_r(reg, self.a)
        self.a = value
        return f"XCH A,R{reg}"

    def _execute_cjne(self, opcode: int, extra: list[int]) -> str:
        if opcode == 0xB4:
            imm = self._fetch8(); rel = self._fetch8(); extra.extend([imm, rel])
            left = self.a; right = imm
        elif opcode == 0xB5:
            direct = self._fetch8(); rel = self._fetch8(); extra.extend([direct, rel])
            left = self.a; right = self._read_direct(direct)
        elif opcode in {0xB6, 0xB7}:
            imm = self._fetch8(); rel = self._fetch8(); extra.extend([imm, rel])
            left = self.memory.read_indirect(self._read_r(0 if opcode == 0xB6 else 1)); right = imm
        else:
            imm = self._fetch8(); rel = self._fetch8(); extra.extend([imm, rel])
            reg = opcode & 0x07
            left = self._read_r(reg); right = imm
        self._set_flag("CY", 1 if left < right else 0)
        if left != right:
            self._set_pc(self._relative_target(rel))
        return "CJNE"

    def _execute_da(self) -> None:
        adjust = 0
        carry_out = bool(self._get_flag("CY"))
        if (self.a & 0x0F) > 0x09 or self._get_flag("AC"):
            adjust |= 0x06
        if self.a > 0x99 or self._get_flag("CY"):
            adjust |= 0x60
            carry_out = True
        result = self.a + adjust
        if result > 0xFF:
            carry_out = True
        self.a = result & 0xFF
        self._set_flag("CY", 1 if carry_out else 0)

    def snapshot(self) -> dict:
        psw = self.memory.read_sfr("PSW")
        registers = {
            "A": self.a,
            "B": self.b,
            "SP": self.sp,
            "PC": self.pc,
            "DPTR": self.dptr,
            "PSW": psw,
            **{f"R{i}": self._read_r(i) for i in range(8)},
        }
        flags = {name: self._get_flag(name) for name in ["P", "OV", "RS0", "RS1", "F0", "AC", "CY"]}
        timers = {
            "t0": {
                "TL": self.memory.read_sfr("TL0"),
                "TH": self.memory.read_sfr("TH0"),
                "TR": self._get_flag("TR0"),
                "TF": self._get_flag("TF0"),
            },
            "t1": {
                "TL": self.memory.read_sfr("TL1"),
                "TH": self.memory.read_sfr("TH1"),
                "TR": self._get_flag("TR1"),
                "TF": self._get_flag("TF1"),
            },
            "t2": {
                "TL": self.memory.read_sfr("TL2"),
                "TH": self.memory.read_sfr("TH2"),
                "RCAPL": self.memory.read_sfr("RCAP2L"),
                "RCAPH": self.memory.read_sfr("RCAP2H"),
                "TR": self._get_flag("TR2"),
                "TF": self._get_flag("TF2"),
            },
        }
        ports = {}
        for port_index, addr in enumerate((0x80, 0x90, 0xA0, 0xB0)):
            port = self.memory.ports[addr]
            ports[f"P{port_index}"] = {
                "latch": port.latch,
                "pin": port.read_pin(),
                "open_drain": port.open_drain,
            }
        return {
            "registers": registers,
            "flags": flags,
            "timers": timers,
            "ports": ports,
            "cycles": self.cycles,
            "clock_hz": self.clock_hz,
            "effective_clock_hz": self.effective_clock_hz(),
            "execution_mode": self.execution_mode,
            "halted": self.halted,
            "last_error": self.last_error,
            "last_interrupt": self.last_interrupt,
            "iram": self.memory.dump_iram(),
            "sfr": self.memory.dump_sfr(),
            "rom": self.memory.dump_rom(self.program.origin + len(self.program.binary) if self.program else 0x100),
            "xram_sample": {idx: self.memory.read_xram(idx) for idx in range(0x20)},
            "breakpoints": [
                {"pc": breakpoint.pc, "condition": breakpoint.condition, "enabled": breakpoint.enabled}
                for breakpoint in self.debugger.breakpoints.values()
            ],
            "watchpoints": [{"space": wp.space, "target": wp.target} for wp in self.debugger.watchpoints if wp.enabled],
            "call_stack": self.debugger.call_stack[:],
            "trace": [
                {
                    "pc": item.pc,
                    "opcode": item.opcode,
                    "mnemonic": item.mnemonic,
                    "bytes": item.bytes_,
                    "cycles": item.cycles,
                    "line": item.line,
                    "text": item.text,
                    "register_diff": item.register_diff,
                    "interrupt": item.interrupt,
                }
                    for item in self.debugger.trace
                ],
            "history_depth": len(self.debugger.history),
            "serial": {
                "tx": self.serial.tx_log[:],
                "rx_pending": list(self.serial.rx_queue),
            },
            "io_reads": [dict(item) for item in self.io_reads],
            "debug_mode": self.debug_mode,
        }

    def hardware_snapshot(self) -> dict:
        ports = {}
        for port_index, addr in enumerate((0x80, 0x90, 0xA0, 0xB0)):
            port = self.memory.ports[addr]
            ports[f"P{port_index}"] = {
                "latch": port.latch,
                "pin": port.read_pin(),
                "open_drain": port.open_drain,
            }
        return {
            "ports": ports,
            "cycles": self.cycles,
            "clock_hz": self.clock_hz,
            "effective_clock_hz": self.effective_clock_hz(),
            "execution_mode": self.execution_mode,
            "halted": self.halted,
            "last_error": self.last_error,
            "last_interrupt": self.last_interrupt,
            "io_reads": [dict(item) for item in self.io_reads],
            "debug_mode": self.debug_mode,
        }

    def hardware_tick_snapshot(self) -> dict:
        ports = {}
        signature: list[tuple[int, int, int]] = []
        for port_index, addr in enumerate((0x80, 0x90, 0xA0, 0xB0)):
            port = self.memory.ports[addr]
            pin_value = port.read_pin()
            ports[f"P{port_index}"] = {
                "latch": port.latch,
                "pin": pin_value,
                "open_drain": port.open_drain,
            }
            signature.append((port.latch & 0xFF, pin_value & 0xFF, 1 if port.open_drain else 0))
        io_reads_delta = [dict(item) for item in self._pending_io_reads]
        self._pending_io_reads.clear()
        signature_tuple = tuple(signature)
        if signature_tuple == self._last_hardware_tick_signature and not io_reads_delta:
            return {}
        self._last_hardware_tick_signature = signature_tuple
        return {
            "ports": ports,
            "cycles": self.cycles,
            "clock_hz": self.clock_hz,
            "effective_clock_hz": self.effective_clock_hz(),
            "execution_mode": self.execution_mode,
            "halted": self.halted,
            "last_error": self.last_error,
            "last_interrupt": self.last_interrupt,
            "io_reads_delta": io_reads_delta,
            "debug_mode": self.debug_mode,
        }

    def runtime_snapshot(self) -> dict:
        return {
            "registers": self._debug_registers(),
            "flags": {name: self._get_flag(name) for name in ["P", "OV", "RS0", "RS1", "F0", "AC", "CY"]},
            "cycles": self.cycles,
            "clock_hz": self.clock_hz,
            "effective_clock_hz": self.effective_clock_hz(),
            "execution_mode": self.execution_mode,
            "halted": self.halted,
            "last_error": self.last_error,
            "last_interrupt": self.last_interrupt,
            "debug_mode": self.debug_mode,
        }

    def signal_runtime_snapshot(self) -> dict:
        return {
            "registers": {"PC": self.pc},
            "cycles": self.cycles,
            "halted": self.halted,
            "last_error": self.last_error,
            "last_interrupt": self.last_interrupt,
        }

    def serialize_state(self) -> dict:
        return {
            "pc": self.pc,
            "cycles": self.cycles,
            "halted": self.halted,
            "last_error": self.last_error,
            "last_interrupt": self.last_interrupt,
            "clock_hz": self.clock_hz,
            "speed_multiplier": self.speed_multiplier,
            "execution_mode": self.execution_mode,
            "debug_mode": self.debug_mode,
            "memory": self.memory.export_state(),
            "serial": {
                "tx_log": self.serial.tx_log[:],
                "rx_queue": list(self.serial.rx_queue),
                "pending_tx_cycles": self.serial.pending_tx_cycles,
                "pending_rx_cycles": self.serial.pending_rx_cycles,
                "tx_byte": self.serial.tx_byte,
            },
            "io_reads": [dict(item) for item in self.io_reads],
            "pending_io_reads": [dict(item) for item in self._pending_io_reads],
            "active_interrupt_priorities": self.active_interrupt_priorities[:],
            "previous_int_pins": self._previous_int_pins.copy(),
            "previous_timer_pins": self._previous_timer_pins.copy(),
            "pending_timer_edges": self._pending_timer_edges.copy(),
            "previous_t2_pins": self._previous_t2_pins.copy(),
            "pending_t2_edges": self._pending_t2_edges.copy(),
            "last_hardware_tick_signature": list(self._last_hardware_tick_signature or []),
            "debugger": {
                "call_stack": self.debugger.call_stack[:],
                "breakpoints": [
                    {
                        "pc": breakpoint.pc,
                        "condition": breakpoint.condition,
                        "enabled": breakpoint.enabled,
                    }
                    for breakpoint in self.debugger.breakpoints.values()
                ],
                "watchpoints": [
                    {"space": wp.space, "target": wp.target, "enabled": wp.enabled}
                    for wp in self.debugger.watchpoints
                ],
                "trace": [
                    {
                        "pc": item.pc,
                        "opcode": item.opcode,
                        "mnemonic": item.mnemonic,
                        "bytes": item.bytes_,
                        "cycles": item.cycles,
                        "line": item.line,
                        "text": item.text,
                        "changes": item.changes,
                        "register_diff": item.register_diff,
                        "interrupt": item.interrupt,
                    }
                    for item in self.debugger.trace
                ],
                "history": [
                    {
                        "trace": {
                            "pc": item.trace.pc,
                            "opcode": item.trace.opcode,
                            "mnemonic": item.trace.mnemonic,
                            "bytes": item.trace.bytes_,
                            "cycles": item.trace.cycles,
                            "line": item.trace.line,
                            "text": item.trace.text,
                            "changes": item.trace.changes,
                            "register_diff": item.trace.register_diff,
                            "interrupt": item.trace.interrupt,
                        },
                        "cycles_before": item.cycles_before,
                        "cycles_after": item.cycles_after,
                        "halted_before": item.halted_before,
                        "halted_after": item.halted_after,
                        "last_error_before": item.last_error_before,
                        "last_error_after": item.last_error_after,
                        "last_interrupt_before": item.last_interrupt_before,
                        "last_interrupt_after": item.last_interrupt_after,
                        "call_stack_before": item.call_stack_before,
                        "call_stack_after": item.call_stack_after,
                        "extra_before": item.extra_before,
                        "extra_after": item.extra_after,
                    }
                    for item in self.debugger.history
                ],
            },
        }

    def load_state(self, state: dict) -> None:
        self.pc = int(state.get("pc", 0)) & 0xFFFF
        self.cycles = int(state.get("cycles", 0))
        self.halted = bool(state.get("halted", True))
        self.last_error = state.get("last_error")
        self.last_interrupt = state.get("last_interrupt")
        self.clock_hz = int(state.get("clock_hz", self.clock_hz))
        self.speed_multiplier = float(state.get("speed_multiplier", self.speed_multiplier))
        self.execution_mode = str(state.get("execution_mode", self.execution_mode))
        self.debug_mode = bool(state.get("debug_mode", False))
        self.memory.import_state(dict(state.get("memory", {})))
        serial = dict(state.get("serial", {}))
        self.serial = SerialPort(
            tx_log=[int(item) & 0xFF for item in serial.get("tx_log", [])],
            rx_queue=deque(int(item) & 0xFF for item in serial.get("rx_queue", [])),
            pending_tx_cycles=int(serial.get("pending_tx_cycles", 0)),
            pending_rx_cycles=int(serial.get("pending_rx_cycles", 0)),
            tx_byte=serial.get("tx_byte"),
        )
        self.io_reads = deque(
            [
                {
                    "signal": str(item.get("signal", "")),
                    "value": int(item.get("value", 0)) & 0x01,
                    "time_ms": float(item.get("time_ms", 0.0) or 0.0),
                    "source": str(item.get("source", "cpu")),
                }
                for item in state.get("io_reads", [])
            ],
            maxlen=128,
        )
        self._pending_io_reads = deque(
            [
                {
                    "signal": str(item.get("signal", "")),
                    "value": int(item.get("value", 0)) & 0x01,
                    "time_ms": float(item.get("time_ms", 0.0) or 0.0),
                    "source": str(item.get("source", "cpu")),
                }
                for item in state.get("pending_io_reads", [])
            ]
        )
        self.active_interrupt_priorities = [int(item) for item in state.get("active_interrupt_priorities", [])]
        self._previous_int_pins = {
            0: int(dict(state.get("previous_int_pins", {})).get("0", dict(state.get("previous_int_pins", {})).get(0, 1))),
            1: int(dict(state.get("previous_int_pins", {})).get("1", dict(state.get("previous_int_pins", {})).get(1, 1))),
        }
        self._previous_timer_pins = {
            0: int(dict(state.get("previous_timer_pins", {})).get("0", dict(state.get("previous_timer_pins", {})).get(0, 1))),
            1: int(dict(state.get("previous_timer_pins", {})).get("1", dict(state.get("previous_timer_pins", {})).get(1, 1))),
        }
        self._pending_timer_edges = {
            0: int(dict(state.get("pending_timer_edges", {})).get("0", dict(state.get("pending_timer_edges", {})).get(0, 0))),
            1: int(dict(state.get("pending_timer_edges", {})).get("1", dict(state.get("pending_timer_edges", {})).get(1, 0))),
        }
        self._previous_t2_pins = {
            0: int(dict(state.get("previous_t2_pins", {})).get("0", dict(state.get("previous_t2_pins", {})).get(0, 1))),
            1: int(dict(state.get("previous_t2_pins", {})).get("1", dict(state.get("previous_t2_pins", {})).get(1, 1))),
        }
        self._pending_t2_edges = {
            0: int(dict(state.get("pending_t2_edges", {})).get("0", dict(state.get("pending_t2_edges", {})).get(0, 0))),
            1: int(dict(state.get("pending_t2_edges", {})).get("1", dict(state.get("pending_t2_edges", {})).get(1, 0))),
        }
        signature = state.get("last_hardware_tick_signature", [])
        self._last_hardware_tick_signature = tuple(tuple(int(value) for value in item) for item in signature) if signature else None
        debugger = dict(state.get("debugger", {}))
        self.set_breakpoints(list(debugger.get("breakpoints", [])))
        self.debugger.call_stack = [int(item) for item in debugger.get("call_stack", [])]
        self.set_watchpoints(
            [
                Watchpoint(
                    target=item.get("target", item.get("address")),
                    space=item.get("space", "iram"),
                    enabled=bool(item.get("enabled", True)),
                )
                for item in debugger.get("watchpoints", [])
            ]
        )
        self.debugger.trace.clear()
        for item in debugger.get("trace", []):
            self.debugger.trace.append(
                TraceEntry(
                    pc=int(item["pc"]),
                    opcode=int(item["opcode"]),
                    mnemonic=str(item["mnemonic"]),
                    bytes_=[int(value) for value in item.get("bytes", [])],
                    cycles=int(item.get("cycles", 0)),
                    line=item.get("line"),
                    text=item.get("text"),
                    changes={key: [tuple(change) for change in value] for key, value in dict(item.get("changes", {})).items()},
                    register_diff={key: dict(value) for key, value in dict(item.get("register_diff", {})).items()},
                    interrupt=item.get("interrupt"),
                )
            )
        self.debugger.history.clear()
        for item in debugger.get("history", []):
            trace = dict(item.get("trace", {}))
            self.debugger.history.append(
                ReverseDelta(
                    trace=TraceEntry(
                        pc=int(trace["pc"]),
                        opcode=int(trace["opcode"]),
                        mnemonic=str(trace["mnemonic"]),
                        bytes_=[int(value) for value in trace.get("bytes", [])],
                        cycles=int(trace.get("cycles", 0)),
                        line=trace.get("line"),
                        text=trace.get("text"),
                        changes={key: [tuple(change) for change in value] for key, value in dict(trace.get("changes", {})).items()},
                        register_diff={key: dict(value) for key, value in dict(trace.get("register_diff", {})).items()},
                        interrupt=trace.get("interrupt"),
                    ),
                    cycles_before=int(item.get("cycles_before", 0)),
                    cycles_after=int(item.get("cycles_after", 0)),
                    halted_before=bool(item.get("halted_before", False)),
                    halted_after=bool(item.get("halted_after", False)),
                    last_error_before=item.get("last_error_before"),
                    last_error_after=item.get("last_error_after"),
                    last_interrupt_before=item.get("last_interrupt_before"),
                    last_interrupt_after=item.get("last_interrupt_after"),
                    call_stack_before=[int(value) for value in item.get("call_stack_before", [])],
                    call_stack_after=[int(value) for value in item.get("call_stack_after", [])],
                    extra_before=dict(item.get("extra_before", {})),
                    extra_after=dict(item.get("extra_after", {})),
                )
            )

    def _restore_register_values(self, values: dict[str, int]) -> None:
        for name, value in values.items():
            key = name.upper()
            if key == "A":
                self.a = value
            elif key == "B":
                self.b = value
            elif key == "SP":
                self.sp = value
            elif key == "PC":
                self.pc = value & 0xFFFF
            elif key == "DPTR":
                self.dptr = value
            elif key == "PSW":
                self.memory.write_sfr("PSW", value & 0xFF)
            elif key.startswith("R") and key[1:].isdigit():
                self._write_r(int(key[1:]), value & 0xFF)

    def _capture_extra_state(self) -> dict:
        return {
            "serial": {
                "tx_log": self.serial.tx_log[:],
                "rx_queue": list(self.serial.rx_queue),
                "pending_tx_cycles": self.serial.pending_tx_cycles,
                "pending_rx_cycles": self.serial.pending_rx_cycles,
                "tx_byte": self.serial.tx_byte,
            },
            "pending_io_reads": [dict(item) for item in self._pending_io_reads],
            "active_interrupt_priorities": self.active_interrupt_priorities[:],
            "previous_int_pins": self._previous_int_pins.copy(),
            "previous_timer_pins": self._previous_timer_pins.copy(),
            "pending_timer_edges": self._pending_timer_edges.copy(),
            "previous_t2_pins": self._previous_t2_pins.copy(),
            "pending_t2_edges": self._pending_t2_edges.copy(),
            "last_hardware_tick_signature": list(self._last_hardware_tick_signature or []),
        }

    def _restore_extra_state(self, state: dict) -> None:
        serial = dict(state.get("serial", {}))
        self.serial = SerialPort(
            tx_log=[int(item) & 0xFF for item in serial.get("tx_log", [])],
            rx_queue=deque(int(item) & 0xFF for item in serial.get("rx_queue", [])),
            pending_tx_cycles=int(serial.get("pending_tx_cycles", 0)),
            pending_rx_cycles=int(serial.get("pending_rx_cycles", 0)),
            tx_byte=serial.get("tx_byte"),
        )
        self._pending_io_reads = deque(
            [
                {
                    "signal": str(item.get("signal", "")),
                    "value": int(item.get("value", 0)) & 0x01,
                    "time_ms": float(item.get("time_ms", 0.0) or 0.0),
                    "source": str(item.get("source", "cpu")),
                }
                for item in state.get("pending_io_reads", [])
            ]
        )
        self.active_interrupt_priorities = [int(item) for item in state.get("active_interrupt_priorities", [])]
        previous = dict(state.get("previous_int_pins", {}))
        self._previous_int_pins = {
            0: int(previous.get("0", previous.get(0, 1))),
            1: int(previous.get("1", previous.get(1, 1))),
        }
        timer_previous = dict(state.get("previous_timer_pins", {}))
        self._previous_timer_pins = {
            0: int(timer_previous.get("0", timer_previous.get(0, 1))),
            1: int(timer_previous.get("1", timer_previous.get(1, 1))),
        }
        timer_edges = dict(state.get("pending_timer_edges", {}))
        self._pending_timer_edges = {
            0: int(timer_edges.get("0", timer_edges.get(0, 0))),
            1: int(timer_edges.get("1", timer_edges.get(1, 0))),
        }
        t2_previous = dict(state.get("previous_t2_pins", {}))
        self._previous_t2_pins = {
            0: int(t2_previous.get("0", t2_previous.get(0, 1))),
            1: int(t2_previous.get("1", t2_previous.get(1, 1))),
        }
        t2_edges = dict(state.get("pending_t2_edges", {}))
        self._pending_t2_edges = {
            0: int(t2_edges.get("0", t2_edges.get(0, 0))),
            1: int(t2_edges.get("1", t2_edges.get(1, 0))),
        }
        signature = state.get("last_hardware_tick_signature", [])
        self._last_hardware_tick_signature = tuple(tuple(int(value) for value in item) for item in signature) if signature else None
