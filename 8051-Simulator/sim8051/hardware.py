from __future__ import annotations

import os
import secrets
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, ClassVar

from .exceptions import ValidationError
from .memory import GPIOA_MMIO_BASE as ARM_GPIOA_BASE

_ARM_GPIO_OUT = 0x00
_ARM_GPIO_IN = 0x04
_ARM_GPIO_DIR = 0x08
_TOGGLE_WARNING_MS = 50.0
_DEBUG_TIMING = os.environ.get("HEXLOGIC_DEBUG_TIMING", "").strip().lower() in {"1", "true", "yes", "on"}
_PERIODICITY_WINDOW = 6
_SIGNAL_LOG_LIMIT = 512
_VALIDATION_LOG_LIMIT = 256
_TEST_DURATION_MS = 120
_SEVEN_SEGMENT_MAP = {
    0x3F: "0",
    0x06: "1",
    0x5B: "2",
    0x4F: "3",
    0x66: "4",
    0x6D: "5",
    0x7D: "6",
    0x07: "7",
    0x7F: "8",
    0x6F: "9",
}
_STEPPER_FORWARD = [0x09, 0x0C, 0x06, 0x03]
_STEPPER_REVERSE = [0x03, 0x06, 0x0C, 0x09]
_FAULT_TYPES = {"stuck_high", "stuck_low", "delay", "noise"}


def _device_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4)}"


def _read_word(sample: dict[int, int] | dict[str, int], address: int, *, endian: str) -> int:
    bytes_ = [int(sample.get(address + offset, sample.get(str(address + offset), 0))) & 0xFF for offset in range(4)]
    if endian == "big":
        return (bytes_[0] << 24) | (bytes_[1] << 16) | (bytes_[2] << 8) | bytes_[3]
    return bytes_[0] | (bytes_[1] << 8) | (bytes_[2] << 16) | (bytes_[3] << 24)


@dataclass
class PinState:
    name: str
    level: int
    direction: str
    port: str | None = None
    bit: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "name": self.name,
            "level": int(self.level),
            "direction": self.direction,
        }
        if self.port is not None:
            payload["port"] = self.port
        if self.bit is not None:
            payload["bit"] = self.bit
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


@dataclass
class SignalEvent:
    time_ms: float
    pin: str
    value: int
    direction: str
    source: str = "mcu"
    cycle: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "time_ms": round(self.time_ms, 3),
            "pin": self.pin,
            "value": int(self.value),
            "direction": self.direction,
            "source": self.source,
        }
        if self.cycle is not None:
            payload["cycle"] = int(self.cycle)
        return payload


@dataclass
class ValidationIssue:
    time_ms: float
    device_id: str
    level: str
    message: str
    cause: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "time_ms": round(self.time_ms, 3),
            "device_id": self.device_id,
            "level": self.level,
            "message": self.message,
            "cause": self.cause,
        }


class VirtualDevice:
    type_name: ClassVar[str] = "device"
    display_name: ClassVar[str] = "Device"
    icon: ClassVar[str] = "?"
    connection_schema: ClassVar[list[dict[str, str]]] = []
    default_label: ClassVar[str] = "Device"
    default_size: ClassVar[tuple[int, int]] = (160, 112)

    def __init__(
        self,
        *,
        device_id: str | None = None,
        label: str | None = None,
        connections: dict[str, Any] | None = None,
        position: dict[str, int] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> None:
        self.device_id = device_id or _device_id(self.type_name)
        self.label = label or self.default_label
        self.connections = dict(connections or {})
        self.position = {"x": 0, "y": 0, **dict(position or {})}
        self.settings = dict(settings or {})
        self.runtime: dict[str, Any] = {}

    @classmethod
    def schema(cls) -> dict[str, Any]:
        return {
            "type": cls.type_name,
            "label": cls.display_name,
            "icon": cls.icon,
            "connections": [dict(item) for item in cls.connection_schema],
            "size": {"w": cls.default_size[0], "h": cls.default_size[1]},
        }

    def update(
        self,
        *,
        label: str | None = None,
        connections: dict[str, Any] | None = None,
        position: dict[str, int] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> None:
        if label is not None:
            self.label = label.strip() or self.default_label
        if connections:
            self.connections.update(connections)
        if position:
            self.position.update({key: int(value) for key, value in position.items() if key in {"x", "y"}})
        if settings:
            self.settings.update(settings)

    def connected_signals(self, bus_catalog: dict[str, list[str]]) -> list[dict[str, str]]:
        return []

    def input_bindings(self) -> dict[str, int | None]:
        return {}

    def evaluate(self, pin_states: dict[str, PinState], bus_catalog: dict[str, list[str]], time_ms: float) -> dict[str, Any]:
        _ = pin_states, bus_catalog, time_ms
        return {}

    def validate(
        self,
        *,
        state: dict[str, Any],
        pin_states: dict[str, PinState],
        metrics: dict[str, Any],
        time_ms: float,
        signal_log: list[SignalEvent],
    ) -> list[ValidationIssue]:
        _ = state, pin_states, metrics, time_ms, signal_log
        return []

    def view(
        self,
        *,
        pin_states: dict[str, PinState],
        bus_catalog: dict[str, list[str]],
        time_ms: float,
        metrics: dict[str, Any],
        issues: list[ValidationIssue],
    ) -> dict[str, Any]:
        state = self.evaluate(pin_states, bus_catalog, time_ms)
        return {
            "id": self.device_id,
            "type": self.type_name,
            "label": self.label,
            "icon": self.icon,
            "connections": dict(self.connections),
            "position": dict(self.position),
            "size": {"w": self.default_size[0], "h": self.default_size[1]},
            "settings": dict(self.settings),
            "pins": self.connected_signals(bus_catalog),
            "state": state,
            "metrics": dict(metrics),
            "validation": {
                "status": "fail" if any(issue.level == "error" for issue in issues) else ("warn" if issues else "pass"),
                "issues": [issue.to_dict() for issue in issues],
            },
            "schema": self.schema(),
        }

    def serialize(self) -> dict[str, Any]:
        return {
            "id": self.device_id,
            "type": self.type_name,
            "label": self.label,
            "connections": dict(self.connections),
            "position": dict(self.position),
            "settings": dict(self.settings),
            "runtime": dict(self.runtime),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "VirtualDevice":
        device_cls = DEVICE_TYPES.get(str(payload.get("type", "")).lower())
        if device_cls is None:
            raise ValidationError("Unsupported virtual device type", context={"type": payload.get("type")})
        device = device_cls(
            device_id=str(payload.get("id", "")) or None,
            label=str(payload.get("label", device_cls.default_label)),
            connections=dict(payload.get("connections", {})),
            position=dict(payload.get("position", {})),
            settings=dict(payload.get("settings", {})),
        )
        device.runtime.update(dict(payload.get("runtime", {})))
        return device


class LedDevice(VirtualDevice):
    type_name = "led"
    display_name = "LED"
    icon = "●"
    default_label = "LED"
    connection_schema = [{"key": "pin", "label": "Pin", "kind": "pin"}]
    default_size = (128, 104)

    def connected_signals(self, bus_catalog: dict[str, list[str]]) -> list[dict[str, str]]:
        _ = bus_catalog
        pin_name = str(self.connections.get("pin", "") or "")
        return [{"id": "anode", "label": "Anode", "kind": "input", "signal": pin_name}] if pin_name else []

    def evaluate(self, pin_states: dict[str, PinState], bus_catalog: dict[str, list[str]], time_ms: float) -> dict[str, Any]:
        _ = bus_catalog, time_ms
        pin_name = str(self.connections.get("pin", "") or "")
        pin = pin_states.get(pin_name)
        level = int(pin.level) if pin else 0
        last_level = int(self.runtime.get("last_level", level))
        transition_count = int(self.runtime.get("transition_count", 0))
        if pin and level != last_level:
            transition_count += 1
            self.runtime["last_change_ms"] = time_ms
        self.runtime["last_level"] = level
        self.runtime["transition_count"] = transition_count
        return {
            "connected": bool(pin),
            "on": bool(level),
            "level": level,
            "pin": pin_name or None,
            "direction": pin.direction if pin else "unknown",
            "transition_count": transition_count,
            "last_change_ms": round(float(self.runtime.get("last_change_ms", time_ms if pin else 0.0)), 3),
        }

    def validate(self, *, state: dict[str, Any], pin_states: dict[str, PinState], metrics: dict[str, Any], time_ms: float, signal_log: list[SignalEvent]) -> list[ValidationIssue]:
        _ = metrics
        issues: list[ValidationIssue] = []
        pin_name = state.get("pin")
        if not pin_name:
            issues.append(ValidationIssue(time_ms, self.device_id, "warning", "LED is not connected", "Select a GPIO pin."))
            return issues
        pin = pin_states.get(str(pin_name))
        expected = int(pin.level) if pin else 0
        actual = 1 if state.get("on") else 0
        if expected != actual:
            issues.append(ValidationIssue(time_ms, self.device_id, "error", f"LED expected={expected} actual={actual}", "Signal propagation or render desync."))
        pin_events = [event for event in signal_log if event.pin == pin_name]
        if len(pin_events) >= 2:
            effective_hz = max(1.0, float(metrics.get("effectiveHz", 1.0) or 1.0))
            cycle_intervals = [
                int(pin_events[idx].cycle) - int(pin_events[idx - 1].cycle)
                for idx in range(1, len(pin_events))
                if pin_events[idx].cycle is not None and pin_events[idx - 1].cycle is not None and int(pin_events[idx].cycle) > int(pin_events[idx - 1].cycle)
            ]
            intervals = [pin_events[idx].time_ms - pin_events[idx - 1].time_ms for idx in range(1, len(pin_events)) if pin_events[idx].time_ms > pin_events[idx - 1].time_ms]
            if cycle_intervals:
                period_ms = min(cycle_intervals) / effective_hz * 1000.0
            elif intervals:
                period_ms = min(intervals)
            else:
                period_ms = None
            if period_ms is not None and period_ms < _TOGGLE_WARNING_MS:
                issues.append(
                    ValidationIssue(
                        time_ms,
                        self.device_id,
                        "warning",
                        f"LED toggling too fast ({period_ms:.3f} ms, minimum visible ~{_TOGGLE_WARNING_MS:.0f} ms)",
                        "Transition period is below the human-visible threshold.",
                    )
                )
            if len(intervals) >= 3:
                recent = intervals[-_PERIODICITY_WINDOW:]
                mean = sum(recent) / len(recent)
                spread = max(recent) - min(recent)
                if mean > 0 and spread > (mean * 0.9):
                    issues.append(ValidationIssue(time_ms, self.device_id, "error", f"LED on {pin_name} has no stable periodicity", "Signal transitions are not temporally consistent."))
        return issues


class LedArrayDevice(VirtualDevice):
    type_name = "led_array"
    display_name = "LED Array"
    icon = "◼"
    default_label = "LED Array"
    connection_schema = [{"key": "bus", "label": "Bus", "kind": "bus8"}]
    default_size = (196, 112)

    def connected_signals(self, bus_catalog: dict[str, list[str]]) -> list[dict[str, str]]:
        bus_name = str(self.connections.get("bus", "") or "")
        return [
            {"id": f"bit{index}", "label": f"B{index}", "kind": "input", "signal": pin}
            for index, pin in enumerate(bus_catalog.get(bus_name, []))
        ]

    def evaluate(self, pin_states: dict[str, PinState], bus_catalog: dict[str, list[str]], time_ms: float) -> dict[str, Any]:
        _ = time_ms
        bus_name = str(self.connections.get("bus", "") or "")
        pins = bus_catalog.get(bus_name, [])
        bits = [int(pin_states.get(pin, PinState(pin, 0, "unknown")).level) for pin in pins[:8]]
        while len(bits) < 8:
            bits.append(0)
        value = sum(bit << index for index, bit in enumerate(bits))
        history = list(self.runtime.get("value_history", []))[-7:]
        history.append(value)
        self.runtime["value_history"] = history
        direction = "steady"
        if len(history) >= 3:
            deltas = [(history[idx] - history[idx - 1]) & 0xFF for idx in range(1, len(history))]
            if all(delta in {1, 2, 4, 8, 16, 32, 64, 128} for delta in deltas if delta != 0):
                direction = "shift-left"
            elif all(delta in {255, 254, 252, 248, 240, 224, 192, 128} for delta in deltas if delta != 0):
                direction = "shift-right"
        return {
            "connected": bool(pins),
            "bus": bus_name or None,
            "bits": bits,
            "value": value,
            "pattern": direction,
        }

    def validate(self, *, state: dict[str, Any], pin_states: dict[str, PinState], metrics: dict[str, Any], time_ms: float, signal_log: list[SignalEvent]) -> list[ValidationIssue]:
        _ = metrics, pin_states
        issues: list[ValidationIssue] = []
        bus_name = str(state.get("bus") or "")
        if not bus_name:
            issues.append(ValidationIssue(time_ms, self.device_id, "warning", "LED array is not connected", "Select an 8-bit GPIO bus."))
            return issues
        bits = list(state.get("bits") or [])
        expected = bits[:8] + [0] * max(0, 8 - len(bits))
        actual = bits[:8]
        if actual != expected[: len(actual)]:
            issues.append(ValidationIssue(time_ms, self.device_id, "error", f"LED array pattern mismatch on {bus_name}", "Rendered bit pattern diverged from bus value."))
        bus_prefix = bus_name.replace("_LOW", ".").replace("_HIGH", ".")
        recent_events = [event for event in signal_log[-16:] if event.pin.startswith(bus_prefix)]
        if recent_events and not any(bits):
            issues.append(ValidationIssue(time_ms, self.device_id, "warning", f"LED array on {bus_name} is receiving transitions but all LEDs remain off", "Signal activity is not visible on the rendered bus."))  # noqa: E501
        history = list(self.runtime.get("value_history", []))
        if len(history) >= 4:
            unique_values = [value for value in history[-4:] if value]
            if len(set(unique_values)) >= 3 and state.get("pattern") == "steady":
                issues.append(ValidationIssue(time_ms, self.device_id, "error", f"LED array on {bus_name} did not classify a visible shift pattern", "Expected rotate/shift activity was not recognized."))  # noqa: E501
        return issues


class SevenSegmentDevice(VirtualDevice):
    type_name = "seven_segment"
    display_name = "7-Segment"
    icon = "8"
    default_label = "7-Segment"
    connection_schema = [{"key": "bus", "label": "Segment Bus", "kind": "bus8"}]
    default_size = (144, 152)

    def connected_signals(self, bus_catalog: dict[str, list[str]]) -> list[dict[str, str]]:
        bus_name = str(self.connections.get("bus", "") or "")
        labels = ["a", "b", "c", "d", "e", "f", "g", "dp"]
        return [
            {"id": labels[index], "label": labels[index].upper(), "kind": "input", "signal": pin}
            for index, pin in enumerate(bus_catalog.get(bus_name, [])[:8])
        ]

    def evaluate(self, pin_states: dict[str, PinState], bus_catalog: dict[str, list[str]], time_ms: float) -> dict[str, Any]:
        _ = time_ms
        bus_name = str(self.connections.get("bus", "") or "")
        pins = bus_catalog.get(bus_name, [])
        bits = [int(pin_states.get(pin, PinState(pin, 0, "unknown")).level) for pin in pins[:8]]
        while len(bits) < 8:
            bits.append(0)
        pattern = sum((1 if bit else 0) << index for index, bit in enumerate(bits[:7]))
        digit = _SEVEN_SEGMENT_MAP.get(pattern, "-")
        return {
            "connected": bool(pins),
            "bus": bus_name or None,
            "segments": bits,
            "pattern": pattern,
            "digit": digit,
            "decimal_point": bool(bits[7]),
        }

    def validate(self, *, state: dict[str, Any], pin_states: dict[str, PinState], metrics: dict[str, Any], time_ms: float, signal_log: list[SignalEvent]) -> list[ValidationIssue]:
        _ = pin_states, metrics, signal_log
        issues: list[ValidationIssue] = []
        if not state.get("bus"):
            issues.append(ValidationIssue(time_ms, self.device_id, "warning", "7-segment display is not connected", "Select an 8-bit segment bus."))
            return issues
        pattern = int(state.get("pattern", 0)) & 0x7F
        digit = str(state.get("digit", "-"))
        expected = _SEVEN_SEGMENT_MAP.get(pattern)
        if expected is not None and digit != expected:
            issues.append(ValidationIssue(time_ms, self.device_id, "error", f"7-segment expected digit {expected} but rendered {digit}", "Segment-to-digit decoding is incorrect."))
        if expected is None and digit not in {"-", ""}:
            issues.append(ValidationIssue(time_ms, self.device_id, "error", f"7-segment rendered digit {digit} for invalid pattern 0x{pattern:02X}", "Invalid segment code should not map to a valid numeral."))
        return issues


class SwitchDevice(VirtualDevice):
    type_name = "switch"
    display_name = "Switch"
    icon = "⏻"
    default_label = "Switch"
    connection_schema = [{"key": "pin", "label": "Input Pin", "kind": "pin"}]
    default_size = (144, 116)

    def connected_signals(self, bus_catalog: dict[str, list[str]]) -> list[dict[str, str]]:
        _ = bus_catalog
        pin_name = str(self.connections.get("pin", "") or "")
        return [{"id": "sw", "label": "SW", "kind": "output", "signal": pin_name}] if pin_name else []

    def evaluate(self, pin_states: dict[str, PinState], bus_catalog: dict[str, list[str]], time_ms: float) -> dict[str, Any]:
        _ = bus_catalog
        pin_name = str(self.connections.get("pin", "") or "")
        pin = pin_states.get(pin_name)
        input_level = 1 if self.settings.get("input_level", 0) else 0
        toggles = int(self.runtime.get("toggle_count", 0))
        last_input = int(self.runtime.get("last_input_level", input_level))
        if input_level != last_input:
            toggles += 1
            self.runtime["last_toggle_ms"] = time_ms
        self.runtime["toggle_count"] = toggles
        self.runtime["last_input_level"] = input_level
        return {
            "connected": bool(pin_name),
            "pin": pin_name or None,
            "input_level": input_level,
            "line_level": int(pin.level) if pin else input_level,
            "direction": pin.direction if pin else "input",
            "toggle_count": toggles,
        }

    def input_bindings(self) -> dict[str, int | None]:
        pin_name = str(self.connections.get("pin", "") or "")
        if not pin_name:
            return {}
        return {pin_name: 1 if self.settings.get("input_level", 0) else 0}

    def validate(self, *, state: dict[str, Any], pin_states: dict[str, PinState], metrics: dict[str, Any], time_ms: float, signal_log: list[SignalEvent]) -> list[ValidationIssue]:
        _ = pin_states, signal_log
        issues: list[ValidationIssue] = []
        pin_name = state.get("pin")
        if not pin_name:
            issues.append(ValidationIssue(time_ms, self.device_id, "warning", "Switch is not connected", "Select an MCU input pin."))
            return issues
        expected = int(state.get("input_level", 0))
        actual = int(state.get("line_level", 0))
        if expected != actual:
            issues.append(ValidationIssue(time_ms, self.device_id, "error", f"Switch expected line {expected} but observed {actual}", "Injected input did not reach the MCU pin."))
        if str(state.get("direction", "input")) == "output":
            issues.append(ValidationIssue(time_ms, self.device_id, "warning", f"Switch drives {pin_name} while the MCU pin is configured as output", "This can mask CPU reads or cause contention."))
        last_toggle_ms = float(self.runtime.get("last_toggle_ms", 0.0) or 0.0)
        last_read_time = metrics.get("lastReadTime")
        if int(state.get("toggle_count", 0)) > 0 and (last_read_time is None or float(last_read_time) < last_toggle_ms):
            issues.append(
                ValidationIssue(
                    time_ms,
                    self.device_id,
                    "warning",
                    f"Switch toggled on {pin_name} but firmware has not sampled the input",
                    "The input changed state, but no CPU read of the connected GPIO pin was observed after the last switch toggle.",
                )
            )
        return issues


class StepperMotorDevice(VirtualDevice):
    type_name = "stepper"
    display_name = "Stepper Motor"
    icon = "◎"
    default_label = "Stepper"
    connection_schema = [{"key": "coil_bus", "label": "Coil Bus", "kind": "bus4"}]
    default_size = (176, 136)

    def connected_signals(self, bus_catalog: dict[str, list[str]]) -> list[dict[str, str]]:
        bus_name = str(self.connections.get("coil_bus", "") or "")
        labels = ["A", "B", "C", "D"]
        return [
            {"id": labels[index].lower(), "label": labels[index], "kind": "input", "signal": pin}
            for index, pin in enumerate(bus_catalog.get(bus_name, [])[:4])
        ]

    def evaluate(self, pin_states: dict[str, PinState], bus_catalog: dict[str, list[str]], time_ms: float) -> dict[str, Any]:
        bus_name = str(self.connections.get("coil_bus", "") or "")
        pins = bus_catalog.get(bus_name, [])
        bits = [int(pin_states.get(pin, PinState(pin, 0, "unknown")).level) for pin in pins[:4]]
        while len(bits) < 4:
            bits.append(0)
        pattern = sum(bit << index for index, bit in enumerate(bits))
        history = deque(self.runtime.get("patterns", []), maxlen=4)
        if not history or history[-1] != pattern:
            history.append(pattern)
        self.runtime["patterns"] = list(history)
        step_index = int(self.runtime.get("step_index", 0))
        angle = int(self.runtime.get("angle", 0))
        moved = False
        if len(history) >= 2:
            prev = history[-2]
            current = history[-1]
            if prev in _STEPPER_FORWARD and current in _STEPPER_FORWARD:
                prev_index = _STEPPER_FORWARD.index(prev)
                current_index = _STEPPER_FORWARD.index(current)
                if current_index == (prev_index + 1) % len(_STEPPER_FORWARD):
                    step_index += 1
                    angle = (angle + 90) % 360
                    moved = True
                elif current_index == (prev_index - 1) % len(_STEPPER_FORWARD):
                    step_index -= 1
                    angle = (angle - 90) % 360
                    moved = True
        self.runtime["step_index"] = step_index
        self.runtime["angle"] = angle
        return {
            "connected": bool(pins),
            "bus": bus_name or None,
            "phases": bits,
            "pattern": pattern,
            "angle": angle,
            "step_index": step_index,
            "moved": moved,
            "window": list(history),
        }

    def validate(self, *, state: dict[str, Any], pin_states: dict[str, PinState], metrics: dict[str, Any], time_ms: float, signal_log: list[SignalEvent]) -> list[ValidationIssue]:
        _ = pin_states, metrics, signal_log
        issues: list[ValidationIssue] = []
        if not state.get("bus"):
            issues.append(ValidationIssue(time_ms, self.device_id, "warning", "Stepper motor is not connected", "Select a 4-bit coil bus."))
            return issues
        window = [int(value) & 0x0F for value in list(state.get("window") or []) if value is not None]
        if len(window) >= 2:
            for prev, current in zip(window, window[1:]):
                valid = ((prev in _STEPPER_FORWARD and current in _STEPPER_FORWARD and ((
                    _STEPPER_FORWARD.index(current) == (_STEPPER_FORWARD.index(prev) + 1) % len(_STEPPER_FORWARD)
                ) or (
                    _STEPPER_FORWARD.index(current) == (_STEPPER_FORWARD.index(prev) - 1) % len(_STEPPER_FORWARD)
                ))) or (prev == current))
                if not valid:
                    issues.append(ValidationIssue(time_ms, self.device_id, "error", f"Invalid stepper sequence {window}", "Stepper phases must follow a valid full-step order."))
                    break
        if len(window) >= 2 and window[-1] != window[-2] and not state.get("moved"):
            issues.append(ValidationIssue(time_ms, self.device_id, "error", "Stepper received a valid pattern change but did not move", "Motor state failed to advance on a valid sequence."))
        return issues


DEVICE_TYPES: dict[str, type[VirtualDevice]] = {
    LedDevice.type_name: LedDevice,
    LedArrayDevice.type_name: LedArrayDevice,
    SevenSegmentDevice.type_name: SevenSegmentDevice,
    SwitchDevice.type_name: SwitchDevice,
    StepperMotorDevice.type_name: StepperMotorDevice,
}


class VirtualHardwareManager:
    def __init__(self, architecture: str = "8051") -> None:
        self.architecture = architecture
        self.devices: list[VirtualDevice] = []
        self._last_views: dict[str, dict[str, Any]] = {}
        self._device_states: dict[str, dict[str, Any]] = {}
        self._subscriptions: dict[str, set[str]] = defaultdict(set)
        self._signal_nodes: dict[str, PinState] = {}
        self._signal_meta: dict[str, dict[str, Any]] = {}
        self._wires: list[dict[str, Any]] = []
        self._device_pin_cache: dict[str, list[dict[str, str]]] = {}
        self._bus_lookup_cache: dict[str, list[str]] | None = None
        self._graph_dirty: bool = True
        self._component_metrics: dict[str, dict[str, Any]] = {}
        self._dirty_devices: set[str] = set()
        self._faults: dict[str, dict[str, Any]] = {}
        self._fault_history: dict[str, deque[tuple[float, int]]] = defaultdict(lambda: deque(maxlen=64))
        self._last_signal_cycles: dict[str, int] = {}
        self.signal_log: deque[SignalEvent] = deque(maxlen=_SIGNAL_LOG_LIMIT)
        self.validation_log: deque[ValidationIssue] = deque(maxlen=_VALIDATION_LOG_LIMIT)
        self._device_issues: dict[str, list[ValidationIssue]] = {}
        self._last_time_ms: float = 0.0
        self._last_snapshot_meta: dict[str, Any] = {}
        self._observed_reads: dict[str, float] = {}
        self._stream_signal_events: deque[dict[str, Any]] = deque(maxlen=_SIGNAL_LOG_LIMIT)
        self._stream_signal_names: set[str] = set()
        self._stream_device_updates: dict[str, dict[str, Any]] = {}
        self._stream_removed_ids: set[str] = set()

    def reset_for_architecture(self, architecture: str) -> None:
        self.architecture = architecture
        self.devices = []
        self._last_views = {}
        self._device_states = {}
        self._subscriptions.clear()
        self._signal_nodes = {}
        self._signal_meta = {}
        self._wires = []
        self._device_pin_cache = {}
        self._bus_lookup_cache = None
        self._graph_dirty = True
        self._component_metrics = {}
        self._dirty_devices = set()
        self._faults = {}
        self._fault_history = defaultdict(lambda: deque(maxlen=64))
        self._last_signal_cycles = {}
        self.signal_log.clear()
        self.validation_log.clear()
        self._device_issues = {}
        self._last_time_ms = 0.0
        self._last_snapshot_meta = {}
        self._observed_reads = {}
        self._stream_signal_events.clear()
        self._stream_signal_names = set()
        self._stream_device_updates = {}
        self._stream_removed_ids = set()

    def available_pin_catalog(self) -> dict[str, list[dict[str, Any]]]:
        if self.architecture == "arm":
            pin_names = [f"GPIOA.{index}" for index in range(16)]
            bus8 = {
                "GPIOA_LOW": pin_names[:8],
                "GPIOA_HIGH": pin_names[8:16],
            }
            bus4 = {
                "GPIOA_0_3": pin_names[0:4],
                "GPIOA_4_7": pin_names[4:8],
                "GPIOA_8_11": pin_names[8:12],
                "GPIOA_12_15": pin_names[12:16],
            }
        else:
            pin_names = [f"P{port}.{bit}" for port in range(4) for bit in range(8)]
            bus8 = {f"P{port}": [f"P{port}.{bit}" for bit in range(8)] for port in range(4)}
            bus4 = {
                **{f"P{port}_LOW": [f"P{port}.{bit}" for bit in range(4)] for port in range(4)},
                **{f"P{port}_HIGH": [f"P{port}.{bit}" for bit in range(4, 8)] for port in range(4)},
            }
        return {
            "pins": [{"id": name, "label": name} for name in pin_names],
            "bus8": [{"id": key, "label": key, "pins": value} for key, value in bus8.items()],
            "bus4": [{"id": key, "label": key, "pins": value} for key, value in bus4.items()],
        }

    def _bus_lookup(self) -> dict[str, list[str]]:
        if self._bus_lookup_cache is None:
            catalog = self.available_pin_catalog()
            self._bus_lookup_cache = {
                **{item["id"]: list(item["pins"]) for item in catalog["bus8"]},
                **{item["id"]: list(item["pins"]) for item in catalog["bus4"]},
            }
        return self._bus_lookup_cache

    def _time_ms(self, snapshot: dict[str, Any]) -> float:
        cycles = float(snapshot.get("cycles", 0) or 0)
        hz = float(snapshot.get("effective_clock_hz", 0) or 0)
        if hz <= 0:
            hz = max(1.0, float(snapshot.get("clock_hz", 1) or 1))
            if self.architecture == "8051":
                hz = hz / 12.0
        computed_seconds = cycles / hz
        computed_ms = computed_seconds * 1000.0
        if _DEBUG_TIMING:
            print(
                "[DEBUG_TIMING][hardware]",
                {
                    "architecture": self.architecture,
                    "cycles": cycles,
                    "computed_seconds": round(computed_seconds, 9),
                    "computed_ms": round(computed_ms, 6),
                },
            )
        return computed_ms

    def _timing_payload(self, *, cycles: int, effective_hz: float, time_ms: float) -> dict[str, Any]:
        return {
            "cycles": int(cycles),
            "effective_hz": round(float(effective_hz), 3),
            "seconds": round(float(time_ms) / 1000.0, 9),
            "time_ms": round(float(time_ms), 6),
            "cycle_unit": "8051_machine_cycle" if self.architecture == "8051" else "processor_cycle",
        }

    def _8051_pin_states(self, snapshot: dict[str, Any]) -> dict[str, PinState]:
        ports = dict(snapshot.get("ports", {}))
        sfr = dict(snapshot.get("sfr", {}))
        result: dict[str, PinState] = {}
        for port_index, address in enumerate((0x80, 0x90, 0xA0, 0xB0)):
            port_name = f"P{port_index}"
            port_payload = dict(ports.get(port_name, {}))
            latch = int(port_payload.get("latch", sfr.get(address, sfr.get(str(address), 0xFF)))) & 0xFF
            pin_value = int(port_payload.get("pin", latch)) & 0xFF
            open_drain = bool(port_payload.get("open_drain", port_index == 0))
            for bit in range(8):
                pin_name = f"{port_name}.{bit}"
                result[pin_name] = PinState(
                    name=pin_name,
                    level=(pin_value >> bit) & 0x01,
                    direction="input" if ((latch >> bit) & 0x01) else "output",
                    port=port_name,
                    bit=bit,
                    metadata={"open_drain": open_drain, "latch": (latch >> bit) & 0x01},
                )
        return result

    def _arm_pin_states(self, snapshot: dict[str, Any]) -> dict[str, PinState]:
        gpio_regs = dict(snapshot.get("gpio_regs", {}))
        if gpio_regs:
            out_value = int(gpio_regs.get("out", 0)) & 0xFFFFFFFF
            in_value = int(gpio_regs.get("in", 0)) & 0xFFFFFFFF
            dir_value = int(gpio_regs.get("dir", 0)) & 0xFFFFFFFF
        else:
            sample = dict(snapshot.get("xram_sample", {}))
            endian = str(snapshot.get("endian", "little"))
            out_value = _read_word(sample, _ARM_GPIO_OUT, endian=endian)
            in_value = _read_word(sample, _ARM_GPIO_IN, endian=endian)
            dir_value = _read_word(sample, _ARM_GPIO_DIR, endian=endian)
        result: dict[str, PinState] = {}
        for bit in range(16):
            direction = "output" if ((dir_value >> bit) & 0x01) else "input"
            level_source = out_value if direction == "output" else in_value
            result[f"GPIOA.{bit}"] = PinState(
                name=f"GPIOA.{bit}",
                level=(level_source >> bit) & 0x01,
                direction=direction,
                port="GPIOA",
                bit=bit,
                metadata={
                    "registers": {"out": _ARM_GPIO_OUT, "in": _ARM_GPIO_IN, "dir": _ARM_GPIO_DIR},
                    "latch": (out_value >> bit) & 0x01,
                },
            )
        return result

    def pin_states(self, snapshot: dict[str, Any]) -> dict[str, PinState]:
        pins = self._arm_pin_states(snapshot) if self.architecture == "arm" else self._8051_pin_states(snapshot)
        for pin_name, level in self.input_bindings().items():
            existing = pins.get(pin_name)
            if existing is None:
                continue
            pins[pin_name] = PinState(
                name=existing.name,
                level=1 if level else 0,
                direction="input",
                port=existing.port,
                bit=existing.bit,
                metadata={
                    **existing.metadata,
                    "virtual_input": True,
                    "mcu_direction": existing.direction,
                    "mcu_level": existing.level,
                },
            )
        self._apply_faults(pins, self._time_ms(snapshot))
        return pins

    def _ensure_graph_layout(self, bus_lookup: dict[str, list[str]]) -> None:
        if not self._graph_dirty:
            return
        subscriptions: dict[str, set[str]] = defaultdict(set)
        wires: list[dict[str, Any]] = []
        pin_cache: dict[str, list[dict[str, str]]] = {}
        for device in self.devices:
            pin_descs = [dict(item) for item in device.connected_signals(bus_lookup)]
            pin_cache[device.device_id] = pin_descs
            for pin_desc in pin_descs:
                signal = str(pin_desc.get("signal") or "")
                if not signal:
                    continue
                subscriptions[signal].add(device.device_id)
                wires.append({
                    "id": f"wire-{device.device_id}-{pin_desc['id']}",
                    "fromPin": signal,
                    "toPin": f"{device.device_id}:{pin_desc['id']}",
                    "kind": pin_desc.get("kind", "input"),
                })
        self._subscriptions = subscriptions
        self._wires = wires
        self._device_pin_cache = pin_cache
        self._graph_dirty = False

    def _rebuild_graph(self, bus_lookup: dict[str, list[str]], pin_states: dict[str, PinState], *, time_ms: float) -> list[ValidationIssue]:
        self._ensure_graph_layout(bus_lookup)
        self._signal_meta = {}
        issues: list[ValidationIssue] = []
        output_drivers: dict[str, list[dict[str, Any]]] = defaultdict(list)
        consumers: dict[str, list[str]] = defaultdict(list)
        for name, pin in pin_states.items():
            mcu_direction = str(pin.metadata.get("mcu_direction", pin.direction))
            mcu_level = int(pin.metadata.get("mcu_level", pin.level))
            if mcu_direction == "output":
                output_drivers[name].append({"source": "mcu", "level": mcu_level})
        for device in self.devices:
            output_levels = device.input_bindings()
            for pin_desc in self._device_pin_cache.get(device.device_id, ()):
                signal = str(pin_desc.get("signal") or "")
                if not signal:
                    continue
                consumers[signal].append(device.device_id)
                if pin_desc.get("kind") == "output":
                    output_drivers[signal].append({
                        "source": device.device_id,
                        "level": int(output_levels.get(signal, 0) or 0),
                    })
        for signal in set(pin_states) | set(consumers):
            drivers = output_drivers.get(signal, [])
            driver_sources = [str(driver["source"]) for driver in drivers]
            driver_levels = {int(driver["level"]) for driver in drivers}
            floating = bool(consumers.get(signal)) and not drivers
            contention = len(driver_levels) > 1
            if contention:
                resolved_state = "error"
            elif not drivers:
                resolved_state = "z"
            elif 1 in driver_levels:
                resolved_state = "high"
            else:
                resolved_state = "low"
            self._signal_meta[signal] = {
                "drivers": driver_sources,
                "levels": sorted(driver_levels),
                "consumers": consumers.get(signal, []),
                "floating": floating,
                "contention": contention,
                "state": resolved_state,
            }
            if contention:
                issues.append(
                    ValidationIssue(
                        time_ms,
                        ",".join(driver_sources),
                        "error",
                        f"Bus contention on {signal}",
                        "Multiple outputs are driving conflicting values on the same logical signal.",
                    )
                )
            elif floating:
                issues.append(
                    ValidationIssue(
                        time_ms,
                        signal,
                        "warning",
                        f"Floating signal on {signal}",
                        "An input connection exists without any active driver.",
                    )
                )
        for signal, meta in self._signal_meta.items():
            pin = pin_states.get(signal)
            if pin is None:
                continue
            pin.metadata = {
                **pin.metadata,
                "drivers": meta["drivers"],
                "floating": meta["floating"],
                "contention": meta["contention"],
                "state": meta["state"],
            }
        return issues

    def _record_pin_events(
        self,
        previous_nodes: dict[str, PinState],
        new_states: dict[str, PinState],
        time_ms: float,
        *,
        cycles: int,
    ) -> tuple[set[str], set[str]]:
        impacted: set[str] = set()
        changed_signals: set[str] = set()
        for name, pin in new_states.items():
            previous = previous_nodes.get(name)
            if previous is None or previous.level != pin.level or previous.direction != pin.direction:
                previous_cycle = int(self._last_signal_cycles.get(name, 0))
                accumulated_cycles = max(0, int(cycles) - previous_cycle)
                event = SignalEvent(time_ms=time_ms, pin=name, value=pin.level, direction=pin.direction, cycle=int(cycles))
                self.signal_log.append(event)
                self._stream_signal_events.append(event.to_dict())
                self._stream_signal_names.add(name)
                self._fault_history[name].append((time_ms, int(pin.level)))
                self._last_signal_cycles[name] = int(cycles)
                changed_signals.add(name)
                if _DEBUG_TIMING:
                    print(
                        "[DEBUG_SYNC]",
                        {
                            "signal": name,
                            "cycle_at_transition": int(cycles),
                            "accumulated_cycles_since_last_change": accumulated_cycles,
                            "time_ms": round(time_ms, 6),
                        },
                    )
                impacted.update(self._subscriptions.get(name, set()))
        return impacted, changed_signals

    def _apply_faults(self, pin_states: dict[str, PinState], time_ms: float) -> None:
        for signal, fault in self._faults.items():
            pin = pin_states.get(signal)
            if pin is None:
                continue
            fault_type = str(fault.get("type", "")).lower()
            metadata = dict(pin.metadata)
            metadata["fault"] = fault_type
            level = int(pin.level)
            if fault_type == "stuck_high":
                level = 1
            elif fault_type == "stuck_low":
                level = 0
            elif fault_type == "noise":
                phase = int(time_ms // max(1, int(fault.get("period_ms", 20) or 20)))
                level = (phase + (abs(hash(signal)) % 2)) % 2
            elif fault_type == "delay":
                delay_ms = max(1.0, float(fault.get("delay_ms", 25.0) or 25.0))
                history = self._fault_history.get(signal, deque())
                delayed = None
                for event_time, event_level in reversed(history):
                    if (time_ms - event_time) >= delay_ms:
                        delayed = event_level
                        break
                if delayed is not None:
                    level = delayed
            pin_states[signal] = PinState(
                name=pin.name,
                level=int(level),
                direction=pin.direction,
                port=pin.port,
                bit=pin.bit,
                metadata=metadata,
            )

    def _component_metric_entry(self, device_id: str) -> dict[str, Any]:
        metrics = self._component_metrics.setdefault(device_id, {
            "transitionCount": 0,
            "lastChangeTime": None,
            "lastChangeCycle": None,
            "lastState": None,
            "stableCycles": 0,
            "timing": [],
            "timingCycles": [],
            "togglePeriodMs": None,
            "togglePeriodCycles": None,
            "humanThresholdMs": None,
            "effectiveHz": None,
        })
        return metrics

    def _update_component_metrics(self, device_id: str, state: dict[str, Any], time_ms: float, *, cycles: int, effective_hz: float) -> dict[str, Any]:
        metrics = self._component_metric_entry(device_id)
        metrics["effectiveHz"] = float(effective_hz)
        signature = repr(state)
        if metrics.get("lastState") != signature:
            previous_time = metrics.get("lastChangeTime")
            previous_cycle = metrics.get("lastChangeCycle")
            metrics["transitionCount"] = int(metrics.get("transitionCount", 0)) + 1
            metrics["lastState"] = signature
            metrics["lastChangeTime"] = time_ms
            metrics["lastChangeCycle"] = int(cycles)
            metrics["stableCycles"] = 0
            intervals = list(metrics.get("timing", []))[-(_PERIODICITY_WINDOW - 1):]
            interval_cycles = list(metrics.get("timingCycles", []))[-(_PERIODICITY_WINDOW - 1):]
            if previous_time is not None:
                intervals.append(round(time_ms - float(previous_time), 6))
            if previous_cycle is not None:
                interval_cycles.append(max(0, int(cycles) - int(previous_cycle)))
            metrics["timing"] = intervals
            metrics["timingCycles"] = interval_cycles
            metrics["togglePeriodMs"] = intervals[-1] if intervals else None
            metrics["togglePeriodCycles"] = interval_cycles[-1] if interval_cycles else None
        else:
            last_cycle = metrics.get("lastChangeCycle")
            metrics["stableCycles"] = max(0, int(cycles) - int(last_cycle)) if last_cycle is not None else 0
        return metrics

    def _signal_read_map(self, snapshot: dict[str, Any]) -> dict[str, float]:
        reads = snapshot.get("io_reads_delta")
        if reads is None:
            reads = snapshot.get("io_reads", [])
        for item in list(reads):
            signal = str(item.get("signal", "") or "")
            if not signal:
                continue
            self._observed_reads[signal] = max(self._observed_reads.get(signal, 0.0), float(item.get("time_ms", 0.0) or 0.0))
        return dict(self._observed_reads)

    def _device_last_read(self, device: VirtualDevice, bus_lookup: dict[str, list[str]], observed_reads: dict[str, float]) -> float | None:
        last_seen: float | None = None
        for pin in self._device_pin_cache.get(device.device_id) or device.connected_signals(bus_lookup):
            signal = str(pin.get("signal") or "")
            if not signal or signal not in observed_reads:
                continue
            time_ms = float(observed_reads[signal])
            last_seen = time_ms if last_seen is None else max(last_seen, time_ms)
        return last_seen

    def _signal_view(
        self,
        name: str,
        pin: PinState,
        observed_reads: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        observed = observed_reads or {}
        return {
            "level": pin.level,
            "direction": pin.direction,
            "floating": bool(pin.metadata.get("floating")),
            "contention": bool(pin.metadata.get("contention")),
            "state": pin.metadata.get("state", "low"),
            "drivers": list(pin.metadata.get("drivers", [])),
            "fault": pin.metadata.get("fault"),
            "last_read_ms": observed.get(name),
        }

    def _device_view(
        self,
        device: VirtualDevice,
        *,
        pin_states: dict[str, PinState],
        bus_lookup: dict[str, list[str]],
    ) -> dict[str, Any]:
        metrics = self._component_metric_entry(device.device_id)
        state = self._device_states.get(device.device_id)
        if state is None:
            state = device.evaluate(pin_states, bus_lookup, self._last_time_ms)
            self._device_states[device.device_id] = state
        issues = list(self._device_issues.get(device.device_id, []))
        return {
            "id": device.device_id,
            "type": device.type_name,
            "label": device.label,
            "icon": device.icon,
            "connections": dict(device.connections),
            "position": dict(device.position),
            "size": {"w": device.default_size[0], "h": device.default_size[1]},
            "settings": dict(device.settings),
            "pins": list(self._device_pin_cache.get(device.device_id, [])),
            "state": state,
            "metrics": {
                "transitionCount": int(metrics.get("transitionCount", 0)),
                "lastChangeTime": round(float(metrics.get("lastChangeTime", 0.0) or 0.0), 3),
                "lastChangeCycle": None if metrics.get("lastChangeCycle") is None else int(metrics.get("lastChangeCycle", 0)),
                "stableCycles": int(metrics.get("stableCycles", 0) or 0),
                "timing": [round(float(value), 6) for value in list(metrics.get("timing", []))[-_PERIODICITY_WINDOW:]],
                "timingCycles": [int(value) for value in list(metrics.get("timingCycles", []))[-_PERIODICITY_WINDOW:]],
                "togglePeriodMs": None if metrics.get("togglePeriodMs") is None else round(float(metrics.get("togglePeriodMs", 0.0) or 0.0), 3),
                "togglePeriodCycles": None if metrics.get("togglePeriodCycles") is None else int(metrics.get("togglePeriodCycles", 0)),
                "humanThresholdMs": None if metrics.get("humanThresholdMs") is None else round(float(metrics.get("humanThresholdMs", 0.0) or 0.0), 3),
                "lastReadTime": None if metrics.get("lastReadTime") is None else round(float(metrics.get("lastReadTime", 0.0) or 0.0), 3),
            },
            "validation": {
                "status": "fail" if any(issue.level == "error" for issue in issues) else ("warn" if issues else "pass"),
                "issues": [issue.to_dict() for issue in issues],
            },
            "schema": device.schema(),
        }

    def _append_validation_issue(self, issue: ValidationIssue) -> None:
        previous = self.validation_log[-1] if self.validation_log else None
        if previous and previous.device_id == issue.device_id and previous.message == issue.message and abs(previous.time_ms - issue.time_ms) < 0.0001:
            return
        self.validation_log.append(issue)

    def _advance_state(self, snapshot: dict[str, Any], *, force_all_devices: bool = False) -> dict[str, Any]:
        time_ms = self._time_ms(snapshot)
        cycles = int(snapshot.get("cycles", 0) or 0)
        effective_hz = float(snapshot.get("effective_clock_hz", 0) or 0)
        if effective_hz <= 0:
            clock_hz = max(1.0, float(snapshot.get("clock_hz", 1) or 1))
            effective_hz = (clock_hz / 12.0) if self.architecture == "8051" else clock_hz
        pin_states = self.pin_states(snapshot)
        bus_lookup = self._bus_lookup()
        observed_reads = self._signal_read_map(snapshot)
        previous_nodes = dict(self._signal_nodes)
        graph_issues = self._rebuild_graph(bus_lookup, pin_states, time_ms=time_ms)
        impacted, changed_signals = self._record_pin_events(previous_nodes, pin_states, time_ms, cycles=cycles)
        for signal in observed_reads:
            impacted.update(self._subscriptions.get(signal, set()))
        impacted.update(self._dirty_devices)
        if force_all_devices or not self._device_states:
            impacted.update(device.device_id for device in self.devices)

        device_issues = dict(self._device_issues)
        recent_events = list(self.signal_log)
        for device in self.devices:
            if device.device_id not in impacted and device.device_id in self._device_states:
                continue
            state = device.evaluate(pin_states, bus_lookup, time_ms)
            metrics = self._update_component_metrics(device.device_id, state, time_ms, cycles=cycles, effective_hz=effective_hz)
            metrics["lastReadTime"] = self._device_last_read(device, bus_lookup, observed_reads)
            metrics["humanThresholdMs"] = _TOGGLE_WARNING_MS if device.type_name == "led" else None
            issues = device.validate(state=state, pin_states=pin_states, metrics=metrics, time_ms=time_ms, signal_log=recent_events)
            self._device_states[device.device_id] = state
            device_issues[device.device_id] = issues
            self._device_issues[device.device_id] = issues
            for issue in issues:
                self._append_validation_issue(issue)
            if device.device_id in impacted:
                self._stream_device_updates[device.device_id] = self._device_view(
                    device,
                    pin_states=pin_states,
                    bus_lookup=bus_lookup,
                )

        self._signal_nodes = dict(pin_states)
        self._device_issues = device_issues
        self._dirty_devices.clear()
        self._last_time_ms = time_ms
        self._last_snapshot_meta = {
            "ports": dict(snapshot.get("ports", {})),
            "gpio_regs": dict(snapshot.get("gpio_regs", {})),
            "xram_sample": dict(snapshot.get("xram_sample", {})),
            "endian": snapshot.get("endian", "little"),
            "clock_hz": snapshot.get("clock_hz"),
            "effective_clock_hz": snapshot.get("effective_clock_hz"),
            "cycles": snapshot.get("cycles"),
        }
        for issue in graph_issues:
            self._append_validation_issue(issue)
        return {
            "time_ms": time_ms,
            "cycles": cycles,
            "effective_hz": effective_hz,
            "pin_states": pin_states,
            "bus_lookup": bus_lookup,
            "issues": graph_issues + [issue for issues in device_issues.values() for issue in issues],
            "observed_reads": observed_reads,
            "changed_signals": changed_signals,
        }

    def tick(self, snapshot: dict[str, Any]) -> None:
        self._advance_state(snapshot)

    def has_live_state(self) -> bool:
        return bool(self._signal_nodes)

    def _current_issues(self) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for signal, meta in self._signal_meta.items():
            if meta.get("contention"):
                issues.append(
                    ValidationIssue(
                        self._last_time_ms,
                        ",".join(str(source) for source in meta.get("drivers", [])),
                        "error",
                        f"Bus contention on {signal}",
                        "Multiple outputs are driving conflicting values on the same logical signal.",
                    )
                )
            elif meta.get("floating"):
                issues.append(
                    ValidationIssue(
                        self._last_time_ms,
                        signal,
                        "warning",
                        f"Floating signal on {signal}",
                        "An input connection exists without any active driver.",
                    )
                )
        for device_issues in self._device_issues.values():
            issues.extend(device_issues)
        return issues

    def current_payload(self) -> dict[str, Any] | None:
        if not self.has_live_state():
            return None
        pin_states = dict(self._signal_nodes)
        bus_lookup = self._bus_lookup()
        issues = self._current_issues()
        devices_payload = [
            self._device_view(device, pin_states=pin_states, bus_lookup=bus_lookup)
            for device in self.devices
        ]
        debug_payload = self._build_debug_payload(self._last_snapshot_meta, pin_states, devices_payload, issues)
        return {
            "devices": devices_payload,
            "catalog": self.available_pin_catalog(),
            "device_types": [device_cls.schema() for device_cls in DEVICE_TYPES.values()],
            "pins": {name: pin.to_dict() for name, pin in pin_states.items()},
            "wires": list(self._wires),
            "gpio": {"arm_mmio_base": ARM_GPIOA_BASE, "arm_offsets": {"odr": _ARM_GPIO_OUT, "idr": _ARM_GPIO_IN, "moder": _ARM_GPIO_DIR}},
            "debug": debug_payload,
            "time_ms": round(self._last_time_ms, 3),
            "timing": self._timing_payload(
                cycles=int(self._last_snapshot_meta.get("cycles", 0) or 0),
                effective_hz=float(self._last_snapshot_meta.get("effective_clock_hz", 0) or 0),
                time_ms=self._last_time_ms,
            ),
        }

    def current_diff(self) -> dict[str, Any]:
        issues = self._current_issues()
        return {
            "changed_ids": list(self._stream_device_updates.keys()),
            "devices": dict(self._stream_device_updates),
            "removed_ids": list(self._stream_removed_ids),
            "issues": [issue.to_dict() for issue in issues],
            "signal_changes": list(self._stream_signal_events),
            "time_ms": round(self._last_time_ms, 3),
        }

    def consume_signal_events(self) -> dict[str, Any]:
        signals = {
            name: self._signal_view(name, pin)
            for name, pin in self._signal_nodes.items()
            if name in self._stream_signal_names
        }
        payload = {
            "time_ms": round(self._last_time_ms, 3),
            "signal_changes": list(self._stream_signal_events),
            "signals": signals,
            "devices": dict(self._stream_device_updates),
            "changed_ids": list(self._stream_device_updates.keys()),
            "removed_ids": list(self._stream_removed_ids),
        }
        self._stream_signal_events.clear()
        self._stream_signal_names.clear()
        self._stream_device_updates = {}
        self._stream_removed_ids = set()
        return payload

    def sync(self, snapshot: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        context = self._advance_state(snapshot, force_all_devices=not self._last_views)
        time_ms = float(context["time_ms"])
        pin_states = dict(context["pin_states"])
        bus_lookup = dict(context["bus_lookup"])
        all_issues = list(context["issues"])
        next_views: dict[str, dict[str, Any]] = {}
        changed: dict[str, dict[str, Any]] = {}
        devices_payload: list[dict[str, Any]] = []
        for device in self.devices:
            old_view = self._last_views.get(device.device_id)
            view = self._device_view(device, pin_states=pin_states, bus_lookup=bus_lookup)
            devices_payload.append(view)
            next_views[device.device_id] = view
            if old_view != view:
                changed[device.device_id] = view
        removed_ids = [device_id for device_id in self._last_views.keys() if device_id not in next_views]
        self._last_views = next_views
        debug_payload = self._build_debug_payload(snapshot, pin_states, devices_payload, all_issues)
        payload = {
            "devices": devices_payload,
            "catalog": self.available_pin_catalog(),
            "device_types": [device_cls.schema() for device_cls in DEVICE_TYPES.values()],
            "pins": {name: pin.to_dict() for name, pin in pin_states.items()},
            "wires": list(self._wires),
            "gpio": {"arm_mmio_base": ARM_GPIOA_BASE, "arm_offsets": {"odr": _ARM_GPIO_OUT, "idr": _ARM_GPIO_IN, "moder": _ARM_GPIO_DIR}},
            "debug": debug_payload,
            "time_ms": round(time_ms, 3),
            "timing": self._timing_payload(
                cycles=int(context["cycles"]),
                effective_hz=float(context["effective_hz"]),
                time_ms=time_ms,
            ),
        }
        diff = {
            "changed_ids": list(changed.keys()),
            "devices": changed,
            "removed_ids": removed_ids,
            "issues": [issue.to_dict() for issue in all_issues],
            "signal_changes": [event.to_dict() for event in list(self.signal_log)[-16:]],
            "time_ms": round(time_ms, 3),
        }
        return payload, diff

    def _build_debug_payload(self, snapshot: dict[str, Any], pin_states: dict[str, PinState], devices_payload: list[dict[str, Any]], issues: list[ValidationIssue]) -> dict[str, Any]:
        observed_reads = self._signal_read_map(snapshot)
        port_rows: list[dict[str, Any]] = []
        if self.architecture == "arm":
            gpio_regs = dict(snapshot.get("gpio_regs", {}))
            if gpio_regs:
                out_value = int(gpio_regs.get("out", 0)) & 0xFFFFFFFF
                in_value = int(gpio_regs.get("in", 0)) & 0xFFFFFFFF
                dir_value = int(gpio_regs.get("dir", 0)) & 0xFFFFFFFF
            else:
                sample = dict(snapshot.get("xram_sample", {}))
                endian = str(snapshot.get("endian", "little"))
                out_value = _read_word(sample, _ARM_GPIO_OUT, endian=endian)
                in_value = _read_word(sample, _ARM_GPIO_IN, endian=endian)
                dir_value = _read_word(sample, _ARM_GPIO_DIR, endian=endian)
            port_rows.append({
                "name": "GPIOA",
                "hex": f"0x{out_value & 0xFFFF:04X}",
                "binary": format(out_value & 0xFFFF, "016b"),
                "input_hex": f"0x{in_value & 0xFFFF:04X}",
                "direction_hex": f"0x{dir_value & 0xFFFF:04X}",
            })
        else:
            for port_name, payload in dict(snapshot.get("ports", {})).items():
                latch = int(payload.get("latch", 0)) & 0xFF
                pin_value = int(payload.get("pin", latch)) & 0xFF
                port_rows.append({
                    "name": port_name,
                    "hex": f"0x{pin_value:02X}",
                    "binary": format(pin_value, "08b"),
                    "latch_hex": f"0x{latch:02X}",
                })
        components = [
            {
                "id": device["id"],
                "label": device["label"],
                "type": device["type"],
                "state": device["state"],
                "validation": device["validation"],
                "metrics": device["metrics"],
            }
            for device in devices_payload
        ]
        return {
            "ports": port_rows,
            "components": components,
            "issues": [issue.to_dict() for issue in issues],
            "signal_log": [event.to_dict() for event in list(self.signal_log)[-64:]],
            "signals": {
                name: {
                    "level": pin.level,
                    "direction": pin.direction,
                    "floating": bool(pin.metadata.get("floating")),
                    "contention": bool(pin.metadata.get("contention")),
                    "state": pin.metadata.get("state", "low"),
                    "drivers": list(pin.metadata.get("drivers", [])),
                    "fault": pin.metadata.get("fault"),
                    "last_read_ms": observed_reads.get(name),
                }
                for name, pin in pin_states.items()
            },
            "faults": dict(self._faults),
            "summary": {
                "pass": sum(1 for component in components if component["validation"]["status"] == "pass"),
                "warn": sum(1 for component in components if component["validation"]["status"] == "warn"),
                "fail": sum(1 for component in components if component["validation"]["status"] == "fail"),
            },
        }

    def add_device(self, device_type: str, *, label: str | None = None) -> VirtualDevice:
        device_cls = DEVICE_TYPES.get(device_type.lower())
        if device_cls is None:
            raise ValidationError("Unsupported virtual device type", context={"type": device_type})
        device = device_cls(label=label)
        self.devices.append(device)
        self._dirty_devices.add(device.device_id)
        self._graph_dirty = True
        return device

    def get_device(self, device_id: str) -> VirtualDevice:
        for device in self.devices:
            if device.device_id == device_id:
                return device
        raise ValidationError("Unknown virtual device", context={"device_id": device_id})

    def remove_device(self, device_id: str) -> None:
        device = self.get_device(device_id)
        self.devices = [item for item in self.devices if item.device_id != device.device_id]
        self._last_views.pop(device.device_id, None)
        self._device_states.pop(device.device_id, None)
        self._device_pin_cache.pop(device.device_id, None)
        self._component_metrics.pop(device.device_id, None)
        self._device_issues.pop(device.device_id, None)
        self._dirty_devices.discard(device.device_id)
        self._graph_dirty = True
        self._stream_device_updates.pop(device.device_id, None)
        self._stream_removed_ids.add(device.device_id)

    def update_device(self, device_id: str, *, label: str | None = None, connections: dict[str, Any] | None = None, position: dict[str, int] | None = None, settings: dict[str, Any] | None = None) -> VirtualDevice:
        device = self.get_device(device_id)
        if connections:
            self._validate_connections(device, connections)
        device.update(label=label, connections=connections, position=position, settings=settings)
        self._dirty_devices.add(device.device_id)
        if connections:
            self._graph_dirty = True
        return device

    def set_switch_level(self, device_id: str, level: int | bool) -> VirtualDevice:
        device = self.get_device(device_id)
        if not isinstance(device, SwitchDevice):
            raise ValidationError("Device does not accept interactive input", context={"device_id": device_id, "type": device.type_name})
        previous_level = 1 if device.settings.get("input_level", 0) else 0
        next_level = 1 if level else 0
        if previous_level != next_level:
            device.runtime["last_input_level"] = previous_level
        device.settings["input_level"] = next_level
        self._dirty_devices.add(device.device_id)
        return device

    def inject_fault(self, signal: str, fault_type: str, **options: Any) -> None:
        normalized = str(fault_type or "").lower()
        if normalized not in _FAULT_TYPES:
            raise ValidationError("Unsupported fault type", context={"signal": signal, "fault_type": fault_type, "supported": sorted(_FAULT_TYPES)})
        self._faults[str(signal)] = {"type": normalized, **options}
        self._dirty_devices.update(device.device_id for device in self.devices)

    def clear_fault(self, signal: str) -> None:
        self._faults.pop(str(signal), None)
        self._dirty_devices.update(device.device_id for device in self.devices)

    def input_bindings(self, *, time_ms: float | None = None) -> dict[str, int | None]:
        bindings: dict[str, int | None] = {}
        for device in self.devices:
            bindings.update(device.input_bindings())
        for signal, fault in self._faults.items():
            fault_type = str(fault.get("type", "")).lower()
            if signal not in bindings:
                continue
            if fault_type == "stuck_high":
                bindings[signal] = 1
            elif fault_type == "stuck_low":
                bindings[signal] = 0
            elif fault_type == "noise":
                active_time = self._last_time_ms if time_ms is None else time_ms
                phase = int(active_time // max(1, int(fault.get("period_ms", 20) or 20)))
                bindings[signal] = (phase + (abs(hash(signal)) % 2)) % 2
            elif fault_type == "delay":
                active_time = self._last_time_ms if time_ms is None else time_ms
                delay_ms = max(1.0, float(fault.get("delay_ms", 25.0) or 25.0))
                history = self._fault_history.get(signal, deque())
                delayed = None
                for event_time, event_level in reversed(history):
                    if (active_time - event_time) >= delay_ms:
                        delayed = event_level
                        break
                if delayed is not None:
                    bindings[signal] = delayed
        return bindings

    def export_state(self) -> dict[str, Any]:
        return {
            "architecture": self.architecture,
            "version": 2,
            "faults": dict(self._faults),
            "observed_reads": dict(self._observed_reads),
            "devices": [device.serialize() for device in self.devices],
        }

    def import_state(self, payload: dict[str, Any]) -> None:
        architecture = str(payload.get("architecture", self.architecture))
        if architecture != self.architecture:
            raise ValidationError(
                "Hardware configuration architecture does not match the active simulator",
                context={"hardware_architecture": architecture, "active_architecture": self.architecture},
            )
        self.devices = [VirtualDevice.from_dict(item) for item in list(payload.get("devices", []))]
        self._faults = {str(name): dict(value) for name, value in dict(payload.get("faults", {})).items()}
        self._last_views = {}
        self._device_states = {}
        self._device_pin_cache = {}
        self._graph_dirty = True
        self._component_metrics = {}
        self._device_issues = {}
        self._dirty_devices = {device.device_id for device in self.devices}
        self._fault_history = defaultdict(lambda: deque(maxlen=64))
        self._observed_reads = {str(name): float(value) for name, value in dict(payload.get("observed_reads", {})).items()}
        self.signal_log.clear()
        self.validation_log.clear()

    def run_test_suite(self, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        _ = snapshot
        tests = [
            self._test_led_blink(),
            self._test_led_high(),
            self._test_led_array_shift(),
            self._test_seven_segment_counter(),
            self._test_stepper_valid(),
            self._test_stepper_invalid(),
        ]
        return {
            "passed": all(test["status"] == "pass" for test in tests),
            "results": tests,
        }

    def _test_pin(self, bit: int) -> str:
        if self.architecture == "arm":
            return f"GPIOA.{bit}"
        return f"P1.{bit}"

    def _test_bus8(self, bank: int = 0) -> tuple[str, list[str]]:
        if self.architecture == "arm":
            if bank == 0:
                return "GPIOA_LOW", [f"GPIOA.{bit}" for bit in range(8)]
            return "GPIOA_HIGH", [f"GPIOA.{bit}" for bit in range(8, 16)]
        port = f"P{bank}"
        return port, [f"{port}.{bit}" for bit in range(8)]

    def _test_bus4(self, offset: int = 0) -> tuple[str, list[str]]:
        if self.architecture == "arm":
            start = offset * 4
            return f"GPIOA_{start}_{start + 3}", [f"GPIOA.{bit}" for bit in range(start, start + 4)]
        suffix = "LOW" if offset == 0 else "HIGH"
        bits = range(0, 4) if offset == 0 else range(4, 8)
        return f"P3_{suffix}", [f"P3.{bit}" for bit in bits]

    def _test_led_blink(self) -> dict[str, Any]:
        pin_name = self._test_pin(0)
        led = LedDevice(connections={"pin": pin_name})
        bus_lookup: dict[str, list[str]] = {}
        states = [
            {pin_name: PinState(pin_name, 0, "output", pin_name.split(".", 1)[0], 0)},
            {pin_name: PinState(pin_name, 1, "output", pin_name.split(".", 1)[0], 0)},
            {pin_name: PinState(pin_name, 0, "output", pin_name.split(".", 1)[0], 0)},
        ]
        issues: list[ValidationIssue] = []
        for index, pin_state in enumerate(states):
            state = led.evaluate(pin_state, bus_lookup, index * (_TEST_DURATION_MS / 3))
            issues.extend(led.validate(state=state, pin_states=pin_state, metrics={}, time_ms=index * (_TEST_DURATION_MS / 3), signal_log=[]))
        return {"name": "LED blink", "status": "pass" if not issues else "fail", "reason": issues[0].message if issues else "Blink propagation valid"}

    def _test_led_high(self) -> dict[str, Any]:
        pin_name = self._test_pin(0)
        led = LedDevice(connections={"pin": pin_name})
        state = led.evaluate({pin_name: PinState(pin_name, 1, "output", pin_name.split(".", 1)[0], 0)}, {}, _TEST_DURATION_MS)
        return {"name": "LED steady HIGH", "status": "pass" if state.get("on") else "fail", "reason": "LED follows steady HIGH" if state.get("on") else "LED did not turn on"}

    def _test_led_array_shift(self) -> dict[str, Any]:
        bus_name, pins_for_bus = self._test_bus8(2 if self.architecture != "arm" else 0)
        arr = LedArrayDevice(connections={"bus": bus_name})
        bus_lookup = {bus_name: pins_for_bus}
        states = []
        for value in (0x01, 0x02, 0x04, 0x08):
            pin_states = {
                pin_name: PinState(pin_name, (value >> bit) & 1, "output", pin_name.split(".", 1)[0], bit)
                for bit, pin_name in enumerate(pins_for_bus)
            }
            states.append(arr.evaluate(pin_states, bus_lookup, value))
        shift_ok = states[-1].get("pattern") in {"shift-left", "shift-right"}
        return {"name": "LED array shift", "status": "pass" if shift_ok else "fail", "reason": "Shift pattern recognized" if shift_ok else "Shift pattern not detected"}

    def _test_seven_segment_counter(self) -> dict[str, Any]:
        bus_name, pins_for_bus = self._test_bus8(0)
        seg = SevenSegmentDevice(connections={"bus": bus_name})
        bus_lookup = {bus_name: pins_for_bus}
        ok = True
        for pattern, digit in ((0x3F, "0"), (0x06, "1"), (0x5B, "2"), (0x4F, "3")):
            pins = {
                pin_name: PinState(pin_name, (pattern >> bit) & 1, "output", pin_name.split(".", 1)[0], bit)
                for bit, pin_name in enumerate(pins_for_bus)
            }
            state = seg.evaluate(pins, bus_lookup, pattern)
            if state.get("digit") != digit:
                ok = False
                break
        return {"name": "7-segment counter", "status": "pass" if ok else "fail", "reason": "Digit decoding is exact" if ok else "Digit decoding mismatch"}

    def _test_stepper_valid(self) -> dict[str, Any]:
        bus_name, pins_for_bus = self._test_bus4(0)
        stepper = StepperMotorDevice(connections={"coil_bus": bus_name})
        bus_lookup = {bus_name: pins_for_bus}
        moved = 0
        for pattern in _STEPPER_FORWARD:
            pins = {
                pin_name: PinState(pin_name, (pattern >> bit) & 1, "output", pin_name.split(".", 1)[0], bit)
                for bit, pin_name in enumerate(pins_for_bus)
            }
            state = stepper.evaluate(pins, bus_lookup, pattern)
            if state.get("moved"):
                moved += 1
        return {"name": "Stepper valid sequence", "status": "pass" if moved >= 2 else "fail", "reason": "Stepper advanced on valid full-step sequence" if moved >= 2 else "Stepper did not move on valid sequence"}

    def _test_stepper_invalid(self) -> dict[str, Any]:
        bus_name, pins_for_bus = self._test_bus4(0)
        stepper = StepperMotorDevice(connections={"coil_bus": bus_name})
        bus_lookup = {bus_name: pins_for_bus}
        issues: list[ValidationIssue] = []
        for idx, pattern in enumerate((0x09, 0x06, 0x03)):
            pins = {
                pin_name: PinState(pin_name, (pattern >> bit) & 1, "output", pin_name.split(".", 1)[0], bit)
                for bit, pin_name in enumerate(pins_for_bus)
            }
            state = stepper.evaluate(pins, bus_lookup, idx)
            issues.extend(stepper.validate(state=state, pin_states=pins, metrics={}, time_ms=idx, signal_log=[]))
        return {"name": "Stepper invalid sequence", "status": "pass" if issues else "fail", "reason": issues[0].message if issues else "Invalid sequence was not detected"}

    def _validate_connections(self, device: VirtualDevice, updates: dict[str, Any]) -> None:
        schema_by_key = {item["key"]: item for item in device.schema()["connections"]}
        catalog = self.available_pin_catalog()
        valid_pins = {item["id"] for item in catalog["pins"]}
        valid_bus8 = {item["id"] for item in catalog["bus8"]}
        valid_bus4 = {item["id"] for item in catalog["bus4"]}
        for key, value in updates.items():
            schema = schema_by_key.get(key)
            if schema is None:
                raise ValidationError("Unknown device connection", context={"device_type": device.type_name, "connection": key})
            if value in {"", None}:
                continue
            kind = schema["kind"]
            if kind == "pin" and str(value) not in valid_pins:
                raise ValidationError("Unknown pin connection", context={"connection": key, "value": value})
            if kind == "bus8" and str(value) not in valid_bus8:
                raise ValidationError("Unknown 8-bit bus connection", context={"connection": key, "value": value})
            if kind == "bus4" and str(value) not in valid_bus4:
                raise ValidationError("Unknown 4-bit bus connection", context={"connection": key, "value": value})


def apply_hardware_inputs(session: Any, hardware: VirtualHardwareManager, previous_inputs: dict[str, int | None] | None = None) -> dict[str, int | None]:
    bindings = hardware.input_bindings(time_ms=hardware._last_time_ms)
    previous = dict(previous_inputs or {})
    if session.architecture == "arm":
        if hasattr(session.cpu, "set_pin"):
            for pin_name in previous.keys() - bindings.keys():
                if not pin_name.startswith("GPIOA."):
                    continue
                bit = int(pin_name.split(".", 1)[1])
                session.cpu.set_pin(0, bit, None)
            for pin_name, level in bindings.items():
                if not pin_name.startswith("GPIOA."):
                    continue
                bit = int(pin_name.split(".", 1)[1])
                session.cpu.set_pin(0, bit, level)
        else:
            value = 0
            for pin_name, level in bindings.items():
                if not pin_name.startswith("GPIOA."):
                    continue
                bit = int(pin_name.split(".", 1)[1])
                if level:
                    value |= 1 << bit
            session.cpu.memory.write32(_ARM_GPIO_IN, value, space="xram", endian=session.endian)
        return bindings
    for pin_name in previous.keys() - bindings.keys():
        if not pin_name.startswith("P") or "." not in pin_name:
            continue
        port_name, bit_text = pin_name.split(".", 1)
        session.cpu.set_pin(int(port_name[1:]), int(bit_text), None)
    for pin_name, level in bindings.items():
        if not pin_name.startswith("P") or "." not in pin_name:
            continue
        port_name, bit_text = pin_name.split(".", 1)
        session.cpu.set_pin(int(port_name[1:]), int(bit_text), level)
    return bindings


__all__ = [
    "ARM_GPIOA_BASE",
    "DEVICE_TYPES",
    "PinState",
    "SignalEvent",
    "ValidationIssue",
    "VirtualDevice",
    "VirtualHardwareManager",
    "apply_hardware_inputs",
]
