from __future__ import annotations

import re
from dataclasses import dataclass

from .assembler import DIRECTIVE_BYTES, DIRECTIVE_STOP, evaluate_expression
from .exceptions import AssemblyError
from .model import ProgramImage, SourceLocation

ARM_REGISTERS = {f"R{i}": i for i in range(16)}
ARM_REGISTERS.update({"SP": 13, "LR": 14, "PC": 15})
CONDITION_CODES = {
    "EQ": 0x0,
    "NE": 0x1,
    "CS": 0x2,
    "HS": 0x2,
    "CC": 0x3,
    "LO": 0x3,
    "MI": 0x4,
    "PL": 0x5,
    "VS": 0x6,
    "VC": 0x7,
    "HI": 0x8,
    "LS": 0x9,
    "GE": 0xA,
    "LT": 0xB,
    "GT": 0xC,
    "LE": 0xD,
    "AL": 0xE,
}
SHIFT_CODES = {"LSL": 0, "LSR": 1, "ASR": 2, "ROR": 3}
BRANCH_MNEMONICS = {"B", "BL"}
DP_MNEMONICS = {"AND", "EOR", "SUB", "RSB", "ADD", "ADC", "SBC", "RSC", "TST", "TEQ", "CMP", "CMN", "ORR", "MOV", "BIC", "MVN"}
LOAD_STORE_MNEMONICS = {"LDR", "STR"}
MULTIPLY_MNEMONICS = {"MUL", "MLA"}
MULTIPLY_LONG_MNEMONICS = {"UMULL", "UMLAL", "SMULL", "SMLAL"}
PSEUDO_STACK = {"PUSH", "POP"}
ARM_DIRECTIVES = {"ORG", "END", "WORD", ".WORD", "DCD", "AREA", "ENTRY"}
MNEMONIC_ORDER = sorted(
    DP_MNEMONICS
    | BRANCH_MNEMONICS
    | LOAD_STORE_MNEMONICS
    | MULTIPLY_MNEMONICS
    | MULTIPLY_LONG_MNEMONICS
    | PSEUDO_STACK
    | {"BX"},
    key=len,
    reverse=True,
)


@dataclass
class ParsedArmLine:
    line_no: int
    text: str
    label: str | None
    mnemonic: str | None
    operands: list[str]
    address: int = 0
    size: int = 0
    section: str = "code"


@dataclass
class ArmMnemonic:
    base: str
    condition: int
    condition_suffix: str
    set_flags: bool = False


class AssemblerARM:
    def __init__(self, *, code_size: int = 0x1000, endian: str = "little", data_base: int = 0x100) -> None:
        self.code_size = code_size
        self.endian = endian if endian in {"little", "big"} else "little"
        self.data_base = max(0x40, int(data_base))

    def assemble(self, source: str) -> ProgramImage:
        parsed = self._parse_source(source)
        labels = self._first_pass(parsed)
        return self._second_pass(parsed, labels)

    def _parse_source(self, source: str) -> list[ParsedArmLine]:
        lines: list[ParsedArmLine] = []
        for line_no, raw in enumerate(source.splitlines(), start=1):
            text = raw.rstrip()
            content = raw.split(";", 1)[0].strip()
            if not content:
                continue
            label = None
            label_match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", content)
            if label_match:
                label = label_match.group(1).upper()
                content = label_match.group(2).strip()
            elif content:
                parts = content.split(None, 1)
                token = parts[0].upper().rstrip(",")
                if not self._is_known_token(token):
                    label = parts[0].upper()
                    content = parts[1].strip() if len(parts) > 1 else ""
            if not content:
                lines.append(ParsedArmLine(line_no=line_no, text=text, label=label, mnemonic=None, operands=[]))
                continue
            parts = content.split(None, 1)
            mnemonic = parts[0].upper().rstrip(",")
            operands = self._split_operands(parts[1] if len(parts) > 1 else "")
            lines.append(ParsedArmLine(line_no=line_no, text=text, label=label, mnemonic=mnemonic, operands=operands))
        return lines

    def _is_known_token(self, token: str) -> bool:
        try:
            self._parse_mnemonic(token, 0)
        except AssemblyError:
            return False
        return True

    def _split_operands(self, text: str) -> list[str]:
        if not text:
            return []
        result: list[str] = []
        current: list[str] = []
        depth = 0
        for char in text:
            if char == "[":
                depth += 1
            elif char == "]":
                depth = max(0, depth - 1)
            if char == "," and depth == 0:
                token = "".join(current).strip()
                if token:
                    result.append(token)
                current = []
                continue
            current.append(char)
        token = "".join(current).strip()
        if token:
            result.append(token)
        return result

    def _parse_mnemonic(self, token: str, line_no: int) -> ArmMnemonic:
        upper = token.upper()
        if upper in DIRECTIVE_BYTES or upper in ARM_DIRECTIVES:
            return ArmMnemonic(base=upper, condition=CONDITION_CODES["AL"], condition_suffix="AL")
        for base in MNEMONIC_ORDER:
            if not upper.startswith(base):
                continue
            suffix = upper[len(base):]
            set_flags = False
            if base in {
                "ADD",
                "ADC",
                "SUB",
                "RSB",
                "SBC",
                "RSC",
                "AND",
                "ORR",
                "EOR",
                "BIC",
                "MOV",
                "MVN",
                "MUL",
                "MLA",
                "UMULL",
                "UMLAL",
                "SMULL",
                "SMLAL",
            } and suffix.endswith("S"):
                set_flags = True
                suffix = suffix[:-1]
            if suffix == "":
                condition_suffix = "AL"
            else:
                condition_suffix = suffix
            if condition_suffix not in CONDITION_CODES:
                continue
            if base in {"CMP", "CMN", "TST", "TEQ"}:
                set_flags = True
            return ArmMnemonic(
                base=base,
                condition=CONDITION_CODES[condition_suffix],
                condition_suffix=condition_suffix,
                set_flags=set_flags,
            )
        raise AssemblyError(f"unsupported instruction `{token}`", line_no)

    def _parse_area_section(self, operands: list[str]) -> str:
        normalized = {operand.strip().upper() for operand in operands}
        if "DATA" in normalized:
            return "data"
        return "code"

    def _first_pass(self, lines: list[ParsedArmLine]) -> dict[str, int]:
        labels: dict[str, int] = {}
        code_pc = 0
        data_pc = self.data_base
        current_section = "code"
        for line in lines:
            line.section = current_section
            if line.label:
                if line.label in labels:
                    raise AssemblyError(f"duplicate label `{line.label}`", line.line_no)
                labels[line.label] = code_pc if current_section == "code" else data_pc
            if not line.mnemonic:
                line.address = code_pc if current_section == "code" else data_pc
                continue
            meta = self._parse_mnemonic(line.mnemonic, line.line_no)
            if meta.base == "AREA":
                current_section = self._parse_area_section(line.operands)
                line.section = current_section
                line.address = code_pc if current_section == "code" else data_pc
                continue
            line.section = current_section
            line.address = code_pc if current_section == "code" else data_pc
            if meta.base == "ENTRY":
                continue
            if meta.base == "ORG":
                if len(line.operands) != 1:
                    raise AssemblyError("ORG expects one operand", line.line_no)
                value = evaluate_expression(
                    line.operands[0],
                    {**labels, "$": code_pc if current_section == "code" else data_pc},
                    line.line_no,
                )
                if current_section == "code":
                    if not 0 <= value < self.code_size:
                        raise AssemblyError("ORG address out of range", line.line_no)
                    code_pc = value
                else:
                    if value < 0:
                        raise AssemblyError("ORG address out of range", line.line_no)
                    data_pc = value
                line.address = value
                continue
            if meta.base in DIRECTIVE_STOP:
                break
            if current_section == "data" and meta.base not in DIRECTIVE_BYTES | {"WORD", ".WORD", "DCD"}:
                raise AssemblyError("instructions are not allowed in DATA areas", line.line_no)
            line.size = self._instruction_size(meta.base, line)
            if current_section == "code":
                code_pc += line.size
            else:
                data_pc += line.size
            if code_pc > self.code_size:
                raise AssemblyError("program exceeds ROM size", line.line_no)
        return labels

    def _second_pass(self, lines: list[ParsedArmLine], labels: dict[str, int]) -> ProgramImage:
        rom = bytearray(self.code_size)
        listing: list[SourceLocation] = []
        address_to_line: dict[int, int] = {}
        used_bytes: dict[int, int] = {}
        xram_init: dict[int, int] = {}
        current_section = "code"
        current_code_pc = 0
        current_data_pc = self.data_base

        for line in lines:
            if line.mnemonic is None:
                continue
            meta = self._parse_mnemonic(line.mnemonic, line.line_no)
            if meta.base == "AREA":
                current_section = self._parse_area_section(line.operands)
                continue
            if meta.base == "ENTRY":
                continue
            if meta.base == "ORG":
                value = evaluate_expression(line.operands[0], {**labels, "$": line.address}, line.line_no)
                if current_section == "code":
                    current_code_pc = value
                else:
                    current_data_pc = value
                continue
            if meta.base in DIRECTIVE_STOP:
                break
            encoded = self._encode_instruction(meta, line, labels)
            current_pc = line.address
            if current_section == "data":
                current_data_pc = current_pc
                for offset, byte in enumerate(encoded):
                    address = current_data_pc + offset
                    xram_init[address] = byte & 0xFF
                listing.append(
                    SourceLocation(
                        line=line.line_no,
                        text=line.text,
                        address=current_data_pc,
                        size=len(encoded),
                        bytes_=list(encoded),
                    )
                )
                continue
            current_code_pc = current_pc
            for offset, byte in enumerate(encoded):
                address = current_code_pc + offset
                rom[address] = byte
                used_bytes[address] = byte
                address_to_line[address] = line.line_no
            listing.append(
                SourceLocation(
                    line=line.line_no,
                    text=line.text,
                    address=current_code_pc,
                    size=len(encoded),
                    bytes_=list(encoded),
                )
            )

        origin = min(used_bytes.keys(), default=0)
        end = max(used_bytes.keys(), default=-1) + 1
        binary = bytes(rom[origin:end]) if end > origin else b""
        return ProgramImage(
            origin=origin,
            rom=rom,
            binary=binary,
            intel_hex=self._to_intel_hex(used_bytes),
            listing=listing,
            address_to_line=address_to_line,
            labels=labels,
            size=len(binary),
            xram_init=xram_init,
        )

    def _instruction_size(self, base: str, line: ParsedArmLine) -> int:
        if base in {"AREA", "ENTRY"}:
            return 0
        if base in DIRECTIVE_BYTES:
            return len(self._parse_db_operands(line))
        if base in {"WORD", ".WORD", "DCD"}:
            return 4 * len(line.operands)
        if base in DIRECTIVE_STOP:
            return 0
        return 4

    def _parse_db_operands(self, line: ParsedArmLine) -> list[int]:
        data: list[int] = []
        for operand in line.operands:
            if operand.startswith('"') and operand.endswith('"') and len(operand) >= 2:
                data.extend(ord(ch) for ch in operand[1:-1])
                continue
            value = evaluate_expression(operand, {"$": line.address}, line.line_no)
            if not 0 <= value <= 0xFF:
                raise AssemblyError("DB value out of 8-bit range", line.line_no)
            data.append(value)
        return data

    def _parse_register(self, token: str, line_no: int) -> int:
        key = token.strip().upper().strip("{}")
        if key not in ARM_REGISTERS:
            raise AssemblyError(f"unknown register `{token}`", line_no)
        return ARM_REGISTERS[key]

    def _parse_immediate(self, token: str, symbols: dict[str, int], line_no: int) -> int:
        if not token.startswith("#"):
            raise AssemblyError(f"expected immediate operand, got `{token}`", line_no)
        return evaluate_expression(token[1:], symbols, line_no)

    def _encode_word(self, value: int) -> bytes:
        return int(value & 0xFFFFFFFF).to_bytes(4, self.endian, signed=False)

    def _encode_immediate_operand(self, value: int, line_no: int) -> tuple[int, int]:
        value &= 0xFFFFFFFF
        for rotate in range(16):
            rotated = ((value << (rotate * 2)) | (value >> (32 - rotate * 2))) & 0xFFFFFFFF if rotate else value
            if rotated <= 0xFF:
                return rotated, rotate
        raise AssemblyError(f"immediate 0x{value:08X} cannot be encoded in ARM rotated-immediate form", line_no)

    def _parse_shift_suffix(self, parts: list[str], symbols: dict[str, int], line_no: int) -> int:
        if not parts:
            return 0
        if len(parts) != 1:
            raise AssemblyError("invalid shift syntax", line_no)
        text = parts[0].strip().upper()
        if not text:
            return 0
        match = re.fullmatch(r"(LSL|LSR|ASR|ROR)\s+(.+)", text)
        if not match:
            raise AssemblyError(f"invalid shift expression `{parts[0]}`", line_no)
        shift_name = match.group(1)
        amount_token = match.group(2).strip()
        shift_code = SHIFT_CODES[shift_name]
        if amount_token.startswith("#"):
            amount = self._parse_immediate(amount_token, symbols, line_no)
            if not 0 <= amount <= 31:
                raise AssemblyError("ARM immediate shift out of range", line_no)
            return ((amount & 0x1F) << 7) | (shift_code << 5)
        rs = self._parse_register(amount_token, line_no)
        return (rs << 8) | (shift_code << 5) | (1 << 4)

    def _parse_operand2(self, mnemonic: ArmMnemonic, operands: list[str], symbols: dict[str, int], line: ParsedArmLine) -> tuple[int, int]:
        if mnemonic.base in {"MOV", "MVN"}:
            source_index = 1
        elif mnemonic.base in {"CMP", "CMN", "TST", "TEQ"}:
            source_index = 1
        else:
            source_index = 2
        operand = operands[source_index].strip()
        shift_bits = self._parse_shift_suffix(operands[source_index + 1 :], symbols, line.line_no)
        if operand.startswith("#"):
            if shift_bits:
                raise AssemblyError("shifts are not allowed on immediate operand2", line.line_no)
            imm8, rotate = self._encode_immediate_operand(self._parse_immediate(operand, symbols, line.line_no), line.line_no)
            return (1 << 25) | (rotate << 8) | imm8, 0
        rm = self._parse_register(operand, line.line_no)
        return shift_bits | rm, 0

    def _parse_memory_operands(self, operands: list[str], symbols: dict[str, int], line_no: int) -> tuple[int, int, int, int, int, int]:
        if len(operands) < 2:
            raise AssemblyError("memory instruction expects a register and an address operand", line_no)
        mem = operands[1].strip()
        extra = operands[2:]
        pre_index = 1
        write_back = 0
        up = 1
        offset = 0

        if mem.endswith("!"):
            pre_index = 1
            write_back = 1
            mem = mem[:-1].strip()
        match = re.fullmatch(r"\[(.+?)\]", mem)
        if not match:
            raise AssemblyError(f"invalid memory operand `{mem}`", line_no)
        inside = match.group(1)
        parts = self._split_operands(inside)
        rn = self._parse_register(parts[0], line_no)
        offset_operand = parts[1].strip() if len(parts) > 1 else None
        post_operand = extra[0].strip() if extra else None
        if post_operand is not None:
            pre_index = 0
            write_back = 0
            offset_operand = post_operand

        if offset_operand is None:
            return rn, pre_index, up, write_back, 0, 0
        if offset_operand.startswith("#"):
            value = self._parse_immediate(offset_operand, symbols, line_no)
            if not -4095 <= value <= 4095:
                raise AssemblyError("ARM memory immediate offset out of range", line_no)
            if value < 0:
                up = 0
                value = -value
            offset = value & 0xFFF
            return rn, pre_index, up, write_back, 0, offset
        rm = self._parse_register(offset_operand, line_no)
        return rn, pre_index, up, write_back, 1, rm

    def _encode_instruction(self, mnemonic: ArmMnemonic, line: ParsedArmLine, labels: dict[str, int]) -> bytes:
        base = mnemonic.base
        symbols = {**labels, "$": line.address}
        operands = line.operands

        if base in DIRECTIVE_BYTES:
            return bytes(self._parse_db_operands(line))
        if base in {"WORD", ".WORD", "DCD"}:
            data = bytearray()
            for operand in operands:
                data.extend(self._encode_word(evaluate_expression(operand, symbols, line.line_no)))
            return bytes(data)
        if base in {"MUL", "MLA"}:
            return self._encode_multiply(mnemonic, operands, line)
        if base in {"UMULL", "UMLAL", "SMULL", "SMLAL"}:
            return self._encode_multiply_long(mnemonic, operands, line)
        if base == "B":
            return self._encode_branch(mnemonic, operands, symbols, line, link=False)
        if base == "BL":
            return self._encode_branch(mnemonic, operands, symbols, line, link=True)
        if base == "BX":
            if len(operands) != 1:
                raise AssemblyError("BX expects one operand", line.line_no)
            rm = self._parse_register(operands[0], line.line_no)
            word = (mnemonic.condition << 28) | 0x012FFF10 | rm
            return self._encode_word(word)
        if base in {"MOV", "MVN", "ADD", "ADC", "SUB", "SBC", "RSB", "RSC", "AND", "ORR", "EOR", "BIC", "CMP", "CMN", "TST", "TEQ"}:
            return self._encode_data_processing(mnemonic, operands, symbols, line)
        if base in {"LDR", "STR"}:
            return self._encode_load_store(mnemonic, operands, symbols, line)
        if base == "PUSH":
            if len(operands) != 1:
                raise AssemblyError("PUSH expects one register", line.line_no)
            rd = self._parse_register(operands[0], line.line_no)
            word = (mnemonic.condition << 28) | 0x052D0004 | (rd << 12)
            return self._encode_word(word)
        if base == "POP":
            if len(operands) != 1:
                raise AssemblyError("POP expects one register", line.line_no)
            rd = self._parse_register(operands[0], line.line_no)
            word = (mnemonic.condition << 28) | 0x049D0004 | (rd << 12) | (1 << 20)
            return self._encode_word(word)
        raise AssemblyError(f"unsupported instruction `{line.mnemonic}`", line.line_no)

    def _encode_multiply(self, mnemonic: ArmMnemonic, operands: list[str], line: ParsedArmLine) -> bytes:
        expected = 4 if mnemonic.base == "MLA" else 3
        if len(operands) != expected:
            raise AssemblyError(f"{mnemonic.base} expects {expected} registers", line.line_no)
        rd = self._parse_register(operands[0], line.line_no)
        rm = self._parse_register(operands[1], line.line_no)
        rs = self._parse_register(operands[2], line.line_no)
        rn = self._parse_register(operands[3], line.line_no) if mnemonic.base == "MLA" else 0
        accumulate = 1 if mnemonic.base == "MLA" else 0
        word = (
            (mnemonic.condition << 28)
            | (accumulate << 21)
            | ((1 if mnemonic.set_flags else 0) << 20)
            | (rd << 16)
            | (rn << 12)
            | (rs << 8)
            | 0x90
            | rm
        )
        return self._encode_word(word)

    def _encode_multiply_long(self, mnemonic: ArmMnemonic, operands: list[str], line: ParsedArmLine) -> bytes:
        if len(operands) != 4:
            raise AssemblyError(f"{mnemonic.base} expects four registers", line.line_no)
        rd_lo = self._parse_register(operands[0], line.line_no)
        rd_hi = self._parse_register(operands[1], line.line_no)
        rm = self._parse_register(operands[2], line.line_no)
        rs = self._parse_register(operands[3], line.line_no)
        signed = 1 if mnemonic.base in {"SMULL", "SMLAL"} else 0
        accumulate = 1 if mnemonic.base in {"UMLAL", "SMLAL"} else 0
        word = (
            (mnemonic.condition << 28)
            | 0x00800090
            | (signed << 22)
            | (accumulate << 21)
            | ((1 if mnemonic.set_flags else 0) << 20)
            | (rd_hi << 16)
            | (rd_lo << 12)
            | (rs << 8)
            | rm
        )
        return self._encode_word(word)

    def _encode_branch(self, mnemonic: ArmMnemonic, operands: list[str], symbols: dict[str, int], line: ParsedArmLine, *, link: bool) -> bytes:
        if len(operands) != 1:
            raise AssemblyError(f"{mnemonic.base} expects one operand", line.line_no)
        target = evaluate_expression(operands[0], symbols, line.line_no)
        displacement = target - (line.address + 8)
        if displacement % 4:
            raise AssemblyError("ARM branch target must be word aligned", line.line_no)
        imm24 = displacement >> 2
        if not -(1 << 23) <= imm24 < (1 << 23):
            raise AssemblyError("ARM branch target out of range", line.line_no)
        word = (mnemonic.condition << 28) | 0x0A000000 | ((1 if link else 0) << 24) | (imm24 & 0x00FFFFFF)
        return self._encode_word(word)

    def _encode_data_processing(self, mnemonic: ArmMnemonic, operands: list[str], symbols: dict[str, int], line: ParsedArmLine) -> bytes:
        opcode_map = {
            "AND": 0x0,
            "EOR": 0x1,
            "SUB": 0x2,
            "RSB": 0x3,
            "ADD": 0x4,
            "ADC": 0x5,
            "SBC": 0x6,
            "RSC": 0x7,
            "TST": 0x8,
            "TEQ": 0x9,
            "CMP": 0xA,
            "CMN": 0xB,
            "ORR": 0xC,
            "MOV": 0xD,
            "BIC": 0xE,
            "MVN": 0xF,
        }
        opcode = opcode_map[mnemonic.base]
        set_flags = mnemonic.set_flags
        cond = mnemonic.condition << 28

        if mnemonic.base in {"MOV", "MVN"}:
            if len(operands) < 2 or len(operands) > 3:
                raise AssemblyError(f"{mnemonic.base} expects two operands and optional shift", line.line_no)
            rd = self._parse_register(operands[0], line.line_no)
            operand2, _ = self._parse_operand2(mnemonic, operands, symbols, line)
            word = cond | (opcode << 21) | ((1 if set_flags else 0) << 20) | (rd << 12) | operand2
            return self._encode_word(word)

        if mnemonic.base in {"CMP", "CMN", "TST", "TEQ"}:
            if len(operands) < 2 or len(operands) > 3:
                raise AssemblyError(f"{mnemonic.base} expects two operands and optional shift", line.line_no)
            rn = self._parse_register(operands[0], line.line_no)
            operand2, _ = self._parse_operand2(mnemonic, operands, symbols, line)
            word = cond | (opcode << 21) | (1 << 20) | (rn << 16) | operand2
            return self._encode_word(word)

        if len(operands) < 3 or len(operands) > 4:
            raise AssemblyError(f"{mnemonic.base} expects three operands and optional shift", line.line_no)
        rd = self._parse_register(operands[0], line.line_no)
        rn = self._parse_register(operands[1], line.line_no)
        operand2, _ = self._parse_operand2(mnemonic, operands, symbols, line)
        word = cond | (opcode << 21) | ((1 if set_flags else 0) << 20) | (rn << 16) | (rd << 12) | operand2
        return self._encode_word(word)

    def _encode_load_store(self, mnemonic: ArmMnemonic, operands: list[str], symbols: dict[str, int], line: ParsedArmLine) -> bytes:
        if len(operands) < 2 or len(operands) > 3:
            raise AssemblyError(f"{mnemonic.base} expects a register and memory operand", line.line_no)
        rd = self._parse_register(operands[0], line.line_no)
        if mnemonic.base == "LDR" and len(operands) == 2 and operands[1].strip().startswith("="):
            value = evaluate_expression(operands[1].strip()[1:], symbols, line.line_no) & 0xFFFFFFFF
            try:
                imm8, rotate = self._encode_immediate_operand(value, line.line_no)
                word = (mnemonic.condition << 28) | (1 << 25) | (0xD << 21) | (rd << 12) | (rotate << 8) | imm8
                return self._encode_word(word)
            except AssemblyError:
                imm8, rotate = self._encode_immediate_operand((~value) & 0xFFFFFFFF, line.line_no)
                word = (mnemonic.condition << 28) | (1 << 25) | (0xF << 21) | (rd << 12) | (rotate << 8) | imm8
                return self._encode_word(word)
        rn, pre_index, up, write_back, register_offset, offset = self._parse_memory_operands(operands, symbols, line.line_no)
        load = 1 if mnemonic.base == "LDR" else 0
        word = (
            (mnemonic.condition << 28)
            | 0x04000000
            | (register_offset << 25)
            | (pre_index << 24)
            | (up << 23)
            | (write_back << 21)
            | (load << 20)
            | (rn << 16)
            | (rd << 12)
            | (int(offset) & 0xFFF)
        )
        return self._encode_word(word)

    def _to_intel_hex(self, used_bytes: dict[int, int]) -> str:
        if not used_bytes:
            return ":00000001FF"
        records: list[str] = []
        current_upper = None
        addresses = sorted(used_bytes)
        pointer = 0
        while pointer < len(addresses):
            address = addresses[pointer]
            upper = address >> 16
            if upper != current_upper:
                current_upper = upper
                records.append(self._hex_record(0, 0x04, [upper >> 8, upper & 0xFF]))
            chunk_start = address & 0xFFFF
            chunk: list[int] = []
            while pointer < len(addresses):
                next_addr = addresses[pointer]
                if (next_addr >> 16) != current_upper:
                    break
                if chunk and next_addr != address + len(chunk):
                    break
                chunk.append(used_bytes[next_addr])
                pointer += 1
                if len(chunk) == 16:
                    break
            records.append(self._hex_record(chunk_start, 0x00, chunk))
        records.append(":00000001FF")
        return "\n".join(records)

    def _hex_record(self, address: int, record_type: int, data: list[int]) -> str:
        total = len(data) + ((address >> 8) & 0xFF) + (address & 0xFF) + record_type + sum(data)
        checksum = ((~total + 1) & 0xFF)
        payload = "".join(f"{byte & 0xFF:02X}" for byte in data)
        return f":{len(data):02X}{address & 0xFFFF:04X}{record_type:02X}{payload}{checksum:02X}"
