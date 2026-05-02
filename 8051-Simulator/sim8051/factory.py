from __future__ import annotations

from typing import Any

from .assembler import Assembler8051
from .assembler_arm import AssemblerARM
from .cpu import CPU8051
from .cpu_arm import CPUARM
from .exceptions import ExecutionError
from .plugin import ArchitectureRegistry, CPUPlugin
from .version import CPU_MODEL_VERSIONS


_REGISTRY = ArchitectureRegistry()


def _register_builtins() -> None:
    if _REGISTRY.supported():
        return
    _REGISTRY.register_architecture(
        name="8051",
        cpu_builder=CPU8051,
        assembler_builder=Assembler8051,
        cpu_model_version=CPU_MODEL_VERSIONS["8051"],
        description="Intel MCS-51 / AT89C51 compatible core",
        capabilities={"interrupts", "timers", "uart", "bit-addressing"},
    )
    _REGISTRY.register_architecture(
        name="arm",
        cpu_builder=CPUARM,
        assembler_builder=AssemblerARM,
        cpu_model_version=CPU_MODEL_VERSIONS["arm"],
        description="Functional/minimal teaching ARM core with GPIO, timer, and IRQ support; timing is approximate and not cycle-accurate",
        capabilities={"endian-switching", "word-memory", "interrupts", "timers", "gpio"},
    )


_register_builtins()


def supported_architectures() -> tuple[str, ...]:
    return _REGISTRY.supported()


SUPPORTED_ARCHITECTURES = supported_architectures()


def normalize_architecture(architecture: str | None) -> str:
    normalized = (architecture or "8051").strip().lower()
    if _REGISTRY.get(normalized) is None:
        raise ExecutionError(f"Unsupported architecture `{architecture}`")
    return normalized


def create_cpu(architecture: str, **kwargs: Any):
    registration = _REGISTRY.get(normalize_architecture(architecture))
    if registration is None:
        raise ExecutionError(f"Unsupported architecture `{architecture}`")
    return registration.cpu_builder(**kwargs)


def create_assembler(architecture: str, **kwargs: Any):
    registration = _REGISTRY.get(normalize_architecture(architecture))
    if registration is None:
        raise ExecutionError(f"Unsupported architecture `{architecture}`")
    return registration.assembler_builder(**kwargs)


def architecture_metadata() -> dict[str, dict[str, object]]:
    return _REGISTRY.metadata()


def register_plugin(plugin: CPUPlugin) -> None:
    _REGISTRY.register_plugin(plugin)
    global SUPPORTED_ARCHITECTURES
    SUPPORTED_ARCHITECTURES = _REGISTRY.supported()
