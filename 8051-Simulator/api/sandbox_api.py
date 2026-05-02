from __future__ import annotations

import json
import time
from typing import Any

from flask import Blueprint, Response, current_app, jsonify, make_response, request, stream_with_context
from werkzeug.exceptions import RequestEntityTooLarge

from sim8051 import AssemblyError, ExecutionError, SessionStore, ValidationError

sandbox_api = Blueprint("sandbox_api", __name__)
_SESSION_COOKIE = "hexalogic_session"
_ARCHITECTURES = {"8051", "arm"}
_EXECUTION_MODES = {"realtime", "fast"}


def _session_store() -> SessionStore:
    store = current_app.extensions.get("hexalogic_session_store") or current_app.extensions.get("hexlogic_session_store")
    if store is None:
        store = SessionStore()
    current_app.extensions["hexalogic_session_store"] = store
    current_app.extensions["hexlogic_session_store"] = store
    return store


def _get_session():
    # Prefer explicit session ids for stability across cross-origin/proxy setups where cookies
    # might not be stored/sent. This prevents "hardware disappears on reset/run" behavior when
    # the frontend accidentally talks to a fresh session.
    existing = (
        request.headers.get("X-Hexlogic-Session")
        or request.args.get("session_id")
        or request.cookies.get(_SESSION_COOKIE)
    )
    session = _session_store().get(existing)
    created = existing != session.session_id
    return session, created


def _json(payload, created_session=False, status=200):
    if isinstance(payload, dict):
        telemetry = dict(payload.get("telemetry", {}))
        telemetry["server_generated_at_ms"] = round(time.time() * 1000.0, 3)
        payload = {**payload, "telemetry": telemetry}
    response = make_response(jsonify(payload), status)
    session_id = payload.get("session_id") if isinstance(payload, dict) else None
    if session_id:
        response.headers["X-Hexlogic-Session"] = str(session_id)
    if created_session:
        response.set_cookie(_SESSION_COOKIE, payload.get("session_id") or request.cookies.get(_SESSION_COOKIE, ""), httponly=True, samesite="Lax")
    return response


def _error(error_type: str, message: str, *, context: dict[str, Any] | None = None, session_id: str | None = None, status: int = 400, created_session: bool = False):
    payload: dict[str, Any] = {
        "error": {
            "type": error_type,
            "message": message,
            "context": context or {},
        }
    }
    if session_id is not None:
        payload["session_id"] = session_id
    return _json(payload, created_session, status)


def _apply_rate_limit() -> None:
    hook = current_app.extensions.get("hexlogic_rate_limit_hook")
    if callable(hook):
        hook(request)


def _json_body() -> dict[str, Any]:
    _apply_rate_limit()
    data = request.get_json(silent=True)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValidationError("Request body must be a JSON object")
    return data


def _require_architecture(value: Any) -> str:
    architecture = str(value or "8051").lower()
    if architecture not in _ARCHITECTURES:
        raise ValidationError("Unsupported architecture", context={"supported": sorted(_ARCHITECTURES), "provided": architecture})
    return architecture


def _require_execution_mode(value: Any) -> str:
    mode = str(value or "realtime").lower()
    if mode not in _EXECUTION_MODES:
        raise ValidationError("Unsupported execution mode", context={"supported": sorted(_EXECUTION_MODES), "provided": mode})
    return mode


def _require_int(data: dict[str, Any], key: str, *, default: int | None = None, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = data.get(key, default)
    if raw is None:
        raise ValidationError(f"Missing integer field `{key}`", context={"field": key})
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Invalid integer field `{key}`", context={"field": key, "value": raw}) from exc
    if minimum is not None and value < minimum:
        raise ValidationError(f"Field `{key}` is below minimum", context={"field": key, "minimum": minimum, "value": value})
    if maximum is not None and value > maximum:
        raise ValidationError(f"Field `{key}` is above maximum", context={"field": key, "maximum": maximum, "value": value})
    return value


def _require_float(
    data: dict[str, Any],
    key: str,
    *,
    default: float | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    raw = data.get(key, default)
    if raw is None:
        raise ValidationError(f"Missing float field `{key}`", context={"field": key})
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Invalid float field `{key}`", context={"field": key, "value": raw}) from exc
    if minimum is not None and value < minimum:
        raise ValidationError(f"Field `{key}` is below minimum", context={"field": key, "minimum": minimum, "value": value})
    if maximum is not None and value > maximum:
        raise ValidationError(f"Field `{key}` is above maximum", context={"field": key, "maximum": maximum, "value": value})
    return value


def _require_int_list(data: dict[str, Any], key: str, *, minimum: int | None = None, maximum: int | None = None) -> list[int]:
    raw = data.get(key, [])
    if not isinstance(raw, list):
        raise ValidationError(f"Field `{key}` must be a list", context={"field": key})
    return [_coerce_int(item, field=key, minimum=minimum, maximum=maximum) for item in raw]


def _require_breakpoints(data: dict[str, Any], key: str = "pcs") -> list[int | dict[str, Any]]:
    raw = data.get(key, [])
    if not isinstance(raw, list):
        raise ValidationError(f"Field `{key}` must be a list", context={"field": key})
    result: list[int | dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            pc = _coerce_int(item.get("pc"), field="pc", minimum=0, maximum=0xFFFFFFFF)
            condition = item.get("condition")
            if condition is not None and not isinstance(condition, str):
                raise ValidationError("Breakpoint condition must be a string", context={"field": "condition"})
            result.append({"pc": pc, "condition": condition, "enabled": bool(item.get("enabled", True))})
        else:
            result.append(_coerce_int(item, field=key, minimum=0, maximum=0xFFFFFFFF))
    return result


def _coerce_int(value: Any, *, field: str, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Invalid integer value for `{field}`", context={"field": field, "value": value}) from exc
    if minimum is not None and parsed < minimum:
        raise ValidationError(f"Value below minimum for `{field}`", context={"field": field, "minimum": minimum, "value": parsed})
    if maximum is not None and parsed > maximum:
        raise ValidationError(f"Value above maximum for `{field}`", context={"field": field, "maximum": maximum, "value": parsed})
    return parsed


def _save_session(session) -> None:
    _session_store().save(session)


def _metrics():
    return current_app.extensions.get("hexalogic_metrics") or current_app.extensions.get("hexlogic_metrics")


def _replace_session(session) -> None:
    _session_store().save(session)


@sandbox_api.errorhandler(AssemblyError)
def _handle_assembly_error(exc: AssemblyError):
    session, created = _get_session()
    return _error("assembly", str(exc), context={"line": exc.line}, session_id=session.session_id, status=400, created_session=created)


@sandbox_api.errorhandler(ExecutionError)
def _handle_execution_error(exc: ExecutionError):
    session, created = _get_session()
    return _error("execution", str(exc), context={"pc": exc.pc}, session_id=session.session_id, status=400, created_session=created)


@sandbox_api.errorhandler(ValidationError)
def _handle_validation_error(exc: ValidationError):
    session, created = _get_session()
    return _error("validation", str(exc), context=exc.context, session_id=session.session_id, status=400, created_session=created)


@sandbox_api.errorhandler(RequestEntityTooLarge)
def _handle_payload_too_large(_exc):
    session, created = _get_session()
    return _error("validation", "Request payload too large", context={}, session_id=session.session_id, status=413, created_session=created)


@sandbox_api.route("/api/v2/state", methods=["GET"])
def get_state():
    _apply_rate_limit()
    session, created = _get_session()
    payload = session.snapshot(include_program=True)
    return _json(payload, created)


@sandbox_api.route("/api/v2/events/runtime", methods=["GET"])
def runtime_events():
    _apply_rate_limit()
    session, created = _get_session()
    session_id = session.session_id

    @stream_with_context
    def _stream():
        last_token = None
        while True:
            active = _session_store().get(session_id)
            payload = active.runtime_state()
            state = payload.get("state", {})
            token = (
                int(state.get("cycles", 0) or 0),
                float(state.get("hardware", {}).get("time_ms", 0.0) or 0.0),
                float(active.updated_at or 0.0),
            )
            if token != last_token:
                message = json.dumps(
                    {
                        "session_id": session_id,
                        **payload,
                        "telemetry": {"server_generated_at_ms": round(time.time() * 1000.0, 3), "channel": "runtime"},
                    },
                    separators=(",", ":"),
                )
                yield f"data:{message}\n\n"
                last_token = token
            else:
                yield ":keepalive\n\n"
            time.sleep(0.05)

    response = Response(_stream(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    if created:
        response.set_cookie(_SESSION_COOKIE, session_id, httponly=True, samesite="Lax")
    return response


@sandbox_api.route("/api/v2/events/signals", methods=["GET"])
def signal_events():
    _apply_rate_limit()
    session, created = _get_session()
    session_id = session.session_id

    @stream_with_context
    def _stream():
        while True:
            active = _session_store().get(session_id)
            payload = active.signal_event_state()
            hardware = payload.get("hardware", {})
            if hardware.get("signal_changes") or hardware.get("changed_ids") or hardware.get("removed_ids"):
                message = json.dumps(
                    {
                        "session_id": session_id,
                        **payload,
                        "telemetry": {"server_generated_at_ms": round(time.time() * 1000.0, 3), "channel": "signals"},
                    },
                    separators=(",", ":"),
                )
                yield f"data:{message}\n\n"
            else:
                yield ":keepalive\n\n"
            time.sleep(0.02)

    response = Response(_stream(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    if created:
        response.set_cookie(_SESSION_COOKIE, session_id, httponly=True, samesite="Lax")
    return response


@sandbox_api.route("/api/v2/reset", methods=["POST"])
def reset():
    session, created = _get_session()
    payload = session.reset()
    _save_session(session)
    return _json(payload, created)


@sandbox_api.route("/api/v2/assemble", methods=["POST"])
def assemble():
    session, created = _get_session()
    data = _json_body()
    # Backwards/alt client compatibility: some callers send `source_code`.
    code = str(data.get("code", data.get("source_code", "")))
    max_chars = int(current_app.config.get("HEXLOGIC_MAX_SOURCE_CHARS", 200_000))
    if len(code) > max_chars:
        raise ValidationError("Source code exceeds configured size limit", context={"limit": max_chars})
    payload = session.assemble(code)
    _save_session(session)
    return _json(payload, created)


@sandbox_api.route("/api/v2/step", methods=["POST"])
def step():
    session, created = _get_session()
    payload = session.step()
    payload["session_id"] = session.session_id
    _save_session(session)
    return _json(payload, created)


@sandbox_api.route("/api/v2/step-over", methods=["POST"])
def step_over():
    session, created = _get_session()
    payload = session.step_over()
    payload["session_id"] = session.session_id
    _save_session(session)
    return _json(payload, created)


@sandbox_api.route("/api/v2/step-out", methods=["POST"])
def step_out():
    session, created = _get_session()
    payload = session.step_out()
    payload["session_id"] = session.session_id
    _save_session(session)
    return _json(payload, created)


@sandbox_api.route("/api/v2/step-back", methods=["POST"])
def step_back():
    session, created = _get_session()
    payload = session.step_back()
    payload["session_id"] = session.session_id
    _save_session(session)
    return _json(payload, created)


@sandbox_api.route("/api/v2/run", methods=["POST"])
def run():
    session, created = _get_session()
    data = _json_body()
    max_limit = int(current_app.config.get("HEXLOGIC_MAX_RUN_STEPS", 100_000))
    payload = session.run(
        max_steps=_require_int(data, "max_steps", default=1000, minimum=1, maximum=max_limit),
        speed_multiplier=_require_float(data, "speed_multiplier", default=1.0, minimum=0.1, maximum=10.0),
    )
    metrics = _metrics()
    if metrics is not None:
        metrics.record_run(steps=int(payload["metrics"]["steps"]), elapsed_seconds=float(payload["metrics"]["elapsed_ms"]) / 1000.0)
    payload["session_id"] = session.session_id
    _save_session(session)
    return _json(payload, created)


@sandbox_api.route("/api/v2/breakpoints", methods=["POST"])
def breakpoints():
    session, created = _get_session()
    data = _json_body()
    payload = session.set_breakpoints(_require_breakpoints(data, "pcs"))
    _save_session(session)
    return _json(payload, created)


@sandbox_api.route("/api/v2/watchpoints", methods=["POST"])
def watchpoints():
    session, created = _get_session()
    data = _json_body()
    watchpoints = data.get("watchpoints", [])
    if not isinstance(watchpoints, list):
        raise ValidationError("Field `watchpoints` must be a list", context={"field": "watchpoints"})
    for item in watchpoints:
        if not isinstance(item, dict):
            raise ValidationError("Each watchpoint must be an object", context={"field": "watchpoints"})
        space = str(item.get("space", "iram"))
        if space not in {"iram", "sfr", "xram", "code", "bit", "register"}:
            raise ValidationError("Unsupported watchpoint space", context={"space": space})
        target = item.get("target", item.get("address", item.get("register")))
        if target is None:
            raise ValidationError("Watchpoint target is required", context={"field": "target"})
    payload = session.set_watchpoints(watchpoints)
    _save_session(session)
    return _json(payload, created)


@sandbox_api.route("/api/v2/pins", methods=["POST"])
def pins():
    session, created = _get_session()
    data = _json_body()
    payload = session.inject_pin(
        _require_int(data, "port", minimum=0, maximum=3),
        _require_int(data, "bit", minimum=0, maximum=7),
        data.get("level"),
    )
    _save_session(session)
    return _json(payload, created)


@sandbox_api.route("/api/v2/serial/rx", methods=["POST"])
def serial_rx():
    session, created = _get_session()
    data = _json_body()
    payload = session.inject_serial_rx(_require_int_list(data, "bytes", minimum=0, maximum=0xFF))
    _save_session(session)
    return _json(payload, created)


@sandbox_api.route("/api/v2/memory", methods=["POST"])
def memory():
    session, created = _get_session()
    data = _json_body()
    space = str(data.get("space", "")).lower()
    if space not in {"iram", "sfr", "xram"}:
        raise ValidationError("Unsupported memory space", context={"space": space})
    limit = 0xFFFF if space == "xram" else 0xFF
    payload = session.edit_memory(
        space=space,
        address=_require_int(data, "address", minimum=0, maximum=limit),
        value=_require_int(data, "value", minimum=0, maximum=0xFF),
    )
    _save_session(session)
    return _json(payload, created)


@sandbox_api.route("/api/v2/clock", methods=["POST"])
def clock():
    session, created = _get_session()
    data = _json_body()
    payload = session.set_clock(_require_int(data, "hz", default=11_059_200, minimum=1, maximum=10_000_000_000))
    _save_session(session)
    return _json(payload, created)


@sandbox_api.route("/api/v2/architecture", methods=["POST"])
def architecture():
    session, created = _get_session()
    data = _json_body()
    payload = session.set_architecture(_require_architecture(data.get("architecture")))
    if payload["architecture"] == "arm" and "endian" in data:
        payload = session.set_endian(str(data.get("endian", "little")))
    _save_session(session)
    return _json(payload, created)


@sandbox_api.route("/api/v2/endian", methods=["POST"])
def endian():
    session, created = _get_session()
    data = _json_body()
    payload = session.set_endian(str(data.get("endian", "little")))
    _save_session(session)
    return _json(payload, created)


@sandbox_api.route("/api/v2/debug", methods=["POST"])
def debug():
    session, created = _get_session()
    data = _json_body()
    payload = session.set_debug_mode(bool(data.get("enabled", False)))
    _save_session(session)
    return _json(payload, created)


@sandbox_api.route("/api/v2/execution-mode", methods=["POST"])
def execution_mode():
    session, created = _get_session()
    data = _json_body()
    payload = session.set_execution_mode(_require_execution_mode(data.get("mode")))
    _save_session(session)
    return _json(payload, created)


@sandbox_api.route("/api/v2/export", methods=["GET"])
def export_session():
    _apply_rate_limit()
    session, created = _get_session()
    return _json({"session_id": session.session_id, "export": session.export_state()}, created)


@sandbox_api.route("/api/v2/import", methods=["POST"])
def import_session():
    session, created = _get_session()
    data = _json_body()
    payload = data.get("session")
    if not isinstance(payload, dict):
        raise ValidationError("Field `session` must be an object", context={"field": "session"})
    restored = type(session).import_state(payload)
    restored.session_id = session.session_id
    restored.touch()
    _replace_session(restored)
    return _json(restored.snapshot(include_program=True), created)


def _require_str(data: dict[str, Any], key: str, *, default: str | None = None) -> str:
    raw = data.get(key, default)
    if raw is None:
        raise ValidationError(f"Missing string field `{key}`", context={"field": key})
    return str(raw)


@sandbox_api.route("/api/v2/hardware/device", methods=["POST"])
def hardware_add_device():
    session, created = _get_session()
    data = _json_body()
    label = data.get("label")
    if label is not None:
        label = str(label)
    device = session.hardware.add_device(_require_str(data, "type"), label=label)
    _save_session(session)
    # Keep 200 for compatibility with existing clients/tests; include id as extra metadata.
    return _json({**session.snapshot(), "created_device_id": device.device_id}, created, status=200)


@sandbox_api.route("/api/v2/hardware/device", methods=["PATCH"])
def hardware_update_device():
    session, created = _get_session()
    data = _json_body()
    device_id = _require_str(data, "id")
    session.hardware.update_device(
        device_id,
        label=data.get("label"),
        connections=data.get("connections") if isinstance(data.get("connections"), dict) else None,
        position=data.get("position") if isinstance(data.get("position"), dict) else None,
        settings=data.get("settings") if isinstance(data.get("settings"), dict) else None,
    )
    _save_session(session)
    return _json(session.snapshot(), created)


@sandbox_api.route("/api/v2/hardware/device", methods=["DELETE"])
def hardware_remove_device():
    session, created = _get_session()
    data = _json_body()
    session.hardware.remove_device(_require_str(data, "id"))
    _save_session(session)
    return _json(session.snapshot(), created)


@sandbox_api.route("/api/v2/hardware/switch", methods=["POST"])
def hardware_switch():
    session, created = _get_session()
    data = _json_body()
    session.hardware.set_switch_level(_require_str(data, "device_id"), _require_int(data, "level", minimum=0, maximum=1))
    _save_session(session)
    return _json(session.snapshot(), created)


@sandbox_api.route("/api/v2/hardware/fault", methods=["POST"])
def hardware_fault():
    session, created = _get_session()
    data = _json_body()
    signal = _require_str(data, "signal")
    fault_type = _require_str(data, "type")
    enabled = bool(data.get("enabled", True))
    if enabled:
        options = {}
        if "delay_ms" in data:
            options["delay_ms"] = _require_int(data, "delay_ms", minimum=1, maximum=60_000)
        if "period_ms" in data:
            options["period_ms"] = _require_int(data, "period_ms", minimum=1, maximum=60_000)
        session.hardware.inject_fault(signal, fault_type, **options)
    else:
        session.hardware.clear_fault(signal)
    _save_session(session)
    return _json(session.snapshot(), created)


@sandbox_api.route("/api/v2/hardware/export", methods=["GET"])
def hardware_export():
    _apply_rate_limit()
    session, created = _get_session()
    return _json({"session_id": session.session_id, "hardware": session.hardware.export_state()}, created)


@sandbox_api.route("/api/v2/hardware/import", methods=["POST"])
def hardware_import():
    session, created = _get_session()
    data = _json_body()
    payload = data.get("hardware")
    if not isinstance(payload, dict):
        raise ValidationError("Field `hardware` must be an object", context={"field": "hardware"})
    session.hardware.import_state(payload)
    _save_session(session)
    return _json(session.snapshot(), created)


@sandbox_api.route("/api/v2/hardware/bridge", methods=["GET"])
def hardware_bridge():
    """Optional: GPIO snapshot for external tooling / hybrid setups."""
    _apply_rate_limit()
    session, created = _get_session()
    payload = session.cpu.snapshot()
    full, _ = session.hardware.sync(payload)
    return _json(
        {
            "session_id": session.session_id,
            "architecture": session.architecture,
            "endian": session.endian,
            "pins": full.get("pins"),
            "gpio": full.get("gpio"),
            "devices": full.get("devices"),
        },
        created,
    )


@sandbox_api.route("/api/v2/hardware/test", methods=["POST"])
def hardware_test():
    session, created = _get_session()
    payload = session.run_hardware_test()
    payload["session_id"] = session.session_id
    _save_session(session)
    return _json(payload, created)


@sandbox_api.route("/api/v2/metrics", methods=["GET"])
def metrics():
    _apply_rate_limit()
    stats = _session_store().stats()
    metrics_registry = _metrics()
    payload = metrics_registry.snapshot(active_sessions=stats["active_sessions"], estimated_bytes=stats["estimated_bytes"]) if metrics_registry else stats
    return _json({"metrics": payload}, False)
