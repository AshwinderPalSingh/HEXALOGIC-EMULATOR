from __future__ import annotations

from collections import deque
import os

from .base_cpu import BaseCPU
from .exceptions import DecodeError, ExecutionError
from .memory import GPIOA_MMIO_BASE, MemoryMap
from .model import ProgramImage, ReverseDelta, TraceEntry, Watchpoint

COND_NAMES = {
    0x0: "EQ",
    0x1: "NE",
    0x2: "CS",
    0x3: "CC",
    0x4: "MI",
    0x5: "PL",
    0x6: "VS",
    0x7: "VC",
    0x8: "HI",
    0x9: "LS",
    0xA: "GE",
    0xB: "LT",
    0xC: "GT",
    0xD: "LE",
    0xE: "AL",
}
SHIFT_NAMES = {0: "LSL", 1: "LSR", 2: "ASR", 3: "ROR"}
DATA_PROCESSING_NAMES = {
    0x0: "AND",
    0x1: "EOR",
    0x2: "SUB",
    0x3: "RSB",
    0x4: "ADD",
    0x5: "ADC",
    0x6: "SBC",
    0x7: "RSC",
    0x8: "TST",
    0x9: "TEQ",
    0xA: "CMP",
    0xB: "CMN",
    0xC: "ORR",
    0xD: "MOV",
    0xE: "BIC",
    0xF: "MVN",
}
_DEBUG_TIMING = os.environ.get("HEXLOGIC_DEBUG_TIMING", "").strip().lower() in {"1", "true", "yes", "on"}
_ARM_GPIO_OUT = 0x00
_ARM_GPIO_IN = 0x04
_ARM_GPIO_DIR = 0x08
_ARM_GPIO_IRQ_ENABLE = 0x0C
_ARM_GPIO_IRQ_RISE = 0x10
_ARM_GPIO_IRQ_FALL = 0x14
_ARM_GPIO_IRQ_PENDING = 0x18
_ARM_TIMER_LOAD = 0x1C
_ARM_TIMER_VALUE = 0x20
_ARM_TIMER_CTRL = 0x24
_ARM_TIMER_PENDING = 0x28
_ARM_TIMER_CTRL_ENABLE = 0x01
_ARM_TIMER_CTRL_PERIODIC = 0x02
_ARM_TIMER_CTRL_IRQ_ENABLE = 0x04
_ARM_IRQ_VECTOR = 0x18


class CPUARM(BaseCPU):
    def __init__(self, *, code_size: int = 0x1000, data_size: int = 0x10000, endian: str = "little") -> None:
        super().__init__()
        self.endian = endian if endian in {"little", "big"} else "little"
        self.memory = MemoryMap(code_size=code_size, xram_size=data_size, upper_iram=False)
        self.registers = [0] * 16
        self.io_reads: deque[dict[str, int | float | str]] = deque(maxlen=128)
        self._pending_io_reads: deque[dict[str, int | float | str]] = deque()
        self.flag_n = 0
        self.flag_z = 0
        self.flag_c = 0
        self.flag_v = 0
        self._gpio_input_mask = 0
        self._gpio_input_value = 0
        self._previous_gpio_inputs = 0
        self._arm_interrupt_stack: list[int] = []
        self._last_hardware_tick_signature = None
        self._listing_text_by_address: dict[int, str] = {}
        self.reset(hard=True)

    def reset(self, *, hard: bool = False) -> None:
        self.memory.reset()
        self.registers = [0] * 16
        self.flag_n = 0
        self.flag_z = 0
        self.flag_c = 0
        self.flag_v = 0
        self.pc = 0
        self.cycles = 0
        self.halted = self.program is None
        self.last_error = None
        self.last_interrupt = None
        self.debugger.call_stack.clear()
        self.io_reads.clear()
        self._pending_io_reads.clear()
        self._gpio_input_mask = 0
        self._gpio_input_value = 0
        self._previous_gpio_inputs = 0
        self._arm_interrupt_stack = []
        self._last_hardware_tick_signature = None
        self._write_mmio32(_ARM_GPIO_OUT, 0)
        self._write_mmio32(_ARM_GPIO_IN, 0)
        self._write_mmio32(_ARM_GPIO_DIR, 0)
        self._write_mmio32(_ARM_GPIO_IRQ_ENABLE, 0)
        self._write_mmio32(_ARM_GPIO_IRQ_RISE, 0)
        self._write_mmio32(_ARM_GPIO_IRQ_FALL, 0)
        self._write_mmio32(_ARM_GPIO_IRQ_PENDING, 0)
        self._write_mmio32(_ARM_TIMER_LOAD, 0)
        self._write_mmio32(_ARM_TIMER_VALUE, 0)
        self._write_mmio32(_ARM_TIMER_CTRL, 0)
        self._write_mmio32(_ARM_TIMER_PENDING, 0)
        self.memory.consume_changes()

    def load_program(self, program: ProgramImage) -> None:
        self.program = program
        self._listing_text_by_address = {item.address: item.text for item in program.listing}
        self.memory.reset()
        self.memory.load_rom(0, program.rom)
        self.registers = [0] * 16
        self.flag_n = 0
        self.flag_z = 0
        self.flag_c = 0
        self.flag_v = 0
        stack_base = min(max(0x40, self.memory.xram_size // 4), max(0x100, self.memory.xram_size - 4))
        self._write_reg(13, stack_base & 0xFFFFFFFF)
        self._write_reg(14, 0)
        self._set_pc(program.origin)
        self.cycles = 0
        self.halted = False
        self.last_error = None
        self.last_interrupt = None
        self.debugger.call_stack.clear()
        self.io_reads.clear()
        self._pending_io_reads.clear()
        self._gpio_input_mask = 0
        self._gpio_input_value = 0
        self._previous_gpio_inputs = 0
        self._arm_interrupt_stack = []
        self._last_hardware_tick_signature = None
        self._write_mmio32(_ARM_GPIO_OUT, 0)
        self._write_mmio32(_ARM_GPIO_IN, 0)
        self._write_mmio32(_ARM_GPIO_DIR, 0)
        self._write_mmio32(_ARM_GPIO_IRQ_ENABLE, 0)
        self._write_mmio32(_ARM_GPIO_IRQ_RISE, 0)
        self._write_mmio32(_ARM_GPIO_IRQ_FALL, 0)
        self._write_mmio32(_ARM_GPIO_IRQ_PENDING, 0)
        self._write_mmio32(_ARM_TIMER_LOAD, 0)
        self._write_mmio32(_ARM_TIMER_VALUE, 0)
        self._write_mmio32(_ARM_TIMER_CTRL, 0)
        self._write_mmio32(_ARM_TIMER_PENDING, 0)
        for address, value in sorted(program.xram_init.items()):
            self.memory.write_xram(address, value)
        self.memory.consume_changes()

    def set_endian(self, endian: str) -> None:
        if endian not in {"little", "big"}:
            raise ExecutionError(f"Unsupported endian `{endian}`", pc=self.pc)
        self.endian = endian

    def _set_pc(self, value: int) -> None:
        self.pc = value & 0xFFFFFFFF
        self.registers[15] = self.pc

    def _write_reg(self, index: int, value: int) -> None:
        value &= 0xFFFFFFFF
        self.registers[index] = value
        if index == 15:
            self.pc = value

    def _read_reg(self, index: int, *, current_pc: int | None = None) -> int:
        if index == 15:
            base_pc = self.pc if current_pc is None else current_pc
            return (base_pc + 8) & 0xFFFFFFFF
        return self.registers[index] & 0xFFFFFFFF

    def _current_opcode(self) -> int:
        return self.memory.read32(self.pc, space="code", endian=self.endian)

    def _read_mmio32(self, offset: int) -> int:
        return self.memory.read32(offset, space="xram", endian=self.endian)

    def _write_mmio32(self, offset: int, value: int) -> None:
        self.memory.write32(offset, value & 0xFFFFFFFF, space="xram", endian=self.endian)

    def _mmio_offset_for_address(self, address: int) -> int | None:
        address &= 0xFFFFFFFF
        if GPIOA_MMIO_BASE <= address < GPIOA_MMIO_BASE + 0x40:
            return address - GPIOA_MMIO_BASE
        if 0 <= address < 0x40:
            return address
        return None

    def _current_time_ms(self) -> float:
        cycles = float(self.cycles)
        computed_seconds = cycles / max(1.0, float(self.clock_hz))
        computed_ms = computed_seconds * 1000.0
        if _DEBUG_TIMING:
            print(
                "[DEBUG_TIMING][cpuarm]",
                {
                    "cycles": cycles,
                    "computed_seconds": round(computed_seconds, 9),
                    "computed_ms": round(computed_ms, 6),
                },
            )
        return computed_ms

    def _record_gpio_read(self, address: int, value: int) -> None:
        if address not in {0x04, GPIOA_MMIO_BASE + 0x04}:
            return
        time_ms = round(self._current_time_ms(), 6)
        for bit in range(16):
            event = {
                "signal": f"GPIOA.{bit}",
                "value": (value >> bit) & 0x01,
                "time_ms": time_ms,
                "source": "cpu",
            }
            self.io_reads.append(event)
            self._pending_io_reads.append(dict(event))

    def set_pin(self, port_index: int, bit: int, level: int | bool | None) -> None:
        if port_index != 0 or not 0 <= int(bit) < 16:
            return
        mask = 1 << int(bit)
        previous_inputs = self._gpio_input_value
        if level is None:
            self._gpio_input_mask &= ~mask
            self._gpio_input_value &= ~mask
        else:
            self._gpio_input_mask |= mask
            if int(bool(level)):
                self._gpio_input_value |= mask
            else:
                self._gpio_input_value &= ~mask
        dir_value = self._read_mmio32(_ARM_GPIO_DIR)
        out_value = self._read_mmio32(_ARM_GPIO_OUT)
        visible_inputs = self._gpio_input_value & self._gpio_input_mask
        input_value = (visible_inputs & ~dir_value) | (out_value & dir_value)
        self._write_mmio32(_ARM_GPIO_IN, input_value)
        self._update_gpio_interrupts(previous_inputs, input_value)

    def _gpio_regs(self) -> dict[str, int]:
        return {
            "out": self._read_mmio32(_ARM_GPIO_OUT),
            "in": self._read_mmio32(_ARM_GPIO_IN),
            "dir": self._read_mmio32(_ARM_GPIO_DIR),
            "irq_enable": self._read_mmio32(_ARM_GPIO_IRQ_ENABLE),
            "irq_rise": self._read_mmio32(_ARM_GPIO_IRQ_RISE),
            "irq_fall": self._read_mmio32(_ARM_GPIO_IRQ_FALL),
            "irq_pending": self._read_mmio32(_ARM_GPIO_IRQ_PENDING),
            "timer_load": self._read_mmio32(_ARM_TIMER_LOAD),
            "timer_value": self._read_mmio32(_ARM_TIMER_VALUE),
            "timer_ctrl": self._read_mmio32(_ARM_TIMER_CTRL),
            "timer_pending": self._read_mmio32(_ARM_TIMER_PENDING),
        }

    def _update_gpio_interrupts(self, previous_inputs: int, current_inputs: int) -> None:
        rising = (~previous_inputs & current_inputs) & 0xFFFF
        falling = (previous_inputs & ~current_inputs) & 0xFFFF
        enable = self._read_mmio32(_ARM_GPIO_IRQ_ENABLE) & 0xFFFF
        rises = self._read_mmio32(_ARM_GPIO_IRQ_RISE) & 0xFFFF
        falls = self._read_mmio32(_ARM_GPIO_IRQ_FALL) & 0xFFFF
        pending = self._read_mmio32(_ARM_GPIO_IRQ_PENDING) & 0xFFFF
        pending |= ((rising & rises) | (falling & falls)) & enable
        self._write_mmio32(_ARM_GPIO_IRQ_PENDING, pending)
        self._previous_gpio_inputs = current_inputs & 0xFFFF

    def _peripherals_active(self) -> bool:
        return bool(
            (self._read_mmio32(_ARM_TIMER_CTRL) & _ARM_TIMER_CTRL_ENABLE)
            or self._read_mmio32(_ARM_TIMER_PENDING)
            or self._read_mmio32(_ARM_GPIO_IRQ_ENABLE)
            or self._read_mmio32(_ARM_GPIO_IRQ_PENDING)
            or self._arm_interrupt_stack
        )

    def _tick_peripherals(self, cycles: int) -> None:
        ctrl = self._read_mmio32(_ARM_TIMER_CTRL)
        if not (ctrl & _ARM_TIMER_CTRL_ENABLE):
            return
        load = self._read_mmio32(_ARM_TIMER_LOAD) & 0xFFFFFFFF
        value = self._read_mmio32(_ARM_TIMER_VALUE) & 0xFFFFFFFF
        if value == 0:
            value = load
        remaining = int(value) - int(cycles)
        pending = self._read_mmio32(_ARM_TIMER_PENDING) & 0xFFFFFFFF
        while load > 0 and remaining <= 0:
            if ctrl & _ARM_TIMER_CTRL_IRQ_ENABLE:
                pending = 1
            if ctrl & _ARM_TIMER_CTRL_PERIODIC:
                remaining += int(load)
            else:
                ctrl &= ~_ARM_TIMER_CTRL_ENABLE
                remaining = 0
                break
        self._write_mmio32(_ARM_TIMER_VALUE, max(0, remaining))
        self._write_mmio32(_ARM_TIMER_PENDING, pending)
        self._write_mmio32(_ARM_TIMER_CTRL, ctrl)

    def _maybe_take_interrupt(self) -> str | None:
        if self._arm_interrupt_stack:
            return None
        timer_ctrl = self._read_mmio32(_ARM_TIMER_CTRL)
        timer_pending = self._read_mmio32(_ARM_TIMER_PENDING)
        gpio_enable = self._read_mmio32(_ARM_GPIO_IRQ_ENABLE)
        gpio_pending = self._read_mmio32(_ARM_GPIO_IRQ_PENDING)
        interrupt_name = None
        if (timer_ctrl & _ARM_TIMER_CTRL_IRQ_ENABLE) and timer_pending:
            interrupt_name = "IRQ_TIMER"
            self._write_mmio32(_ARM_TIMER_PENDING, 0)
        elif gpio_enable & gpio_pending:
            interrupt_name = "IRQ_GPIOA"
            self._write_mmio32(_ARM_GPIO_IRQ_PENDING, 0)
        if interrupt_name is None:
            return None
        return_address = self.pc & 0xFFFFFFFF
        self._arm_interrupt_stack.append(return_address)
        self._write_reg(14, return_address)
        self.debugger.call_stack.append(return_address)
        self._set_pc(_ARM_IRQ_VECTOR)
        self.last_interrupt = interrupt_name
        return interrupt_name

    def _instruction_length_preview(self, opcode: int) -> int:
        return 4

    def _is_call_opcode(self, opcode: int) -> bool:
        return ((opcode >> 25) & 0x7) == 0b101 and bool((opcode >> 24) & 0x1)

    def _debug_registers(self) -> dict[str, int]:
        payload = {f"R{index}": value & 0xFFFFFFFF for index, value in enumerate(self.registers)}
        payload.update({"SP": payload["R13"], "LR": payload["R14"], "PC": self.pc & 0xFFFFFFFF})
        return payload

    def _register_diff(self, before: dict[str, int], after: dict[str, int]) -> dict[str, dict[str, int]]:
        return {
            name: {"before": before[name], "after": after[name]}
            for name in after
            if before.get(name) != after[name]
        }

    def _flags_dict(self) -> dict[str, int]:
        return {"N": self.flag_n, "Z": self.flag_z, "C": self.flag_c, "V": self.flag_v}

    def _set_nz(self, result: int) -> None:
        self.flag_n = 1 if result & 0x80000000 else 0
        self.flag_z = 1 if (result & 0xFFFFFFFF) == 0 else 0

    def _set_logic_flags(self, result: int, carry: int | None = None) -> None:
        self._set_nz(result)
        if carry is not None:
            self.flag_c = 1 if carry else 0

    def _set_add_flags(self, left: int, right: int, carry_in: int, result: int) -> None:
        total = left + right + carry_in
        self._set_nz(result)
        self.flag_c = 1 if total > 0xFFFFFFFF else 0
        self.flag_v = 1 if (~(left ^ right) & (left ^ result) & 0x80000000) != 0 else 0

    def _set_sub_flags(self, left: int, right: int, borrow_in: int, result: int) -> None:
        total = left - right - borrow_in
        self._set_nz(result)
        self.flag_c = 1 if total >= 0 else 0
        self.flag_v = 1 if ((left ^ right) & (left ^ result) & 0x80000000) != 0 else 0

    def _condition_passed(self, cond: int) -> bool:
        if cond == 0x0:
            return self.flag_z == 1
        if cond == 0x1:
            return self.flag_z == 0
        if cond == 0x2:
            return self.flag_c == 1
        if cond == 0x3:
            return self.flag_c == 0
        if cond == 0x4:
            return self.flag_n == 1
        if cond == 0x5:
            return self.flag_n == 0
        if cond == 0x6:
            return self.flag_v == 1
        if cond == 0x7:
            return self.flag_v == 0
        if cond == 0x8:
            return self.flag_c == 1 and self.flag_z == 0
        if cond == 0x9:
            return self.flag_c == 0 or self.flag_z == 1
        if cond == 0xA:
            return self.flag_n == self.flag_v
        if cond == 0xB:
            return self.flag_n != self.flag_v
        if cond == 0xC:
            return self.flag_z == 0 and self.flag_n == self.flag_v
        if cond == 0xD:
            return self.flag_z == 1 or self.flag_n != self.flag_v
        if cond == 0xE:
            return True
        return False

    def _rotate_right(self, value: int, amount: int) -> int:
        amount %= 32
        if amount == 0:
            return value & 0xFFFFFFFF
        return ((value >> amount) | (value << (32 - amount))) & 0xFFFFFFFF

    def _apply_shift_with_carry(self, value: int, shift_type: int, amount: int, *, carry_in: int, immediate_form: bool) -> tuple[int, int]:
        value &= 0xFFFFFFFF
        if shift_type == 0:  # LSL
            if amount == 0:
                return value, carry_in
            if amount < 32:
                return (value << amount) & 0xFFFFFFFF, (value >> (32 - amount)) & 1
            if amount == 32:
                return 0, value & 1
            return 0, 0
        if shift_type == 1:  # LSR
            if amount == 0 and immediate_form:
                amount = 32
            if amount == 0:
                return value, carry_in
            if amount < 32:
                return value >> amount, (value >> (amount - 1)) & 1
            if amount == 32:
                return 0, (value >> 31) & 1
            return 0, 0
        if shift_type == 2:  # ASR
            if amount == 0 and immediate_form:
                amount = 32
            if amount == 0:
                return value, carry_in
            sign = (value >> 31) & 1
            if amount >= 32:
                return (0xFFFFFFFF if sign else 0), sign
            shifted = (value >> amount) | (((1 << amount) - 1) << (32 - amount) if sign else 0)
            return shifted & 0xFFFFFFFF, (value >> (amount - 1)) & 1
        if amount == 0 and immediate_form:  # RRX
            return (((carry_in & 1) << 31) | (value >> 1)) & 0xFFFFFFFF, value & 1
        if amount == 0:
            return value, carry_in
        amount &= 31
        if amount == 0:
            return value, (value >> 31) & 1
        rotated = self._rotate_right(value, amount)
        return rotated, (rotated >> 31) & 1

    def _decode_operand2(self, opcode: int, current_pc: int) -> tuple[int, int, str]:
        if opcode & (1 << 25):
            rotate = ((opcode >> 8) & 0xF) * 2
            immediate = opcode & 0xFF
            value = self._rotate_right(immediate, rotate)
            carry = self.flag_c if rotate == 0 else (value >> 31) & 1
            return value & 0xFFFFFFFF, carry, f"#0x{value & 0xFFFFFFFF:08X}"

        rm = opcode & 0xF
        shift_type = (opcode >> 5) & 0x3
        if opcode & (1 << 4):
            if opcode & (1 << 7):
                raise DecodeError("ARM register-shift form is invalid", pc=current_pc)
            rs = (opcode >> 8) & 0xF
            amount = self._read_reg(rs, current_pc=current_pc) & 0xFF
            value, carry = self._apply_shift_with_carry(
                self._read_reg(rm, current_pc=current_pc),
                shift_type,
                amount,
                carry_in=self.flag_c,
                immediate_form=False,
            )
            return value, carry, f"R{rm}, {SHIFT_NAMES[shift_type]} R{rs}"

        amount = (opcode >> 7) & 0x1F
        value, carry = self._apply_shift_with_carry(
            self._read_reg(rm, current_pc=current_pc),
            shift_type,
            amount,
            carry_in=self.flag_c,
            immediate_form=True,
        )
        if amount == 0 and shift_type == 0:
            return value, carry, f"R{rm}"
        suffix = "RRX" if shift_type == 3 and amount == 0 else f"{SHIFT_NAMES[shift_type]} #{amount}"
        return value, carry, f"R{rm}, {suffix}"

    def _decode_memory_offset(self, opcode: int, current_pc: int) -> tuple[int, str]:
        if ((opcode >> 25) & 0x1) == 0:
            offset = opcode & 0xFFF
            return offset, f"#{offset}"
        rm = opcode & 0xF
        shift_type = (opcode >> 5) & 0x3
        shift_amount = (opcode >> 7) & 0x1F
        if opcode & (1 << 4):
            raise DecodeError("ARM load/store register shifts by register are not implemented", pc=current_pc)
        offset, _ = self._apply_shift_with_carry(
            self._read_reg(rm, current_pc=current_pc),
            shift_type,
            shift_amount,
            carry_in=self.flag_c,
            immediate_form=True,
        )
        if shift_amount == 0 and shift_type == 0:
            return offset, f"R{rm}"
        return offset, f"R{rm}, {SHIFT_NAMES[shift_type]} #{shift_amount}"

    def _execute_data_processing(self, opcode: int, current_pc: int) -> tuple[str, int]:
        opcode_id = (opcode >> 21) & 0xF
        if opcode_id not in DATA_PROCESSING_NAMES:
            raise DecodeError(f"ARM opcode 0x{opcode:08X} is not implemented", pc=current_pc)
        mnemonic = DATA_PROCESSING_NAMES[opcode_id]
        set_flags = bool((opcode >> 20) & 0x1)
        rd = (opcode >> 12) & 0xF
        rn = (opcode >> 16) & 0xF
        operand2, shifter_carry, operand_text = self._decode_operand2(opcode, current_pc)
        left = self._read_reg(rn, current_pc=current_pc)

        if opcode_id == 0xD:  # MOV
            result = operand2 & 0xFFFFFFFF
            self._write_reg(rd, result)
            if set_flags:
                self._set_logic_flags(result, shifter_carry)
            return f"MOV R{rd},{operand_text}", 1
        if opcode_id == 0xF:  # MVN
            result = (~operand2) & 0xFFFFFFFF
            self._write_reg(rd, result)
            if set_flags:
                self._set_logic_flags(result, shifter_carry)
            return f"MVN R{rd},{operand_text}", 1
        if opcode_id == 0x0:  # AND
            result = left & operand2
            self._write_reg(rd, result)
            if set_flags:
                self._set_logic_flags(result, shifter_carry)
            return f"AND R{rd},R{rn},{operand_text}", 1
        if opcode_id == 0x1:  # EOR
            result = left ^ operand2
            self._write_reg(rd, result)
            if set_flags:
                self._set_logic_flags(result, shifter_carry)
            return f"EOR R{rd},R{rn},{operand_text}", 1
        if opcode_id == 0xC:  # ORR
            result = left | operand2
            self._write_reg(rd, result)
            if set_flags:
                self._set_logic_flags(result, shifter_carry)
            return f"ORR R{rd},R{rn},{operand_text}", 1
        if opcode_id == 0xE:  # BIC
            result = left & (~operand2 & 0xFFFFFFFF)
            self._write_reg(rd, result)
            if set_flags:
                self._set_logic_flags(result, shifter_carry)
            return f"BIC R{rd},R{rn},{operand_text}", 1
        if opcode_id == 0x8:  # TST
            result = left & operand2
            self._set_logic_flags(result, shifter_carry)
            return f"TST R{rn},{operand_text}", 1
        if opcode_id == 0x9:  # TEQ
            result = left ^ operand2
            self._set_logic_flags(result, shifter_carry)
            return f"TEQ R{rn},{operand_text}", 1
        if opcode_id == 0xA:  # CMP
            result = (left - operand2) & 0xFFFFFFFF
            self._set_sub_flags(left, operand2, 0, result)
            return f"CMP R{rn},{operand_text}", 1
        if opcode_id == 0xB:  # CMN
            result = (left + operand2) & 0xFFFFFFFF
            self._set_add_flags(left, operand2, 0, result)
            return f"CMN R{rn},{operand_text}", 1
        if opcode_id == 0x4:  # ADD
            result = (left + operand2) & 0xFFFFFFFF
            self._write_reg(rd, result)
            if set_flags:
                self._set_add_flags(left, operand2, 0, result)
            return f"ADD R{rd},R{rn},{operand_text}", 1
        if opcode_id == 0x5:  # ADC
            carry_in = self.flag_c
            result = (left + operand2 + carry_in) & 0xFFFFFFFF
            self._write_reg(rd, result)
            if set_flags:
                self._set_add_flags(left, operand2, carry_in, result)
            return f"ADC R{rd},R{rn},{operand_text}", 1
        if opcode_id == 0x2:  # SUB
            result = (left - operand2) & 0xFFFFFFFF
            self._write_reg(rd, result)
            if set_flags:
                self._set_sub_flags(left, operand2, 0, result)
            return f"SUB R{rd},R{rn},{operand_text}", 1
        if opcode_id == 0x3:  # RSB
            result = (operand2 - left) & 0xFFFFFFFF
            self._write_reg(rd, result)
            if set_flags:
                self._set_sub_flags(operand2, left, 0, result)
            return f"RSB R{rd},R{rn},{operand_text}", 1
        if opcode_id == 0x6:  # SBC
            borrow = 1 - self.flag_c
            result = (left - operand2 - borrow) & 0xFFFFFFFF
            self._write_reg(rd, result)
            if set_flags:
                self._set_sub_flags(left, operand2, borrow, result)
            return f"SBC R{rd},R{rn},{operand_text}", 1
        if opcode_id == 0x7:  # RSC
            borrow = 1 - self.flag_c
            result = (operand2 - left - borrow) & 0xFFFFFFFF
            self._write_reg(rd, result)
            if set_flags:
                self._set_sub_flags(operand2, left, borrow, result)
            return f"RSC R{rd},R{rn},{operand_text}", 1
        raise DecodeError(f"ARM opcode 0x{opcode:08X} is not implemented", pc=current_pc)

    def _execute_load_store(self, opcode: int, current_pc: int) -> tuple[str, int]:
        if ((opcode >> 26) & 0x3) != 0b01 or ((opcode >> 22) & 0x1) != 0:
            raise DecodeError("ARM load/store form outside the supported subset is not implemented", pc=current_pc)
        pre_index = (opcode >> 24) & 0x1
        up = (opcode >> 23) & 0x1
        write_back = (opcode >> 21) & 0x1
        load = (opcode >> 20) & 0x1
        rn = (opcode >> 16) & 0xF
        rd = (opcode >> 12) & 0xF
        base = self._read_reg(rn, current_pc=current_pc)
        offset, offset_text = self._decode_memory_offset(opcode, current_pc)
        adjusted = (base + offset) & 0xFFFFFFFF if up else (base - offset) & 0xFFFFFFFF
        address = adjusted if pre_index else base
        if load:
            value = self.memory.read32(address, space="xram", endian=self.endian)
            self._record_gpio_read(address, value)
            self._write_reg(rd, value)
        else:
            store_value = self._read_reg(rd, current_pc=current_pc)
            mmio_offset = self._mmio_offset_for_address(address)
            if mmio_offset == _ARM_GPIO_IRQ_PENDING:
                pending = self._read_mmio32(_ARM_GPIO_IRQ_PENDING)
                self._write_mmio32(_ARM_GPIO_IRQ_PENDING, pending & ~store_value)
            elif mmio_offset == _ARM_TIMER_PENDING:
                pending = self._read_mmio32(_ARM_TIMER_PENDING)
                self._write_mmio32(_ARM_TIMER_PENDING, pending & ~store_value)
            else:
                self.memory.write32(address, store_value, space="xram", endian=self.endian)
                if mmio_offset in {_ARM_GPIO_OUT, _ARM_GPIO_DIR}:
                    dir_value = self._read_mmio32(_ARM_GPIO_DIR)
                    out_value = self._read_mmio32(_ARM_GPIO_OUT)
                    input_value = ((self._gpio_input_value & self._gpio_input_mask) & ~dir_value) | (out_value & dir_value)
                    self._write_mmio32(_ARM_GPIO_IN, input_value)
                elif mmio_offset == _ARM_TIMER_LOAD and self._read_mmio32(_ARM_TIMER_VALUE) == 0:
                    self._write_mmio32(_ARM_TIMER_VALUE, store_value)
        if pre_index and write_back:
            self._write_reg(rn, adjusted)
        elif not pre_index:
            self._write_reg(rn, adjusted)
        suffix = f"[R{rn}]"
        if offset:
            suffix = f"[R{rn}, {'#' if offset_text.startswith('#') else ''}{offset_text.lstrip('#')}]"
        if pre_index and write_back:
            suffix += "!"
        elif not pre_index and offset:
            suffix = f"[R{rn}], {offset_text}"
        return f"{'LDR' if load else 'STR'} R{rd},{suffix}", 3

    def _is_multiply_long_opcode(self, opcode: int) -> bool:
        return ((opcode >> 23) & 0x1F) == 0x01 and ((opcode >> 4) & 0xF) == 0x9

    def _is_multiply_opcode(self, opcode: int) -> bool:
        return (opcode & 0x0FC000F0) == 0x00000090

    def _execute_multiply(self, opcode: int, current_pc: int) -> tuple[str, int]:
        accumulate = (opcode >> 21) & 0x1
        set_flags = (opcode >> 20) & 0x1
        rd = (opcode >> 16) & 0xF
        rn = (opcode >> 12) & 0xF
        rs = (opcode >> 8) & 0xF
        rm = opcode & 0xF
        left = self._read_reg(rm, current_pc=current_pc) & 0xFFFFFFFF
        right = self._read_reg(rs, current_pc=current_pc) & 0xFFFFFFFF
        result = (left * right) & 0xFFFFFFFF
        if accumulate:
            result = (result + (self._read_reg(rn, current_pc=current_pc) & 0xFFFFFFFF)) & 0xFFFFFFFF
        self._write_reg(rd, result)
        if set_flags:
            self.flag_n = 1 if result & 0x80000000 else 0
            self.flag_z = 1 if result == 0 else 0
        mnemonic = "MLA" if accumulate else "MUL"
        if accumulate:
            return f"{mnemonic} R{rd},R{rm},R{rs},R{rn}", 2
        return f"{mnemonic} R{rd},R{rm},R{rs}", 2

    def _signed32(self, value: int) -> int:
        value &= 0xFFFFFFFF
        return value - 0x100000000 if value & 0x80000000 else value

    def _execute_multiply_long(self, opcode: int, current_pc: int) -> tuple[str, int]:
        signed = (opcode >> 22) & 0x1
        accumulate = (opcode >> 21) & 0x1
        set_flags = (opcode >> 20) & 0x1
        rd_hi = (opcode >> 16) & 0xF
        rd_lo = (opcode >> 12) & 0xF
        rs = (opcode >> 8) & 0xF
        rm = opcode & 0xF
        left_raw = self._read_reg(rm, current_pc=current_pc) & 0xFFFFFFFF
        right_raw = self._read_reg(rs, current_pc=current_pc) & 0xFFFFFFFF
        if signed:
            product = (self._signed32(left_raw) * self._signed32(right_raw)) & 0xFFFFFFFFFFFFFFFF
        else:
            product = (left_raw * right_raw) & 0xFFFFFFFFFFFFFFFF
        accumulator = 0
        if accumulate:
            accumulator = ((self._read_reg(rd_hi, current_pc=current_pc) & 0xFFFFFFFF) << 32) | (
                self._read_reg(rd_lo, current_pc=current_pc) & 0xFFFFFFFF
            )
        result = (product + accumulator) & 0xFFFFFFFFFFFFFFFF
        self._write_reg(rd_lo, result & 0xFFFFFFFF)
        self._write_reg(rd_hi, (result >> 32) & 0xFFFFFFFF)
        if set_flags:
            self.flag_n = 1 if (result >> 63) & 0x1 else 0
            self.flag_z = 1 if result == 0 else 0
        if signed:
            mnemonic = "SMLAL" if accumulate else "SMULL"
        else:
            mnemonic = "UMLAL" if accumulate else "UMULL"
        return f"{mnemonic} R{rd_lo},R{rd_hi},R{rm},R{rs}", 2

    def _execute_branch(self, opcode: int, current_pc: int) -> tuple[str, int]:
        link = (opcode >> 24) & 0x1
        imm24 = opcode & 0x00FFFFFF
        if imm24 & 0x00800000:
            imm24 -= 0x01000000
        target = (current_pc + 8 + ((imm24 << 2) & 0xFFFFFFFF)) & 0xFFFFFFFF
        if link:
            return_address = (current_pc + 4) & 0xFFFFFFFF
            self._write_reg(14, return_address)
            self.debugger.call_stack.append(return_address)
        self._set_pc(target)
        return f"{'BL' if link else 'B'} 0x{target:08X}", 3

    def _execute_bx(self, opcode: int, current_pc: int) -> tuple[str, int]:
        rm = opcode & 0xF
        target = self._read_reg(rm, current_pc=current_pc) & 0xFFFFFFFE
        if rm == 14 and self._arm_interrupt_stack and target == (self._arm_interrupt_stack[-1] & 0xFFFFFFFE):
            self._arm_interrupt_stack.pop()
        if rm == 14 and self.debugger.call_stack:
            self.debugger.call_stack.pop()
        self._set_pc(target)
        return f"BX R{rm}", 3

    def _tight_loop_fast_path_allowed(self) -> bool:
        return self.compact_execution_allowed() and not self.debugger.breakpoints and not self._peripherals_active()

    def _branch_target(self, opcode: int, current_pc: int) -> int:
        imm24 = opcode & 0x00FFFFFF
        if imm24 & 0x00800000:
            imm24 -= 0x01000000
        return (current_pc + 8 + ((imm24 << 2) & 0xFFFFFFFF)) & 0xFFFFFFFF

    def _try_fast_branch_self(self, *, max_steps: int, max_cycles: int) -> dict | None:
        current_pc = self.pc
        opcode = self.memory.read32(current_pc, space="code", endian=self.endian)
        cond = (opcode >> 28) & 0xF
        if cond != 0xE or ((opcode >> 25) & 0x7) != 0b101 or ((opcode >> 24) & 0x1):
            return None
        if self._branch_target(opcode, current_pc) != current_pc:
            return None
        steps = min(int(max_steps), int(max_cycles) // 3)
        if steps <= 0:
            return None
        self.cycles += steps * 3
        return {"steps": steps, "cycles": steps * 3}

    def _try_fast_subs_bne_loop(self, *, max_steps: int, max_cycles: int) -> dict | None:
        current_pc = self.pc
        branch = self.memory.read32(current_pc, space="code", endian=self.endian)
        if ((branch >> 25) & 0x7) != 0b101 or ((branch >> 24) & 0x1):
            return None
        if ((branch >> 28) & 0xF) != 0x1 or self.flag_z:
            return None
        target = self._branch_target(branch, current_pc)
        if target != ((current_pc - 4) & 0xFFFFFFFF):
            return None
        alu = self.memory.read32(target, space="code", endian=self.endian)
        if ((alu >> 26) & 0x3) != 0b00 or ((alu >> 25) & 0x1) != 1:
            return None
        if ((alu >> 21) & 0xF) != 0x2 or ((alu >> 20) & 0x1) != 1:
            return None
        rn = (alu >> 16) & 0xF
        rd = (alu >> 12) & 0xF
        if rn != rd:
            return None
        rotate = (alu >> 8) & 0xF
        imm8 = alu & 0xFF
        if rotate != 0 or imm8 != 1:
            return None
        remaining = self.registers[rd] & 0xFFFFFFFF
        if remaining <= 0:
            return None
        steps_needed = (2 * remaining) + 1
        cycles_needed = (4 * remaining) + 1
        if steps_needed > int(max_steps) or cycles_needed > int(max_cycles):
            return None
        self._write_reg(rd, 0)
        self._set_sub_flags(1, 1, 0, 0)
        self.cycles += cycles_needed
        self._set_pc((current_pc + 4) & 0xFFFFFFFF)
        return {"steps": steps_needed, "cycles": cycles_needed}

    def try_fast_realtime_slice(self, *, max_steps: int, max_cycles: int) -> dict | None:
        if not self._tight_loop_fast_path_allowed() or max_steps <= 0 or max_cycles <= 0 or self.halted:
            return None
        return self._try_fast_subs_bne_loop(max_steps=max_steps, max_cycles=max_cycles) or self._try_fast_branch_self(
            max_steps=max_steps,
            max_cycles=max_cycles,
        )

    def _step_impl(self) -> TraceEntry:
        if self.halted:
            raise ExecutionError("CPU halted", pc=self.pc)
        self.last_interrupt = self._maybe_take_interrupt()
        current_pc = self.pc
        registers_before = self._debug_registers()
        opcode = self.memory.read32(current_pc, space="code", endian=self.endian)
        self._set_pc((current_pc + 4) & 0xFFFFFFFF)
        mnemonic = f".word 0x{opcode:08X}"
        cycles = 1
        try:
            cond = (opcode >> 28) & 0xF
            if not self._condition_passed(cond):
                mnemonic = f"SKIP.{COND_NAMES.get(cond, '??')}"
            elif (opcode & 0x0FFFFFF0) == 0x012FFF10:
                mnemonic, cycles = self._execute_bx(opcode, current_pc)
            elif self._is_multiply_long_opcode(opcode):
                mnemonic, cycles = self._execute_multiply_long(opcode, current_pc)
            elif self._is_multiply_opcode(opcode):
                mnemonic, cycles = self._execute_multiply(opcode, current_pc)
            else:
                category = (opcode >> 25) & 0x7
                if category == 0b101:
                    mnemonic, cycles = self._execute_branch(opcode, current_pc)
                elif ((opcode >> 26) & 0x3) == 0b01:
                    mnemonic, cycles = self._execute_load_store(opcode, current_pc)
                elif ((opcode >> 26) & 0x3) == 0b00:
                    mnemonic, cycles = self._execute_data_processing(opcode, current_pc)
                else:
                    raise DecodeError(f"Unsupported ARM opcode 0x{opcode:08X}", pc=current_pc)
        except Exception as exc:
            self.halted = True
            self.last_error = str(exc)
            if isinstance(exc, ExecutionError):
                raise
            raise ExecutionError(str(exc), pc=current_pc) from exc

        self.cycles += cycles
        self._tick_peripherals(cycles)
        changes = self.memory.consume_changes()
        registers_after = self._debug_registers()
        line = self.program.address_to_line.get(current_pc) if self.program else None
        text = self._listing_text_by_address.get(current_pc) if self.program else None
        trace = TraceEntry(
            pc=current_pc,
            opcode=opcode,
            mnemonic=mnemonic,
            bytes_=list(int(opcode & 0xFFFFFFFF).to_bytes(4, self.endian, signed=False)),
            cycles=cycles,
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
        current_pc = self.pc
        opcode = self.memory.read32(current_pc, space="code", endian=self.endian)
        self._set_pc((current_pc + 4) & 0xFFFFFFFF)
        mnemonic = f".word 0x{opcode:08X}"
        cycles = 1
        try:
            cond = (opcode >> 28) & 0xF
            if not self._condition_passed(cond):
                mnemonic = f"SKIP.{COND_NAMES.get(cond, '??')}"
            elif (opcode & 0x0FFFFFF0) == 0x012FFF10:
                mnemonic, cycles = self._execute_bx(opcode, current_pc)
            elif self._is_multiply_long_opcode(opcode):
                mnemonic, cycles = self._execute_multiply_long(opcode, current_pc)
            elif self._is_multiply_opcode(opcode):
                mnemonic, cycles = self._execute_multiply(opcode, current_pc)
            else:
                category = (opcode >> 25) & 0x7
                if category == 0b101:
                    mnemonic, cycles = self._execute_branch(opcode, current_pc)
                elif ((opcode >> 26) & 0x3) == 0b01:
                    mnemonic, cycles = self._execute_load_store(opcode, current_pc)
                elif ((opcode >> 26) & 0x3) == 0b00:
                    mnemonic, cycles = self._execute_data_processing(opcode, current_pc)
                else:
                    raise DecodeError(f"Unsupported ARM opcode 0x{opcode:08X}", pc=current_pc)
        except Exception as exc:
            self.halted = True
            self.last_error = str(exc)
            if isinstance(exc, ExecutionError):
                raise
            raise ExecutionError(str(exc), pc=current_pc) from exc

        self.cycles += cycles
        self._tick_peripherals(cycles)
        changes = self.memory.consume_changes()
        if self.program and self.pc >= (self.program.origin + len(self.program.binary)):
            self.halted = True
        return TraceEntry(
            pc=current_pc,
            opcode=opcode,
            mnemonic=mnemonic,
            bytes_=list(int(opcode & 0xFFFFFFFF).to_bytes(4, self.endian, signed=False)),
            cycles=cycles,
            line=self.program.address_to_line.get(current_pc) if self.program else None,
            text=self._listing_text_by_address.get(current_pc) if self.program else None,
            changes=changes,
            register_diff={},
            interrupt=self.last_interrupt,
        )

    def step_compact_payload(self) -> dict:
        if self.halted:
            raise ExecutionError("CPU halted", pc=self.pc)
        self.last_interrupt = self._maybe_take_interrupt()
        current_pc = self.pc
        opcode = self.memory.read32(current_pc, space="code", endian=self.endian)
        self._set_pc((current_pc + 4) & 0xFFFFFFFF)
        mnemonic = f".word 0x{opcode:08X}"
        cycles = 1
        try:
            cond = (opcode >> 28) & 0xF
            if not self._condition_passed(cond):
                mnemonic = f"SKIP.{COND_NAMES.get(cond, '??')}"
            elif (opcode & 0x0FFFFFF0) == 0x012FFF10:
                mnemonic, cycles = self._execute_bx(opcode, current_pc)
            elif self._is_multiply_long_opcode(opcode):
                mnemonic, cycles = self._execute_multiply_long(opcode, current_pc)
            elif self._is_multiply_opcode(opcode):
                mnemonic, cycles = self._execute_multiply(opcode, current_pc)
            else:
                category = (opcode >> 25) & 0x7
                if category == 0b101:
                    mnemonic, cycles = self._execute_branch(opcode, current_pc)
                elif ((opcode >> 26) & 0x3) == 0b01:
                    mnemonic, cycles = self._execute_load_store(opcode, current_pc)
                elif ((opcode >> 26) & 0x3) == 0b00:
                    mnemonic, cycles = self._execute_data_processing(opcode, current_pc)
                else:
                    raise DecodeError(f"Unsupported ARM opcode 0x{opcode:08X}", pc=current_pc)
        except Exception as exc:
            self.halted = True
            self.last_error = str(exc)
            if isinstance(exc, ExecutionError):
                raise
            raise ExecutionError(str(exc), pc=current_pc) from exc

        self.cycles += cycles
        self._tick_peripherals(cycles)
        changes = self.memory.consume_changes()
        if self.program and self.pc >= (self.program.origin + len(self.program.binary)):
            self.halted = True
        return {
            "pc": current_pc,
            "opcode": opcode,
            "mnemonic": mnemonic,
            "bytes": list(int(opcode & 0xFFFFFFFF).to_bytes(4, self.endian, signed=False)),
            "cycles": cycles,
            "line": self.program.address_to_line.get(current_pc) if self.program else None,
            "text": self._listing_text_by_address.get(current_pc) if self.program else None,
            "changes": changes,
            "interrupt": self.last_interrupt,
        }

    def snapshot(self) -> dict:
        return {
            "registers": self._debug_registers(),
            "flags": self._flags_dict(),
            "cycles": self.cycles,
            "clock_hz": self.clock_hz,
            "effective_clock_hz": self.effective_clock_hz(),
            "execution_mode": self.execution_mode,
            "halted": self.halted,
            "last_error": self.last_error,
            "last_interrupt": self.last_interrupt,
            "endian": self.endian,
            "iram": {},
            "sfr": {},
            "rom": self.memory.dump_rom(self.program.origin + len(self.program.binary) if self.program else 0x100),
            "xram_sample": {idx: self.memory.read_xram(idx) for idx in range(0x40)},
            "gpio_regs": self._gpio_regs(),
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
            "serial": {"tx": [], "rx_pending": []},
            "timers": {
                "sys": {
                    "load": self._read_mmio32(_ARM_TIMER_LOAD),
                    "value": self._read_mmio32(_ARM_TIMER_VALUE),
                    "ctrl": self._read_mmio32(_ARM_TIMER_CTRL),
                    "pending": self._read_mmio32(_ARM_TIMER_PENDING),
                }
            },
            "io_reads": [dict(item) for item in self.io_reads],
            "debug_mode": self.debug_mode,
        }

    def hardware_snapshot(self) -> dict:
        return {
            "cycles": self.cycles,
            "clock_hz": self.clock_hz,
            "effective_clock_hz": self.effective_clock_hz(),
            "execution_mode": self.execution_mode,
            "halted": self.halted,
            "last_error": self.last_error,
            "last_interrupt": self.last_interrupt,
            "endian": self.endian,
            "xram_sample": {idx: self.memory.read_xram(idx) for idx in range(0x40)},
            "gpio_regs": self._gpio_regs(),
            "io_reads": [dict(item) for item in self.io_reads],
            "debug_mode": self.debug_mode,
        }

    def hardware_tick_snapshot(self) -> dict:
        gpio_regs = self._gpio_regs()
        io_reads_delta = [dict(item) for item in self._pending_io_reads]
        self._pending_io_reads.clear()
        signature = (
            int(gpio_regs.get("out", 0)),
            int(gpio_regs.get("in", 0)),
            int(gpio_regs.get("dir", 0)),
            int(gpio_regs.get("irq_pending", 0)),
            int(gpio_regs.get("timer_pending", 0)),
        )
        if signature == self._last_hardware_tick_signature and not io_reads_delta:
            return {}
        self._last_hardware_tick_signature = signature
        return {
            "cycles": self.cycles,
            "clock_hz": self.clock_hz,
            "effective_clock_hz": self.effective_clock_hz(),
            "execution_mode": self.execution_mode,
            "halted": self.halted,
            "last_error": self.last_error,
            "last_interrupt": self.last_interrupt,
            "endian": self.endian,
            "gpio_regs": gpio_regs,
            "io_reads_delta": io_reads_delta,
            "debug_mode": self.debug_mode,
        }

    def runtime_snapshot(self) -> dict:
        return {
            "registers": self._debug_registers(),
            "flags": self._flags_dict(),
            "cycles": self.cycles,
            "clock_hz": self.clock_hz,
            "effective_clock_hz": self.effective_clock_hz(),
            "execution_mode": self.execution_mode,
            "halted": self.halted,
            "last_error": self.last_error,
            "last_interrupt": self.last_interrupt,
            "endian": self.endian,
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
            "endian": self.endian,
            "debug_mode": self.debug_mode,
            "registers": self.registers[:],
            "flags": self._flags_dict(),
            "memory": self.memory.export_state(),
            "io_reads": [dict(item) for item in self.io_reads],
            "pending_io_reads": [dict(item) for item in self._pending_io_reads],
            "arm_state": {
                "gpio_input_mask": self._gpio_input_mask,
                "gpio_input_value": self._gpio_input_value,
                "previous_gpio_inputs": self._previous_gpio_inputs,
                "interrupt_stack": self._arm_interrupt_stack[:],
                "last_hardware_tick_signature": list(self._last_hardware_tick_signature or []),
            },
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
        self.pc = int(state.get("pc", 0)) & 0xFFFFFFFF
        self.cycles = int(state.get("cycles", 0))
        self.halted = bool(state.get("halted", True))
        self.last_error = state.get("last_error")
        self.last_interrupt = state.get("last_interrupt")
        self.clock_hz = int(state.get("clock_hz", self.clock_hz))
        self.speed_multiplier = float(state.get("speed_multiplier", self.speed_multiplier))
        self.execution_mode = str(state.get("execution_mode", self.execution_mode))
        self.endian = str(state.get("endian", self.endian))
        self.debug_mode = bool(state.get("debug_mode", False))
        self.registers = [int(value) & 0xFFFFFFFF for value in state.get("registers", [0] * 16)]
        if len(self.registers) < 16:
            self.registers.extend([0] * (16 - len(self.registers)))
        flags = dict(state.get("flags", {}))
        self.flag_n = int(flags.get("N", 0)) & 1
        self.flag_z = int(flags.get("Z", 0)) & 1
        self.flag_c = int(flags.get("C", 0)) & 1
        self.flag_v = int(flags.get("V", 0)) & 1
        self._set_pc(self.pc)
        self.memory.import_state(dict(state.get("memory", {})))
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
        arm_state = dict(state.get("arm_state", {}))
        self._gpio_input_mask = int(arm_state.get("gpio_input_mask", 0)) & 0xFFFF
        self._gpio_input_value = int(arm_state.get("gpio_input_value", 0)) & 0xFFFF
        self._previous_gpio_inputs = int(arm_state.get("previous_gpio_inputs", 0)) & 0xFFFF
        self._arm_interrupt_stack = [int(value) & 0xFFFFFFFF for value in arm_state.get("interrupt_stack", [])]
        signature = arm_state.get("last_hardware_tick_signature", [])
        self._last_hardware_tick_signature = tuple(int(value) for value in signature) if signature else None
        debugger = dict(state.get("debugger", {}))
        self.set_breakpoints(list(debugger.get("breakpoints", [])))
        self.debugger.call_stack = [int(item) for item in debugger.get("call_stack", [])]
        self.set_watchpoints(
            [
                Watchpoint(
                    target=item.get("target", item.get("address")),
                    space=item.get("space", "xram"),
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
        aliases = {"SP": 13, "LR": 14, "PC": 15}
        for name, value in values.items():
            key = name.upper()
            if key.startswith("R") and key[1:].isdigit():
                index = int(key[1:])
            else:
                index = aliases.get(key)
            if index is None:
                continue
            self._write_reg(index, value & 0xFFFFFFFF)

    def _capture_extra_state(self) -> dict:
        return {
            "flags": self._flags_dict(),
            "pending_io_reads": [dict(item) for item in self._pending_io_reads],
            "arm_state": {
                "gpio_input_mask": self._gpio_input_mask,
                "gpio_input_value": self._gpio_input_value,
                "previous_gpio_inputs": self._previous_gpio_inputs,
                "interrupt_stack": self._arm_interrupt_stack[:],
                "last_hardware_tick_signature": list(self._last_hardware_tick_signature or []),
            },
        }

    def _restore_extra_state(self, state: dict) -> None:
        flags = dict(state.get("flags", {}))
        self.flag_n = int(flags.get("N", self.flag_n)) & 1
        self.flag_z = int(flags.get("Z", self.flag_z)) & 1
        self.flag_c = int(flags.get("C", self.flag_c)) & 1
        self.flag_v = int(flags.get("V", self.flag_v)) & 1
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
        arm_state = dict(state.get("arm_state", {}))
        self._gpio_input_mask = int(arm_state.get("gpio_input_mask", self._gpio_input_mask)) & 0xFFFF
        self._gpio_input_value = int(arm_state.get("gpio_input_value", self._gpio_input_value)) & 0xFFFF
        self._previous_gpio_inputs = int(arm_state.get("previous_gpio_inputs", self._previous_gpio_inputs)) & 0xFFFF
        self._arm_interrupt_stack = [int(value) & 0xFFFFFFFF for value in arm_state.get("interrupt_stack", self._arm_interrupt_stack)]
        signature = arm_state.get("last_hardware_tick_signature", [])
        self._last_hardware_tick_signature = tuple(int(value) for value in signature) if signature else None
