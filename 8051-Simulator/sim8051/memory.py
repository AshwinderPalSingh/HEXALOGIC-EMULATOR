from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .exceptions import MemoryAccessError

Endian = Literal["little", "big"]
MemorySpace = Literal["code", "xram", "direct", "indirect", "sfr"]

# ARM virtual GPIO block (mirrors the low XRAM region used by the simplified GPIO model).
GPIOA_MMIO_BASE = 0x40000000
GPIOA_MMIO_SIZE = 0x40

BIT_ADDRESSABLE_SFRS = {0x80, 0x88, 0x90, 0x98, 0xA0, 0xA8, 0xB0, 0xB8, 0xC8, 0xD0, 0xE0, 0xF0}

SFR_ADDRESSES = {
    "P0": 0x80,
    "SP": 0x81,
    "DPL": 0x82,
    "DPH": 0x83,
    "PCON": 0x87,
    "TCON": 0x88,
    "TMOD": 0x89,
    "TL0": 0x8A,
    "TL1": 0x8B,
    "TH0": 0x8C,
    "TH1": 0x8D,
    "P1": 0x90,
    "SCON": 0x98,
    "SBUF": 0x99,
    "RCAP2L": 0xCA,
    "RCAP2H": 0xCB,
    "TL2": 0xCC,
    "TH2": 0xCD,
    "T2MOD": 0xC9,
    "P2": 0xA0,
    "IE": 0xA8,
    "IP": 0xB8,
    "P3": 0xB0,
    "PSW": 0xD0,
    "ACC": 0xE0,
    "A": 0xE0,
    "B": 0xF0,
    "T2CON": 0xC8,
}

BIT_ALIASES = {
    "IT0": (0x88, 0),
    "IE0": (0x88, 1),
    "IT1": (0x88, 2),
    "IE1": (0x88, 3),
    "TR0": (0x88, 4),
    "TF0": (0x88, 5),
    "TR1": (0x88, 6),
    "TF1": (0x88, 7),
    "RI": (0x98, 0),
    "TI": (0x98, 1),
    "RB8": (0x98, 2),
    "TB8": (0x98, 3),
    "REN": (0x98, 4),
    "SM2": (0x98, 5),
    "SM1": (0x98, 6),
    "SM0": (0x98, 7),
    "EX0": (0xA8, 0),
    "ET0": (0xA8, 1),
    "EX1": (0xA8, 2),
    "ET1": (0xA8, 3),
    "ES": (0xA8, 4),
    "EA": (0xA8, 7),
    "ET2": (0xA8, 5),
    "PX0": (0xB8, 0),
    "PT0": (0xB8, 1),
    "PX1": (0xB8, 2),
    "PT1": (0xB8, 3),
    "PS": (0xB8, 4),
    "PT2": (0xB8, 5),
    "CP/RL2": (0xC8, 0),
    "C/T2": (0xC8, 1),
    "TR2": (0xC8, 2),
    "EXEN2": (0xC8, 3),
    "TCLK": (0xC8, 4),
    "RCLK": (0xC8, 5),
    "EXF2": (0xC8, 6),
    "TF2": (0xC8, 7),
    "P": (0xD0, 0),
    "UD": (0xD0, 1),
    "OV": (0xD0, 2),
    "RS0": (0xD0, 3),
    "RS1": (0xD0, 4),
    "F0": (0xD0, 5),
    "AC": (0xD0, 6),
    "CY": (0xD0, 7),
    "C": (0xD0, 7),
}

for port_name, base in (("P0", 0x80), ("P1", 0x90), ("P2", 0xA0), ("P3", 0xB0)):
    for bit in range(8):
        BIT_ALIASES[f"{port_name}.{bit}"] = (base, bit)

for sfr_name in ("IE", "IP", "TCON", "SCON", "PSW"):
    base = SFR_ADDRESSES[sfr_name]
    for bit in range(8):
        BIT_ALIASES[f"{sfr_name}.{bit}"] = (base, bit)

BIT_ALIASES["EI.7"] = BIT_ALIASES["IE.7"]
BIT_ALIASES["EI.1"] = BIT_ALIASES["IE.1"]


@dataclass
class PortState:
    latch: int = 0xFF
    external_mask: int = 0x00
    external_value: int = 0xFF
    open_drain: bool = False

    def read_pin(self) -> int:
        value = 0
        for bit in range(8):
            mask = 1 << bit
            if self.external_mask & mask:
                bit_value = 1 if self.external_value & mask else 0
            elif self.latch & mask:
                bit_value = 1
            else:
                bit_value = 0
            value |= bit_value << bit
        return value & 0xFF

    def set_input(self, bit: int, level: int | bool | None) -> None:
        mask = 1 << bit
        if level is None:
            self.external_mask &= ~mask & 0xFF
            self.external_value |= mask
            return
        self.external_mask |= mask
        if level:
            self.external_value |= mask
        else:
            self.external_value &= ~mask & 0xFF


@dataclass
class MemorySnapshot:
    iram: dict[int, int]
    sfr: dict[int, int]
    xram: dict[int, int]
    code: dict[int, int]


class MemoryMap:
    def __init__(self, *, code_size: int = 0x10000, xram_size: int = 0x10000, upper_iram: bool = False) -> None:
        self.code_size = code_size
        self.xram_size = xram_size
        self.upper_iram_enabled = upper_iram
        self.rom = bytearray(code_size)
        self.xram = bytearray(xram_size)
        self.iram_low = bytearray(0x80)
        self.iram_high = bytearray(0x80) if upper_iram else bytearray()
        self.sfr = bytearray(0x80)
        self.ports = {
            0x80: PortState(open_drain=True),
            0x90: PortState(),
            0xA0: PortState(),
            0xB0: PortState(),
        }
        self._changes: dict[str, list[tuple[int, int, int]]] = {"iram": [], "sfr": [], "xram": [], "code": []}
        self.reset()

    def reset(self) -> None:
        self.rom[:] = b"\x00" * len(self.rom)
        self.xram[:] = b"\x00" * len(self.xram)
        self.iram_low[:] = b"\x00" * len(self.iram_low)
        if self.iram_high:
            self.iram_high[:] = b"\x00" * len(self.iram_high)
        self.sfr[:] = b"\x00" * len(self.sfr)
        for address, port in self.ports.items():
            port.latch = 0xFF
            port.external_mask = 0x00
            port.external_value = 0xFF
            self.sfr[address - 0x80] = 0xFF
        self.write_direct(0x81, 0x07)
        self._changes = {"iram": [], "sfr": [], "xram": [], "code": []}

    def load_rom(self, start: int, data: bytes | bytearray) -> None:
        end = start + len(data)
        if not 0 <= start < self.code_size or end > self.code_size:
            raise MemoryAccessError("ROM write out of range")
        for offset, byte in enumerate(data):
            address = start + offset
            old = self.rom[address]
            new = byte & 0xFF
            self.rom[address] = new
            if old != new:
                self._record_change("code", address, old, new)

    def read_code(self, address: int) -> int:
        if not 0 <= address < self.code_size:
            raise MemoryAccessError("ROM address out of range", pc=address)
        return self.rom[address]

    def read8(self, address: int, *, space: MemorySpace = "direct", rmw: bool = False) -> int:
        if space == "code":
            return self.read_code(address)
        if space == "xram":
            return self.read_xram(address)
        if space == "direct":
            return self.read_direct(address, rmw=rmw)
        if space == "indirect":
            return self.read_indirect(address)
        if space == "sfr":
            return self.read_direct(address, rmw=rmw)
        raise MemoryAccessError(f"Unsupported memory space `{space}`")

    def write8(self, address: int, value: int, *, space: MemorySpace = "direct") -> None:
        if space == "code":
            self.load_rom(address, bytes([value & 0xFF]))
            return
        if space == "xram":
            self.write_xram(address, value)
            return
        if space == "direct":
            self.write_direct(address, value)
            return
        if space == "indirect":
            self.write_indirect(address, value)
            return
        if space == "sfr":
            self.write_direct(address, value)
            return
        raise MemoryAccessError(f"Unsupported memory space `{space}`")

    def read16(self, address: int, *, space: MemorySpace = "direct", endian: Endian = "little", rmw: bool = False) -> int:
        first = self.read8(address, space=space, rmw=rmw)
        second = self.read8(address + 1, space=space, rmw=rmw)
        if endian == "little":
            return first | (second << 8)
        return (first << 8) | second

    def write16(self, address: int, value: int, *, space: MemorySpace = "direct", endian: Endian = "little") -> None:
        low = value & 0xFF
        high = (value >> 8) & 0xFF
        if endian == "little":
            self.write8(address, low, space=space)
            self.write8(address + 1, high, space=space)
            return
        self.write8(address, high, space=space)
        self.write8(address + 1, low, space=space)

    def read32(self, address: int, *, space: MemorySpace = "direct", endian: Endian = "little", rmw: bool = False) -> int:
        words = [self.read8(address + offset, space=space, rmw=rmw) for offset in range(4)]
        if endian == "little":
            return words[0] | (words[1] << 8) | (words[2] << 16) | (words[3] << 24)
        return (words[0] << 24) | (words[1] << 16) | (words[2] << 8) | words[3]

    def write32(self, address: int, value: int, *, space: MemorySpace = "direct", endian: Endian = "little") -> None:
        bytes_ = [
            value & 0xFF,
            (value >> 8) & 0xFF,
            (value >> 16) & 0xFF,
            (value >> 24) & 0xFF,
        ]
        if endian == "big":
            bytes_.reverse()
        for offset, byte in enumerate(bytes_):
            self.write8(address + offset, byte, space=space)

    def _normalize_xram_address(self, address: int) -> int:
        address &= 0xFFFFFFFF
        if GPIOA_MMIO_BASE <= address < GPIOA_MMIO_BASE + GPIOA_MMIO_SIZE:
            return address - GPIOA_MMIO_BASE
        if not 0 <= address < self.xram_size:
            raise MemoryAccessError("XRAM address out of range")
        return address

    def read_xram(self, address: int) -> int:
        address = self._normalize_xram_address(address)
        return self.xram[address]

    def write_xram(self, address: int, value: int) -> None:
        address = self._normalize_xram_address(address)
        value &= 0xFF
        old = self.xram[address]
        self.xram[address] = value
        if old != value:
            self._record_change("xram", address, old, value)

    def read_direct(self, address: int, *, rmw: bool = False) -> int:
        address &= 0xFF
        if address < 0x80:
            return self.iram_low[address]
        if address in self.ports and not rmw:
            return self.ports[address].read_pin()
        return self.sfr[address - 0x80]

    def write_direct(self, address: int, value: int) -> None:
        address &= 0xFF
        value &= 0xFF
        if address < 0x80:
            old = self.iram_low[address]
            self.iram_low[address] = value
            if old != value:
                self._record_change("iram", address, old, value)
            return
        old = self.sfr[address - 0x80]
        self.sfr[address - 0x80] = value
        if address in self.ports:
            self.ports[address].latch = value
        if old != value:
            self._record_change("sfr", address, old, value)

    def read_indirect(self, address: int) -> int:
        address &= 0xFF
        if address < 0x80:
            return self.iram_low[address]
        if not self.upper_iram_enabled:
            raise MemoryAccessError(f"Indirect IRAM address 0x{address:02X} is not available on this target")
        return self.iram_high[address - 0x80]

    def write_indirect(self, address: int, value: int) -> None:
        address &= 0xFF
        value &= 0xFF
        if address < 0x80:
            old = self.iram_low[address]
            self.iram_low[address] = value
            if old != value:
                self._record_change("iram", address, old, value)
            return
        if not self.upper_iram_enabled:
            raise MemoryAccessError(f"Indirect IRAM address 0x{address:02X} is not available on this target")
        old = self.iram_high[address - 0x80]
        self.iram_high[address - 0x80] = value
        if old != value:
            self._record_change("iram", address, old, value)

    def resolve_sfr(self, name: str) -> int:
        key = name.strip().upper()
        if key == "EI":
            key = "IE"
        if key not in SFR_ADDRESSES:
            raise MemoryAccessError(f"Unknown SFR `{name}`")
        return SFR_ADDRESSES[key]

    def read_sfr(self, name: str, *, rmw: bool = False) -> int:
        return self.read_direct(self.resolve_sfr(name), rmw=rmw)

    def write_sfr(self, name: str, value: int) -> None:
        self.write_direct(self.resolve_sfr(name), value)

    def get_port(self, port_index: int) -> PortState:
        address = 0x80 + (port_index * 0x10)
        return self.ports[address]

    def set_pin_input(self, port_index: int, bit: int, level: int | bool | None) -> None:
        if not 0 <= port_index <= 3 or not 0 <= bit <= 7:
            raise MemoryAccessError("Pin out of range")
        self.ports[0x80 + (port_index * 0x10)].set_input(bit, level)

    def _decode_bit_address(self, bit_address: int) -> tuple[int, int, str]:
        bit_address &= 0xFF
        if bit_address < 0x80:
            byte_address = 0x20 + (bit_address // 8)
            return byte_address, bit_address % 8, "iram"
        byte_address = bit_address & 0xF8
        if byte_address not in BIT_ADDRESSABLE_SFRS:
            raise MemoryAccessError(f"Address 0x{byte_address:02X} is not bit-addressable")
        return byte_address, bit_address % 8, "sfr"

    def resolve_bit_operand(self, token: str) -> tuple[int, int]:
        key = token.strip().upper()
        if key in BIT_ALIASES:
            return BIT_ALIASES[key]
        if "." in key:
            base, bit = key.split(".", 1)
            if base in SFR_ADDRESSES and bit.isdigit():
                bit_idx = int(bit, 10)
                if not 0 <= bit_idx <= 7:
                    raise MemoryAccessError(f"Bit index out of range in `{token}`")
                return SFR_ADDRESSES[base], bit_idx
        raise MemoryAccessError(f"Unknown bit operand `{token}`")

    def read_bit(self, bit_address: int) -> int:
        byte_address, bit, space = self._decode_bit_address(bit_address)
        source = self.read_direct(byte_address, rmw=(space == "sfr")) if space == "sfr" else self.iram_low[byte_address]
        return (source >> bit) & 0x01

    def write_bit(self, bit_address: int, value: int | bool) -> None:
        byte_address, bit, space = self._decode_bit_address(bit_address)
        current = self.read_direct(byte_address, rmw=True) if space == "sfr" else self.iram_low[byte_address]
        mask = 1 << bit
        new_value = (current | mask) if value else (current & ~mask & 0xFF)
        if space == "sfr":
            self.write_direct(byte_address, new_value)
        else:
            old = self.iram_low[byte_address]
            self.iram_low[byte_address] = new_value
            if old != new_value:
                self._record_change("iram", byte_address, old, new_value)

    def read_named_bit(self, token: str) -> int:
        address, bit = self.resolve_bit_operand(token)
        value = self.read_direct(address, rmw=True)
        return (value >> bit) & 0x01

    def write_named_bit(self, token: str, value: int | bool) -> None:
        address, bit = self.resolve_bit_operand(token)
        current = self.read_direct(address, rmw=True)
        mask = 1 << bit
        new_value = (current | mask) if value else (current & ~mask & 0xFF)
        self.write_direct(address, new_value)

    def _record_change(self, space: str, address: int, old: int, new: int) -> None:
        self._changes[space].append((address, old & 0xFF, new & 0xFF))

    def consume_changes(self) -> dict[str, list[tuple[int, int, int]]]:
        if not any(self._changes.values()):
            return {}
        payload = {}
        for space, values in self._changes.items():
            if not values:
                continue
            payload[space] = values[:]
            values.clear()
        return payload

    def dump_iram(self) -> dict[int, int]:
        data = {addr: value for addr, value in enumerate(self.iram_low)}
        if self.upper_iram_enabled:
            data.update({0x80 + idx: value for idx, value in enumerate(self.iram_high)})
        return data

    def dump_sfr(self) -> dict[int, int]:
        return {0x80 + idx: value for idx, value in enumerate(self.sfr)}

    def dump_rom(self, size: int | None = None) -> dict[int, int]:
        limit = len(self.rom) if size is None else min(size, len(self.rom))
        return {idx: self.rom[idx] for idx in range(limit)}

    def export_state(self) -> dict[str, object]:
        return {
            "code_size": self.code_size,
            "xram_size": self.xram_size,
            "upper_iram_enabled": self.upper_iram_enabled,
            "rom_hex": self.rom.hex(),
            "xram_hex": self.xram.hex(),
            "iram_low_hex": self.iram_low.hex(),
            "iram_high_hex": self.iram_high.hex(),
            "sfr_hex": self.sfr.hex(),
            "ports": {
                f"0x{address:02X}": {
                    "latch": port.latch,
                    "external_mask": port.external_mask,
                    "external_value": port.external_value,
                    "open_drain": port.open_drain,
                }
                for address, port in self.ports.items()
            },
        }

    def import_state(self, state: dict[str, object]) -> None:
        def _load_hex(raw: object, length: int) -> bytes:
            data = bytes.fromhex(str(raw or ""))
            if len(data) >= length:
                return data[:length]
            return data + (b"\x00" * (length - len(data)))

        self.rom[:] = _load_hex(state.get("rom_hex", ""), len(self.rom))
        self.xram[:] = _load_hex(state.get("xram_hex", ""), len(self.xram))
        self.iram_low[:] = _load_hex(state.get("iram_low_hex", ""), len(self.iram_low))
        if self.iram_high:
            self.iram_high[:] = _load_hex(state.get("iram_high_hex", ""), len(self.iram_high))
        self.sfr[:] = _load_hex(state.get("sfr_hex", ""), len(self.sfr))
        for key, raw_port in dict(state.get("ports", {})).items():
            address = int(str(key), 16)
            if address not in self.ports:
                continue
            port = self.ports[address]
            port_state = dict(raw_port)
            port.latch = int(port_state.get("latch", 0xFF)) & 0xFF
            port.external_mask = int(port_state.get("external_mask", 0x00)) & 0xFF
            port.external_value = int(port_state.get("external_value", 0xFF)) & 0xFF
            port.open_drain = bool(port_state.get("open_drain", port.open_drain))
        self._changes = {"iram": [], "sfr": [], "xram": [], "code": []}
