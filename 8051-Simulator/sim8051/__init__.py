from .assembler import Assembler8051
from .assembler_arm import AssemblerARM
from .base_cpu import BaseCPU, DebuggerState
from .cpu import CPU8051
from .cpu_arm import CPUARM
from .exceptions import AssemblyError, DecodeError, ExecutionError, MemoryAccessError, SimulatorError, ValidationError
from .hardware import (
    ARM_GPIOA_BASE,
    DEVICE_TYPES,
    VirtualDevice,
    VirtualHardwareManager,
    apply_hardware_inputs,
)
from .factory import SUPPORTED_ARCHITECTURES, architecture_metadata, create_assembler, create_cpu, normalize_architecture, register_plugin, supported_architectures
from .model import Breakpoint, ProgramImage, ReverseDelta, RunResult, SourceLocation, TraceEntry, Watchpoint
from .observability import MetricsRegistry
from .plugin import ArchitectureRegistration, ArchitectureRegistry, CPUPlugin
from .session import InMemorySessionBackend, RedisSessionBackend, SessionBackend, SessionStore, SimulatorSession, build_session_store_from_env
from .version import API_VERSION, CPU_MODEL_VERSIONS, SESSION_FORMAT_VERSION

__all__ = [
    "Assembler8051",
    "AssemblerARM",
    "BaseCPU",
    "DebuggerState",
    "CPU8051",
    "CPUARM",
    "SUPPORTED_ARCHITECTURES",
    "supported_architectures",
    "architecture_metadata",
    "create_assembler",
    "create_cpu",
    "normalize_architecture",
    "register_plugin",
    "AssemblyError",
    "DecodeError",
    "ExecutionError",
    "MemoryAccessError",
    "SimulatorError",
    "ValidationError",
    "Breakpoint",
    "ProgramImage",
    "ReverseDelta",
    "RunResult",
    "SourceLocation",
    "TraceEntry",
    "Watchpoint",
    "MetricsRegistry",
    "ArchitectureRegistration",
    "ArchitectureRegistry",
    "CPUPlugin",
    "DEVICE_TYPES",
    "VirtualDevice",
    "VirtualHardwareManager",
    "apply_hardware_inputs",
    "ARM_GPIOA_BASE",
    "SessionBackend",
    "InMemorySessionBackend",
    "RedisSessionBackend",
    "SessionStore",
    "SimulatorSession",
    "build_session_store_from_env",
    "API_VERSION",
    "SESSION_FORMAT_VERSION",
    "CPU_MODEL_VERSIONS",
]
