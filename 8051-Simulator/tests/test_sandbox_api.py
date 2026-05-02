from api.index import app


def _read_first_sse_chunk(client, path: str) -> tuple[int, str]:
    response = client.get(path, buffered=False)
    try:
        chunk = next(response.response).decode("utf-8", "replace")
        return response.status_code, chunk
    finally:
        response.close()


def test_v2_api_creates_isolated_sessions_and_uses_json_state():
    app.testing = True

    with app.test_client() as first:
        first_state = first.get("/api/v2/state")
        assert first_state.status_code == 200
        first_payload = first_state.get_json()
        first_session = first_payload["session_id"]
        assert first_payload["telemetry"]["server_generated_at_ms"] > 0
        assemble_one = first.post("/api/v2/assemble", json={"code": "MOV A,#01H\nEND"})
        assert assemble_one.status_code == 200
        step_one = first.post("/api/v2/step")
        assert step_one.status_code == 200
        assert step_one.get_json()["state"]["registers"]["A"] == 0x01

    with app.test_client() as second:
        second_state = second.get("/api/v2/state")
        assert second_state.status_code == 200
        second_session = second_state.get_json()["session_id"]
        assert first_session != second_session

        assemble_two = second.post("/api/v2/assemble", json={"code": "MOV A,#02H\nEND"})
        assert assemble_two.status_code == 200
        step_two = second.post("/api/v2/step")
        assert step_two.status_code == 200
        assert step_two.get_json()["state"]["registers"]["A"] == 0x02


def test_v2_api_reports_structured_assembly_errors():
    app.testing = True

    with app.test_client() as client:
        response = client.post("/api/v2/assemble", json={"code": "MOVX @DPTR\nEND"})

        assert response.status_code == 400
        payload = response.get_json()
        assert payload["error"]["type"] == "assembly"
        assert payload["error"]["context"]["line"] == 1


def test_v2_api_validates_input_ranges():
    app.testing = True

    with app.test_client() as client:
        response = client.post("/api/v2/pins", json={"port": 9, "bit": 0, "level": 1})

        assert response.status_code == 400
        payload = response.get_json()
        assert payload["error"]["type"] == "validation"
        assert payload["error"]["context"]["field"] == "port"


def test_v2_api_supports_architecture_switch_endian_debug_and_metrics():
    app.testing = True

    with app.test_client() as client:
        response = client.post("/api/v2/architecture", json={"architecture": "arm"})
        assert response.status_code == 200
        assert response.get_json()["architecture"] == "arm"

        response = client.post("/api/v2/endian", json={"endian": "big"})
        assert response.status_code == 200
        assert response.get_json()["endian"] == "big"

        response = client.post("/api/v2/debug", json={"enabled": True})
        assert response.status_code == 200
        assert response.get_json()["debug_mode"] is True

        assemble = client.post(
            "/api/v2/assemble",
            json={
                "code": "\n".join(
                    [
                        "ORG 0000H",
                        "MOV R0, #4",
                        "MOV R1, #12",
                        "ADD R2, R0, R1",
                        "END",
                    ]
                )
            },
        )
        assert assemble.status_code == 200
        step = client.post("/api/v2/step")
        assert step.status_code == 200
        assert step.get_json()["state"]["architecture"] == "arm"

        metrics = client.get("/api/v2/metrics")
        assert metrics.status_code == 200
        payload = metrics.get_json()["metrics"]
        assert payload["active_sessions"] >= 1
        assert "api_requests" in payload


def test_v2_api_rejects_oversized_source_payloads():
    app.testing = True
    previous_limit = app.config["HEXLOGIC_MAX_SOURCE_CHARS"]
    app.config["HEXLOGIC_MAX_SOURCE_CHARS"] = 8

    try:
        with app.test_client() as client:
            response = client.post("/api/v2/assemble", json={"code": "MOV A,#01H\nEND"})

            assert response.status_code == 400
            payload = response.get_json()
            assert payload["error"]["type"] == "validation"
    finally:
        app.config["HEXLOGIC_MAX_SOURCE_CHARS"] = previous_limit


def test_v2_api_step_back_and_session_export_import():
    app.testing = True

    with app.test_client() as client:
        assemble = client.post("/api/v2/assemble", json={"code": "MOV A,#01H\nINC A\nEND"})
        assert assemble.status_code == 200

        step_one = client.post("/api/v2/step")
        assert step_one.status_code == 200
        step_two = client.post("/api/v2/step")
        assert step_two.status_code == 200
        assert step_two.get_json()["state"]["registers"]["A"] == 0x02

        rewind = client.post("/api/v2/step-back")
        assert rewind.status_code == 200
        assert rewind.get_json()["state"]["registers"]["A"] == 0x01

        exported = client.get("/api/v2/export")
        assert exported.status_code == 200
        session_payload = exported.get_json()["export"]

    with app.test_client() as client:
        imported = client.post("/api/v2/import", json={"session": session_payload})
        assert imported.status_code == 200
        payload = imported.get_json()
        assert payload["registers"]["A"] == 0x01
        assert payload["has_program"] is True


def test_v2_api_step_response_includes_telemetry():
    app.testing = True

    with app.test_client() as client:
        assemble = client.post("/api/v2/assemble", json={"code": "MOV A,#01H\nEND"})
        assert assemble.status_code == 200

        step = client.post("/api/v2/step")
        assert step.status_code == 200
        payload = step.get_json()
        assert payload["telemetry"]["server_generated_at_ms"] > 0


def test_v2_api_exercises_full_route_surface_for_8051_hardware_and_arm():
    app.testing = True

    with app.test_client() as client:
        execution_mode = client.post("/api/v2/execution-mode", json={"mode": "fast"})
        assert execution_mode.status_code == 200
        assert execution_mode.get_json()["execution_mode"] == "fast"

        debug_mode = client.post("/api/v2/debug", json={"enabled": True})
        assert debug_mode.status_code == 200
        assert debug_mode.get_json()["debug_mode"] is True

        clock = client.post("/api/v2/clock", json={"hz": 12_000_000})
        assert clock.status_code == 200
        assert clock.get_json()["clock_hz"] == 12_000_000

        source_8051 = "\n".join(
            [
                "ORG 0000H",
                "MOV P1,#0FEH",
                "ACALL SUB",
                "MOV SBUF,A",
                "SJMP DONE",
                "SUB:",
                "MOV A,#055H",
                "RET",
                "DONE:",
                "END",
            ]
        )
        assemble = client.post("/api/v2/assemble", json={"code": source_8051})
        assert assemble.status_code == 200

        first_step = client.post("/api/v2/step")
        assert first_step.status_code == 200
        step_payload = first_step.get_json()
        assert step_payload["state"]["hardware"]["pins"]["P1.0"]["level"] == 0

        step_over = client.post("/api/v2/step-over")
        assert step_over.status_code == 200
        step_over_payload = step_over.get_json()
        assert step_over_payload["state"]["registers"]["A"] == 0x55
        assert client.get("/api/v2/state").get_json()["call_stack"] == []

        rewind = client.post("/api/v2/step-back")
        assert rewind.status_code == 200

        reassemble = client.post("/api/v2/assemble", json={"code": source_8051})
        assert reassemble.status_code == 200
        assert client.post("/api/v2/step").status_code == 200
        assert client.post("/api/v2/step").status_code == 200
        step_out = client.post("/api/v2/step-out")
        assert step_out.status_code == 200
        step_out_payload = step_out.get_json()
        assert step_out_payload["state"]["registers"]["A"] == 0x55
        assert client.get("/api/v2/state").get_json()["call_stack"] == []

        breakpoints = client.post("/api/v2/breakpoints", json={"pcs": [3]})
        assert breakpoints.status_code == 200
        assert breakpoints.get_json()["breakpoints"][0]["pc"] == 3

        watchpoints = client.post("/api/v2/watchpoints", json={"watchpoints": [{"space": "sfr", "target": 0x99}]})
        assert watchpoints.status_code == 200
        assert watchpoints.get_json()["watchpoints"][0]["target"] == 0x99

        pins = client.post("/api/v2/pins", json={"port": 1, "bit": 0, "level": 1})
        assert pins.status_code == 200
        assert pins.get_json()["hardware"]["pins"]["P1.0"]["level"] == 1

        serial_rx = client.post("/api/v2/serial/rx", json={"bytes": [0x41, 0x42]})
        assert serial_rx.status_code == 200
        assert serial_rx.get_json()["serial"]["rx_pending"] == [0x41, 0x42]

        memory = client.post("/api/v2/memory", json={"space": "iram", "address": 0x20, "value": 0xAA})
        assert memory.status_code == 200
        assert memory.get_json()["iram"]["32"] == 0xAA

        run_8051 = client.post("/api/v2/run", json={"max_steps": 16, "speed_multiplier": 1.0})
        assert run_8051.status_code == 200
        assert run_8051.get_json()["state"]["registers"]["A"] == 0x55

        runtime_status, runtime_chunk = _read_first_sse_chunk(client, "/api/v2/events/runtime")
        assert runtime_status == 200
        assert runtime_chunk.startswith("data:") or runtime_chunk.startswith(":keepalive")

        led_response = client.post("/api/v2/hardware/device", json={"type": "led", "label": "Audit LED"})
        assert led_response.status_code == 200
        led_id = led_response.get_json()["hardware"]["devices"][-1]["id"]

        switch_response = client.post("/api/v2/hardware/device", json={"type": "switch", "label": "Audit Switch"})
        assert switch_response.status_code == 200
        switch_id = [device["id"] for device in switch_response.get_json()["hardware"]["devices"] if device["type"] == "switch"][-1]

        update_device = client.patch(
            "/api/v2/hardware/device",
            json={"id": led_id, "connections": {"pin": "P1.0"}, "position": {"x": 12, "y": 24}},
        )
        assert update_device.status_code == 200
        led_device = [device for device in update_device.get_json()["hardware"]["devices"] if device["id"] == led_id][0]
        assert led_device["connections"]["pin"] == "P1.0"

        switch_level = client.post("/api/v2/hardware/switch", json={"device_id": switch_id, "level": 1})
        assert switch_level.status_code == 200
        switch_device = [device for device in switch_level.get_json()["hardware"]["devices"] if device["id"] == switch_id][0]
        assert switch_device["state"]["input_level"] == 1

        fault_enable = client.post("/api/v2/hardware/fault", json={"signal": "P1.0", "type": "noise", "enabled": True, "period_ms": 5})
        assert fault_enable.status_code == 200
        assert fault_enable.get_json()["hardware"]["debug"]["signals"]["P1.0"]["fault"] == "noise"

        bridge = client.get("/api/v2/hardware/bridge")
        assert bridge.status_code == 200
        bridge_payload = bridge.get_json()
        assert len(bridge_payload["devices"]) >= 2

        hardware_export = client.get("/api/v2/hardware/export")
        assert hardware_export.status_code == 200
        exported_hardware = hardware_export.get_json()["hardware"]

        hardware_import = client.post("/api/v2/hardware/import", json={"hardware": exported_hardware})
        assert hardware_import.status_code == 200
        assert len(hardware_import.get_json()["hardware"]["devices"]) >= 2

        hardware_test = client.post("/api/v2/hardware/test")
        assert hardware_test.status_code == 200
        assert len(hardware_test.get_json()["hardware_test"]["results"]) > 0

        signal_status, signal_chunk = _read_first_sse_chunk(client, "/api/v2/events/signals")
        assert signal_status == 200
        assert signal_chunk.startswith("data:") or signal_chunk.startswith(":keepalive")

        fault_disable = client.post("/api/v2/hardware/fault", json={"signal": "P1.0", "type": "noise", "enabled": False})
        assert fault_disable.status_code == 200
        assert fault_disable.get_json()["hardware"]["debug"]["signals"]["P1.0"]["fault"] is None

        delete_switch = client.delete("/api/v2/hardware/device", json={"id": switch_id})
        assert delete_switch.status_code == 200
        delete_led = client.delete("/api/v2/hardware/device", json={"id": led_id})
        assert delete_led.status_code == 200
        remaining_devices = {device["id"] for device in delete_led.get_json()["hardware"]["devices"]}
        assert switch_id not in remaining_devices
        assert led_id not in remaining_devices

        exported_session = client.get("/api/v2/export")
        assert exported_session.status_code == 200
        session_payload = exported_session.get_json()["export"]

        imported_session = client.post("/api/v2/import", json={"session": session_payload})
        assert imported_session.status_code == 200
        assert imported_session.get_json()["has_program"] is True

        architecture = client.post("/api/v2/architecture", json={"architecture": "arm", "endian": "little"})
        assert architecture.status_code == 200
        assert architecture.get_json()["architecture"] == "arm"
        assert architecture.get_json()["endian"] == "little"

        arm_source = "\n".join(
            [
                "AREA PROGRAM, CODE, READONLY",
                "ENTRY",
                "MAIN",
                "LDR R0, =NUM1",
                "LDR R1, =NUM2",
                "LDR R2, =RESULT",
                "LDR R3, [R0]",
                "LDR R4, [R0, #4]",
                "LDR R5, [R1]",
                "LDR R6, [R1, #4]",
                "UMLAL R3, R4, R5, R6",
                "STR R3, [R2]",
                "STR R4, [R2, #4]",
                "LDR R0, =NUM1",
                "LDR R1, =NUM2",
                "LDR R2, =RESULT",
                "LDR R3, [R0]",
                "LDR R4, [R1]",
                "ADDS R5, R3, R4",
                "STR R5, [R2]",
                "LDR R6, [R0, #4]",
                "LDR R7, [R1, #4]",
                "ADC R8, R6, R7",
                "STR R8, [R2, #4]",
                "STOP B STOP",
                "AREA PROGRAM, DATA, READONLY",
                "NUM1 DCD 0xFFFFFFFF",
                " DCD 0x00000001",
                "NUM2 DCD 0x00000001",
                " DCD 0x00000002",
                "RESULT DCD 0x00000000",
                " DCD 0x00000000",
                "END",
            ]
        )
        arm_assemble = client.post("/api/v2/assemble", json={"code": arm_source})
        assert arm_assemble.status_code == 200

        arm_run = client.post("/api/v2/run", json={"max_steps": 32, "speed_multiplier": 1.0})
        assert arm_run.status_code == 200
        arm_run_payload = arm_run.get_json()
        arm_state = arm_run_payload["state"]
        assert arm_state["architecture"] == "arm"
        assert arm_state["registers"]["R8"] == 4
        xram_changes = {address: after for address, _before, after in arm_run_payload["diff"]["memory"]["xram"]}
        assert xram_changes[272] == 0
        assert xram_changes[276] == 4

        endian = client.post("/api/v2/endian", json={"endian": "big"})
        assert endian.status_code == 200
        assert endian.get_json()["endian"] == "big"

        reset = client.post("/api/v2/reset")
        assert reset.status_code == 200
        assert reset.get_json()["cycles"] == 0

        metrics = client.get("/api/v2/metrics")
        assert metrics.status_code == 200
        assert "api_requests" in metrics.get_json()["metrics"]


def test_v2_api_external_interrupt_and_delayed_serial_rx_behave_end_to_end():
    app.testing = True

    source = "\n".join(
        [
            "ORG 0000H",
            "LJMP MAIN",
            "ORG 0003H",
            "EX0_ISR:",
            "INC A",
            "RETI",
            "ORG 0023H",
            "SER_ISR:",
            "MOV A,SBUF",
            "CLR RI",
            "INC R0",
            "RETI",
            "ORG 0030H",
            "MAIN:",
            "MOV A,#00H",
            "MOV R0,#00H",
            "SETB EX0",
            "SETB ES",
            "SETB EA",
            "SETB IT0",
            "NOP",
            "NOP",
            "MOV SCON,#050H",
            "WAIT:",
            "SJMP WAIT",
            "END",
        ]
    )

    with app.test_client() as client:
        assemble = client.post("/api/v2/assemble", json={"code": source})
        assert assemble.status_code == 200

        for _ in range(7):
            step = client.post("/api/v2/step")
            assert step.status_code == 200

        falling_edge = client.post("/api/v2/pins", json={"port": 3, "bit": 2, "level": 0})
        assert falling_edge.status_code == 200

        interrupt_step = client.post("/api/v2/step")
        assert interrupt_step.status_code == 200
        interrupt_payload = interrupt_step.get_json()
        assert interrupt_payload["trace"]["interrupt"] == "EX0"
        assert interrupt_payload["state"]["registers"]["A"] == 0x01

        serial_rx = client.post("/api/v2/serial/rx", json={"bytes": [0x41]})
        assert serial_rx.status_code == 200
        assert serial_rx.get_json()["serial"]["rx_pending"] == [0x41]

        run = client.post("/api/v2/run", json={"max_steps": 12, "speed_multiplier": 1.0})
        assert run.status_code == 200
        run_payload = run.get_json()
        assert run_payload["state"]["registers"]["A"] == 0x41
        assert run_payload["state"]["registers"]["R0"] == 0x01
        assert "SER" in run_payload["diff"]["interrupts"]


def test_api_cors_headers_advertise_hardware_patch_and_delete_methods():
    app.testing = True

    with app.test_client() as client:
        response = client.options(
            "/api/v2/hardware/device",
            headers={"Origin": "https://hexalogic-simulator.netlify.app", "Access-Control-Request-Method": "PATCH"},
        )

        assert response.status_code == 200
        methods = response.headers["Access-Control-Allow-Methods"]
        assert "PATCH" in methods
        assert "DELETE" in methods
        headers = response.headers["Access-Control-Allow-Headers"]
        assert "X-Hexlogic-Session" in headers


def test_api_cors_exposes_session_header_for_direct_cross_origin_clients():
    app.testing = True

    with app.test_client() as client:
        response = client.get("/api/v2/state", headers={"Origin": "https://hexalogic.netlify.app"})

        assert response.status_code == 200
        assert response.headers["Access-Control-Allow-Origin"] == "https://hexalogic.netlify.app"
        assert "X-Hexlogic-Session" in response.headers["Access-Control-Expose-Headers"]
        assert response.headers["X-Hexlogic-Session"] == response.get_json()["session_id"]


def test_v2_hardware_devices_persist_with_explicit_session_header():
    app.testing = True

    with app.test_client(use_cookies=False) as client:
        led_response = client.post("/api/v2/hardware/device", json={"type": "led", "label": "Sticky LED"})
        assert led_response.status_code == 200
        session_id = led_response.headers["X-Hexlogic-Session"]
        led_payload = led_response.get_json()
        led_id = led_payload["created_device_id"]
        headers = {"X-Hexlogic-Session": session_id}

        connect = client.patch("/api/v2/hardware/device", json={"id": led_id, "connections": {"pin": "P1.0"}}, headers=headers)
        assert connect.status_code == 200
        assert any(device["id"] == led_id for device in connect.get_json()["hardware"]["devices"])

        assembled = client.post(
            "/api/v2/assemble",
            json={"code": "ORG 0\nMAIN: CPL P1.0\nSJMP MAIN\n"},
            headers=headers,
        )
        assert assembled.status_code == 200

        reset = client.post("/api/v2/reset", headers=headers)
        assert reset.status_code == 200
        assert any(device["id"] == led_id for device in reset.get_json()["hardware"]["devices"])

        run = client.post("/api/v2/run", json={"max_steps": 8, "speed_multiplier": 1.0}, headers=headers)
        assert run.status_code == 200
        run_devices = run.get_json()["state"]["hardware"]["devices"]
        assert any(device["id"] == led_id for device in run_devices)

        state = client.get("/api/v2/state", headers=headers)
        assert state.status_code == 200
        assert any(device["id"] == led_id for device in state.get_json()["hardware"]["devices"])


def test_v2_led_array_on_p1_survives_posted_delay_program_run():
    app.testing = True
    source = "\n".join(
        [
            "ORG 0000H",
            "MOV A, #01H",
            "START:",
            "MOV P1, A",
            "ACALL DELAY",
            "RL A",
            "SJMP START",
            "DELAY:",
            "MOV R7, #200",
            "D1: MOV R6, #250",
            "D2: DJNZ R6, D2",
            "DJNZ R7, D1",
            "RET",
            "END",
        ]
    )

    with app.test_client(use_cookies=False) as client:
        added = client.post("/api/v2/hardware/device", json={"type": "led_array", "label": "P1 Array"})
        assert added.status_code == 200
        session_id = added.headers["X-Hexlogic-Session"]
        headers = {"X-Hexlogic-Session": session_id}
        device_id = added.get_json()["created_device_id"]

        connected = client.patch(
            "/api/v2/hardware/device",
            json={"id": device_id, "connections": {"bus": "P1"}},
            headers=headers,
        )
        assert connected.status_code == 200
        assert any(device["id"] == device_id for device in connected.get_json()["hardware"]["devices"])

        assert client.post("/api/v2/clock", json={"hz": 12_000_000}, headers=headers).status_code == 200
        assert client.post("/api/v2/execution-mode", json={"mode": "fast"}, headers=headers).status_code == 200
        assembled = client.post("/api/v2/assemble", json={"code": source}, headers=headers)
        assert assembled.status_code == 200
        assert any(device["id"] == device_id for device in assembled.get_json()["hardware"]["devices"])

        run = client.post("/api/v2/run", json={"max_steps": 70_000, "speed_multiplier": 1.0}, headers=headers)
        assert run.status_code == 200
        payload = run.get_json()
        devices = payload["state"]["hardware"]["devices"]
        led_array = next(device for device in devices if device["id"] == device_id)
        assert led_array["connections"]["bus"] == "P1"
        assert led_array["state"]["connected"] is True
        assert led_array["state"]["value"] in {0x01, 0x02}
        assert payload["state"]["hardware"]["timing"]["effective_hz"] == 1_000_000

        state = client.get("/api/v2/state", headers=headers)
        assert state.status_code == 200
        assert any(device["id"] == device_id for device in state.get_json()["hardware"]["devices"])


def test_v2_exact_led_on_p10_program_keeps_device_and_drives_expected_pin_levels():
    app.testing = True
    source = "\n".join(
        [
            "ORG 0000H",
            "MOV A,#01H",
            "MOV R0,#20H",
            "MOV @R0,A",
            "INC A",
            "MOV P1,A",
            "SJMP $",
            "END",
        ]
    )

    with app.test_client(use_cookies=False) as client:
        added = client.post("/api/v2/hardware/device", json={"type": "led", "label": "P1.0 LED"})
        assert added.status_code == 200
        session_id = added.headers["X-Hexlogic-Session"]
        headers = {"X-Hexlogic-Session": session_id}
        device_id = added.get_json()["created_device_id"]

        connected = client.patch(
            "/api/v2/hardware/device",
            json={"id": device_id, "connections": {"pin": "P1.0"}},
            headers=headers,
        )
        assert connected.status_code == 200

        assert client.post("/api/v2/execution-mode", json={"mode": "fast"}, headers=headers).status_code == 200
        assembled = client.post("/api/v2/assemble", json={"code": source}, headers=headers)
        assert assembled.status_code == 200

        run = client.post("/api/v2/run", json={"max_steps": 16, "speed_multiplier": 1.0}, headers=headers)
        assert run.status_code == 200
        payload = run.get_json()["state"]
        led = next(device for device in payload["hardware"]["devices"] if device["id"] == device_id)

        assert led["connections"]["pin"] == "P1.0"
        assert led["state"]["connected"] is True
        assert led["state"]["on"] is False
        assert payload["hardware"]["pins"]["P1.0"]["level"] == 0
        assert payload["hardware"]["pins"]["P1.1"]["level"] == 1
        assert payload["registers"]["A"] == 0x02
        assert payload["registers"]["R0"] == 0x20
        assert payload["registers"]["PC"] == 0x0008

        state = client.get("/api/v2/state", headers=headers)
        assert state.status_code == 200
        assert state.get_json()["iram"][str(0x20)] == 0x01
