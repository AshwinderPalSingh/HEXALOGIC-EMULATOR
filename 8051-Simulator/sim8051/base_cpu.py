from __future__ import annotations

from abc import ABC, abstractmethod
import ast
from collections import deque
from dataclasses import dataclass, field
import logging
import os
import time
from typing import Callable

from .exceptions import ValidationError
from .model import Breakpoint, ProgramImage, ReverseDelta, RunResult, TraceEntry, Watchpoint

_DEBUG_TIMING = os.environ.get("HEXLOGIC_DEBUG_TIMING", "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class DebuggerState:
    breakpoints: dict[int, Breakpoint] = field(default_factory=dict)
    watchpoints: list[Watchpoint] = field(default_factory=list)
    trace: deque[TraceEntry] = field(default_factory=lambda: deque(maxlen=512))
    history: deque[ReverseDelta] = field(default_factory=lambda: deque(maxlen=512))
    call_stack: list[int] = field(default_factory=list)


class BaseCPU(ABC):
    def __init__(self) -> None:
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self.debugger = DebuggerState()
        self.clock_hz = 11_059_200
        self.execution_mode = "realtime"
        self.speed_multiplier = 1.0
        self.program: ProgramImage | None = None
        self.pc = 0
        self.cycles = 0
        self.halted = True
        self.last_error: str | None = None
        self.last_interrupt: str | None = None
        self.debug_mode = False
        self.max_cycles_per_request = 1_000_000
        self.max_run_seconds = 0.5
        self.max_history_entries = 512

    def set_clock_hz(self, clock_hz: int) -> None:
        self.clock_hz = max(1, int(clock_hz))

    def set_execution_mode(self, mode: str) -> None:
        normalized = str(mode or "realtime").lower()
        if normalized not in {"realtime", "fast"}:
            raise ValidationError("Unsupported execution mode", context={"mode": mode, "supported": ["fast", "realtime"]})
        self.execution_mode = normalized

    def set_speed_multiplier(self, multiplier: float) -> None:
        try:
            value = float(multiplier)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Invalid speed multiplier", context={"multiplier": multiplier}) from exc
        self.speed_multiplier = min(10.0, max(0.1, value))

    def set_debug_mode(self, enabled: bool) -> None:
        self.debug_mode = bool(enabled)

    def effective_clock_hz(self) -> float:
        return float(max(1, self.clock_hz)) * float(max(0.1, self.speed_multiplier))

    def _log_run_audit(self, *, mode: str, steps: int, cycles_executed: int, elapsed_wall_time_sec: float) -> None:
        effective_hz_expected = max(1.0, float(self.effective_clock_hz()))
        computed_simulated_time_sec = cycles_executed / effective_hz_expected
        cycles_per_sec_actual = (cycles_executed / elapsed_wall_time_sec) if elapsed_wall_time_sec > 0 else 0.0
        instructions_per_sec = (steps / elapsed_wall_time_sec) if elapsed_wall_time_sec > 0 else 0.0
        payload = {
            "mode": mode,
            "instructions_executed": steps,
            "cycles_executed": cycles_executed,
            "elapsed_wall_time_sec": round(elapsed_wall_time_sec, 6),
            "computed_simulated_time_sec": round(computed_simulated_time_sec, 6),
            "effective_hz_expected": round(effective_hz_expected, 3),
            "cycles_per_sec_actual": round(cycles_per_sec_actual, 3),
            "instructions_per_sec": round(instructions_per_sec, 3),
        }
        if _DEBUG_TIMING:
            print("[DEBUG_TIMING][cpu.run]", payload)
        elif self.debug_mode:
            self.logger.debug("run_audit=%s", payload)

    def set_breakpoints(self, pcs: list[int | dict]) -> None:
        breakpoints: dict[int, Breakpoint] = {}
        for item in pcs:
            if isinstance(item, dict):
                pc = int(item.get("pc", 0))
                breakpoints[pc] = Breakpoint(
                    pc=pc,
                    condition=(str(item["condition"]).strip() or None) if "condition" in item else None,
                    enabled=bool(item.get("enabled", True)),
                )
            else:
                pc = int(item)
                breakpoints[pc] = Breakpoint(pc=pc)
        self.debugger.breakpoints = breakpoints

    def set_watchpoints(self, watchpoints: list[Watchpoint]) -> None:
        self.debugger.watchpoints = watchpoints[:]

    def _check_watchpoints(self, trace: TraceEntry) -> bool:
        if not self.debugger.watchpoints:
            return False
        for watch in self.debugger.watchpoints:
            if not watch.enabled:
                continue
            if watch.space == "register":
                if str(watch.target).upper() in trace.register_diff:
                    return True
                continue
            for address, _, _ in trace.changes.get(watch.space, []):
                if address == int(watch.target):
                    return True
        return False

    def _record_trace(self, trace: TraceEntry) -> None:
        self.debugger.trace.append(trace)
        if self.debug_mode:
            self.logger.debug("pc=0x%04X opcode=0x%02X mnemonic=%s cycles=%s", trace.pc, trace.opcode, trace.mnemonic, trace.cycles)

    def compact_execution_allowed(self) -> bool:
        return not self.debug_mode and not self.debugger.watchpoints

    def step_compact(self) -> TraceEntry:
        return self._step_impl_compact()

    def step_compact_payload(self) -> dict:
        trace = self.step_compact()
        return {
            "pc": trace.pc,
            "opcode": trace.opcode,
            "mnemonic": trace.mnemonic,
            "bytes": list(trace.bytes_),
            "cycles": trace.cycles,
            "line": trace.line,
            "text": trace.text,
            "changes": trace.changes,
            "interrupt": trace.interrupt,
        }

    def hardware_tick_snapshot(self) -> dict:
        hardware_snapshot = getattr(self, "hardware_snapshot", None)
        if callable(hardware_snapshot):
            return hardware_snapshot()
        return self.snapshot()

    def signal_runtime_snapshot(self) -> dict:
        runtime_snapshot = getattr(self, "runtime_snapshot", None)
        if callable(runtime_snapshot):
            return runtime_snapshot()
        return self.snapshot()

    def try_fast_realtime_slice(self, *, max_steps: int, max_cycles: int) -> dict | None:
        return None

    def step(self) -> TraceEntry:
        return self._step_with_history()

    def step_into(self) -> TraceEntry:
        return self._step_with_history()

    def step_back(self) -> ReverseDelta | None:
        if not self.debugger.history:
            return None
        delta = self.debugger.history.pop()
        self._restore_reverse_delta(delta)
        if self.debugger.trace and self.debugger.trace[-1].pc == delta.trace.pc and self.debugger.trace[-1].opcode == delta.trace.opcode:
            self.debugger.trace.pop()
        return delta

    def run(self, *, max_steps: int = 1000, after_step: Callable[[TraceEntry], None] | None = None) -> RunResult:
        steps: list[TraceEntry] = []
        reason = "max_steps"
        start_cycles = self.cycles
        start_time = time.perf_counter()
        deadline = start_time + self.max_run_seconds
        for _ in range(max_steps):
            if self.halted:
                reason = "halted"
                break
            if (self.cycles - start_cycles) >= self.max_cycles_per_request:
                reason = "cycle_cap"
                break
            if time.perf_counter() >= deadline:
                reason = "timeout"
                break
            if self._active_breakpoint():
                reason = "breakpoint"
                break
            trace = self.step_into()
            steps.append(trace)
            if after_step is not None:
                after_step(trace)
            if self._check_watchpoints(trace):
                reason = "watchpoint"
                break
            if self.halted:
                reason = "halted"
                break
        self._log_run_audit(
            mode="run",
            steps=len(steps),
            cycles_executed=max(0, self.cycles - start_cycles),
            elapsed_wall_time_sec=max(0.0, time.perf_counter() - start_time),
        )
        return RunResult(halted=self.halted, reason=reason, steps=steps)

    def step_over(self, *, after_step: Callable[[TraceEntry], None] | None = None) -> RunResult:
        opcode = self._current_opcode()
        if not self._is_call_opcode(opcode):
            trace = self.step_into()
            if after_step is not None:
                after_step(trace)
            self._log_run_audit(mode="step_over", steps=1, cycles_executed=trace.cycles, elapsed_wall_time_sec=0.0)
            return RunResult(halted=self.halted, reason="step", steps=[trace])
        target_depth = len(self.debugger.call_stack)
        return_address = (self.pc + self._instruction_length_preview(opcode)) & 0xFFFF
        steps: list[TraceEntry] = []
        reason = "step"
        start_cycles = self.cycles
        start_time = time.perf_counter()
        deadline = start_time + self.max_run_seconds
        while True:
            if self.halted:
                reason = "halted"
                break
            if (self.cycles - start_cycles) >= self.max_cycles_per_request:
                reason = "cycle_cap"
                break
            if time.perf_counter() >= deadline:
                reason = "timeout"
                break
            if self._active_breakpoint():
                reason = "breakpoint"
                break
            trace = self.step_into()
            steps.append(trace)
            if after_step is not None:
                after_step(trace)
            if self._check_watchpoints(trace):
                reason = "watchpoint"
                break
            if self.halted:
                reason = "halted"
                break
            if self.pc == return_address and len(self.debugger.call_stack) <= target_depth:
                reason = "step_over"
                break
        self._log_run_audit(
            mode="step_over",
            steps=len(steps),
            cycles_executed=max(0, self.cycles - start_cycles),
            elapsed_wall_time_sec=max(0.0, time.perf_counter() - start_time),
        )
        return RunResult(halted=self.halted, reason=reason, steps=steps)

    def step_out(self, *, after_step: Callable[[TraceEntry], None] | None = None) -> RunResult:
        start_depth = len(self.debugger.call_stack)
        if start_depth == 0:
            trace = self.step_into()
            if after_step is not None:
                after_step(trace)
            self._log_run_audit(mode="step_out", steps=1, cycles_executed=trace.cycles, elapsed_wall_time_sec=0.0)
            return RunResult(halted=self.halted, reason="step", steps=[trace])
        steps: list[TraceEntry] = []
        reason = "step"
        start_cycles = self.cycles
        start_time = time.perf_counter()
        deadline = start_time + self.max_run_seconds
        while True:
            if self.halted:
                reason = "halted"
                break
            if (self.cycles - start_cycles) >= self.max_cycles_per_request:
                reason = "cycle_cap"
                break
            if time.perf_counter() >= deadline:
                reason = "timeout"
                break
            if self._active_breakpoint():
                reason = "breakpoint"
                break
            trace = self.step_into()
            steps.append(trace)
            if after_step is not None:
                after_step(trace)
            if self._check_watchpoints(trace):
                reason = "watchpoint"
                break
            if self.halted:
                reason = "halted"
                break
            if len(self.debugger.call_stack) < start_depth:
                reason = "step_out"
                break
        self._log_run_audit(
            mode="step_out",
            steps=len(steps),
            cycles_executed=max(0, self.cycles - start_cycles),
            elapsed_wall_time_sec=max(0.0, time.perf_counter() - start_time),
        )
        return RunResult(halted=self.halted, reason=reason, steps=steps)

    @abstractmethod
    def reset(self, *, hard: bool = False) -> None:
        raise NotImplementedError

    @abstractmethod
    def load_program(self, program: ProgramImage) -> None:
        raise NotImplementedError

    @abstractmethod
    def _current_opcode(self) -> int:
        raise NotImplementedError

    @abstractmethod
    def _instruction_length_preview(self, opcode: int) -> int:
        raise NotImplementedError

    @abstractmethod
    def _is_call_opcode(self, opcode: int) -> bool:
        raise NotImplementedError

    @abstractmethod
    def _step_impl(self) -> TraceEntry:
        raise NotImplementedError

    def _step_impl_compact(self) -> TraceEntry:
        return self._step_impl()

    @abstractmethod
    def snapshot(self) -> dict:
        raise NotImplementedError

    @abstractmethod
    def serialize_state(self) -> dict:
        raise NotImplementedError

    @abstractmethod
    def load_state(self, state: dict) -> None:
        raise NotImplementedError

    @abstractmethod
    def _restore_register_values(self, values: dict[str, int]) -> None:
        raise NotImplementedError

    def _capture_extra_state(self) -> dict:
        return {}

    def _restore_extra_state(self, state: dict) -> None:
        _ = state

    def _step_with_history(self) -> TraceEntry:
        before = {
            "cycles": self.cycles,
            "halted": self.halted,
            "last_error": self.last_error,
            "last_interrupt": self.last_interrupt,
            "call_stack": self.debugger.call_stack[:],
            "extra": self._capture_extra_state(),
        }
        trace = self._step_impl()
        after = {
            "cycles": self.cycles,
            "halted": self.halted,
            "last_error": self.last_error,
            "last_interrupt": self.last_interrupt,
            "call_stack": self.debugger.call_stack[:],
            "extra": self._capture_extra_state(),
        }
        self.debugger.history.append(
            ReverseDelta(
                trace=trace,
                cycles_before=int(before["cycles"]),
                cycles_after=int(after["cycles"]),
                halted_before=bool(before["halted"]),
                halted_after=bool(after["halted"]),
                last_error_before=before["last_error"],
                last_error_after=after["last_error"],
                last_interrupt_before=before["last_interrupt"],
                last_interrupt_after=after["last_interrupt"],
                call_stack_before=list(before["call_stack"]),
                call_stack_after=list(after["call_stack"]),
                extra_before=dict(before["extra"]),
                extra_after=dict(after["extra"]),
            )
        )
        if _DEBUG_TIMING:
            print(
                "[DEBUG_TIMING][instruction]",
                {
                    "instruction": trace.mnemonic,
                    "pc": trace.pc,
                    "cycles_before": int(before["cycles"]),
                    "cycles_after": int(after["cycles"]),
                },
            )
        return trace

    def _restore_reverse_delta(self, delta: ReverseDelta) -> None:
        register_values = {
            name: int(change["before"])
            for name, change in delta.trace.register_diff.items()
        }
        if register_values:
            self._restore_register_values(register_values)
        self._restore_memory_changes(delta.trace.changes)
        self.cycles = int(delta.cycles_before)
        self.halted = bool(delta.halted_before)
        self.last_error = delta.last_error_before
        self.last_interrupt = delta.last_interrupt_before
        self.debugger.call_stack = delta.call_stack_before[:]
        self._restore_extra_state(delta.extra_before)
        memory = getattr(self, "memory", None)
        if memory is not None and hasattr(memory, "consume_changes"):
            memory.consume_changes()

    def _restore_memory_changes(self, changes: dict[str, list[tuple[int, int, int]]]) -> None:
        memory = getattr(self, "memory", None)
        if memory is None:
            return
        for space, entries in changes.items():
            for address, old_value, _new_value in reversed(entries):
                if space in {"iram", "sfr"}:
                    memory.write_direct(int(address), int(old_value))
                elif space == "xram":
                    memory.write_xram(int(address), int(old_value))
                elif space == "code":
                    memory.write8(int(address), int(old_value), space="code")

    def _active_breakpoint(self) -> bool:
        breakpoint = self.debugger.breakpoints.get(self.pc)
        if breakpoint is None or not breakpoint.enabled:
            return False
        if not breakpoint.condition:
            return True
        return self._evaluate_condition(breakpoint.condition, self._debug_context())

    def _debug_context(self) -> dict[str, int | bool]:
        snapshot = self.snapshot()
        context: dict[str, int | bool] = {"PC": int(self.pc), "CYCLES": int(self.cycles), "HALTED": bool(self.halted)}
        for name, value in dict(snapshot.get("registers", {})).items():
            context[str(name).upper()] = int(value)
        for name, value in dict(snapshot.get("flags", {})).items():
            context[str(name).upper()] = bool(value)
        return context

    def _evaluate_condition(self, expression: str, context: dict[str, int | bool]) -> bool:
        tree = ast.parse(expression, mode="eval")

        def _eval(node):
            if isinstance(node, ast.Expression):
                return _eval(node.body)
            if isinstance(node, ast.Constant):
                return node.value
            if isinstance(node, ast.Name):
                key = node.id.upper()
                if key not in context:
                    raise ValidationError("Unknown breakpoint symbol", context={"symbol": node.id})
                return context[key]
            if isinstance(node, ast.UnaryOp):
                operand = _eval(node.operand)
                if isinstance(node.op, ast.Not):
                    return not bool(operand)
                if isinstance(node.op, ast.USub):
                    return -int(operand)
                if isinstance(node.op, ast.Invert):
                    return ~int(operand)
            if isinstance(node, ast.BoolOp):
                values = [_eval(value) for value in node.values]
                if isinstance(node.op, ast.And):
                    return all(bool(value) for value in values)
                if isinstance(node.op, ast.Or):
                    return any(bool(value) for value in values)
            if isinstance(node, ast.BinOp):
                left = _eval(node.left)
                right = _eval(node.right)
                if isinstance(node.op, ast.Add):
                    return int(left) + int(right)
                if isinstance(node.op, ast.Sub):
                    return int(left) - int(right)
                if isinstance(node.op, ast.BitAnd):
                    return int(left) & int(right)
                if isinstance(node.op, ast.BitOr):
                    return int(left) | int(right)
                if isinstance(node.op, ast.BitXor):
                    return int(left) ^ int(right)
                if isinstance(node.op, ast.LShift):
                    return int(left) << int(right)
                if isinstance(node.op, ast.RShift):
                    return int(left) >> int(right)
            if isinstance(node, ast.Compare):
                left = _eval(node.left)
                for operator, comparator in zip(node.ops, node.comparators):
                    right = _eval(comparator)
                    if isinstance(operator, ast.Eq):
                        matched = left == right
                    elif isinstance(operator, ast.NotEq):
                        matched = left != right
                    elif isinstance(operator, ast.Lt):
                        matched = int(left) < int(right)
                    elif isinstance(operator, ast.LtE):
                        matched = int(left) <= int(right)
                    elif isinstance(operator, ast.Gt):
                        matched = int(left) > int(right)
                    elif isinstance(operator, ast.GtE):
                        matched = int(left) >= int(right)
                    else:
                        raise ValidationError("Unsupported breakpoint comparison", context={"expression": expression})
                    if not matched:
                        return False
                    left = right
                return True
            raise ValidationError("Unsupported breakpoint condition", context={"expression": expression})

        return bool(_eval(tree))
