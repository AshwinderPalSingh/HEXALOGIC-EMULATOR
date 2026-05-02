"""Virtual hardware API and signal propagation coverage."""

from api.index import app
from sim8051.hardware import VirtualHardwareManager
from sim8051.memory import GPIOA_MMIO_BASE, MemoryMap
from sim8051.session import SimulatorSession


def _snapshot_8051(*, p0=0xFF, p1=0xFF, p2=0xFF, p3=0xFF, cycles=0, clock_hz=12_000_000, io_reads=None):
    def _port_payload(value, open_drain=False):
        return {"latch": value & 0xFF, "pin": value & 0xFF, "open_drain": open_drain}

    return {
        "ports": {
            "P0": _port_payload(p0, open_drain=True),
            "P1": _port_payload(p1),
            "P2": _port_payload(p2),
            "P3": _port_payload(p3),
        },
        "sfr": {},
        "cycles": cycles,
        "clock_hz": clock_hz,
        "io_reads": list(io_reads or []),
    }


def _snapshot_arm(*, gpio_out=0, gpio_in=0, gpio_dir=0xFFFF, cycles=0, clock_hz=48_000_000, endian="little"):
    sample = {}
    for offset, value in ((0x00, gpio_out), (0x04, gpio_in), (0x08, gpio_dir)):
        for byte_index in range(4):
            if endian == "big":
                shift = (3 - byte_index) * 8
            else:
                shift = byte_index * 8
            sample[offset + byte_index] = (value >> shift) & 0xFF
    return {
        "xram_sample": sample,
        "cycles": cycles,
        "clock_hz": clock_hz,
        "endian": endian,
    }


def test_gpio_mmio_mirrors_low_xram():
    mem = MemoryMap(xram_size=0x1000)
    mem.write_xram(0x04, 0xAB)
    assert mem.read_xram(GPIOA_MMIO_BASE + 0x04) == 0xAB
    mem.write_xram(GPIOA_MMIO_BASE + 0x08, 0x12)
    assert mem.read_xram(0x08) == 0x12


def test_hardware_api_add_connect_export():
    app.testing = True
    with app.test_client() as client:
        add = client.post("/api/v2/hardware/device", json={"type": "led", "label": "L1"})
        assert add.status_code == 200
        state = add.get_json()
        assert "hardware" in state
        devs = state["hardware"]["devices"]
        assert len(devs) == 1
        did = devs[0]["id"]
        assert state["created_device_id"] == did
        patch = client.patch("/api/v2/hardware/device", json={"id": did, "connections": {"pin": "P1.0"}})
        assert patch.status_code == 200
        exp = client.get("/api/v2/hardware/export")
        assert exp.status_code == 200
        assert exp.get_json()["hardware"]["devices"][0]["connections"]["pin"] == "P1.0"
        bridge = client.get("/api/v2/hardware/bridge")
        assert bridge.status_code == 200
        assert "pins" in bridge.get_json()


def test_hardware_import_roundtrip():
    app.testing = True
    payload = {
        "architecture": "8051",
        "devices": [
            {
                "id": "led-test-1",
                "type": "led",
                "label": "X",
                "connections": {"pin": "P2.3"},
                "position": {"x": 10, "y": 20},
                "settings": {},
                "runtime": {},
            }
        ],
    }
    with app.test_client() as client:
        imp = client.post("/api/v2/hardware/import", json={"hardware": payload})
        assert imp.status_code == 200
        st = imp.get_json()
        dev = st["hardware"]["devices"][0]
        assert dev["connections"]["pin"] == "P2.3"
        assert dev["position"]["x"] == 10


def test_hardware_manager_led_and_debug_flow():
    hw = VirtualHardwareManager("8051")
    device = hw.add_device("led", label="Status")
    hw.update_device(device.device_id, connections={"pin": "P1.0"})

    first, first_diff = hw.sync(_snapshot_8051(p1=0x00, cycles=12))
    second, second_diff = hw.sync(_snapshot_8051(p1=0x01, cycles=60012))

    assert first["devices"][0]["state"]["on"] is False
    assert second["devices"][0]["state"]["on"] is True
    assert device.device_id in second_diff["changed_ids"]
    assert any(event["pin"] == "P1.0" for event in second["debug"]["signal_log"])
    assert second["debug"]["summary"]["fail"] == 0
    assert second["debug"]["summary"]["pass"] >= 1
    assert first_diff["time_ms"] <= second_diff["time_ms"]


def test_8051_hardware_timing_uses_machine_cycles_from_oscillator_clock():
    hw = VirtualHardwareManager("8051")
    payload, _ = hw.sync(_snapshot_8051(cycles=1_000_000, clock_hz=12_000_000))

    assert payload["time_ms"] == 1000.0
    assert payload["timing"]["seconds"] == 1.0
    assert payload["timing"]["effective_hz"] == 1_000_000
    assert payload["timing"]["cycle_unit"] == "8051_machine_cycle"


def test_hardware_manager_stepper_invalid_sequence_detected():
    hw = VirtualHardwareManager("8051")
    device = hw.add_device("stepper")
    hw.update_device(device.device_id, connections={"coil_bus": "P3_LOW"})

    hw.sync(_snapshot_8051(p3=0x09, cycles=12))
    payload, _ = hw.sync(_snapshot_8051(p3=0x06, cycles=24))

    issues = payload["debug"]["issues"]
    assert any("Invalid stepper sequence" in issue["message"] for issue in issues)
    assert payload["devices"][0]["validation"]["status"] == "fail"


def test_hardware_manager_detects_floating_input_and_contention():
    hw = VirtualHardwareManager("8051")
    led = hw.add_device("led")
    hw.update_device(led.device_id, connections={"pin": "P1.0"})

    floating_payload, _ = hw.sync(_snapshot_8051(p1=0xFF, cycles=12))
    floating_signal = floating_payload["debug"]["signals"]["P1.0"]
    assert floating_signal["floating"] is True
    assert floating_signal["contention"] is False

    switch = hw.add_device("switch")
    hw.update_device(switch.device_id, connections={"pin": "P1.0"})
    hw.set_switch_level(switch.device_id, 1)
    contention_payload, _ = hw.sync(_snapshot_8051(p1=0x00, cycles=24))
    signal = contention_payload["debug"]["signals"]["P1.0"]
    assert signal["contention"] is True
    assert any("Bus contention" in issue["message"] for issue in contention_payload["debug"]["issues"])
    assert signal["state"] == "error"


def test_switch_validation_warns_until_cpu_reads_input():
    hw = VirtualHardwareManager("8051")
    switch = hw.add_device("switch")
    hw.update_device(switch.device_id, connections={"pin": "P1.0"})
    hw.set_switch_level(switch.device_id, 1)

    warned_payload, _ = hw.sync(_snapshot_8051(p1=0xFF, cycles=12))
    warned_messages = [issue["message"] for issue in warned_payload["devices"][0]["validation"]["issues"]]
    assert any("firmware has not sampled the input" in message for message in warned_messages)

    observed_payload, _ = hw.sync(
        _snapshot_8051(
            p1=0xFF,
            cycles=24,
            io_reads=[{"signal": "P1.0", "value": 1, "time_ms": 0.03, "source": "cpu"}],
        )
    )
    observed_messages = [issue["message"] for issue in observed_payload["devices"][0]["validation"]["issues"]]
    assert not any("firmware has not sampled the input" in message for message in observed_messages)
    assert observed_payload["debug"]["signals"]["P1.0"]["last_read_ms"] == 0.03


def test_fault_injection_persists_through_export_import():
    hw = VirtualHardwareManager("8051")
    led = hw.add_device("led")
    hw.update_device(led.device_id, connections={"pin": "P1.0"})
    hw.inject_fault("P1.0", "stuck_high")

    payload, _ = hw.sync(_snapshot_8051(p1=0x00, cycles=12))
    assert payload["pins"]["P1.0"]["metadata"]["fault"] == "stuck_high"

    exported = hw.export_state()
    restored = VirtualHardwareManager("8051")
    restored.import_state(exported)
    assert restored.export_state()["faults"]["P1.0"]["type"] == "stuck_high"


def test_hardware_manager_arm_led_and_suite():
    hw = VirtualHardwareManager("arm")
    device = hw.add_device("led", label="GPIOA0")
    hw.update_device(device.device_id, connections={"pin": "GPIOA.0"})

    payload, diff = hw.sync(_snapshot_arm(gpio_out=0x0001, gpio_dir=0xFFFF, cycles=48))

    assert payload["devices"][0]["state"]["on"] is True
    assert device.device_id in diff["changed_ids"]
    suite = hw.run_test_suite()
    assert suite["passed"] is True
    assert len(suite["results"]) == 6


def test_hardware_test_endpoint_returns_suite_results():
    app.testing = True
    with app.test_client() as client:
        response = client.post("/api/v2/hardware/test")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["hardware_test"]["passed"] is True
        assert len(payload["hardware_test"]["results"]) == 6


def test_hardware_fault_endpoint_roundtrip():
    app.testing = True
    with app.test_client() as client:
        response = client.post("/api/v2/hardware/fault", json={"signal": "P1.0", "type": "stuck_high", "enabled": True})
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["hardware"]["debug"]["faults"]["P1.0"]["type"] == "stuck_high"

        cleared = client.post("/api/v2/hardware/fault", json={"signal": "P1.0", "type": "stuck_high", "enabled": False})
        assert cleared.status_code == 200
        assert "P1.0" not in cleared.get_json()["hardware"]["debug"]["faults"]


def test_step_response_includes_hardware_diff():
    app.testing = True
    source = "\n".join(
        [
            "ORG 0000H",
            "CLR P1.0",
            "SETB P1.0",
            "SJMP $",
            "END",
        ]
    )
    with app.test_client() as client:
        added = client.post("/api/v2/hardware/device", json={"type": "led", "label": "L1"})
        device_id = added.get_json()["hardware"]["devices"][0]["id"]
        patched = client.patch("/api/v2/hardware/device", json={"id": device_id, "connections": {"pin": "P1.0"}})
        assert patched.status_code == 200
        assembled = client.post("/api/v2/assemble", json={"code": source})
        assert assembled.status_code == 200
        stepped = client.post("/api/v2/step")
        assert stepped.status_code == 200
        payload = stepped.get_json()
        assert "hardware" in payload["diff"]
        assert device_id in payload["diff"]["hardware"]["changed_ids"]
        led_state = payload["state"]["hardware"]["devices"][0]["state"]
        assert led_state["on"] is False


def test_8051_all_gpio_ports_drive_leds_and_led_arrays_from_firmware():
    source = "\n".join(
        [
            "ORG 0000H",
            "MOV P0,#055H",
            "MOV P1,#0AAH",
            "MOV P2,#0F0H",
            "MOV P3,#00FH",
            "SJMP $",
            "END",
        ]
    )
    expected = {"P0": 0x55, "P1": 0xAA, "P2": 0xF0, "P3": 0x0F}

    session = SimulatorSession(session_id="test-8051-gpio-output-matrix")
    session.set_execution_mode("fast")
    assert session.assemble(source)["has_program"] is True

    led_ids: dict[str, str] = {}
    array_ids: dict[str, str] = {}
    for port_name in expected:
        array = session.hardware.add_device("led_array", label=f"{port_name} array")
        session.hardware.update_device(array.device_id, connections={"bus": port_name})
        array_ids[port_name] = array.device_id
        for bit in range(8):
            signal = f"{port_name}.{bit}"
            led = session.hardware.add_device("led", label=signal)
            session.hardware.update_device(led.device_id, connections={"pin": signal})
            led_ids[signal] = led.device_id

    result = session.run(max_steps=12)
    hardware = result["state"]["hardware"]
    devices = {device["id"]: device for device in hardware["devices"]}

    for port_name, value in expected.items():
        assert hardware["pins"][f"{port_name}.0"]["port"] == port_name
        assert devices[array_ids[port_name]]["state"]["value"] == value
        assert devices[array_ids[port_name]]["validation"]["status"] in {"pass", "warn"}
        for bit in range(8):
            signal = f"{port_name}.{bit}"
            level = (value >> bit) & 0x01
            assert hardware["pins"][signal]["level"] == level
            assert devices[led_ids[signal]]["state"]["on"] is bool(level)


def test_8051_switch_inputs_reach_cpu_reads_on_every_port():
    source = "\n".join(
        [
            "ORG 0000H",
            "MOV A,P0",
            "MOV 20H,A",
            "MOV A,P1",
            "MOV 21H,A",
            "MOV A,P2",
            "MOV 22H,A",
            "MOV A,P3",
            "MOV 23H,A",
            "SJMP $",
            "END",
        ]
    )
    expected = {"P0": 0x3C, "P1": 0xA5, "P2": 0x5A, "P3": 0xC3}

    session = SimulatorSession(session_id="test-8051-gpio-input-matrix")
    session.set_execution_mode("fast")
    assert session.assemble(source)["has_program"] is True

    for port_name, value in expected.items():
        for bit in range(8):
            switch = session.hardware.add_device("switch", label=f"SW {port_name}.{bit}")
            session.hardware.update_device(switch.device_id, connections={"pin": f"{port_name}.{bit}"})
            session.hardware.set_switch_level(switch.device_id, (value >> bit) & 0x01)

    result = session.run(max_steps=12)
    hardware = result["state"]["hardware"]

    for index, port_name in enumerate(("P0", "P1", "P2", "P3")):
        assert session.cpu.memory.read_direct(0x20 + index) == expected[port_name]
        for bit in range(8):
            signal = f"{port_name}.{bit}"
            assert hardware["pins"][signal]["level"] == ((expected[port_name] >> bit) & 0x01)
            assert hardware["debug"]["signals"][signal]["last_read_ms"] is not None
    assert hardware["debug"]["summary"]["fail"] == 0


def test_arm_gpioa_outputs_and_inputs_stay_synchronized_with_virtual_hardware():
    output_source = "\n".join(
        [
            "ORG 0000H",
            "MOV R0,#0",
            "MOV R1,#255",
            "STR R1,[R0,#8]",
            "MOV R1,#90",
            "STR R1,[R0,#0]",
            "B $",
            "END",
        ]
    )

    output_session = SimulatorSession(session_id="test-arm-gpio-output-matrix", architecture="arm")
    output_session.set_execution_mode("fast")
    assert output_session.assemble(output_source)["has_program"] is True
    low_array = output_session.hardware.add_device("led_array", label="GPIOA low")
    output_session.hardware.update_device(low_array.device_id, connections={"bus": "GPIOA_LOW"})
    for bit in range(8):
        led = output_session.hardware.add_device("led", label=f"GPIOA.{bit}")
        output_session.hardware.update_device(led.device_id, connections={"pin": f"GPIOA.{bit}"})

    output_result = output_session.run(max_steps=10)
    output_hardware = output_result["state"]["hardware"]
    output_devices = {device["id"]: device for device in output_hardware["devices"]}
    assert output_devices[low_array.device_id]["state"]["value"] == 0x5A
    for bit in range(8):
        assert output_hardware["pins"][f"GPIOA.{bit}"]["level"] == ((0x5A >> bit) & 0x01)

    input_source = "\n".join(
        [
            "ORG 0000H",
            "MOV R0,#0",
            "MOV R2,#0",
            "STR R2,[R0,#8]",
            "LDR R1,[R0,#4]",
            "B $",
            "END",
        ]
    )
    input_session = SimulatorSession(session_id="test-arm-gpio-input-matrix", architecture="arm")
    input_session.set_execution_mode("fast")
    assert input_session.assemble(input_source)["has_program"] is True
    for bit in range(16):
        switch = input_session.hardware.add_device("switch", label=f"GPIOA.{bit}")
        input_session.hardware.update_device(switch.device_id, connections={"pin": f"GPIOA.{bit}"})
        input_session.hardware.set_switch_level(switch.device_id, (0xBEEF >> bit) & 0x01)

    input_result = input_session.run(max_steps=10)
    assert input_result["state"]["registers"]["R1"] & 0xFFFF == 0xBEEF
    assert input_result["state"]["hardware"]["debug"]["summary"]["fail"] == 0
