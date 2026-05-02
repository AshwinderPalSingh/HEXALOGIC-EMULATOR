from __future__ import annotations

from pathlib import Path

from sim8051.session import SimulatorSession


ROOT = Path(__file__).resolve().parents[1]


def test_led_blink_example_assembles_and_drives_p10_low_first():
    source = (ROOT / "examples" / "led_blink_2s.asm").read_text(encoding="utf-8")

    session = SimulatorSession(session_id="test-example-led")
    session.set_execution_mode("fast")

    assembled = session.assemble(source)
    assert assembled["has_program"] is True

    step = session.step()
    assert step["trace"]["mnemonic"].startswith("CLR")
    assert step["state"]["hardware"]["pins"]["P1.0"]["level"] == 0


def test_arm_keil_arithmetic_example_produces_expected_result_words():
    source = (ROOT / "examples" / "arm_keil_arithmetic.asm").read_text(encoding="utf-8")

    session = SimulatorSession(session_id="test-example-arm", architecture="arm")
    session.set_endian("little")
    session.set_execution_mode("fast")

    assembled = session.assemble(source)
    assert assembled["has_program"] is True

    run = session.run(max_steps=128)
    assert run["state"]["registers"]["R8"] == 4

    xram_changes = {address: after for address, _before, after in run["diff"]["memory"]["xram"]}
    assert xram_changes[272] == 0
    assert xram_changes[276] == 4


def test_pasted_8051_program_preserves_led_connection_and_drives_p1_bits():
    source = """ORG 0000H
MOV A,#01H
MOV R0,#20H
MOV @R0,A
INC A
MOV P1,A
SJMP $
END
"""

    session = SimulatorSession(session_id="test-pasted-led-workflow")
    session.set_execution_mode("fast")
    assert session.assemble(source)["has_program"] is True

    led_p10 = session.hardware.add_device("led", label="LED P1.0")
    led_p11 = session.hardware.add_device("led", label="LED P1.1")
    session.hardware.update_device(led_p10.device_id, connections={"pin": "P1.0"})
    session.hardware.update_device(led_p11.device_id, connections={"pin": "P1.1"})

    run = session.run(max_steps=12)
    devices = {device["id"]: device for device in run["state"]["hardware"]["devices"]}
    pins = run["state"]["hardware"]["pins"]

    assert led_p10.device_id in devices
    assert led_p11.device_id in devices
    assert pins["P1.0"]["level"] == 0
    assert pins["P1.1"]["level"] == 1
    assert devices[led_p10.device_id]["state"]["on"] is False
    assert devices[led_p11.device_id]["state"]["on"] is True


def test_step_into_restarts_loaded_program_after_halt_without_cpu_halted_error():
    session = SimulatorSession(session_id="test-step-halted-restart")
    session.set_execution_mode("fast")
    assert session.assemble("ORG 0000H\nMOV A,#01H\nEND\n")["has_program"] is True

    first = session.step()
    assert first["state"]["halted"] is True
    assert first["state"]["registers"]["A"] == 0x01

    second = session.step()
    assert second["trace"]["mnemonic"].startswith("MOV A")
    assert second["state"]["registers"]["A"] == 0x01
