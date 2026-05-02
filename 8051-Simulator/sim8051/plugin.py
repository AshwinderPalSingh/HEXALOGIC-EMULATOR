from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


CpuBuilder = Callable[..., Any]
AssemblerBuilder = Callable[..., Any]


@dataclass
class ArchitectureRegistration:
    name: str
    cpu_builder: CpuBuilder
    assembler_builder: AssemblerBuilder
    cpu_model_version: str
    description: str = ""
    capabilities: set[str] = field(default_factory=set)


class CPUPlugin(Protocol):
    def register(self, registry: "ArchitectureRegistry") -> None: ...


class ArchitectureRegistry:
    def __init__(self) -> None:
        self._registrations: dict[str, ArchitectureRegistration] = {}

    def register_architecture(
        self,
        *,
        name: str,
        cpu_builder: CpuBuilder,
        assembler_builder: AssemblerBuilder,
        cpu_model_version: str,
        description: str = "",
        capabilities: set[str] | None = None,
    ) -> None:
        normalized = name.strip().lower()
        self._registrations[normalized] = ArchitectureRegistration(
            name=normalized,
            cpu_builder=cpu_builder,
            assembler_builder=assembler_builder,
            cpu_model_version=cpu_model_version,
            description=description,
            capabilities=set(capabilities or set()),
        )

    def supported(self) -> tuple[str, ...]:
        return tuple(sorted(self._registrations))

    def get(self, name: str) -> ArchitectureRegistration | None:
        return self._registrations.get(name.strip().lower())

    def metadata(self) -> dict[str, dict[str, object]]:
        return {
            name: {
                "cpu_model_version": registration.cpu_model_version,
                "description": registration.description,
                "capabilities": sorted(registration.capabilities),
            }
            for name, registration in self._registrations.items()
        }

    def register_plugin(self, plugin: CPUPlugin) -> None:
        plugin.register(self)
