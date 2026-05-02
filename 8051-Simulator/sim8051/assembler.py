from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from core.opcodes import opcodes_lookup

from .exceptions import AssemblyError
from .memory import BIT_ALIASES, SFR_ADDRESSES
from .model import ProgramImage, SourceLocation

DIRECTIVE_STOP = {"END"}
DIRECTIVE_BYTES = {"DB", "BYTE"}
AJMP_OPCODES = {0x01, 0x21, 0x41, 0x61, 0x81, 0xA1, 0xC1, 0xE1}
ACALL_OPCODES = {0x11, 0x31, 0x51, 0x71, 0x91, 0xB1, 0xD1, 0xF1}
REGISTER_NAMES = {f"R{i}": i for i in range(8)}
INDIRECT_NAMES = {"@R0", "@R1"}
SPECIAL_OPERANDS = {"A", "AB", "C", "DPTR", "@DPTR", "@A+DPTR", "@A+PC"}
_BRANCH_WITH_REL = {"SJMP", "JZ", "JNZ", "JC", "JNC", "DJNZ", "CJNE", "JB", "JNB", "JBC"}


@dataclass
class ParsedLine:
    line_no: int
    text: str
    label: str | None
    mnemonic: str | None
    operands: list[str]
    address: int = 0
    size: int = 0


SAFE_AST_NODES = {
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.FloorDiv,
    ast.Mod,
    ast.LShift,
    ast.RShift,
    ast.BitOr,
    ast.BitAnd,
    ast.BitXor,
    ast.Invert,
    ast.USub,
    ast.UAdd,
    ast.Constant,
    ast.Load,
    ast.Name,
    ast.ParenExpr if hasattr(ast, "ParenExpr") else ast.Expression,
}


def _normalize_expression(expr: str, symbols: dict[str, int]) -> str:
    text = expr.strip()
    text = re.sub(r"\$", str(symbols.get("$", 0)), text)

    def replace_hex_suffix(match: re.Match[str]) -> str:
        body = match.group(1)
        return f"0x{body}"

    def replace_bin_suffix(match: re.Match[str]) -> str:
        body = match.group(1)
        return f"0b{body}"

    text = re.sub(r"\b([0-9A-Fa-f]+)[Hh]\b", replace_hex_suffix, text)
    text = re.sub(r"\b([01]+)[Bb]\b", replace_bin_suffix, text)

    def replace_char(match: re.Match[str]) -> str:
        return str(ord(match.group(1)))

    text = re.sub(r"'(.?)'", replace_char, text)

    def replace_name(match: re.Match[str]) -> str:
        name = match.group(0).upper()
        if name in symbols:
            return str(symbols[name])
        if name in SFR_ADDRESSES:
            return str(SFR_ADDRESSES[name])
        raise KeyError(name)

    return re.sub(r"\b[A-Za-z_][A-Za-z0-9_]*\b", replace_name, text)


def evaluate_expression(expr: str, symbols: dict[str, int], line_no: int) -> int:
    try:
        normalized = _normalize_expression(expr, symbols)
    except KeyError as exc:
        raise AssemblyError(f"unknown symbol `{exc.args[0]}`", line_no) from None
    try:
        tree = ast.parse(normalized, mode="eval")
    except SyntaxError as exc:  # pragma: no cover - parser handles most input first
        raise AssemblyError(f"invalid expression `{expr}`", line_no) from exc

    for node in ast.walk(tree):
        if type(node) not in SAFE_AST_NODES:
            raise AssemblyError(f"unsupported expression `{expr}`", line_no)

    try:
        value = eval(compile(tree, "<expr>", "eval"), {"__builtins__": {}}, {})
    except Exception as exc:  # pragma: no cover - defensive
        raise AssemblyError(f"invalid expression `{expr}`", line_no) from exc
    return int(value)


class Assembler8051:
    def __init__(self, *, code_size: int = 0x1000) -> None:
        self.code_size = code_size
        self.lookup = {key.upper(): value for key, value in opcodes_lookup.items() if key != "undefined"}

    def assemble(self, source: str) -> ProgramImage:
        parsed = self._parse_source(source)
        labels = self._first_pass(parsed)
        return self._second_pass(parsed, labels)

    def _parse_source(self, source: str) -> list[ParsedLine]:
        lines: list[ParsedLine] = []
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
            if not content:
                lines.append(ParsedLine(line_no=line_no, text=text, label=label, mnemonic=None, operands=[]))
                continue
            parts = content.split(None, 1)
            mnemonic = parts[0].upper().rstrip(",")
            if mnemonic == "RRL":
                mnemonic = "RL"
            operand_text = parts[1] if len(parts) > 1 else ""
            operand_text = operand_text.lstrip(",").strip()
            operands = [item.strip() for item in operand_text.split(",") if item.strip()] if operand_text else []
            lines.append(ParsedLine(line_no=line_no, text=text, label=label, mnemonic=mnemonic, operands=operands))
        return lines

    def _first_pass(self, lines: list[ParsedLine]) -> dict[str, int]:
        labels: dict[str, int] = {}
        pc = 0
        for line in lines:
            if line.label:
                if line.label in labels:
                    raise AssemblyError(f"duplicate label `{line.label}`", line.line_no)
                labels[line.label] = pc
            if not line.mnemonic:
                line.address = pc
                continue
            line.address = pc
            if line.mnemonic == "ORG":
                if len(line.operands) != 1:
                    raise AssemblyError("ORG expects one operand", line.line_no)
                pc = evaluate_expression(line.operands[0], {**labels, "$": pc}, line.line_no)
                if not 0 <= pc < self.code_size:
                    raise AssemblyError("ORG address out of range", line.line_no)
                line.address = pc
                line.size = 0
                continue
            if line.mnemonic in DIRECTIVE_STOP:
                line.size = 0
                break
            line.size = self._instruction_size(line)
            pc += line.size
            if pc > self.code_size:
                raise AssemblyError("program exceeds ROM size", line.line_no)
        return labels

    def _second_pass(self, lines: list[ParsedLine], labels: dict[str, int]) -> ProgramImage:
        rom = bytearray(self.code_size)
        listing: list[SourceLocation] = []
        address_to_line: dict[int, int] = {}
        current_pc = 0
        used_bytes: dict[int, int] = {}

        for line in lines:
            symbols = {**labels, "$": line.address}
            if line.mnemonic is None:
                continue
            if line.mnemonic == "ORG":
                current_pc = evaluate_expression(line.operands[0], symbols, line.line_no)
                continue
            if line.mnemonic in DIRECTIVE_STOP:
                break
            encoded = self._encode_instruction(line, labels)
            if current_pc != line.address:
                current_pc = line.address
            for offset, byte in enumerate(encoded):
                address = current_pc + offset
                if address >= self.code_size:
                    raise AssemblyError("ROM address out of range", line.line_no)
                rom[address] = byte
                used_bytes[address] = byte
                address_to_line[address] = line.line_no
            listing.append(
                SourceLocation(
                    line=line.line_no,
                    text=line.text,
                    address=current_pc,
                    size=len(encoded),
                    bytes_=encoded,
                )
            )
            current_pc += len(encoded)

        origin = min(used_bytes.keys(), default=0)
        end = max(used_bytes.keys(), default=-1) + 1
        binary = bytes(rom[origin:end]) if end > origin else b""
        intel_hex = self._to_intel_hex(used_bytes)
        return ProgramImage(
            origin=origin,
            rom=rom,
            binary=binary,
            intel_hex=intel_hex,
            listing=listing,
            address_to_line=address_to_line,
            labels=labels,
            size=len(binary),
            xram_init={},
        )

    def _instruction_size(self, line: ParsedLine) -> int:
        if line.mnemonic in DIRECTIVE_BYTES:
            return len(self._parse_db_operands(line))
        dummy_labels = {label: 0 for label in {line.label or ""} if label}
        return len(self._encode_instruction(line, dummy_labels, resolve=False))

    def _parse_db_operands(self, line: ParsedLine) -> list[int]:
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

    def _direct_value(self, operand: str, labels: dict[str, int], line_no: int) -> int:
        key = operand.strip().upper()
        if key in SFR_ADDRESSES:
            return SFR_ADDRESSES[key]
        value = evaluate_expression(operand, labels, line_no)
        if not 0 <= value <= 0xFF:
            raise AssemblyError("direct address must be 8-bit", line_no)
        return value

    def _bit_value(self, operand: str, labels: dict[str, int], line_no: int) -> int:
        key = operand.strip().upper()
        if key.startswith("/"):
            key = key[1:]
        if key in BIT_ALIASES:
            byte_addr, bit = BIT_ALIASES[key]
            return (byte_addr & 0xF8) + bit if byte_addr >= 0x80 else byte_addr
        value = evaluate_expression(key, labels, line_no)
        if not 0 <= value <= 0xFF:
            raise AssemblyError("bit address must be 8-bit", line_no)
        return value

    def _relative_offset(self, target: int, next_pc: int, line_no: int) -> int:
        offset = target - next_pc
        if not -128 <= offset <= 127:
            raise AssemblyError(f"relative target 0x{target:04X} out of range", line_no)
        return offset & 0xFF

    def _classify_operand(self, operand: str, mnemonic: str, position: int, operands: list[str] | None = None) -> str:
        token = operand.strip().upper()
        if token in SPECIAL_OPERANDS:
            return token
        if token in INDIRECT_NAMES:
            return token
        if token in REGISTER_NAMES:
            return token
        if token.startswith("#"):
            return "#IMMED"
        if token.startswith("/"):
            return "/BIT"
        if self._operand_is_bit_address(token, mnemonic, position, operands or []):
            return "BIT"
        return "DIRECT"

    def _operand_is_bit_address(self, token: str, mnemonic: str, position: int, operands: list[str]) -> bool:
        if "." in token or token in BIT_ALIASES:
            return True
        if mnemonic in {"JB", "JNB", "JBC"}:
            return position == 0
        if mnemonic in {"SETB", "CLR", "CPL"}:
            return token not in {"A", "C"}
        if mnemonic == "MOV":
            if len(operands) != 2:
                return False
            first = operands[0].strip().upper()
            second = operands[1].strip().upper()
            if position == 0:
                return second == "C" and token != "C"
            if position == 1:
                return first == "C" and token != "C"
        if mnemonic in {"ORL", "ANL"} and len(operands) == 2:
            return position == 1 and operands[0].strip().upper() == "C" and token != "C"
        return False

    def _lookup_opcode(self, mnemonic: str, forms: list[str], line_no: int) -> int:
        key = " ".join([mnemonic, *forms]).upper().strip()
        opcode = self.lookup.get(key)
        if opcode is None:
            raise AssemblyError(f"unsupported instruction form `{key}`", line_no)
        return int(opcode, 16)

    def _encode_instruction(self, line: ParsedLine, labels: dict[str, int], *, resolve: bool = True) -> list[int]:
        mnemonic = line.mnemonic or ""
        operands = line.operands
        local_symbols = {**labels, "$": line.address}

        if mnemonic in DIRECTIVE_BYTES:
            return self._parse_db_operands(line)
        if mnemonic == "ORG":
            return []
        if mnemonic in DIRECTIVE_STOP:
            return []

        if mnemonic in {"AJMP", "ACALL"}:
            if len(operands) != 1:
                raise AssemblyError(f"{mnemonic} expects one operand", line.line_no)
            target = evaluate_expression(operands[0], local_symbols, line.line_no) if resolve else 0
            next_pc = (line.address + 2) & 0xFFFF
            if resolve and (target & 0xF800) != (next_pc & 0xF800):
                raise AssemblyError(f"{mnemonic} target must stay inside the current 2 KB page", line.line_no)
            upper = ((target >> 8) & 0x07) << 5
            base = 0x01 if mnemonic == "AJMP" else 0x11
            return [base | upper, target & 0xFF]

        if mnemonic in {"LJMP", "LCALL"}:
            if len(operands) != 1:
                raise AssemblyError(f"{mnemonic} expects one operand", line.line_no)
            target = evaluate_expression(operands[0], local_symbols, line.line_no) if resolve else 0
            opcode = self._lookup_opcode(mnemonic, ["ADDR16"], line.line_no)
            return [opcode, (target >> 8) & 0xFF, target & 0xFF]

        if mnemonic in {"SJMP", "JZ", "JNZ", "JC", "JNC"}:
            if len(operands) != 1:
                raise AssemblyError(f"{mnemonic} expects one operand", line.line_no)
            target = evaluate_expression(operands[0], local_symbols, line.line_no) if resolve else line.address + 2
            opcode = self._lookup_opcode(mnemonic, ["DIRECT", "DIRECT"], line.line_no)
            rel = self._relative_offset(target, line.address + 2, line.line_no) if resolve else 0
            return [opcode, rel]

        if mnemonic in {"JB", "JNB", "JBC"}:
            if len(operands) != 2:
                raise AssemblyError(f"{mnemonic} expects two operands", line.line_no)
            bit_value = self._bit_value(operands[0], local_symbols, line.line_no)
            target = evaluate_expression(operands[1], local_symbols, line.line_no) if resolve else line.address + 3
            opcode = self._lookup_opcode(mnemonic, ["BIT", "DIRECT", "DIRECT"], line.line_no)
            rel = self._relative_offset(target, line.address + 3, line.line_no) if resolve else 0
            return [opcode, bit_value, rel]

        if mnemonic == "DJNZ":
            if len(operands) != 2:
                raise AssemblyError("DJNZ expects two operands", line.line_no)
            target = evaluate_expression(operands[1], local_symbols, line.line_no) if resolve else line.address + (3 if self._classify_operand(operands[0], mnemonic, 0, operands) == "DIRECT" else 2)
            first = self._classify_operand(operands[0], mnemonic, 0, operands)
            if first == "DIRECT":
                opcode = self._lookup_opcode("DJNZ", ["DIRECT", "DIRECT"], line.line_no)
                rel = self._relative_offset(target, line.address + 3, line.line_no) if resolve else 0
                return [opcode, self._direct_value(operands[0], local_symbols, line.line_no), rel]
            if first in REGISTER_NAMES:
                opcode = self._lookup_opcode("DJNZ", [first, "DIRECT", "DIRECT"], line.line_no)
                rel = self._relative_offset(target, line.address + 2, line.line_no) if resolve else 0
                return [opcode, rel]
            raise AssemblyError("unsupported DJNZ operand", line.line_no)

        if mnemonic == "CJNE":
            if len(operands) != 3:
                raise AssemblyError("CJNE expects three operands", line.line_no)
            first = self._classify_operand(operands[0], mnemonic, 0, operands)
            second = self._classify_operand(operands[1], mnemonic, 1, operands)
            target = evaluate_expression(operands[2], local_symbols, line.line_no) if resolve else line.address + 3
            forms = [first, second, "DIRECT", "DIRECT"]
            opcode = self._lookup_opcode("CJNE", forms, line.line_no)
            rel = self._relative_offset(target, line.address + 3, line.line_no) if resolve else 0
            bytes_ = [opcode]
            if first == "A" and second == "DIRECT":
                bytes_.append(self._direct_value(operands[1], local_symbols, line.line_no))
            elif second == "#IMMED":
                bytes_.append(evaluate_expression(operands[1][1:], local_symbols, line.line_no) & 0xFF if resolve else 0)
            else:
                raise AssemblyError("unsupported CJNE operand combination", line.line_no)
            bytes_.append(rel)
            return bytes_

        if mnemonic == "JMP":
            if operands and operands[0].strip().upper() == "@A+DPTR":
                opcode = self._lookup_opcode("JMP", ["@A+DPTR"], line.line_no)
                return [opcode]
            raise AssemblyError("JMP only supports @A+DPTR", line.line_no)

        forms = [self._classify_operand(op, mnemonic, idx, operands) for idx, op in enumerate(operands)]
        opcode = self._lookup_opcode(mnemonic, forms, line.line_no)
        encoded = [opcode]

        if mnemonic == "MOV" and forms == ["DPTR", "#IMMED"]:
            value = evaluate_expression(operands[1][1:], local_symbols, line.line_no) if resolve else 0
            encoded.extend([(value >> 8) & 0xFF, value & 0xFF])
            return encoded

        for idx, form in enumerate(forms):
            operand = operands[idx]
            if form == "DIRECT":
                encoded.append(self._direct_value(operand, local_symbols, line.line_no) if resolve else 0)
            elif form == "BIT":
                encoded.append(self._bit_value(operand, local_symbols, line.line_no) if resolve else 0)
            elif form == "/BIT":
                encoded.append(self._bit_value(operand[1:], local_symbols, line.line_no) if resolve else 0)
            elif form == "#IMMED":
                raw = operand[1:]
                value = evaluate_expression(raw, local_symbols, line.line_no) if resolve else 0
                if mnemonic == "MOV" and idx == 1 and forms[0] == "DPTR":
                    encoded.extend([(value >> 8) & 0xFF, value & 0xFF])
                else:
                    encoded.append(value & 0xFF)
        return encoded

    def _to_intel_hex(self, used_bytes: dict[int, int]) -> str:
        if not used_bytes:
            return ":00000001FF"
        records: list[str] = []
        addresses = sorted(used_bytes)
        chunk: list[int] = []
        start = addresses[0]
        previous = start - 1
        for address in addresses:
            if address != previous + 1 or len(chunk) >= 16:
                if chunk:
                    records.append(self._hex_record(start, chunk))
                start = address
                chunk = []
            chunk.append(used_bytes[address])
            previous = address
        if chunk:
            records.append(self._hex_record(start, chunk))
        records.append(":00000001FF")
        return "\n".join(records)

    def _hex_record(self, address: int, data: list[int]) -> str:
        count = len(data)
        payload = [count, (address >> 8) & 0xFF, address & 0xFF, 0x00, *data]
        checksum = ((~sum(payload) + 1) & 0xFF)
        return ":" + "".join(f"{byte:02X}" for byte in [*payload, checksum])
