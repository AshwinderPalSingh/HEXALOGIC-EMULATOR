from rich.console import Console

from core.exceptions import OPCODENotFound, SyntaxError
from core.memory import Byte, LinkedRegister, SuperMemory
from core.opcodes import opcodes_lookup
from core.util import decompose_byte, ishex, tohex


class Operations:
    def __init__(self) -> None:
        self.console = Console()
        self.super_memory = SuperMemory()
        self.memory_rom = self.super_memory.memory_rom
        self.memory_ram = self.super_memory.memory_ram
        self.flags = self.super_memory.PSW
        self.flags.reset()
        self.super_memory.PC("0x0000")
        self._registers_list = {
            "A": self.super_memory.A,  # Accumulator
            "ACC": self.super_memory.A,  # Accumulator
            "PSW": self.flags._PSW,  # Program Status Word
            "B": self.super_memory.B,  # Register B
            "C": self.super_memory.C,
            "SP": self.super_memory.SP,  # Stack Pointer
            "PC": self.super_memory.PC,  # Program Counter
            "DPL": self.super_memory.DPL,  # Data pointer low
            "DPH": self.super_memory.DPH,  # Data pointer high
            "DPTR": self.super_memory.DPTR,  # Data pointer
            "R0": self.super_memory.R0,
            "R1": self.super_memory.R1,
            "R2": self.super_memory.R2,
            "R3": self.super_memory.R3,
            "R4": self.super_memory.R4,
            "R5": self.super_memory.R5,
            "R6": self.super_memory.R6,
            "R7": self.super_memory.R7,
        }
        self._direct_alias_addresses = {
            "B": "0xF0",
            "PSW": "0xD0",
            "SP": "0x81",
            "DPL": "0x82",
            "DPH": "0x83",
            "P0": "0x80",
            "P1": "0x90",
            "P2": "0xA0",
            "P3": "0xB0",
            "PCON": "0x87",
            "TCON": "0x88",
            "TMOD": "0x89",
            "TL0": "0x8A",
            "TL1": "0x8B",
            "TH0": "0x8C",
            "TH1": "0x8D",
            "SCON": "0x98",
            "SBUF": "0x99",
            "IE": "0xA8",
            "EI": "0xA8",
            "IP": "0xB8",
        }
        for alias, address in self._direct_alias_addresses.items():
            if alias not in self._registers_list:
                self._registers_list[alias] = LinkedRegister(self.memory_ram, address)
        # General purpose registers
        self._register_banks = self.super_memory._general_purpose_registers
        self._lookup_opcodes_dir = {key.upper(): value for key, value in opcodes_lookup.items()}

        self._keywords = []
        self._generate_keywords()
        self._assembler = {}
        self._internal_PC = []

        # Jump instructions
        self._jump_instructions = [
            "SJMP",
            "AJMP",
            "LJMP",
            "JMP",
            "JC",
            "JNC",
            "JB",
            "JNB",
            "JBC",
            "JZ",
            "JNZ",
            "DJNZ",
            "CJNE",
            "ACALL",
            "LCALL",
            "RET",
            "RETI",
        ]
        pass

    def _generate_keywords(self):
        _keywords = [*self._lookup_opcodes_dir.keys(), *self._registers_list.keys()]
        for key in _keywords:
            self._keywords.extend(key.split(" "))
        self._keywords = set(self._keywords)
        return

    def iskeyword(self, arg):
        """
        opcodes + registers
        """
        if arg.upper() in self._keywords:
            return True
        return False

    def inspect(self):
        return self.super_memory.inspect()

    def _parse_addr(self, addr):
        addr = addr.upper()
        return self._registers_list.get(addr, None)

    def _get_register(self, addr):
        addr = addr.upper()
        _register = self._registers_list.get(addr, None)
        if _register:
            return _register
        raise SyntaxError(msg="next link not found; check the instruction")

    def _opcode_fetch(self, opcode, *args, **kwargs) -> None:
        opcode = opcode.upper()
        args = list(args)
        _args_params = []
        _args_hexs = []

        def _is_bit_operand(_arg, _idx):
            _arg = _arg.upper()
            if "." in _arg:
                return True
            if opcode in {"JB", "JNB", "JBC"} and _idx == 0:
                return True
            if opcode in {"SETB", "CLR", "CPL"} and _idx == 0 and _arg != "C":
                return True
            if opcode == "MOV" and len(args) >= 2:
                if _idx == 1 and args[0].upper() == "C":
                    return True
                if _idx == 0 and args[1].upper() == "C":
                    return True
            if opcode in {"ORL", "ANL"} and len(args) >= 2 and _idx == 1 and args[0].upper() == "C":
                return True
            return False

        for idx, x in enumerate(args):
            if not x:
                continue
            x_upper = x.upper()
            x_clean = x_upper[1:] if x_upper.startswith("/") else x_upper

            if x_upper in self._direct_alias_addresses:
                _args_params.append("DIRECT")
                _args_hexs.append([self._direct_alias_addresses[x_upper]])
                continue

            if x_upper.startswith("/") and _is_bit_operand(x_clean, idx):
                _args_params.append("/BIT")
                if ishex(x_clean) and opcode not in self._jump_instructions:
                    _args_hexs.append(decompose_byte(tohex(x_clean)))
                continue

            if self.iskeyword(x_upper) or (x_upper.startswith("@") and self.iskeyword(x_upper[1:])):
                if x_upper.startswith("@"):
                    _args_params.append(x_upper)
                elif x_upper == "B":
                    _args_params.append("DIRECT")
                    _args_hexs.append(["0xF0"])  # memory location for `B`
                else:
                    _args_params.append(x_upper)
                continue

            if x_upper.startswith("#"):
                _args_params.append("#IMMED")
                if ishex(x_upper[1:]):
                    _args_hexs.append(decompose_byte(tohex(x_upper[1:])))
                continue

            if _is_bit_operand(x_upper, idx):
                _args_params.append("BIT")
                if ishex(x_upper) and (
                    opcode not in self._jump_instructions or (opcode in {"JB", "JNB", "JBC"} and idx == 0)
                ):
                    _args_hexs.append(decompose_byte(tohex(x_upper)))
                continue

            _args_params.append("DIRECT")
            if ishex(x_upper):
                should_emit_data = opcode not in self._jump_instructions
                if opcode == "DJNZ" and idx == 0:
                    should_emit_data = True
                if opcode == "CJNE" and idx in {0, 1}:
                    should_emit_data = True
                if should_emit_data:
                    _args_hexs.append(decompose_byte(tohex(x_upper)))

        if opcode in {"AJMP", "ACALL"}:
            _args_params = ["ADDR11"]
        elif opcode in {"LJMP", "LCALL"}:
            _args_params = ["ADDR16"]
        elif opcode == "JMP" and args:
            if args[0].upper() == "@A+DPTR":
                _args_params = ["@A+DPTR"]

        if opcode in {"SJMP", "JC", "JNC", "JZ", "JNZ", "DJNZ", "CJNE", "JB", "JNB", "JBC", "AJMP", "ACALL"}:
            _args_hexs.append(["0x00"])
        elif opcode in {"LJMP", "LCALL"}:
            _args_hexs.append(["0x00", "0x00"])

        _opcode_search_params = " ".join([opcode, *_args_params]).upper().strip()
        _opcode_hex = self._lookup_opcodes_dir.get(_opcode_search_params)
        if not _opcode_hex:
            for key, value in self._lookup_opcodes_dir.items():
                if key == opcode or key.startswith(f"{opcode} "):
                    _opcode_hex = value
                    break
        self.console.log(f"OPCODE: {_opcode_search_params} = {_opcode_hex}")
        if _opcode_hex:
            if _opcode_hex == "0xFFFFFFDB":  # trick to accomodate database directives
                _opcode_hex = None
            return _opcode_hex, _args_hexs
        raise OPCODENotFound(" ".join([opcode, *args]))

    def prepare_operation(self, command: str, opcode: str, *args, **kwargs) -> bool:
        _opcode_hex, _args_hex = self._opcode_fetch(opcode, *args)
        if not _opcode_hex:
            """Database directive"""
            self.console.log("Database directive")
            self._internal_PC.append([])
            return True

        _assembler = [_opcode_hex]
        for x in _args_hex:
            for y in x[::-1]:
                _assembler.append(y)
        self._internal_PC.append([[x] for x in _assembler])
        self._assembler[command] = " ".join(_assembler).lower()
        return True

    def memory_read(self, addr: str, RAM: bool = True) -> Byte:
        print(f"memory read {addr}")
        if str(addr).upper() == "SP":
            return self.super_memory.SP._SP.read()
        _parsed_addr = self._parse_addr(addr)
        if _parsed_addr:
            return _parsed_addr.read(addr)
        if RAM:
            return self.memory_ram.read(addr)
        return self.memory_rom.read(addr)

    def memory_write(self, addr: str, data, RAM: bool = True) -> bool:
        addr = str(addr)
        print(f"memory write {addr}|{data}")
        _parsed_addr = self._parse_addr(addr)
        if _parsed_addr:
            if addr == "SP":
                return _parsed_addr._SP.write(data)
            return _parsed_addr.write(data, addr)
        if RAM:
            return self.memory_ram.write(addr, data)
        return self.memory_rom.write(addr, data)

    def _decode_bit_address(self, addr: str) -> tuple:
        bit_addr = int(tohex(addr), 16)
        if bit_addr < 0x80:
            byte_addr = 0x20 + (bit_addr // 8)
        else:
            byte_addr = bit_addr & 0xF8
        bit = bit_addr % 8
        return format(byte_addr, "#04x"), str(bit)

    def _bit_read_from_byte(self, addr: str, bit: str) -> bool:
        data = int(str(self.memory_read(addr)), 16)
        return bool((data >> int(bit)) & 0x01)

    def _bit_write_to_byte(self, addr: str, bit: str, val) -> bool:
        data = int(str(self.memory_read(addr)), 16)
        mask = 1 << int(bit)
        if bool(val):
            data |= mask
        else:
            data &= ~mask & 0xFF
        return self.memory_write(addr, format(data, "#04x"))

    def bit_read(self, addr: str) -> bool:
        addr = str(addr).strip()
        if addr.startswith("/"):
            addr = addr[1:]

        bit = None
        if "." in addr:
            addr, bit = addr.split(".")
        addr = addr.upper()
        _parsed_addr = self._parse_addr(addr)
        if _parsed_addr:
            print(addr, _parsed_addr)
            if bit:
                return _parsed_addr.bit_get(bit)
            return _parsed_addr.bit_get()
        if bit and ishex(addr):
            return self._bit_read_from_byte(tohex(addr), bit)
        if ishex(addr):
            _addr, _bit = self._decode_bit_address(addr)
            return self._bit_read_from_byte(_addr, _bit)
        return False

    def bit_write(self, addr: str, val: str) -> bool:
        addr = str(addr).strip()
        if addr.startswith("/"):
            addr = addr[1:]

        bit = None
        if "." in addr:
            addr, bit = addr.split(".")
        addr = addr.upper()
        _parsed_addr = self._parse_addr(addr)
        if _parsed_addr:
            print(addr, _parsed_addr)
            if bit:
                return _parsed_addr.bit_set(bit, val)
            return _parsed_addr.bit_set(val)
        if bit and ishex(addr):
            return self._bit_write_to_byte(tohex(addr), bit, val)
        if ishex(addr):
            _addr, _bit = self._decode_bit_address(addr)
            return self._bit_write_to_byte(_addr, _bit, val)
        return False

    def register_pair_read(self, addr) -> Byte:
        print(f"register pair read {addr}")
        _register = self._get_register(addr)
        if hasattr(_register, "read_pair"):
            return _register.read_pair()
        return _register.read()

    def register_pair_write(self, addr, data) -> bool:
        print(f"register pair write {addr}|{data}")
        _register = self._get_register(addr)
        if hasattr(_register, "write_pair"):
            _register.write_pair(data)
        else:
            _register.write(data)
        return True

    pass
