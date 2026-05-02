#!/usr/bin/env python3
"""
HexaLogic lightweight regression + hard-audit runner.

Goals:
- Exercise the critical user workflows (step/run/reverse) end-to-end through the
  backend session + emulator + virtual hardware layers.
- Validate the trace contract is crash-proof for the frontend (pc/mnemonic/cycles always present).
- Validate hardware interactions (LED, switch) and signal log activity (waveform input source).

This script intentionally avoids external dependencies. Flask API-level checks are
skipped if Flask isn't installed in the current Python environment.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable

import pathlib


# Ensure repo root is on sys.path when executing from scripts/.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


REQUIRED_TRACE_KEYS = ("pc", "mnemonic", "cycles")


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # PASS/WARN/FAIL/SKIP
    duration_ms: float
    detail: str = ""


class Auditor:
    def __init__(self) -> None:
        self.results: list[CheckResult] = []

    def run(self, name: str, fn: Callable[[], None], *, required: bool = True) -> None:
        started = time.perf_counter()
        try:
            fn()
        except SkipCheck as exc:
            duration_ms = (time.perf_counter() - started) * 1000.0
            self.results.append(CheckResult(name=name, status="SKIP", duration_ms=duration_ms, detail=str(exc)))
            return
        except Exception as exc:  # noqa: BLE001 - audit runner wants full exception capture
            duration_ms = (time.perf_counter() - started) * 1000.0
            self.results.append(
                CheckResult(
                    name=name,
                    status="FAIL" if required else "WARN",
                    duration_ms=duration_ms,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
            return
        duration_ms = (time.perf_counter() - started) * 1000.0
        self.results.append(CheckResult(name=name, status="PASS", duration_ms=duration_ms))

    def summarize(self) -> dict[str, Any]:
        counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "SKIP": 0}
        for item in self.results:
            counts[item.status] = counts.get(item.status, 0) + 1
        return {
            "counts": counts,
            "total": len(self.results),
            "failed": counts.get("FAIL", 0),
            "warned": counts.get("WARN", 0),
            "skipped": counts.get("SKIP", 0),
        }

    def print_report(self, *, strict: bool = False) -> int:
        summary = self.summarize()
        print("=== HEXALOGIC REGRESSION AUDIT REPORT ===")
        print(f"timestamp_utc: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
        print(f"python: {sys.version.split()[0]}")
        print(f"platform: {platform.platform()}")
        print("")
        for item in self.results:
            detail = f" - {item.detail}" if item.detail else ""
            print(f"[{item.status}] {item.name} ({item.duration_ms:.1f}ms){detail}")
        print("")
        print("summary:", json.dumps(summary, separators=(",", ":")))
        if summary["failed"] > 0:
            return 2
        if strict and summary["warned"] > 0:
            return 1
        return 0


class SkipCheck(RuntimeError):
    pass


def _require_trace_entry(entry: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValueError(f"{label}: trace entry is not a dict (got {type(entry).__name__})")
    for key in REQUIRED_TRACE_KEYS:
        if key not in entry:
            raise ValueError(f"{label}: missing key `{key}` in trace entry")
    if not isinstance(entry["pc"], int):
        raise ValueError(f"{label}: pc must be int (got {type(entry['pc']).__name__})")
    if not isinstance(entry["mnemonic"], str) or not entry["mnemonic"]:
        raise ValueError(f"{label}: mnemonic must be non-empty str")
    if not isinstance(entry["cycles"], int):
        raise ValueError(f"{label}: cycles must be int (got {type(entry['cycles']).__name__})")
    return entry


def _find_device(payload: dict[str, Any], *, device_type: str) -> dict[str, Any] | None:
    for device in payload.get("devices", []) or []:
        if isinstance(device, dict) and device.get("type") == device_type:
            return device
    return None


def _core_step_into_trace_contract() -> None:
    from sim8051.session import SimulatorSession

    session = SimulatorSession(session_id="audit-step-into")
    session.set_execution_mode("fast")
    session.set_debug_mode(True)
    session.assemble("ORG 0\nNOP\nNOP\nSJMP $\n")
    response = session.step()
    _require_trace_entry(response.get("trace"), label="step.trace")
    # Step responses use a compact runtime payload; trace history lives on the full snapshot.
    snapshot = session.snapshot(include_program=False)
    trace = snapshot.get("trace", [])
    if not isinstance(trace, list) or not trace:
        raise ValueError("snapshot.trace is empty after stepping (expected trace history)")
    _require_trace_entry(trace[-1], label="snapshot.trace[-1]")


def _core_run_loop_compact_trace_summarization() -> None:
    from sim8051.session import SimulatorSession

    session = SimulatorSession(session_id="audit-run-realtime")
    session.set_execution_mode("realtime")
    session.set_debug_mode(True)
    session.assemble("ORG 0\nHERE: SJMP HERE\n")
    response = session.run(max_steps=1000)
    result = response.get("result", {})
    if not isinstance(result, dict):
        raise ValueError("run.result must be a dict")
    step_count = int(result.get("step_count") or 0)
    steps = result.get("steps", [])
    if step_count < 1:
        raise ValueError("run.result.step_count < 1")
    if not isinstance(steps, list) or not steps:
        raise ValueError("run.result.steps empty (frontend would crash without guards)")
    for idx, step in enumerate(steps[:5]):
        _require_trace_entry(step, label=f"run.result.steps[{idx}]")


def _core_reverse_step_trace_contract() -> None:
    from sim8051.session import SimulatorSession

    session = SimulatorSession(session_id="audit-reverse")
    session.set_execution_mode("fast")
    session.set_debug_mode(True)
    session.assemble("ORG 0\nINC A\nINC A\nSJMP $\n")
    for _ in range(6):
        session.step()
    back = session.step_back()
    reverted = back.get("reverted")
    if not reverted:
        raise ValueError("step_back.reverted missing/empty")
    _require_trace_entry(reverted, label="step_back.reverted")


def _core_hardware_led_toggle_and_signal_log() -> None:
    from sim8051.session import SimulatorSession

    session = SimulatorSession(session_id="audit-led-toggle")
    session.set_execution_mode("fast")
    session.set_debug_mode(True)
    session.assemble("ORG 0\nMAIN: CPL P1.0\nSJMP MAIN\n")

    led = session.hardware.add_device("led", label="LED0")
    session.hardware.update_device(led.device_id, connections={"pin": "P1.0"})

    on_samples: list[bool] = []
    for _ in range(12):
        session.step()
        snap = session.snapshot()
        hw = snap.get("hardware") or {}
        device = _find_device(hw, device_type="led")
        if not device:
            raise ValueError("hardware.devices missing LED device")
        state = device.get("state") or {}
        on_samples.append(bool(state.get("on")))

    if len(set(on_samples)) < 2:
        raise ValueError(f"LED never toggled (samples={on_samples[:8]}...)")

    hw = session.snapshot().get("hardware") or {}
    debug = hw.get("debug") or {}
    signal_log = debug.get("signal_log") or []
    if not isinstance(signal_log, list) or not signal_log:
        raise ValueError("hardware.debug.signal_log empty (waveform source missing)")
    if not any(isinstance(evt, dict) and evt.get("pin") == "P1.0" for evt in signal_log):
        raise ValueError("signal_log has no P1.0 transitions after CPL loop")


def _core_hardware_switch_input_propagation() -> None:
    from sim8051.session import SimulatorSession

    # NOTE: Bit-addressed reads on 8051 ports are modeled as latch reads (RMW semantics).
    # To validate external switch input propagation deterministically, use byte port reads.
    code = (
        "ORG 0\n"
        "    SETB P1.1\n"
        "    CLR P1.0\n"
        "MAIN:\n"
        "    MOV A,P1\n"
        "    ANL A,#02H\n"
        "    JZ LOW\n"
        "    SETB P1.0\n"
        "    SJMP MAIN\n"
        "LOW:\n"
        "    CLR P1.0\n"
        "    SJMP MAIN\n"
    )
    session = SimulatorSession(session_id="audit-switch")
    session.set_execution_mode("fast")
    session.set_debug_mode(True)
    session.assemble(code)

    led = session.hardware.add_device("led", label="LED_OUT")
    sw = session.hardware.add_device("switch", label="SW_IN")
    session.hardware.update_device(led.device_id, connections={"pin": "P1.0"})
    session.hardware.update_device(sw.device_id, connections={"pin": "P1.1"})

    def sample_p1_bit(bit: int) -> int:
        snap = session.snapshot()
        ports = snap.get("ports") or {}
        p1 = ports.get("P1") or {}
        pin_value = int(p1.get("pin", 0) or 0) & 0xFF
        return (pin_value >> bit) & 1

    def observe_bit0(expected: int, *, budget_steps: int = 18) -> bool:
        for _ in range(budget_steps):
            session.step()
            if sample_p1_bit(0) == expected:
                return True
        return False

    # Force low, expect P1.0 to eventually reflect 0.
    session.hardware.set_switch_level(sw.device_id, 0)
    if not observe_bit0(0):
        raise ValueError("P1.0 did not reflect switch=0 within step budget")

    # Force high, expect P1.0 to eventually reflect 1.
    session.hardware.set_switch_level(sw.device_id, 1)
    if not observe_bit0(1):
        raise ValueError("P1.0 did not reflect switch=1 within step budget")


def _core_waveform_generation_proxy() -> None:
    """
    Frontend waveforms are derived from (ports/registers + hardware.debug.signal_log).
    This check ensures the backend produces enough signal log activity to drive the UI.
    """

    from sim8051.session import SimulatorSession

    session = SimulatorSession(session_id="audit-waveform")
    session.set_execution_mode("fast")
    session.set_debug_mode(True)
    session.assemble("ORG 0\nMAIN: CPL P1.0\nSJMP MAIN\n")
    for _ in range(20):
        session.step()
    snap = session.snapshot()
    hw = snap.get("hardware") or {}
    debug = hw.get("debug") or {}
    signal_log = debug.get("signal_log") or []
    if len(signal_log) < 6:
        raise ValueError(f"signal_log too small ({len(signal_log)}) for waveform history")
    ports = snap.get("ports") or {}
    p1 = ports.get("P1") or {}
    if "pin" not in p1 or "latch" not in p1:
        raise ValueError("snapshot.ports.P1 missing pin/latch fields")


def _core_example_program_matrix() -> None:
    from sim8051.session import SimulatorSession

    led_blink = (_REPO_ROOT / "examples" / "led_blink_2s.asm").read_text(encoding="utf-8")
    arm_arithmetic = (_REPO_ROOT / "examples" / "arm_keil_arithmetic.asm").read_text(encoding="utf-8")

    session_8051 = SimulatorSession(session_id="audit-example-8051")
    session_8051.set_execution_mode("fast")
    assembled_8051 = session_8051.assemble(led_blink)
    if not assembled_8051.get("has_program"):
        raise ValueError("examples/led_blink_2s.asm did not assemble")
    step = session_8051.step()
    state = step.get("state", {})
    p10 = (((state.get("hardware") or {}).get("pins") or {}).get("P1.0") or {}).get("level")
    if p10 != 0:
        raise ValueError(f"examples/led_blink_2s.asm first observable output expected P1.0 low, got {p10!r}")

    session_arm = SimulatorSession(session_id="audit-example-arm", architecture="arm")
    session_arm.set_endian("little")
    session_arm.set_execution_mode("fast")
    assembled_arm = session_arm.assemble(arm_arithmetic)
    if not assembled_arm.get("has_program"):
        raise ValueError("examples/arm_keil_arithmetic.asm did not assemble")
    arm_run = session_arm.run(max_steps=128)
    registers = (arm_run.get("state") or {}).get("registers") or {}
    if int(registers.get("R8", -1)) != 4:
        raise ValueError(f"examples/arm_keil_arithmetic.asm expected R8=4, got {registers.get('R8')!r}")
    xram_changes = {
        int(address): int(after)
        for address, _before, after in (((arm_run.get("diff") or {}).get("memory") or {}).get("xram") or [])
    }
    if xram_changes.get(272) != 0 or xram_changes.get(276) != 4:
        raise ValueError(f"ARM example produced unexpected RESULT words: {xram_changes}")


def _optional_frontend_smoke_build() -> None:
    """
    Very lightweight frontend smoke check: verify the boot overlay id is consistent.
    This does not execute a browser; it just checks for the known id mismatch class of bug.
    """

    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    index = (root / "index.html").read_text(encoding="utf-8", errors="replace")
    if 'id="boot-error-box"' not in index:
        raise ValueError("index.html missing #boot-error-box")
    if 'id="error-box"' not in index:
        raise ValueError("index.html missing in-app #error-box (debug panel)")
    dist_index = root / "dist" / "index.html"
    if dist_index.exists():
        built = dist_index.read_text(encoding="utf-8", errors="replace")
        if 'id="boot-error-box"' not in built:
            raise ValueError("dist/index.html missing #boot-error-box (run `npm run build`)")
    else:
        raise SkipCheck("dist/index.html not present (run `npm run build` to generate)")


def _optional_flask_api_suite() -> None:
    try:
        import flask  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise SkipCheck(f"Flask not available in this environment: {type(exc).__name__}: {exc}")

    from api.index import app

    client = app.test_client(use_cookies=False)
    state = client.get("/api/v2/state")
    if state.status_code != 200:
        raise ValueError(f"/api/v2/state status {state.status_code}")
    payload = state.get_json() or {}
    if "architecture" not in payload or "registers" not in payload:
        raise ValueError("/api/v2/state missing required top-level fields")
    session_id = state.headers.get("X-Hexlogic-Session") or payload.get("session_id")
    if not session_id:
        raise ValueError("/api/v2/state did not return a stable session id")
    headers = {"X-Hexlogic-Session": str(session_id)}

    code = "ORG 0\nHERE: SJMP HERE\n"
    assembled = client.post("/api/v2/assemble", json={"code": code}, headers=headers)
    if assembled.status_code != 200:
        raise ValueError(f"/api/v2/assemble status {assembled.status_code}")
    assembled_payload = assembled.get_json() or {}
    if not assembled_payload.get("has_program"):
        raise ValueError("/api/v2/assemble did not set has_program")

    # Make API run deterministic: realtime mode can legitimately return 0 steps if no wall-time advanced.
    mode = client.post("/api/v2/execution-mode", json={"mode": "fast"}, headers=headers)
    if mode.status_code != 200:
        raise ValueError(f"/api/v2/execution-mode status {mode.status_code}")

    step = client.post("/api/v2/step", json={}, headers=headers)
    if step.status_code != 200:
        raise ValueError(f"/api/v2/step status {step.status_code}")
    step_payload = step.get_json() or {}
    _require_trace_entry(step_payload.get("trace"), label="api.step.trace")

    run = client.post("/api/v2/run", json={"max_steps": 600, "speed_multiplier": 1}, headers=headers)
    if run.status_code != 200:
        raise ValueError(f"/api/v2/run status {run.status_code}")
    run_payload = run.get_json() or {}
    steps = (((run_payload.get("result") or {}).get("steps")) or [])
    if not steps:
        raise ValueError("/api/v2/run returned empty steps")
    _require_trace_entry(steps[0], label="api.run.steps[0]")

    back = client.post("/api/v2/step-back", json={}, headers=headers)
    if back.status_code != 200:
        raise ValueError(f"/api/v2/step-back status {back.status_code}")
    back_payload = back.get_json() or {}
    reverted = back_payload.get("reverted")
    if reverted:
        _require_trace_entry(reverted, label="api.step_back.reverted")

    # SSE endpoints should at least respond with the correct mimetype.
    sig = client.get(f"/api/v2/events/signals?session_id={session_id}")
    if sig.status_code != 200:
        raise ValueError(f"/api/v2/events/signals status {sig.status_code}")
    if "text/event-stream" not in (sig.mimetype or ""):
        raise ValueError(f"/api/v2/events/signals mimetype={sig.mimetype}")
    runtime = client.get(f"/api/v2/events/runtime?session_id={session_id}")
    if runtime.status_code != 200:
        raise ValueError(f"/api/v2/events/runtime status {runtime.status_code}")
    if "text/event-stream" not in (runtime.mimetype or ""):
        raise ValueError(f"/api/v2/events/runtime mimetype={runtime.mimetype}")

    # Hardware endpoints: create LED + switch, connect, toggle switch, run a few steps, and run test suite.
    led = client.post("/api/v2/hardware/device", json={"type": "led", "label": "LED0"}, headers=headers)
    if led.status_code not in {200, 201}:
        raise ValueError(f"/api/v2/hardware/device(led) status {led.status_code}")
    led_id = (led.get_json() or {}).get("created_device_id")
    if not led_id:
        raise ValueError("hardware LED creation did not return id")

    sw = client.post("/api/v2/hardware/device", json={"type": "switch", "label": "SW0"}, headers=headers)
    if sw.status_code not in {200, 201}:
        raise ValueError(f"/api/v2/hardware/device(switch) status {sw.status_code}")
    sw_id = (sw.get_json() or {}).get("created_device_id")
    if not sw_id:
        raise ValueError("hardware switch creation did not return id")

    upd_led = client.patch("/api/v2/hardware/device", json={"id": led_id, "connections": {"pin": "P1.0"}}, headers=headers)
    if upd_led.status_code != 200:
        raise ValueError(f"/api/v2/hardware/device PATCH(led) status {upd_led.status_code}")

    upd_sw = client.patch("/api/v2/hardware/device", json={"id": sw_id, "connections": {"pin": "P1.1"}}, headers=headers)
    if upd_sw.status_code != 200:
        raise ValueError(f"/api/v2/hardware/device PATCH(switch) status {upd_sw.status_code}")

    # Firmware that mirrors P1.1 -> P1.0 using byte port reads (external inputs), not latch bit reads.
    mirror = (
        "ORG 0\n"
        "    SETB P1.1\n"
        "    CLR P1.0\n"
        "MAIN:\n"
        "    MOV A,P1\n"
        "    ANL A,#02H\n"
        "    JZ LOW\n"
        "    SETB P1.0\n"
        "    SJMP MAIN\n"
        "LOW:\n"
        "    CLR P1.0\n"
        "    SJMP MAIN\n"
    )
    assembled2 = client.post("/api/v2/assemble", json={"code": mirror}, headers=headers)
    if assembled2.status_code != 200:
        raise ValueError(f"/api/v2/assemble(mirror) status {assembled2.status_code}")

    level0 = client.post("/api/v2/hardware/switch", json={"device_id": sw_id, "level": 0}, headers=headers)
    if level0.status_code != 200:
        raise ValueError(f"/api/v2/hardware/switch level0 status {level0.status_code}")
    client.post("/api/v2/run", json={"max_steps": 24, "speed_multiplier": 1}, headers=headers)

    level1 = client.post("/api/v2/hardware/switch", json={"device_id": sw_id, "level": 1}, headers=headers)
    if level1.status_code != 200:
        raise ValueError(f"/api/v2/hardware/switch level1 status {level1.status_code}")
    client.post("/api/v2/run", json={"max_steps": 24, "speed_multiplier": 1}, headers=headers)

    hw_test = client.post("/api/v2/hardware/test", json={}, headers=headers)
    if hw_test.status_code != 200:
        raise ValueError(f"/api/v2/hardware/test status {hw_test.status_code}")
    hw_payload = hw_test.get_json() or {}
    if "hardware_test" not in hw_payload:
        raise ValueError("/api/v2/hardware/test missing hardware_test payload")

    # Reset/run should not drop attached hardware devices.
    before_reset = client.get("/api/v2/state", headers=headers).get_json() or {}
    before_ids = [d.get("id") for d in (before_reset.get("hardware", {}).get("devices") or []) if isinstance(d, dict)]
    reset_resp = client.post("/api/v2/reset", json={}, headers=headers)
    if reset_resp.status_code != 200:
        raise ValueError(f"/api/v2/reset status {reset_resp.status_code}")
    after_reset = reset_resp.get_json() or {}
    after_ids = [d.get("id") for d in (after_reset.get("hardware", {}).get("devices") or []) if isinstance(d, dict)]
    if led_id not in after_ids or sw_id not in after_ids:
        raise ValueError("hardware devices disappeared after reset")
    if set(before_ids) - set(after_ids):
        raise ValueError("some hardware devices missing after reset")

    run_again = client.post("/api/v2/run", json={"max_steps": 8, "speed_multiplier": 1.0}, headers=headers)
    if run_again.status_code != 200:
        raise ValueError(f"/api/v2/run(second) status {run_again.status_code}")
    run_state = run_again.get_json() or {}
    run_ids = [d.get("id") for d in (run_state.get("state", {}).get("hardware", {}).get("devices") or []) if isinstance(d, dict)]
    if led_id not in run_ids or sw_id not in run_ids:
        raise ValueError("hardware devices disappeared after run")

    state_again = client.get("/api/v2/state", headers=headers)
    if state_again.status_code != 200:
        raise ValueError(f"/api/v2/state(second) status {state_again.status_code}")
    state_again_ids = [d.get("id") for d in ((state_again.get_json() or {}).get("hardware", {}).get("devices") or []) if isinstance(d, dict)]
    if led_id not in state_again_ids or sw_id not in state_again_ids:
        raise ValueError("hardware devices disappeared after explicit session state refresh")


def _optional_cors_session_header_contract() -> None:
    try:
        import flask  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise SkipCheck(f"Flask not available in this environment: {type(exc).__name__}: {exc}")

    from api.index import app

    with app.test_client() as client:
        response = client.options(
            "/api/v2/hardware/device",
            headers={
                "Origin": "https://hexalogic.netlify.app",
                "Access-Control-Request-Method": "PATCH",
                "Access-Control-Request-Headers": "Content-Type, X-Hexlogic-Session",
            },
        )
        if response.status_code != 200:
            raise ValueError(f"CORS preflight failed with status {response.status_code}")
        allowed_headers = response.headers.get("Access-Control-Allow-Headers", "")
        if "X-Hexlogic-Session" not in allowed_headers:
            raise ValueError(f"CORS allow-headers missing X-Hexlogic-Session: {allowed_headers!r}")

        state = client.get("/api/v2/state", headers={"Origin": "https://hexalogic.netlify.app"})
        if state.status_code != 200:
            raise ValueError(f"CORS state request failed with status {state.status_code}")
        exposed_headers = state.headers.get("Access-Control-Expose-Headers", "")
        session_id = state.headers.get("X-Hexlogic-Session")
        payload = state.get_json() or {}
        if "X-Hexlogic-Session" not in exposed_headers:
            raise ValueError(f"CORS expose-headers missing X-Hexlogic-Session: {exposed_headers!r}")
        if not session_id or session_id != payload.get("session_id"):
            raise ValueError("state response did not expose a stable session id header")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="HexaLogic regression audit runner")
    parser.add_argument("--strict", action="store_true", help="Treat WARN as failure exit code")
    parser.add_argument("--json-out", default="", help="Optional JSON report output path")
    args = parser.parse_args(argv)

    auditor = Auditor()

    auditor.run("Core: Step Into trace contract", _core_step_into_trace_contract, required=True)
    auditor.run("Core: Run loop summarization contract", _core_run_loop_compact_trace_summarization, required=True)
    auditor.run("Core: Reverse step trace contract", _core_reverse_step_trace_contract, required=True)
    auditor.run("Core: Hardware LED toggle + signal log", _core_hardware_led_toggle_and_signal_log, required=True)
    auditor.run("Core: Hardware switch input propagation", _core_hardware_switch_input_propagation, required=True)
    auditor.run("Core: Waveform generation proxy (signal log)", _core_waveform_generation_proxy, required=True)
    auditor.run("Core: Example program matrix", _core_example_program_matrix, required=True)

    auditor.run("Optional: Frontend boot overlay id smoke", _optional_frontend_smoke_build, required=False)
    auditor.run("Optional: Flask API suite", _optional_flask_api_suite, required=False)
    auditor.run("Optional: CORS session header contract", _optional_cors_session_header_contract, required=False)

    if args.json_out:
        payload = {
            "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "python": sys.version,
            "platform": platform.platform(),
            "results": [item.__dict__ for item in auditor.results],
            "summary": auditor.summarize(),
        }
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)

    return auditor.print_report(strict=args.strict)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
