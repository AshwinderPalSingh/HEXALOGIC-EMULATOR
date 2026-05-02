from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class SourceLocation:
    line: int
    text: str
    address: int
    size: int
    bytes_: list[int]


@dataclass
class ProgramImage:
    origin: int
    rom: bytearray
    binary: bytes
    intel_hex: str
    listing: list[SourceLocation]
    address_to_line: dict[int, int]
    labels: dict[str, int]
    size: int
    xram_init: dict[int, int] = field(default_factory=dict)


@dataclass
class TraceEntry:
    pc: int
    opcode: int
    mnemonic: str
    bytes_: list[int]
    cycles: int
    line: int | None
    text: str | None
    changes: dict[str, list[tuple[int, int, int]]] = field(default_factory=dict)
    register_diff: dict[str, dict[str, int]] = field(default_factory=dict)
    interrupt: str | None = None


@dataclass
class Breakpoint:
    pc: int
    condition: str | None = None
    enabled: bool = True


@dataclass
class Watchpoint:
    target: int | str
    space: Literal["iram", "sfr", "xram", "code", "bit", "register"] = "iram"
    enabled: bool = True


@dataclass
class RunResult:
    halted: bool
    reason: str
    steps: list[TraceEntry] = field(default_factory=list)
    step_count: int = 0
    register_diff: dict[str, dict[str, int]] = field(default_factory=dict)
    memory_changes: dict[str, list[tuple[int, int, int]]] = field(default_factory=dict)
    interrupts: list[str] = field(default_factory=list)


@dataclass
class SerialSnapshot:
    tx_buffer: list[int] = field(default_factory=list)
    rx_buffer: list[int] = field(default_factory=list)
    busy: bool = False


@dataclass
class TimerSnapshot:
    t0: dict[str, int | bool]
    t1: dict[str, int | bool]


@dataclass
class ReverseDelta:
    trace: TraceEntry
    cycles_before: int
    cycles_after: int
    halted_before: bool
    halted_after: bool
    last_error_before: str | None
    last_error_after: str | None
    last_interrupt_before: str | None
    last_interrupt_after: str | None
    call_stack_before: list[int] = field(default_factory=list)
    call_stack_after: list[int] = field(default_factory=list)
    extra_before: dict[str, Any] = field(default_factory=dict)
    extra_after: dict[str, Any] = field(default_factory=dict)
