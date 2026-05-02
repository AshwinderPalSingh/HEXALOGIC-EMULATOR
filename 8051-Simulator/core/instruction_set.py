from core.util import construct_hex, decompose_byte, ishex, tohex


class Instructions:
    def __init__(self, op) -> None:
        self.op = op
        self._jump_flag = False
        self._jump_instructions = op._jump_instructions
        self._base = 16
        self.flags = self.op.super_memory.PSW
        # Set by Controller at runtime.
        self.controller = None

    def _is_jump_opcode(self, opcode) -> bool:
        opcode = opcode.upper()
        if opcode not in self._jump_instructions:
            return False
        return True

    def _next_addr(self, addr) -> str:
        return format(int(str(addr), 16) + 1, "#06x")

    def _to_int(self, value) -> int:
        if value is None:
            return 0
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value

        value = str(value).strip()
        if not value:
            return 0
        if ishex(value):
            return int(tohex(value), 16)
        return int(value)

    def _to_hex8(self, value: int) -> str:
        return format(value & 0xFF, "#04x")

    def _to_hex16(self, value: int) -> str:
        return format(value & 0xFFFF, "#06x")

    def _update_parity(self, value: int) -> None:
        # 8051 parity flag is 1 for odd parity in accumulator.
        self.flags.P = bool(bin(value & 0xFF).count("1") % 2)

    def _update_accumulator_flags(self, value: int) -> None:
        self._update_parity(value)

    def _resolve_addressing_mode(self, addr, data=None) -> tuple:
        addr = str(addr)
        if addr.startswith("@"):
            addr = str(self.op.memory_read(addr[1:]))

        if data is None:
            return addr, data

        data = str(data)
        if data.startswith("@"):
            indirect_addr = str(self.op.memory_read(data[1:]))
            data = self.op.memory_read(indirect_addr)
        elif data.startswith("#"):
            raw = data[1:]
            data = tohex(raw) if ishex(raw) else raw
        else:
            data = self.op.memory_read(data)
        return addr, data

    def _read_operand_value(self, operand) -> int:
        operand = str(operand)
        if operand.startswith("#"):
            raw = operand[1:]
            return self._to_int(tohex(raw) if ishex(raw) else raw)
        if operand.startswith("@"):
            indirect_addr = str(self.op.memory_read(operand[1:]))
            return self._to_int(self.op.memory_read(indirect_addr))
        return self._to_int(self.op.memory_read(operand))

    def _jump(self, label, **kwargs) -> bool:
        bounce_to_label = kwargs.get("bounce_to_label")
        if not bounce_to_label:
            return False
        return bounce_to_label(label)

    def _push_return_index(self, idx: int) -> None:
        data_h, data_l = decompose_byte(self._to_hex16(idx))
        # 8051 pushes low byte first, then high byte.
        self.op.super_memory.SP.write(data_l)
        self.op.super_memory.SP.write(data_h)

    def _pop_return_index(self):
        sp = self._to_int(self.op.super_memory.SP)
        if sp <= 0x07:
            return None
        high = self.op.super_memory.SP.read()
        low = self.op.super_memory.SP.read()
        return self._to_int(construct_hex(high, low))

    def _write_and_update_a(self, addr: str, value: int) -> bool:
        ret = self.op.memory_write(addr, self._to_hex8(value))
        if str(addr).upper() in {"A", "ACC"}:
            self._update_accumulator_flags(value)
        return ret

    def nop(self, *args, **kwargs) -> bool:
        return True

    def mov(self, addr, data) -> bool:
        addr_u = str(addr).upper()
        data_u = str(data).upper()
        if addr_u == "C":
            return self.op.bit_write("C", bool(self.op.bit_read(data)))
        if data_u == "C":
            return self.op.bit_write(addr, bool(self.op.bit_read("C")))

        addr, data = self._resolve_addressing_mode(addr, data)
        ret = self.op.memory_write(addr, data)
        if str(addr).upper() in {"A", "ACC"}:
            self._update_accumulator_flags(self._to_int(self.op.memory_read(addr)))
        return ret

    def add(self, addr, data) -> bool:
        addr, _ = self._resolve_addressing_mode(addr, data)
        data_1 = self._to_int(self.op.memory_read(addr))
        data_2 = self._read_operand_value(data)
        result_full = data_1 + data_2
        result = result_full & 0xFF

        self.flags.CY = result_full > 0xFF
        self.flags.AC = ((data_1 & 0x0F) + (data_2 & 0x0F)) > 0x0F
        self.flags.OV = bool((~(data_1 ^ data_2) & (data_1 ^ result) & 0x80))

        return self._write_and_update_a(addr, result)

    def addc(self, addr, data) -> bool:
        addr, _ = self._resolve_addressing_mode(addr, data)
        data_1 = self._to_int(self.op.memory_read(addr))
        data_2 = self._read_operand_value(data)
        carry = int(bool(self.flags.CY))
        result_full = data_1 + data_2 + carry
        result = result_full & 0xFF

        self.flags.CY = result_full > 0xFF
        self.flags.AC = ((data_1 & 0x0F) + (data_2 & 0x0F) + carry) > 0x0F
        self.flags.OV = bool((~(data_1 ^ data_2) & (data_1 ^ result) & 0x80))

        return self._write_and_update_a(addr, result)

    def subb(self, addr, data) -> bool:
        addr, _ = self._resolve_addressing_mode(addr, data)
        data_1 = self._to_int(self.op.memory_read(addr))
        data_2 = self._read_operand_value(data)
        borrow = int(bool(self.flags.CY))
        result_full = data_1 - data_2 - borrow
        result = result_full & 0xFF

        self.flags.CY = result_full < 0
        self.flags.AC = (data_1 & 0x0F) < ((data_2 & 0x0F) + borrow)
        self.flags.OV = bool(((data_1 ^ data_2) & (data_1 ^ result) & 0x80))

        return self._write_and_update_a(addr, result)

    def anl(self, addr_1, addr_2) -> bool:
        if str(addr_1).upper() == "C":
            bit_expr = str(addr_2).strip()
            invert = bit_expr.startswith("/")
            if invert:
                bit_expr = bit_expr[1:]
            value = self.op.bit_read(bit_expr)
            if invert:
                value = not value
            return self.op.bit_write("C", bool(self.op.bit_read("C") and value))

        addr_1, _ = self._resolve_addressing_mode(addr_1)
        data_1 = self._to_int(self.op.memory_read(addr_1))
        data_2 = self._read_operand_value(addr_2)
        result = data_1 & data_2
        return self._write_and_update_a(addr_1, result)

    def orl(self, addr_1, addr_2) -> bool:
        if str(addr_1).upper() == "C":
            bit_expr = str(addr_2).strip()
            invert = bit_expr.startswith("/")
            if invert:
                bit_expr = bit_expr[1:]
            value = self.op.bit_read(bit_expr)
            if invert:
                value = not value
            return self.op.bit_write("C", bool(self.op.bit_read("C") or value))

        addr_1, _ = self._resolve_addressing_mode(addr_1)
        data_1 = self._to_int(self.op.memory_read(addr_1))
        data_2 = self._read_operand_value(addr_2)
        result = data_1 | data_2
        return self._write_and_update_a(addr_1, result)

    def xrl(self, addr_1, addr_2) -> bool:
        addr_1, _ = self._resolve_addressing_mode(addr_1)
        data_1 = self._to_int(self.op.memory_read(addr_1))
        data_2 = self._read_operand_value(addr_2)
        result = data_1 ^ data_2
        return self._write_and_update_a(addr_1, result)

    def inc(self, addr) -> bool:
        if str(addr).upper() == "DPTR":
            data = self._to_int(self.op.register_pair_read("DPTR"))
            return self.op.register_pair_write("DPTR", self._to_hex16(data + 1))

        addr, _ = self._resolve_addressing_mode(addr)
        data = self._to_int(self.op.memory_read(addr))
        return self._write_and_update_a(addr, data + 1)

    def dec(self, addr) -> bool:
        addr, _ = self._resolve_addressing_mode(addr)
        data = self._to_int(self.op.memory_read(addr))
        return self._write_and_update_a(addr, data - 1)

    def rl(self, addr) -> bool:
        addr, _ = self._resolve_addressing_mode(addr)
        data = self._to_int(self.op.memory_read(addr))
        result = ((data << 1) | (data >> 7)) & 0xFF
        return self._write_and_update_a(addr, result)

    def rr(self, addr) -> bool:
        addr, _ = self._resolve_addressing_mode(addr)
        data = self._to_int(self.op.memory_read(addr))
        result = ((data >> 1) | ((data & 0x01) << 7)) & 0xFF
        return self._write_and_update_a(addr, result)

    def rlc(self, addr) -> bool:
        addr, _ = self._resolve_addressing_mode(addr)
        data = self._to_int(self.op.memory_read(addr))
        carry = int(bool(self.flags.CY))
        new_carry = bool(data & 0x80)
        result = ((data << 1) | carry) & 0xFF
        self.flags.CY = new_carry
        return self._write_and_update_a(addr, result)

    def rrc(self, addr) -> bool:
        addr, _ = self._resolve_addressing_mode(addr)
        data = self._to_int(self.op.memory_read(addr))
        carry = int(bool(self.flags.CY))
        new_carry = bool(data & 0x01)
        result = ((carry << 7) | (data >> 1)) & 0xFF
        self.flags.CY = new_carry
        return self._write_and_update_a(addr, result)

    def swap(self, addr) -> bool:
        addr, _ = self._resolve_addressing_mode(addr)
        data = self._to_int(self.op.memory_read(addr))
        result = ((data & 0x0F) << 4) | ((data & 0xF0) >> 4)
        return self._write_and_update_a(addr, result)

    def da(self, addr: str) -> bool:
        addr, _ = self._resolve_addressing_mode(addr)
        data = self._to_int(self.op.memory_read(addr))

        adjust = 0
        carry_out = bool(self.flags.CY)

        if (data & 0x0F) > 0x09 or self.flags.AC:
            adjust |= 0x06
        if data > 0x99 or self.flags.CY:
            adjust |= 0x60
            carry_out = True

        result = data + adjust
        if result > 0xFF:
            carry_out = True

        self.flags.CY = carry_out
        return self._write_and_update_a(addr, result)

    def org(self, addr) -> bool:
        """Database directive origin"""
        return self.op.super_memory.PC(addr)

    def setb(self, bit: str) -> bool:
        """Set a bit to true"""
        return self.op.bit_write(bit, True)

    def clr(self, bit: str) -> bool:
        """Clears a bit or register."""
        bit_u = str(bit).upper()
        if bit_u in {"A", "ACC"}:
            return self._write_and_update_a("A", 0x00)
        return self.op.bit_write(bit, False)

    def cpl(self, bit: str) -> bool:
        """Complements a bit or accumulator."""
        bit_u = str(bit).upper()
        if bit_u in {"A", "ACC"}:
            data = self._to_int(self.op.memory_read("A"))
            return self._write_and_update_a("A", (~data) & 0xFF)

        _data = self.op.bit_read(bit)
        return self.op.bit_write(bit, not _data)

    def push(self, addr: str) -> bool:
        """Pushes the content of the memory location to the stack."""
        data = self.op.memory_read(addr)
        return self.op.super_memory.SP.write(data)

    def pop(self, addr: str) -> bool:
        """Pop the stack as the content of the memory location."""
        data = self.op.super_memory.SP.read()
        ret = self.op.memory_write(addr, data)
        if str(addr).upper() in {"A", "ACC"}:
            self._update_accumulator_flags(self._to_int(self.op.memory_read(addr)))
        return ret

    def jz(self, label, *args, **kwargs) -> bool:
        """Jump if accumulator is zero"""
        if self._to_int(self.op.memory_read("A")) == 0:
            return self._jump(label, **kwargs)
        return True

    def jnz(self, label, *args, **kwargs) -> bool:
        """Jump if accumulator is not zero"""
        if self._to_int(self.op.memory_read("A")) != 0:
            return self._jump(label, **kwargs)
        return True

    def jc(self, label, *args, **kwargs) -> bool:
        """Jump if carry"""
        if self.flags.CY:
            return self._jump(label, **kwargs)
        return True

    def jnc(self, label, *args, **kwargs) -> bool:
        """Jump if no carry"""
        if not self.flags.CY:
            return self._jump(label, **kwargs)
        return True

    def sjmp(self, label, *args, **kwargs) -> bool:
        return self._jump(label, **kwargs)

    def ajmp(self, label, *args, **kwargs) -> bool:
        return self._jump(label, **kwargs)

    def ljmp(self, label, *args, **kwargs) -> bool:
        return self._jump(label, **kwargs)

    def jmp(self, label="@A+DPTR", *args, **kwargs) -> bool:
        if str(label).upper() == "@A+DPTR":
            if self.controller is None:
                return True
            target = self._to_int(self.op.register_pair_read("DPTR")) + self._to_int(self.op.memory_read("A"))
            return self.controller._jump_to_pc(target)
        return self._jump(label, **kwargs)

    def acall(self, label, *args, **kwargs) -> bool:
        if self.controller is not None:
            self._push_return_index(self.controller._run_idx)
        return self._jump(label, **kwargs)

    def lcall(self, label, *args, **kwargs) -> bool:
        if self.controller is not None:
            self._push_return_index(self.controller._run_idx)
        return self._jump(label, **kwargs)

    def ret(self, *args, **kwargs) -> bool:
        if self.controller is None:
            return True
        return_index = self._pop_return_index()
        if return_index is None:
            return True
        self.controller._run_idx = return_index
        return True

    def reti(self, *args, **kwargs) -> bool:
        return self.ret(*args, **kwargs)

    def djnz(self, addr, label, *args, **kwargs) -> bool:
        """Decrement and jump if operand is not zero."""
        addr, _ = self._resolve_addressing_mode(addr)
        data = self._to_int(self.op.memory_read(addr))
        result = (data - 1) & 0xFF
        self.op.memory_write(addr, self._to_hex8(result))
        if result != 0:
            return self._jump(label, **kwargs)
        return True

    def cjne(self, addr, addr2, label, *args, **kwargs) -> bool:
        """Compare and jump if not equal."""
        data_1 = self._read_operand_value(addr)
        data_2 = self._read_operand_value(addr2)
        self.flags.CY = data_1 < data_2
        if data_1 != data_2:
            return self._jump(label, **kwargs)
        return True

    def jb(self, addr, label, *args, **kwargs) -> bool:
        """Jump if bit is true."""
        if self.op.bit_read(addr):
            return self._jump(label, **kwargs)
        return True

    def jnb(self, addr, label, *args, **kwargs) -> bool:
        """Jump if bit is false."""
        if not self.op.bit_read(addr):
            return self._jump(label, **kwargs)
        return True

    def jbc(self, addr, label, *args, **kwargs) -> bool:
        """Jump if bit is true and clear bit."""
        if self.op.bit_read(addr):
            self.op.bit_write(addr, False)
            return self._jump(label, **kwargs)
        return True

    def movc(self, addr, data) -> bool:
        addr = str(addr)
        data = str(data).upper()
        base = 0
        if data == "@A+DPTR":
            base = self._to_int(self.op.register_pair_read("DPTR"))
        elif data == "@A+PC":
            base = self._to_int(self.op.memory_read("PC"))
        target = self._to_hex16(base + self._to_int(self.op.memory_read("A")))
        value = self.op.memory_read(target, RAM=False)
        ret = self.op.memory_write(addr, value)
        if addr.upper() in {"A", "ACC"}:
            self._update_accumulator_flags(self._to_int(self.op.memory_read(addr)))
        return ret

    def movx(self, addr, data) -> bool:
        def _resolve_external_address(token: str) -> str:
            token = token.upper()
            if token == "@DPTR":
                return str(self.op.register_pair_read("DPTR"))
            if token.startswith("@R"):
                return str(self.op.memory_read(token[1:]))
            return token

        addr_u = str(addr).upper()
        data_u = str(data).upper()

        if addr_u in {"A", "ACC"}:
            external_addr = _resolve_external_address(data_u)
            value = self.op.memory_read(external_addr)
            ret = self.op.memory_write(addr, value)
            self._update_accumulator_flags(self._to_int(self.op.memory_read(addr)))
            return ret

        external_addr = _resolve_external_address(addr_u)
        if data_u in {"A", "ACC"}:
            value = self.op.memory_read("A")
        else:
            value = self._read_operand_value(data)
            value = self._to_hex8(value)
        return self.op.memory_write(external_addr, value)

    def mul(self, addr_1="A", addr_2="B", *args, **kwargs) -> bool:
        if str(addr_1).upper() == "AB":
            addr_1, addr_2 = "A", "B"
        a = self._to_int(self.op.memory_read(addr_1))
        b = self._to_int(self.op.memory_read(addr_2))
        result = a * b
        self.op.memory_write(addr_1, self._to_hex8(result))
        self.op.memory_write(addr_2, self._to_hex8(result >> 8))
        self.flags.CY = False
        self.flags.OV = result > 0xFF
        self._update_accumulator_flags(self._to_int(self.op.memory_read(addr_1)))
        return True

    def div(self, addr_1="A", addr_2="B", *args, **kwargs) -> bool:
        if str(addr_1).upper() == "AB":
            addr_1, addr_2 = "A", "B"
        a = self._to_int(self.op.memory_read(addr_1))
        b = self._to_int(self.op.memory_read(addr_2))
        self.flags.CY = False
        if b == 0:
            self.flags.OV = True
            return True
        quotient = a // b
        remainder = a % b
        self.op.memory_write(addr_1, self._to_hex8(quotient))
        self.op.memory_write(addr_2, self._to_hex8(remainder))
        self.flags.OV = False
        self._update_accumulator_flags(self._to_int(self.op.memory_read(addr_1)))
        return True

    def xch(self, addr_1, addr_2) -> bool:
        addr_1, _ = self._resolve_addressing_mode(addr_1)
        addr_2, _ = self._resolve_addressing_mode(addr_2)
        data_1 = self.op.memory_read(addr_1)
        data_2 = self.op.memory_read(addr_2)
        self.op.memory_write(addr_1, data_2)
        self.op.memory_write(addr_2, data_1)
        if str(addr_1).upper() in {"A", "ACC"} or str(addr_2).upper() in {"A", "ACC"}:
            self._update_accumulator_flags(self._to_int(self.op.memory_read("A")))
        return True

    def xchd(self, addr_1, addr_2) -> bool:
        addr_1, _ = self._resolve_addressing_mode(addr_1)
        addr_2, _ = self._resolve_addressing_mode(addr_2)
        data_1 = self._to_int(self.op.memory_read(addr_1))
        data_2 = self._to_int(self.op.memory_read(addr_2))

        data_1_new = (data_1 & 0xF0) | (data_2 & 0x0F)
        data_2_new = (data_2 & 0xF0) | (data_1 & 0x0F)

        self.op.memory_write(addr_1, self._to_hex8(data_1_new))
        self.op.memory_write(addr_2, self._to_hex8(data_2_new))
        if str(addr_1).upper() in {"A", "ACC"} or str(addr_2).upper() in {"A", "ACC"}:
            self._update_accumulator_flags(self._to_int(self.op.memory_read("A")))
        return True
