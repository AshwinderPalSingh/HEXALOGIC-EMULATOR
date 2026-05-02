from __future__ import annotations

class SimulatorError(Exception):
    """Base exception for the sandboxed 8051 simulator."""


class AssemblyError(SimulatorError):
    def __init__(self, message: str, line: int | None = None) -> None:
        self.line = line
        if line is not None:
            super().__init__(f"Line {line}: {message}")
        else:
            super().__init__(message)


class ExecutionError(SimulatorError):
    def __init__(self, message: str, pc: int | None = None) -> None:
        self.pc = pc
        if pc is not None:
            super().__init__(f"PC 0x{pc:04X}: {message}")
        else:
            super().__init__(message)


class MemoryAccessError(ExecutionError):
    pass


class DecodeError(ExecutionError):
    pass


class ValidationError(SimulatorError):
    def __init__(self, message: str, *, context: dict | None = None) -> None:
        self.context = context or {}
        super().__init__(message)
