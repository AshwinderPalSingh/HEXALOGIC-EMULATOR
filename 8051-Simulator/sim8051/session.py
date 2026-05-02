from __future__ import annotations

import json
import math
import os
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Protocol

from .exceptions import AssemblyError, ExecutionError
from .factory import architecture_metadata, create_assembler, create_cpu, normalize_architecture
from .hardware import VirtualHardwareManager, apply_hardware_inputs
from .model import ProgramImage, ReverseDelta, RunResult, SourceLocation, Watchpoint
from .version import API_VERSION, CPU_MODEL_VERSIONS, SESSION_FORMAT_VERSION

try:  # pragma: no cover - optional dependency
    import redis as redis_module
except Exception:  # pragma: no cover - optional dependency
    redis_module = None


_DEBUG_TIMING = os.environ.get("HEXLOGIC_DEBUG_TIMING", "").strip().lower() in {"1", "true", "yes", "on"}
_MAX_RETURNED_RUN_STEPS = 256
_REALTIME_RUN_SLICE_SECONDS = 0.1
_REALTIME_COMPACT_STEP_BUDGET = 2_000_000
_REALTIME_COMPACT_CYCLE_CAP = 8_000_000


def _program_to_dict(program: ProgramImage | None) -> dict[str, Any] | None:
    if program is None:
        return None
    return {
        "session_format_version": SESSION_FORMAT_VERSION,
        "origin": program.origin,
        "rom_hex": program.rom.hex(),
        "binary_hex": program.binary.hex(),
        "intel_hex": program.intel_hex,
        "listing": [
            {
                "line": row.line,
                "text": row.text,
                "address": row.address,
                "size": row.size,
                "bytes": row.bytes_,
            }
            for row in program.listing
        ],
        "address_to_line": {str(address): line for address, line in program.address_to_line.items()},
        "labels": program.labels,
        "size": program.size,
        "xram_init": {str(address): value for address, value in program.xram_init.items()},
    }


def _program_from_dict(data: dict[str, Any] | None) -> ProgramImage | None:
    if not data:
        return None
    listing = [
        SourceLocation(
            line=int(item["line"]),
            text=str(item["text"]),
            address=int(item["address"]),
            size=int(item["size"]),
            bytes_=[int(value) & 0xFF for value in item.get("bytes", [])],
        )
        for item in data.get("listing", [])
    ]
    rom = bytearray.fromhex(str(data.get("rom_hex", "")))
    return ProgramImage(
        origin=int(data.get("origin", 0)),
        rom=rom,
        binary=bytes.fromhex(str(data.get("binary_hex", ""))),
        intel_hex=str(data.get("intel_hex", ":00000001FF")),
        listing=listing,
        address_to_line={int(address): int(line) for address, line in dict(data.get("address_to_line", {})).items()},
        labels={str(label): int(value) for label, value in dict(data.get("labels", {})).items()},
        size=int(data.get("size", 0)),
        xram_init={int(address): int(value) & 0xFF for address, value in dict(data.get("xram_init", {})).items()},
    )


@dataclass
class SimulatorSession:
    session_id: str
    architecture: str = "8051"
    endian: str = "little"
    execution_mode: str = "realtime"
    code_size: int = 0x1000
    xram_size: int = 0x10000
    upper_iram: bool = False
    debug_mode: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    assembler: Any = field(init=False, default=None)
    cpu: Any = field(init=False)
    hardware: Any = field(init=False)
    _hardware_input_cache: dict[str, int | None] | None = field(init=False, default=None)
    source_code: str = field(init=False, default="")
    program: ProgramImage | None = field(init=False, default=None)
    _live_hardware_payload: dict[str, Any] | None = field(init=False, default=None)
    _live_hardware_diff: dict[str, Any] | None = field(init=False, default=None)
    _simulated_time_sec: float = field(init=False, default=0.0)
    _target_sim_time_sec: float = field(init=False, default=0.0)
    _last_wall_time_sec: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        self.architecture = normalize_architecture(self.architecture)
        self.endian = self.endian if self.endian in {"little", "big"} else "little"
        self.assembler = self._build_assembler()
        self.cpu = self._build_cpu()
        self.cpu.set_execution_mode(self.execution_mode)
        self.cpu.set_debug_mode(self.debug_mode)
        self.source_code = ""
        self.program = None
        self.hardware = VirtualHardwareManager(self.architecture)
        self._reinitialize_realtime_state()

    def _hardware_sync_payload(self) -> dict[str, Any]:
        hardware_snapshot = getattr(self.cpu, "hardware_snapshot", None)
        if callable(hardware_snapshot):
            return hardware_snapshot()
        return self.cpu.snapshot()

    def _hardware_tick_payload(self) -> dict[str, Any]:
        hardware_tick_snapshot = getattr(self.cpu, "hardware_tick_snapshot", None)
        if callable(hardware_tick_snapshot):
            return hardware_tick_snapshot()
        return self._hardware_sync_payload()

    def _runtime_payload(self) -> dict[str, Any]:
        runtime_snapshot = getattr(self.cpu, "runtime_snapshot", None)
        if callable(runtime_snapshot):
            return runtime_snapshot()
        return self.cpu.snapshot()

    def _signal_runtime_payload(self) -> dict[str, Any]:
        signal_runtime_snapshot = getattr(self.cpu, "signal_runtime_snapshot", None)
        if callable(signal_runtime_snapshot):
            return signal_runtime_snapshot()
        return self._runtime_payload()

    def _sync_hardware_after_instruction(self, _trace=None) -> None:
        payload = self._hardware_tick_payload()
        if not payload:
            return
        self.hardware.tick(payload)
        self._live_hardware_payload = None
        self._live_hardware_diff = None

    def _effective_execution_hz(self) -> float:
        return max(1.0, float(self.cpu.effective_clock_hz()))

    def _cycles_to_simulated_seconds(self, cycles: int) -> float:
        return max(0, int(cycles)) / self._effective_execution_hz()

    def _reinitialize_realtime_state(self) -> None:
        self._simulated_time_sec = self._cycles_to_simulated_seconds(int(self.cpu.cycles))
        self._target_sim_time_sec = self._simulated_time_sec
        self._last_wall_time_sec = time.perf_counter()

    def _align_realtime_state(self) -> None:
        self._target_sim_time_sec = self._simulated_time_sec
        self._last_wall_time_sec = time.perf_counter()

    def _advance_realtime_target(self) -> float:
        now = time.perf_counter()
        if not math.isfinite(self._last_wall_time_sec) or self._last_wall_time_sec <= 0:
            self._last_wall_time_sec = now
        elapsed = max(0.0, now - self._last_wall_time_sec)
        self._last_wall_time_sec = now
        self._target_sim_time_sec += elapsed
        return elapsed

    def _run_realtime(self, *, max_steps: int) -> RunResult:
        steps: deque[Any] = deque(maxlen=_MAX_RETURNED_RUN_STEPS)
        reason = "max_steps"
        start_cycles = int(self.cpu.cycles)
        deadline = time.perf_counter() + min(self.cpu.max_run_seconds, _REALTIME_RUN_SLICE_SECONDS)
        effective_hz = self._effective_execution_hz()
        step_count = 0
        register_diff: dict[str, dict[str, int]] = {}
        memory_changes: dict[str, list[tuple[int, int, int]]] = {}
        interrupts: list[str] = []
        compact_mode = self.cpu.compact_execution_allowed()
        compact_payload_mode = compact_mode and callable(getattr(self.cpu, "step_compact_payload", None))
        runtime_before = dict(self._runtime_payload().get("registers", {})) if compact_mode else {}
        has_breakpoints = bool(getattr(self.cpu.debugger, "breakpoints", {}))
        step_budget = max_steps if not compact_mode else max(int(max_steps), _REALTIME_COMPACT_STEP_BUDGET)
        cycle_cap = int(self.cpu.max_cycles_per_request)
        if compact_mode:
            realtime_cycles = int(math.ceil(effective_hz * _REALTIME_RUN_SLICE_SECONDS * 1.5))
            cycle_cap = max(cycle_cap, min(_REALTIME_COMPACT_CYCLE_CAP, realtime_cycles))
        while self._simulated_time_sec < self._target_sim_time_sec:
            if step_count >= step_budget:
                reason = "max_steps"
                break
            if self.cpu.halted:
                reason = "halted"
                break
            if (int(self.cpu.cycles) - start_cycles) >= cycle_cap:
                reason = "cycle_cap"
                break
            if (step_count & 0x3F) == 0 and time.perf_counter() >= deadline:
                reason = "timeout"
                break
            if has_breakpoints and self.cpu._active_breakpoint():
                reason = "breakpoint"
                break
            if compact_mode:
                remaining_steps = step_budget - step_count
                remaining_cycles = min(
                    cycle_cap - max(0, int(self.cpu.cycles) - start_cycles),
                    max(0, int(math.ceil((self._target_sim_time_sec - self._simulated_time_sec) * effective_hz))),
                )
                pc_before = int(getattr(self.cpu, "pc", 0) or 0)
                fast_slice = self.cpu.try_fast_realtime_slice(max_steps=remaining_steps, max_cycles=remaining_cycles)
                if fast_slice:
                    fast_steps = int(fast_slice.get("steps", 0) or 0)
                    step_count += fast_steps
                    fast_cycles = int(fast_slice.get("cycles", 0) or 0)
                    self._simulated_time_sec += max(0.0, float(fast_cycles) / effective_hz)
                    for space, changes in dict(fast_slice.get("memory_changes", {})).items():
                        memory_changes.setdefault(space, []).extend(changes)
                    interrupts.extend(str(item) for item in list(fast_slice.get("interrupts", [])))
                    steps_payloads = list(fast_slice.get("steps_payloads", []) or [])
                    if not steps_payloads and fast_steps > 0:
                        steps_payloads = [
                            {
                                "pc": pc_before,
                                "opcode": 0,
                                "mnemonic": f"FAST_SLICE x{fast_steps}",
                                "bytes": [],
                                "cycles": fast_cycles,
                                "line": None,
                                "text": None,
                                "changes": dict(fast_slice.get("memory_changes", {}) or {}),
                                "register_diff": {},
                                "interrupt": None,
                            }
                        ]
                    for item in steps_payloads:
                        if not item:
                            continue
                        steps.append(item)
                    if fast_slice.get("hardware_sync"):
                        self._sync_hardware_after_instruction(None)
                    if self.cpu.halted:
                        reason = "halted"
                        break
                    continue
            trace = self.cpu.step_compact_payload() if compact_payload_mode else (self.cpu.step_compact() if compact_mode else self.cpu.step_into())
            step_count += 1
            steps.append(trace)
            trace_register_diff = trace.get("register_diff", {}) if isinstance(trace, dict) else trace.register_diff
            trace_changes = trace.get("changes", {}) if isinstance(trace, dict) else trace.changes
            trace_interrupt = trace.get("interrupt") if isinstance(trace, dict) else trace.interrupt
            trace_cycles = int(trace.get("cycles", 0)) if isinstance(trace, dict) else int(trace.cycles)
            if trace_register_diff:
                register_diff.update(trace_register_diff)
            if trace_changes:
                for space, changes in trace_changes.items():
                    memory_changes.setdefault(space, []).extend(changes)
            if trace_interrupt:
                interrupts.append(trace_interrupt)
            self._sync_hardware_after_instruction(trace)
            self._simulated_time_sec += max(0.0, float(trace_cycles) / effective_hz)
            if not compact_mode and self.cpu._check_watchpoints(trace):
                reason = "watchpoint"
                break
            if self.cpu.halted:
                reason = "halted"
                break
        if compact_mode:
            runtime_after = dict(self._runtime_payload().get("registers", {}))
            register_diff = {
                name: {"before": int(runtime_before.get(name, 0)), "after": int(value)}
                for name, value in runtime_after.items()
                if runtime_before.get(name) != value
            }
        return RunResult(
            halted=self.cpu.halted,
            reason=reason,
            steps=list(steps),
            step_count=step_count,
            register_diff=register_diff,
            memory_changes=memory_changes,
            interrupts=interrupts,
        )

    def _cpu_kwargs(self) -> dict[str, Any]:
        if self.architecture == "8051":
            return {"code_size": self.code_size, "xram_size": self.xram_size, "upper_iram": self.upper_iram}
        return {"code_size": self.code_size, "data_size": self.xram_size, "endian": self.endian}

    def _assembler_kwargs(self) -> dict[str, Any]:
        if self.architecture == "8051":
            return {"code_size": self.code_size}
        return {"code_size": self.code_size, "endian": self.endian}

    def _build_cpu(self):
        return create_cpu(self.architecture, **self._cpu_kwargs())

    def _build_assembler(self):
        return create_assembler(self.architecture, **self._assembler_kwargs())

    def touch(self) -> None:
        self.updated_at = time.time()

    def set_architecture(self, architecture: str) -> dict:
        normalized = normalize_architecture(architecture)
        if normalized == self.architecture:
            return self.snapshot(include_program=True)
        self.architecture = normalized
        self.endian = "little"
        self.assembler = self._build_assembler()
        self.cpu = self._build_cpu()
        self.cpu.set_execution_mode(self.execution_mode)
        self.cpu.set_debug_mode(self.debug_mode)
        self.source_code = ""
        self.program = None
        self.hardware.reset_for_architecture(normalized)
        self._hardware_input_cache = None
        self._live_hardware_payload = None
        self._live_hardware_diff = None
        self._reinitialize_realtime_state()
        self.touch()
        return self.snapshot(include_program=True)

    def set_endian(self, endian: str) -> dict:
        normalized = (endian or "little").lower()
        if normalized not in {"little", "big"}:
            raise ExecutionError(f"Unsupported endian `{endian}`")
        if self.architecture != "arm":
            self.endian = "little"
            return self.snapshot(include_program=True)
        if normalized == self.endian:
            return self.snapshot(include_program=True)
        self.endian = normalized
        self.assembler = self._build_assembler()
        self.cpu = self._build_cpu()
        self.cpu.set_execution_mode(self.execution_mode)
        self.cpu.set_debug_mode(self.debug_mode)
        if self.source_code:
            self.program = self.assembler.assemble(self.source_code)
            self.cpu.load_program(self.program)
        else:
            self.program = None
        self._hardware_input_cache = None
        self._prime_hardware_inputs()
        self._live_hardware_payload = None
        self._live_hardware_diff = None
        self._reinitialize_realtime_state()
        self.touch()
        return self.snapshot(include_program=True)

    def set_debug_mode(self, enabled: bool) -> dict:
        self.debug_mode = bool(enabled)
        self.cpu.set_debug_mode(self.debug_mode)
        self.touch()
        return self.snapshot(include_program=True)

    def set_execution_mode(self, mode: str) -> dict:
        self.execution_mode = str(mode or "realtime").lower()
        self.cpu.set_execution_mode(self.execution_mode)
        self._align_realtime_state()
        self.touch()
        return self.snapshot(include_program=True)

    def step_back(self) -> dict:
        delta = self.cpu.step_back()
        self.touch()
        self._hardware_input_cache = None
        if delta is None:
            return {"reverted": None, "diff": {"registers": {}, "memory": {}}, "state": self.snapshot(), "reason": "history_empty"}
        self._live_hardware_payload = None
        self._live_hardware_diff = None
        self._reinitialize_realtime_state()
        state, hardware_diff = self._snapshot_payload()
        return {
            "reverted": self._trace_to_dict(delta.trace),
            "diff": {**self._reverse_diff(delta), "hardware": hardware_diff},
            "state": state,
            "reason": "step_back",
        }

    def assemble(self, source_code: str) -> dict:
        self.source_code = source_code
        self.program = self.assembler.assemble(source_code)
        self.cpu.load_program(self.program)
        self.cpu.set_debug_mode(self.debug_mode)
        self._hardware_input_cache = None
        self._prime_hardware_inputs()
        self._live_hardware_payload = None
        self._live_hardware_diff = None
        self._reinitialize_realtime_state()
        self.touch()
        return self.snapshot(include_program=True)

    def reset(self) -> dict:
        if self.program is not None:
            self.cpu.load_program(self.program)
        else:
            self.cpu.reset(hard=True)
        self.cpu.set_debug_mode(self.debug_mode)
        self._hardware_input_cache = None
        self._prime_hardware_inputs()
        self._live_hardware_payload = None
        self._live_hardware_diff = None
        self._reinitialize_realtime_state()
        self.touch()
        return self.snapshot(include_program=True)

    def _prime_hardware_inputs(self) -> None:
        self._hardware_input_cache = apply_hardware_inputs(self, self.hardware, self._hardware_input_cache)

    def step(self) -> dict:
        self._prime_hardware_inputs()
        if self.cpu.halted and self.program is not None:
            self.cpu.load_program(self.program)
            self.cpu.set_debug_mode(self.debug_mode)
            self._hardware_input_cache = None
            self._prime_hardware_inputs()
            self._live_hardware_payload = None
            self._live_hardware_diff = None
            self._reinitialize_realtime_state()
        start_cycles = int(self.cpu.cycles)
        trace = self.cpu.step()
        self._sync_hardware_after_instruction(trace)
        self._simulated_time_sec += self._cycles_to_simulated_seconds(int(self.cpu.cycles) - start_cycles)
        self._align_realtime_state()
        self.touch()
        trace_payload = self._trace_to_dict(trace)
        state, hardware_diff = self._snapshot_payload(compact=True, use_live_hardware=(self.execution_mode == "realtime"))
        return {"trace": trace_payload, "diff": {**self._trace_diff(trace_payload), "hardware": hardware_diff}, "state": state}

    def run(self, max_steps: int = 1000, *, speed_multiplier: float | None = None) -> dict:
        self._prime_hardware_inputs()
        if speed_multiplier is not None:
            self.cpu.set_speed_multiplier(speed_multiplier)
        start_cycles = int(self.cpu.cycles)
        started = time.perf_counter()
        audit_elapsed_seconds = 0.0
        if self.execution_mode == "realtime":
            audit_elapsed_seconds = self._advance_realtime_target()
            result = self._run_realtime(max_steps=max_steps)
        else:
            result = self.cpu.run(max_steps=max_steps, after_step=self._sync_hardware_after_instruction)
            self._simulated_time_sec += self._cycles_to_simulated_seconds(int(self.cpu.cycles) - start_cycles)
        elapsed = time.perf_counter() - started
        if self.execution_mode == "realtime":
            self._last_wall_time_sec = time.perf_counter()
        else:
            self._align_realtime_state()
        if self.execution_mode != "realtime":
            audit_elapsed_seconds = elapsed
        executed_cycles = max(0, int(self.cpu.cycles) - start_cycles)
        self.cpu._log_run_audit(
            mode=f"session_{self.execution_mode}",
            steps=int(result.step_count or len(result.steps)),
            cycles_executed=executed_cycles,
            elapsed_wall_time_sec=audit_elapsed_seconds,
        )
        if _DEBUG_TIMING:
            hz = self._effective_execution_hz()
            computed_seconds = executed_cycles / hz
            computed_ms = computed_seconds * 1000.0
            cycles_per_second = (executed_cycles / audit_elapsed_seconds) if audit_elapsed_seconds > 0 else 0.0
            instructions_per_second = (len(result.steps) / audit_elapsed_seconds) if audit_elapsed_seconds > 0 else 0.0
            print(
                "[DEBUG_TIMING][session.run]",
                {
                    "architecture": self.architecture,
                    "instructions_executed": len(result.steps),
                    "cycles_executed": executed_cycles,
                    "elapsed_wall_time_sec": round(audit_elapsed_seconds, 6),
                    "request_elapsed_wall_time_sec": round(elapsed, 6),
                    "computed_seconds": round(computed_seconds, 9),
                    "computed_ms": round(computed_ms, 6),
                    "effective_hz_expected": round(hz, 3),
                    "cycles_per_sec_actual": round(cycles_per_second, 3),
                    "instructions_per_sec": round(instructions_per_second, 3),
                    "target_sim_time_sec": self._target_sim_time_sec,
                    "simulated_time_sec": self._simulated_time_sec,
                    "expected_hz": round(hz, 3),
                },
            )
        self.touch()
        state, hardware_diff = self._snapshot_payload(compact=True)
        return {
            "result": self._run_result_to_dict(result),
            "diff": {**self._run_diff(result), "hardware": hardware_diff},
            "metrics": self._run_metrics(result, elapsed, audit_elapsed_seconds=audit_elapsed_seconds, cycles_executed=executed_cycles),
            "state": state,
        }

    def step_over(self) -> dict:
        self._prime_hardware_inputs()
        start_cycles = int(self.cpu.cycles)
        started = time.perf_counter()
        result = self.cpu.step_over(after_step=self._sync_hardware_after_instruction)
        self._simulated_time_sec += self._cycles_to_simulated_seconds(int(self.cpu.cycles) - start_cycles)
        self._align_realtime_state()
        elapsed = time.perf_counter() - started
        self.touch()
        state, hardware_diff = self._snapshot_payload(compact=True)
        return {
            "result": self._run_result_to_dict(result),
            "diff": {**self._run_diff(result), "hardware": hardware_diff},
            "metrics": self._run_metrics(result, elapsed, cycles_executed=max(0, int(self.cpu.cycles) - start_cycles)),
            "state": state,
        }

    def step_out(self) -> dict:
        self._prime_hardware_inputs()
        start_cycles = int(self.cpu.cycles)
        started = time.perf_counter()
        result = self.cpu.step_out(after_step=self._sync_hardware_after_instruction)
        self._simulated_time_sec += self._cycles_to_simulated_seconds(int(self.cpu.cycles) - start_cycles)
        self._align_realtime_state()
        elapsed = time.perf_counter() - started
        self.touch()
        state, hardware_diff = self._snapshot_payload(compact=True)
        return {
            "result": self._run_result_to_dict(result),
            "diff": {**self._run_diff(result), "hardware": hardware_diff},
            "metrics": self._run_metrics(result, elapsed, cycles_executed=max(0, int(self.cpu.cycles) - start_cycles)),
            "state": state,
        }

    def set_breakpoints(self, pcs: list[int]) -> dict:
        self.cpu.set_breakpoints(pcs)
        self.touch()
        return self.snapshot()

    def set_watchpoints(self, items: list[dict]) -> dict:
        watchpoints = []
        for item in items:
            space = str(item.get("space", "iram"))
            raw_target = item.get("target", item.get("address", item.get("register")))
            target = str(raw_target).upper() if space == "register" else int(raw_target)
            watchpoints.append(
                Watchpoint(
                    target=target,
                    space=space,
                    enabled=bool(item.get("enabled", True)),
                )
            )
        self.cpu.set_watchpoints(watchpoints)
        self.touch()
        return self.snapshot()

    def inject_pin(self, port: int, bit: int, level: int | bool | None) -> dict:
        if hasattr(self.cpu, "set_pin"):
            self.cpu.set_pin(port, bit, level)
        self.touch()
        return self.snapshot()

    def inject_serial_rx(self, bytes_in: list[int]) -> dict:
        if hasattr(self.cpu, "inject_serial_rx"):
            self.cpu.inject_serial_rx(bytes_in)
        self.touch()
        return self.snapshot()

    def edit_memory(self, *, space: str, address: int, value: int) -> dict:
        if space == "iram":
            self.cpu.memory.write_direct(address, value)
        elif space == "sfr":
            self.cpu.memory.write_direct(address, value)
        elif space == "xram":
            self.cpu.memory.write_xram(address, value)
        elif space == "code":
            self.cpu.memory.write8(address, value, space="code")
        else:
            raise ExecutionError(f"Unsupported memory space `{space}`")
        self.touch()
        return self.snapshot()

    def set_clock(self, hz: int) -> dict:
        self.cpu.set_clock_hz(hz)
        self._live_hardware_payload = None
        self._live_hardware_diff = None
        self._reinitialize_realtime_state()
        self.touch()
        return self.snapshot()

    def run_hardware_test(self) -> dict[str, Any]:
        self._prime_hardware_inputs()
        result = self.hardware.run_test_suite(self.cpu.snapshot())
        self.touch()
        state, hardware_diff = self._snapshot_payload()
        return {"hardware_test": result, "diff": {"hardware": hardware_diff}, "state": state}

    def _snapshot_payload(self, *, include_program: bool = False, compact: bool = False, use_live_hardware: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
        payload = self._runtime_payload() if compact else self.cpu.snapshot()
        hw_diff = self._live_hardware_diff
        hw_full = self._live_hardware_payload.get("hardware") if self._live_hardware_payload is not None else None
        if hw_full is None:
            if use_live_hardware and self.hardware.has_live_state():
                hw_full = self.hardware.current_payload()
                hw_diff = self.hardware.current_diff()
            if hw_full is None:
                hw_full, hw_diff = self.hardware.sync(self._hardware_sync_payload())
        payload["hardware"] = hw_full
        payload.update(
            {
                "session_id": self.session_id,
                "architecture": self.architecture,
                "endian": self.endian,
                "execution_mode": self.execution_mode,
                "debug_mode": self.debug_mode,
                "simulated_time_sec": round(self._simulated_time_sec, 9),
                "target_sim_time_sec": round(self._target_sim_time_sec, 9),
                "has_program": self.program is not None,
                "source_code": self.source_code,
                "session_format_version": SESSION_FORMAT_VERSION,
                "api_version": API_VERSION,
                "cpu_model_version": CPU_MODEL_VERSIONS.get(self.architecture, "unknown"),
                "supported_architectures": architecture_metadata(),
            }
        )
        if include_program and self.program is not None:
            payload["program"] = {
                "origin": self.program.origin,
                "size": self.program.size,
                "intel_hex": self.program.intel_hex,
                "binary_hex": self.program.binary.hex(),
                "labels": self.program.labels,
                "listing": [
                    {
                        "line": row.line,
                        "text": row.text,
                        "address": row.address,
                        "size": row.size,
                        "bytes": row.bytes_,
                    }
                    for row in self.program.listing
                ],
            }
        self._live_hardware_payload = None
        self._live_hardware_diff = None
        return payload, hw_diff or {}

    def snapshot(self, *, include_program: bool = False) -> dict:
        self._align_realtime_state()
        payload, _ = self._snapshot_payload(include_program=include_program)
        return payload

    def runtime_state(self) -> dict[str, Any]:
        state, hardware_diff = self._snapshot_payload(compact=True)
        return {"state": state, "diff": {"hardware": hardware_diff}}

    def signal_event_state(self) -> dict[str, Any]:
        runtime = self._signal_runtime_payload()
        state = {
            "cycles": runtime.get("cycles"),
            "halted": runtime.get("halted"),
            "last_error": runtime.get("last_error"),
            "last_interrupt": runtime.get("last_interrupt"),
            "registers": {"PC": dict(runtime.get("registers", {})).get("PC", self.cpu.pc)},
        }
        return {"state": state, "hardware": self.hardware.consume_signal_events()}

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_format_version": SESSION_FORMAT_VERSION,
            "api_version": API_VERSION,
            "session_id": self.session_id,
            "architecture": self.architecture,
            "endian": self.endian,
            "execution_mode": self.execution_mode,
            "code_size": self.code_size,
            "xram_size": self.xram_size,
            "upper_iram": self.upper_iram,
            "debug_mode": self.debug_mode,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source_code": self.source_code,
            "program": _program_to_dict(self.program),
            "cpu": self.cpu.serialize_state(),
            "hardware": self.hardware.export_state(),
            "runtime": {
                "simulated_time_sec": self._simulated_time_sec,
                "target_sim_time_sec": self._target_sim_time_sec,
                "last_wall_time_sec": self._last_wall_time_sec,
            },
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SimulatorSession":
        session = cls(
            session_id=str(payload["session_id"]),
            architecture=str(payload.get("architecture", "8051")),
            endian=str(payload.get("endian", "little")),
            execution_mode=str(payload.get("execution_mode", "realtime")),
            code_size=int(payload.get("code_size", 0x1000)),
            xram_size=int(payload.get("xram_size", 0x10000)),
            upper_iram=bool(payload.get("upper_iram", False)),
            debug_mode=bool(payload.get("debug_mode", False)),
            created_at=float(payload.get("created_at", time.time())),
            updated_at=float(payload.get("updated_at", time.time())),
        )
        session.source_code = str(payload.get("source_code", ""))
        session.program = _program_from_dict(payload.get("program"))
        if session.program is not None:
            session.cpu.program = session.program
        session.cpu.load_state(dict(payload.get("cpu", {})))
        session.cpu.set_execution_mode(session.execution_mode)
        session.cpu.set_debug_mode(session.debug_mode)
        hw = payload.get("hardware")
        if isinstance(hw, dict):
            session.hardware.import_state(hw)
        session._hardware_input_cache = None
        session._prime_hardware_inputs()
        runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
        session._simulated_time_sec = float(runtime.get("simulated_time_sec", session._cycles_to_simulated_seconds(int(session.cpu.cycles))))
        session._target_sim_time_sec = float(runtime.get("target_sim_time_sec", session._simulated_time_sec))
        session._last_wall_time_sec = float(runtime.get("last_wall_time_sec", time.perf_counter()))
        session._align_realtime_state()
        return session

    def export_state(self) -> dict[str, Any]:
        return self.to_dict()

    @classmethod
    def import_state(cls, payload: dict[str, Any]) -> "SimulatorSession":
        return cls.from_dict(payload)

    def serialized_size(self) -> int:
        return len(json.dumps(self.to_dict(), separators=(",", ":")).encode("utf-8"))

    def _trace_to_dict(self, trace) -> dict:
        def _as_int(value: Any, default: int = 0) -> int:
            try:
                if value is None:
                    return default
                return int(value)
            except (TypeError, ValueError):
                return default

        def _as_str(value: Any, default: str = "UNKNOWN") -> str:
            if value is None:
                return default
            text = str(value)
            return text if text else default

        if not trace:
            return {
                "pc": 0,
                "opcode": 0,
                "mnemonic": "UNKNOWN",
                "bytes": [],
                "cycles": 0,
                "line": None,
                "text": None,
                "changes": {},
                "register_diff": {},
                "interrupt": None,
            }

        if isinstance(trace, dict):
            raw_bytes = trace.get("bytes", [])
            if isinstance(raw_bytes, (bytes, bytearray)):
                bytes_list = list(raw_bytes)
            elif isinstance(raw_bytes, list):
                bytes_list = raw_bytes
            else:
                bytes_list = []
            changes = trace.get("changes", {})
            if not isinstance(changes, dict):
                changes = {}
            register_diff = trace.get("register_diff", {})
            if not isinstance(register_diff, dict):
                register_diff = {}
            return {
                "pc": _as_int(trace.get("pc"), 0),
                "opcode": _as_int(trace.get("opcode"), 0),
                "mnemonic": _as_str(trace.get("mnemonic"), "UNKNOWN"),
                "bytes": [_as_int(b, 0) & 0xFF for b in bytes_list],
                "cycles": _as_int(trace.get("cycles"), 0),
                "line": trace.get("line"),
                "text": trace.get("text"),
                "changes": changes,
                "register_diff": register_diff,
                "interrupt": trace.get("interrupt"),
            }

        return {
            "pc": _as_int(getattr(trace, "pc", 0), 0),
            "opcode": _as_int(getattr(trace, "opcode", 0), 0),
            "mnemonic": _as_str(getattr(trace, "mnemonic", None), "UNKNOWN"),
            "bytes": [_as_int(b, 0) & 0xFF for b in list(getattr(trace, "bytes_", []) or [])],
            "cycles": _as_int(getattr(trace, "cycles", 0), 0),
            "line": getattr(trace, "line", None),
            "text": getattr(trace, "text", None),
            "changes": getattr(trace, "changes", {}) if isinstance(getattr(trace, "changes", {}), dict) else {},
            "register_diff": getattr(trace, "register_diff", {}) if isinstance(getattr(trace, "register_diff", {}), dict) else {},
            "interrupt": getattr(trace, "interrupt", None),
        }

    def _trace_diff(self, trace: dict) -> dict:
        return {
            "registers": trace.get("register_diff", {}),
            "memory": trace.get("changes", {}),
            "interrupt": trace.get("interrupt"),
        }

    def _run_result_to_dict(self, result: RunResult) -> dict:
        raw_steps = [step for step in list(result.steps) if step]
        step_count = int(result.step_count or len(raw_steps))
        dropped_steps = max(0, step_count - len(raw_steps))
        return {
            "halted": result.halted,
            "reason": result.reason,
            "step_count": step_count,
            "dropped_steps": dropped_steps,
            "steps": [self._trace_to_dict(step) for step in raw_steps],
        }

    def _run_diff(self, result: RunResult) -> dict[str, Any]:
        registers: dict[str, dict[str, int]] = dict(result.register_diff)
        memory: dict[str, list[tuple[int, int, int]]] = {
            space: list(changes) for space, changes in result.memory_changes.items()
        }
        interrupts: list[str] = list(result.interrupts)
        if not registers and not memory and not interrupts:
            for step in result.steps:
                if not step:
                    continue
                trace_register_diff = step.get("register_diff", {}) if isinstance(step, dict) else step.register_diff
                trace_changes = step.get("changes", {}) if isinstance(step, dict) else step.changes
                trace_interrupt = step.get("interrupt") if isinstance(step, dict) else step.interrupt
                registers.update(trace_register_diff)
                for space, changes in trace_changes.items():
                    memory.setdefault(space, []).extend(changes)
                if trace_interrupt:
                    interrupts.append(trace_interrupt)
        return {"registers": registers, "memory": memory, "interrupts": interrupts}

    def _run_metrics(
        self,
        result: RunResult,
        elapsed_seconds: float,
        *,
        audit_elapsed_seconds: float | None = None,
        cycles_executed: int,
    ) -> dict[str, float | int]:
        steps = int(result.step_count or len(result.steps))
        elapsed_ms = elapsed_seconds * 1000.0
        effective_elapsed = elapsed_seconds if audit_elapsed_seconds is None else max(0.0, float(audit_elapsed_seconds))
        effective_hz_expected = self._effective_execution_hz()
        computed_simulated_time_sec = cycles_executed / effective_hz_expected
        request_cycles_per_sec = (cycles_executed / elapsed_seconds) if elapsed_seconds > 0 else 0.0
        catch_up_ratio = (computed_simulated_time_sec / effective_elapsed) if effective_elapsed > 0 else 0.0
        return {
            "steps": steps,
            "elapsed_ms": round(elapsed_ms, 3),
            "steps_per_second": round((steps / elapsed_seconds), 3) if elapsed_seconds > 0 else 0.0,
            "cycles_executed": cycles_executed,
            "elapsed_wall_time_sec": round(effective_elapsed, 6),
            "request_elapsed_ms": round(elapsed_ms, 3),
            "computed_simulated_time_sec": round(computed_simulated_time_sec, 6),
            "effective_hz_expected": round(effective_hz_expected, 3),
            "cycles_per_sec_actual": round((cycles_executed / effective_elapsed), 3) if effective_elapsed > 0 else 0.0,
            "request_cycles_per_sec": round(request_cycles_per_sec, 3),
            "instructions_per_sec": round((steps / effective_elapsed), 3) if effective_elapsed > 0 else 0.0,
            "catch_up_ratio": round(catch_up_ratio, 3),
        }

    def _reverse_diff(self, delta: ReverseDelta) -> dict[str, Any]:
        registers = {
            name: {"before": change["after"], "after": change["before"]}
            for name, change in delta.trace.register_diff.items()
        }
        memory = {
            space: [(address, new_value, old_value) for address, old_value, new_value in changes]
            for space, changes in delta.trace.changes.items()
        }
        return {"registers": registers, "memory": memory, "interrupt": delta.last_interrupt_before}


class SessionBackend(Protocol):
    def get(self, session_id: str) -> SimulatorSession | None: ...
    def save(self, session: SimulatorSession) -> None: ...
    def delete(self, session_id: str) -> None: ...
    def cleanup(self, ttl_seconds: int) -> None: ...
    def count(self) -> int: ...
    def estimate_bytes(self) -> int: ...


class SessionStore:
    def __init__(self, *, ttl_seconds: int = 3600, backend: "SessionBackend | None" = None) -> None:
        self.ttl_seconds = ttl_seconds
        self.backend = backend or InMemorySessionBackend()

    def create(self, *, architecture: str = "8051") -> SimulatorSession:
        self.cleanup()
        session_id = secrets.token_urlsafe(24)
        session = SimulatorSession(session_id=session_id, architecture=architecture)
        self.save(session)
        return session

    def get(self, session_id: str | None, *, architecture: str = "8051") -> SimulatorSession:
        self.cleanup()
        if session_id:
            session = self.backend.get(session_id)
            if session is not None:
                session.touch()
                self.save(session)
                return session
        return self.create(architecture=architecture)

    def save(self, session: SimulatorSession) -> None:
        self.backend.save(session)

    def delete(self, session_id: str) -> None:
        self.backend.delete(session_id)

    def cleanup(self) -> None:
        self.backend.cleanup(self.ttl_seconds)

    def stats(self) -> dict[str, int]:
        return {"active_sessions": self.backend.count(), "estimated_bytes": self.backend.estimate_bytes()}


class InMemorySessionBackend:
    def __init__(self) -> None:
        self._sessions: dict[str, SimulatorSession] = {}
        self._lock = RLock()

    def get(self, session_id: str) -> SimulatorSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def save(self, session: SimulatorSession) -> None:
        with self._lock:
            self._sessions[session.session_id] = session

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def cleanup(self, ttl_seconds: int) -> None:
        now = time.time()
        with self._lock:
            expired = [
                session_id
                for session_id, session in self._sessions.items()
                if now - session.updated_at > ttl_seconds
            ]
            for session_id in expired:
                self._sessions.pop(session_id, None)

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def estimate_bytes(self) -> int:
        with self._lock:
            return sum(session.serialized_size() for session in self._sessions.values())


class RedisSessionBackend:
    def __init__(
        self,
        *,
        redis_url: str | None = None,
        ttl_seconds: int = 3600,
        fallback: InMemorySessionBackend | None = None,
        client: Any | None = None,
        key_prefix: str = "hexalogic:session:",
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.fallback = fallback or InMemorySessionBackend()
        self.key_prefix = key_prefix
        self._lock = RLock()
        self._client = client
        if self._client is None and redis_module is not None and redis_url:
            self._client = redis_module.Redis.from_url(redis_url, decode_responses=True)

    @property
    def available(self) -> bool:
        return self._client is not None

    def _key(self, session_id: str) -> str:
        return f"{self.key_prefix}{session_id}"

    def get(self, session_id: str) -> SimulatorSession | None:
        with self._lock:
            if not self.available:
                return self.fallback.get(session_id)
            payload = self._client.get(self._key(session_id))
            if payload is None:
                return None
            session = SimulatorSession.from_dict(json.loads(payload))
            self._client.expire(self._key(session_id), self.ttl_seconds)
            return session

    def save(self, session: SimulatorSession) -> None:
        with self._lock:
            if not self.available:
                self.fallback.save(session)
                return
            self._client.setex(self._key(session.session_id), self.ttl_seconds, json.dumps(session.to_dict(), separators=(",", ":")))

    def delete(self, session_id: str) -> None:
        with self._lock:
            if not self.available:
                self.fallback.delete(session_id)
                return
            self._client.delete(self._key(session_id))

    def cleanup(self, ttl_seconds: int) -> None:
        if not self.available:
            self.fallback.cleanup(ttl_seconds)

    def count(self) -> int:
        with self._lock:
            if not self.available:
                return self.fallback.count()
            return sum(1 for _ in self._client.scan_iter(match=f"{self.key_prefix}*"))

    def estimate_bytes(self) -> int:
        with self._lock:
            if not self.available:
                return self.fallback.estimate_bytes()
            total = 0
            for key in self._client.scan_iter(match=f"{self.key_prefix}*"):
                payload = self._client.get(key)
                total += len(payload.encode("utf-8")) if payload else 0
            return total


def build_session_store_from_env(*, ttl_seconds: int = 3600) -> SessionStore:
    backend_name = os.environ.get("HEXLOGIC_SESSION_BACKEND", "memory").strip().lower()
    if backend_name == "redis":
        backend = RedisSessionBackend(redis_url=os.environ.get("REDIS_URL"), ttl_seconds=ttl_seconds)
        if backend.available:
            return SessionStore(ttl_seconds=ttl_seconds, backend=backend)
    return SessionStore(ttl_seconds=ttl_seconds, backend=InMemorySessionBackend())
