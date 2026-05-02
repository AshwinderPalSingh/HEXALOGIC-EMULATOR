import pytest

from sim8051 import (
    BaseCPU,
    Assembler8051,
    AssemblerARM,
    CPU8051,
    CPUARM,
    InMemorySessionBackend,
    RedisSessionBackend,
    SessionStore,
    SimulatorSession,
    architecture_metadata,
    register_plugin,
)
from sim8051.model import ProgramImage, TraceEntry, Watchpoint
from sim8051.memory import MemoryMap


def test_two_pass_assembler_resolves_relative_branch_and_call_pages():
    assembler = Assembler8051(code_size=0x2000)
    program = assembler.assemble(
        """
        ORG 0000H
        AJMP START
        ORG 0010H
        START:
        MOV A,#01H
        SJMP DONE
        MOV A,#0FFH
        DONE:
        ACALL SUB
        ORG 0020H
        SUB:
        INC A
        RET
        END
        """.strip()
    )

    listing = {row.line: row.bytes_ for row in program.listing}
    assert listing[2] == [0x01, 0x10]
    assert listing[6][0] == 0x80
    assert listing[9][:2] == [0x11, 0x20]
    assert program.intel_hex.endswith(":00000001FF")


def test_pc_driven_execution_uses_rom_and_real_stack_return_addresses():
    assembler = Assembler8051()
    program = assembler.assemble(
        """
        ORG 0000H
        MOV A,#01H
        ACALL SUB
        INC A
        SJMP DONE
        SUB:
        INC A
        RET
        DONE:
        NOP
        END
        """.strip()
    )
    cpu = CPU8051()
    cpu.load_program(program)
    result = cpu.run(max_steps=20)

    assert result.halted is True
    assert cpu.a == 0x03
    assert cpu.sp == 0x07
    assert cpu.memory.read_code(0x0000) == 0x74


def test_movx_uses_xram_not_internal_ram():
    assembler = Assembler8051()
    program = assembler.assemble(
        """
        MOV DPTR,#0123H
        MOV A,#55H
        MOVX @DPTR,A
        MOV A,#00H
        MOVX A,@DPTR
        END
        """.strip()
    )
    cpu = CPU8051(xram_size=0x10000)
    cpu.load_program(program)
    cpu.run(max_steps=10)

    assert cpu.a == 0x55
    assert cpu.memory.read_xram(0x0123) == 0x55
    assert cpu.memory.read_direct(0x23) == 0x00


def test_timer0_mode1_sets_tf0_and_triggers_interrupt_vector():
    assembler = Assembler8051(code_size=0x1000)
    program = assembler.assemble(
        """
        ORG 0000H
        LJMP MAIN
        ORG 000BH
        ISR:
        INC A
        RETI
        ORG 0030H
        MAIN:
        MOV A,#00H
        MOV TMOD,#01H
        MOV TH0,#0FFH
        MOV TL0,#0FFH
        SETB ET0
        SETB EA
        SETB TR0
        NOP
        NOP
        END
        """.strip()
    )
    cpu = CPU8051(code_size=0x1000)
    cpu.load_program(program)
    cpu.run(max_steps=30)

    assert cpu._get_flag("TF0") == 0
    assert cpu.a == 0x01
    assert any(entry.interrupt == "T0" for entry in cpu.debugger.trace)


def test_timer0_interrupt_entry_charges_two_machine_cycles_before_first_isr_instruction():
    assembler = Assembler8051(code_size=0x1000)
    program = assembler.assemble(
        """
        ORG 0000H
        LJMP MAIN
        ORG 000BH
        ISR:
        NOP
        RETI
        ORG 0030H
        MAIN:
        MOV TMOD,#01H
        MOV TH0,#0FFH
        MOV TL0,#0FFH
        SETB ET0
        SETB EA
        SETB TR0
        WAIT:
        SJMP WAIT
        END
        """.strip()
    )
    cpu = CPU8051(code_size=0x1000)
    cpu.load_program(program)
    cpu.run(max_steps=7)

    assert cpu._get_flag("TF0") == 1
    cycles_before_interrupt_step = cpu.cycles

    trace = cpu.step()

    assert trace.interrupt == "T0"
    assert trace.pc == 0x000B
    assert cpu.cycles - cycles_before_interrupt_step == 3


def test_timer0_interrupt_toggle_period_matches_realistic_entry_latency():
    assembler = Assembler8051(code_size=0x2000)
    program = assembler.assemble(
        """
        ORG 0000H
        LJMP MAIN
        ORG 000BH
        ISR:
        CLR TR0
        MOV TH0,#0FCH
        MOV TL0,#018H
        CPL P1.0
        CLR TF0
        SETB TR0
        RETI
        ORG 0030H
        MAIN:
        CLR P1.0
        MOV TMOD,#01H
        MOV TH0,#0FCH
        MOV TL0,#018H
        SETB ET0
        SETB EA
        SETB TR0
        WAIT:
        SJMP WAIT
        END
        """.strip()
    )
    cpu = CPU8051(code_size=0x2000)
    cpu.load_program(program)

    toggle_cycles: list[int] = []
    for _ in range(4000):
        trace = cpu.step()
        if trace.mnemonic == "CPL 0x90":
            toggle_cycles.append(cpu.cycles)
            if len(toggle_cycles) == 2:
                break

    assert len(toggle_cycles) == 2
    assert toggle_cycles[1] - toggle_cycles[0] == 1011


def test_timer0_gate_requires_int0_high_to_advance():
    assembler = Assembler8051(code_size=0x1000)
    program = assembler.assemble(
        """
        ORG 0000H
        LJMP MAIN
        ORG 000BH
        ISR:
        INC A
        RETI
        ORG 0030H
        MAIN:
        MOV A,#00H
        MOV TMOD,#09H
        MOV TH0,#0FFH
        MOV TL0,#0FFH
        SETB ET0
        SETB EA
        SETB TR0
        WAIT:
        NOP
        SJMP WAIT
        END
        """.strip()
    )
    cpu = CPU8051(code_size=0x1000)
    cpu.load_program(program)
    cpu.set_pin(3, 2, 0)

    cpu.run(max_steps=20)
    assert cpu.a == 0x00
    assert cpu._get_flag("TF0") == 0

    cpu.set_pin(3, 2, 1)
    cpu.run(max_steps=20)

    assert cpu.a == 0x01
    assert any(entry.interrupt == "T0" for entry in cpu.debugger.trace)


def test_timer0_counter_mode_counts_external_falling_edges_only():
    assembler = Assembler8051(code_size=0x1000)
    program = assembler.assemble(
        """
        ORG 0000H
        LJMP MAIN
        ORG 000BH
        ISR:
        INC A
        RETI
        ORG 0030H
        MAIN:
        MOV A,#00H
        MOV TMOD,#05H
        MOV TH0,#0FFH
        MOV TL0,#0FFH
        SETB ET0
        SETB EA
        SETB TR0
        WAIT:
        NOP
        SJMP WAIT
        END
        """.strip()
    )
    cpu = CPU8051(code_size=0x1000)
    cpu.load_program(program)
    cpu.set_pin(3, 4, 1)

    cpu.run(max_steps=20)
    assert cpu.a == 0x00
    assert cpu._get_flag("TF0") == 0

    cpu.set_pin(3, 4, 0)
    cpu.run(max_steps=20)
    assert cpu.a == 0x01

    cpu.run(max_steps=20)
    assert cpu.a == 0x01

    cpu.set_pin(3, 4, 1)
    cpu.run(max_steps=4)
    cpu.set_pin(3, 4, 0)
    cpu.run(max_steps=20)
    assert cpu.a == 0x01
    assert cpu.memory.read_sfr("TL0") == 0x01


def test_timer0_counter_mode_preserves_multiple_falling_edges_between_instructions():
    assembler = Assembler8051(code_size=0x1000)
    program = assembler.assemble(
        """
        ORG 0000H
        MOV TMOD,#05H
        SETB TR0
        SJMP $
        END
        """.strip()
    )
    cpu = CPU8051(code_size=0x1000)
    cpu.load_program(program)
    cpu.run(max_steps=3)
    cpu.set_pin(3, 4, 1)
    cpu.set_pin(3, 4, 0)
    cpu.set_pin(3, 4, 1)
    cpu.set_pin(3, 4, 0)
    cpu.run(max_steps=2)

    assert cpu.memory.read_sfr("TL0") == 0x02


def test_external_interrupt0_edge_triggered_from_p32_falling_edge():
    assembler = Assembler8051(code_size=0x1000)
    program = assembler.assemble(
        """
        ORG 0000H
        LJMP MAIN
        ORG 0003H
        ISR:
        INC A
        RETI
        ORG 0030H
        MAIN:
        MOV A,#00H
        SETB EX0
        SETB EA
        SETB IT0
        WAIT:
        SJMP WAIT
        END
        """.strip()
    )
    cpu = CPU8051(code_size=0x1000)
    cpu.load_program(program)
    cpu.run(max_steps=5)

    cpu.set_pin(3, 2, 1)
    cpu.set_pin(3, 2, 0)

    trace = cpu.step()

    assert trace.interrupt == "EX0"
    assert cpu.a == 0x01
    assert cpu._get_flag("IE0") == 0


def test_external_interrupt0_level_triggered_tracks_live_pin_level():
    assembler = Assembler8051(code_size=0x1000)
    program = assembler.assemble(
        """
        ORG 0000H
        LJMP MAIN
        ORG 0003H
        ISR:
        INC A
        RETI
        ORG 0030H
        MAIN:
        MOV A,#00H
        SETB EX0
        SETB EA
        CLR IT0
        WAIT:
        SJMP WAIT
        END
        """.strip()
    )
    cpu = CPU8051(code_size=0x1000)
    cpu.load_program(program)
    cpu.run(max_steps=5)

    cpu.set_pin(3, 2, 0)
    cpu.step()
    cpu.step()
    cpu.step()

    assert cpu.a == 0x02
    assert cpu._get_flag("IE0") == 1

    cpu.set_pin(3, 2, 1)
    trace = cpu.step()

    assert trace.interrupt is None
    assert cpu._get_flag("IE0") == 0


def test_timer2_auto_reload_sets_tf2_and_triggers_interrupt_vector():
    assembler = Assembler8051(code_size=0x1000)
    program = assembler.assemble(
        """
        ORG 0000H
        LJMP MAIN
        ORG 002BH
        ISR:
        INC A
        RETI
        ORG 0030H
        MAIN:
        MOV A,#00H
        MOV RCAP2H,#00H
        MOV RCAP2L,#10H
        MOV TH2,#0FFH
        MOV TL2,#0FFH
        CLR CP/RL2
        SETB ET2
        SETB EA
        SETB TR2
        NOP
        NOP
        END
        """.strip()
    )
    cpu = CPU8051(code_size=0x1000)
    cpu.load_program(program)
    cpu.run(max_steps=40)

    assert cpu.a == 0x01
    assert cpu._get_flag("TF2") == 0
    assert cpu.memory.read_sfr("TH2") == 0x00
    assert cpu.memory.read_sfr("TL2") >= 0x10
    assert any(entry.interrupt == "T2" for entry in cpu.debugger.trace)


def test_timer2_counter_mode_counts_external_t2_falling_edges():
    assembler = Assembler8051(code_size=0x1000)
    program = assembler.assemble(
        """
        ORG 0000H
        MOV T2CON,#06H
        SJMP $
        END
        """.strip()
    )
    cpu = CPU8051(code_size=0x1000)
    cpu.load_program(program)
    cpu.run(max_steps=2)
    cpu.set_pin(1, 0, 1)
    cpu.set_pin(1, 0, 0)
    cpu.set_pin(1, 0, 1)
    cpu.set_pin(1, 0, 0)
    cpu.run(max_steps=2)

    assert cpu.memory.read_sfr("TH2") == 0x00
    assert cpu.memory.read_sfr("TL2") == 0x02


def test_timer2_capture_mode_latches_th2_tl2_into_rcap_on_t2ex_falling_edge():
    assembler = Assembler8051(code_size=0x1000)
    program = assembler.assemble(
        """
        ORG 0000H
        MOV T2CON,#0DH
        SJMP $
        END
        """.strip()
    )
    cpu = CPU8051(code_size=0x1000)
    cpu.load_program(program)
    cpu.run(max_steps=1)
    cpu.memory.write_sfr("TH2", 0x12)
    cpu.memory.write_sfr("TL2", 0x34)
    cpu.set_pin(1, 1, 1)
    cpu.set_pin(1, 1, 0)
    cpu.run(max_steps=1)

    assert cpu.memory.read_sfr("RCAP2H") == 0x12
    assert cpu.memory.read_sfr("RCAP2L") == 0x34
    assert cpu._get_flag("EXF2") == 1


def test_timer2_baud_generator_delays_serial_tx_and_suppresses_tf2():
    assembler = Assembler8051(code_size=0x1000)
    program = assembler.assemble(
        """
        ORG 0000H
        MOV SCON,#050H
        MOV RCAP2H,#0FFH
        MOV RCAP2L,#0FFH
        MOV TH2,#0FFH
        MOV TL2,#0FFH
        CLR CP/RL2
        SETB TCLK
        SETB TR2
        MOV SBUF,#055H
        LOOP:
        NOP
        SJMP LOOP
        END
        """.strip()
    )
    cpu = CPU8051(code_size=0x1000)
    cpu.load_program(program)
    cpu.run(max_steps=9)

    assert cpu._get_flag("TI") == 0
    assert cpu._get_flag("TF2") == 0

    cpu.run(max_steps=200)

    assert cpu.serial.tx_log == [0x55]
    assert cpu._get_flag("TI") == 1
    assert cpu._get_flag("TF2") == 0


def test_timer2_up_down_mode_uses_t2ex_direction_and_toggles_exf2():
    assembler = Assembler8051(code_size=0x1000)
    program = assembler.assemble(
        """
        ORG 0000H
        SJMP $
        END
        """.strip()
    )
    cpu = CPU8051(code_size=0x1000)
    cpu.load_program(program)
    cpu.memory.write_sfr("T2MOD", 0x01)
    cpu.memory.write_sfr("RCAP2H", 0x00)
    cpu.memory.write_sfr("RCAP2L", 0x10)
    cpu.memory.write_sfr("TH2", 0x00)
    cpu.memory.write_sfr("TL2", 0x10)
    cpu.memory.write_sfr("T2CON", 0x04)
    cpu.set_pin(1, 1, 0)

    cpu.run(max_steps=1)

    assert cpu._get_flag("TF2") == 1
    assert cpu._get_flag("EXF2") == 1
    assert cpu.memory.read_sfr("TH2") == 0xFF
    assert cpu.memory.read_sfr("TL2") == 0xFE


def test_timer2_baud_mode_forces_auto_reload_even_if_cp_rl2_is_set():
    assembler = Assembler8051(code_size=0x1000)
    program = assembler.assemble(
        """
        ORG 0000H
        SJMP $
        END
        """.strip()
    )
    cpu = CPU8051(code_size=0x1000)
    cpu.load_program(program)
    cpu.memory.write_sfr("RCAP2H", 0x12)
    cpu.memory.write_sfr("RCAP2L", 0x34)
    cpu.memory.write_sfr("TH2", 0xFF)
    cpu.memory.write_sfr("TL2", 0xFF)
    cpu.memory.write_sfr("T2CON", 0x15)

    cpu.run(max_steps=1)

    assert cpu._get_flag("TF2") == 0
    assert cpu.memory.read_sfr("TH2") == 0x12
    assert cpu.memory.read_sfr("TL2") == 0x34


def test_8051_fast_realtime_slice_collapses_djnz_spin_loop():
    assembler = Assembler8051(code_size=0x1000)
    program = assembler.assemble(
        """
        ORG 0000H
        MOV R7,#03H
        LOOP:
        DJNZ R7,LOOP
        NOP
        END
        """.strip()
    )
    cpu = CPU8051(code_size=0x1000)
    cpu.load_program(program)
    cpu.step()

    burst = cpu.try_fast_realtime_slice(max_steps=16, max_cycles=32)

    assert burst is not None
    assert burst["steps"] == 3
    assert burst["cycles"] == 6
    assert cpu._read_r(7) == 0
    assert cpu.pc == 0x0004


def test_8051_posted_led_array_delay_cycle_count_matches_classic_machine_cycles():
    assembler = Assembler8051(code_size=0x1000)
    program = assembler.assemble(
        """
        ORG 0000H
        MOV A, #01H

        START:
        MOV P1, A
        ACALL DELAY
        RL A
        SJMP START

        DELAY:
        MOV R7, #200
        D1: MOV R6, #250
        D2: DJNZ R6, D2
        DJNZ R7, D1
        RET
        END
        """.strip()
    )
    cpu = CPU8051(code_size=0x1000)
    cpu.set_clock_hz(12_000_000)
    cpu.load_program(program)

    port_write_cycles: list[int] = []
    for _ in range(60_000):
        trace = cpu.step()
        if (trace.text or "").strip().upper().startswith("MOV P1"):
            port_write_cycles.append(cpu.cycles)
            if len(port_write_cycles) == 2:
                break

    assert port_write_cycles == [2, 100_611]
    assert port_write_cycles[1] - port_write_cycles[0] == 100_609
    assert (port_write_cycles[1] - port_write_cycles[0]) / cpu.effective_clock_hz() == pytest.approx(0.100609)


def test_arm_multiply_family_including_keil_long_forms_runs():
    assembler = AssemblerARM(code_size=0x1000)
    program = assembler.assemble(
        """
        ORG 0000H
        MOV R0, #6
        MOV R1, #7
        MUL R2, R0, R1
        MOV R3, #5
        MLA R4, R0, R1, R3
        MVN R5, #1
        MOV R6, #3
        SMULL R7, R8, R5, R6
        MOV R9, #1
        MOV R10, #0
        SMLAL R9, R10, R5, R6
        END
        """.strip()
    )
    cpu = CPUARM(code_size=0x1000)
    cpu.load_program(program)
    cpu.run(max_steps=16)

    assert cpu.registers[2] == 42
    assert cpu.registers[4] == 47
    assert cpu.registers[7] == 0xFFFFFFFA
    assert cpu.registers[8] == 0xFFFFFFFF
    assert cpu.registers[9] == 0xFFFFFFFB
    assert cpu.registers[10] == 0xFFFFFFFF
    mnemonics = [trace.mnemonic.split()[0] for trace in cpu.debugger.trace]
    assert {"MUL", "MLA", "SMULL", "SMLAL"}.issubset(set(mnemonics))


def test_session_store_isolates_simulator_instances():
    store = SessionStore()
    first = store.create()
    second = store.create()

    first.assemble("MOV A,#01H\nEND")
    second.assemble("MOV A,#02H\nEND")
    first.step()
    second.step()

    assert first.cpu.a == 0x01
    assert second.cpu.a == 0x02
    assert first.session_id != second.session_id


def test_serial_tx_sets_ti_and_records_transmitted_byte():
    assembler = Assembler8051()
    program = assembler.assemble(
        """
        MOV SBUF,#055H
        NOP
        END
        """.strip()
    )
    cpu = CPU8051()
    cpu.load_program(program)
    cpu.run(max_steps=4)

    assert cpu.serial.tx_log == [0x55]
    assert cpu._get_flag("TI") == 1


def test_serial_rx_queued_before_ren_is_delivered_once_receiver_enables():
    assembler = Assembler8051(code_size=0x1000)
    program = assembler.assemble(
        """
        ORG 0000H
        LJMP MAIN
        ORG 0030H
        MAIN:
        MOV A,#00H
        NOP
        NOP
        NOP
        MOV SCON,#050H
        WAIT:
        SJMP WAIT
        END
        """.strip()
    )
    cpu = CPU8051(code_size=0x1000)
    cpu.load_program(program)
    cpu.run(max_steps=2)

    cpu.inject_serial_rx([0x41])
    cpu.run(max_steps=8)

    assert cpu._get_flag("REN") == 1
    assert cpu._get_flag("RI") == 1
    assert cpu.memory.read_sfr("SBUF") == 0x41
    assert list(cpu.serial.rx_queue) == []


def test_step_out_returns_to_caller_frame():
    assembler = Assembler8051()
    program = assembler.assemble(
        """
        ORG 0000H
        ACALL OUTER
        NOP
        SJMP DONE
        OUTER:
        ACALL INNER
        INC A
        RET
        INNER:
        INC A
        RET
        DONE:
        NOP
        END
        """.strip()
    )
    cpu = CPU8051()
    cpu.load_program(program)
    cpu.step()  # enter OUTER
    cpu.step()  # enter INNER

    result = cpu.step_out()

    assert result.reason in {"halted", "max_steps", "step", "step_out"}
    assert cpu.a == 0x01
    assert len(cpu.debugger.call_stack) == 1


def test_memory_map_read16_write16_supports_endian_switching():
    memory = MemoryMap(code_size=0x100, xram_size=0x100)
    memory.write16(0x10, 0x1234, space="xram", endian="little")
    assert memory.read16(0x10, space="xram", endian="little") == 0x1234
    assert memory.read_xram(0x10) == 0x34
    assert memory.read_xram(0x11) == 0x12

    memory.write16(0x20, 0x1234, space="xram", endian="big")
    assert memory.read16(0x20, space="xram", endian="big") == 0x1234
    assert memory.read_xram(0x20) == 0x12
    assert memory.read_xram(0x21) == 0x34


def test_arm_cpu_executes_minimal_program_with_branch_and_memory_access():
    assembler = AssemblerARM(code_size=0x200, endian="little")
    program = assembler.assemble(
        """
        ORG 0000H
        MOV R0, #4
        MOV R1, #12
        ADD R2, R0, R1
        MOV R3, #0
        STR R2, [R3, #0]
        LDR R4, [R3, #0]
        B DONE
        MOV R5, #255
        DONE:
        SUB R5, R4, #1
        END
        """.strip()
    )
    cpu = CPUARM(code_size=0x200, data_size=0x100, endian="little")
    cpu.load_program(program)
    result = cpu.run(max_steps=16)

    assert result.halted is True
    assert cpu.registers[2] == 16
    assert cpu.registers[4] == 16
    assert cpu.registers[5] == 15
    assert cpu.memory.read32(0, space="xram", endian="little") == 16


def test_arm_big_endian_memory_access_round_trips_value():
    assembler = AssemblerARM(code_size=0x100, endian="big")
    program = assembler.assemble(
        """
        ORG 0000H
        MOV R0, #0
        MOV R1, #18
        STR R1, [R0, #0]
        LDR R2, [R0, #0]
        END
        """.strip()
    )
    cpu = CPUARM(code_size=0x100, data_size=0x40, endian="big")
    cpu.load_program(program)
    cpu.run(max_steps=8)

    assert cpu.registers[2] == 18
    assert cpu.memory.read32(0, space="xram", endian="big") == 18
    assert [cpu.memory.read_xram(index) for index in range(4)] == [0, 0, 0, 18]


def test_arm_timer_irq_vectors_to_isr_and_returns_via_bx_lr():
    assembler = AssemblerARM(code_size=0x200, endian="little")
    program = assembler.assemble(
        """
        ORG 0000H
        B MAIN
        ORG 0018H
        IRQ:
        ADD R5, R5, #1
        BX LR
        ORG 0040H
        MAIN:
        MOV R0, #0
        MOV R5, #0
        MOV R1, #5
        STR R1, [R0, #28]
        MOV R1, #7
        STR R1, [R0, #36]
        WAIT:
        B WAIT
        END
        """.strip()
    )
    cpu = CPUARM(code_size=0x200, data_size=0x80, endian="little")
    cpu.load_program(program)
    cpu.run(max_steps=64)

    assert cpu.registers[5] >= 1
    assert any(entry.interrupt == "IRQ_TIMER" for entry in cpu.debugger.trace)


def test_arm_gpio_rising_edge_interrupt_fires_from_external_pin_input():
    assembler = AssemblerARM(code_size=0x200, endian="little")
    program = assembler.assemble(
        """
        ORG 0000H
        B MAIN
        ORG 0018H
        IRQ:
        ADD R6, R6, #1
        BX LR
        ORG 0040H
        MAIN:
        MOV R0, #0
        MOV R6, #0
        MOV R1, #1
        STR R1, [R0, #12]
        STR R1, [R0, #16]
        WAIT:
        B WAIT
        END
        """.strip()
    )
    cpu = CPUARM(code_size=0x200, data_size=0x80, endian="little")
    cpu.load_program(program)
    cpu.run(max_steps=8)

    assert cpu.registers[6] == 0
    cpu.set_pin(0, 0, 1)
    cpu.run(max_steps=16)

    assert cpu.registers[6] == 1
    assert any(entry.interrupt == "IRQ_GPIOA" for entry in cpu.debugger.trace)


def test_arm_fast_realtime_slice_collapses_subs_bne_delay_loop():
    assembler = AssemblerARM(code_size=0x200, endian="little")
    program = assembler.assemble(
        """
        ORG 0000H
        MOV R0, #3
        LOOP:
        SUBS R0, R0, #1
        BNE LOOP
        MOV R1, #7
        END
        """.strip()
    )
    cpu = CPUARM(code_size=0x200, data_size=0x80, endian="little")
    cpu.load_program(program)
    cpu.step()
    cpu.step()

    burst = cpu.try_fast_realtime_slice(max_steps=16, max_cycles=64)

    assert burst is not None
    assert burst["steps"] == 5
    assert burst["cycles"] == 9
    assert cpu.registers[0] == 0
    assert cpu.flag_z == 1
    assert cpu.pc == 0x000C


def test_session_round_trip_preserves_arm_state_and_program():
    session = SimulatorSession(session_id="arm-session", architecture="arm", endian="big")
    session.assemble(
        """
        ORG 0000H
        MOV R0, #2
        MOV R1, #3
        ADD R2, R0, R1
        END
        """.strip()
    )
    session.step()
    session.step()
    restored = SimulatorSession.from_dict(session.to_dict())

    assert restored.architecture == "arm"
    assert restored.endian == "big"
    assert restored.program is not None
    assert restored.program.intel_hex == session.program.intel_hex
    assert restored.cpu.registers[:3] == session.cpu.registers[:3]


class _FakeRedisClient:
    def __init__(self):
        self.storage = {}
        self.expirations = {}

    def get(self, key):
        return self.storage.get(key)

    def setex(self, key, ttl, value):
        self.storage[key] = value
        self.expirations[key] = ttl

    def delete(self, key):
        self.storage.pop(key, None)
        self.expirations.pop(key, None)

    def expire(self, key, ttl):
        if key in self.storage:
            self.expirations[key] = ttl

    def scan_iter(self, match=None):
        prefix = (match or "").rstrip("*")
        for key in list(self.storage):
            if not prefix or key.startswith(prefix):
                yield key


def test_redis_session_backend_round_trips_serialized_sessions():
    backend = RedisSessionBackend(client=_FakeRedisClient(), ttl_seconds=120, fallback=InMemorySessionBackend())
    session = SimulatorSession(session_id="redis-session")
    session.assemble("MOV A,#01H\nEND")
    session.step()

    backend.save(session)
    restored = backend.get("redis-session")

    assert restored is not None
    assert restored.cpu.a == 0x01
    assert backend.count() == 1
    assert backend.estimate_bytes() > 0
    backend.delete("redis-session")
    assert backend.get("redis-session") is None


def test_step_back_reverses_8051_execution_delta():
    session = SimulatorSession(session_id="rewind-8051")
    session.assemble("MOV A,#01H\nINC A\nEND")
    session.step()
    session.step()

    response = session.step_back()

    assert response["reason"] == "step_back"
    assert response["state"]["registers"]["A"] == 0x01
    assert response["state"]["history_depth"] == 1


def test_step_back_reverses_arm_execution_delta():
    session = SimulatorSession(session_id="rewind-arm", architecture="arm")
    session.assemble("ORG 0000H\nMOV R0, #4\nMOV R1, #12\nADD R2, R0, R1\nEND")
    session.step()
    session.step()
    session.step()

    response = session.step_back()

    assert response["reason"] == "step_back"
    assert response["state"]["registers"]["R2"] == 0
    assert response["state"]["registers"]["R1"] == 12


def test_8051_bit_addressed_instructions_and_rotation_aliases_work_with_numeric_bits():
    assembler = Assembler8051()
    program = assembler.assemble(
        """
        ORG 0000H
        MOV A,#081H
        RR A
        RRL A
        RRC A
        RLC A
        SWAP A
        CPL A
        SETB 20H
        MOV C,20H
        CPL 20H
        MOV 20H,C
        CLR 20H
        END
        """.strip()
    )
    cpu = CPU8051()
    cpu.load_program(program)
    cpu.run(max_steps=20)

    assert cpu.a == 0xE7
    assert cpu._get_flag("CY") == 1
    assert cpu.memory.read_bit(0x20) == 0


def test_8051_djnz_and_cjne_follow_coursework_control_flow():
    assembler = Assembler8051()
    program = assembler.assemble(
        """
        ORG 0000H
        MOV R0,#02H
        MOV A,#01H
        LOOP:
        DJNZ R0,LOOP
        CJNE A,#02H,NOTEQ
        MOV R1,#00H
        SJMP DONE
        NOTEQ:
        MOV R1,#0AAH
        DONE:
        END
        """.strip()
    )
    cpu = CPU8051()
    cpu.load_program(program)
    cpu.run(max_steps=20)

    assert cpu._read_r(0) == 0
    assert cpu._read_r(1) == 0xAA
    assert cpu._get_flag("CY") == 1


def test_arm_flags_support_multiword_add_and_subtract_sequences():
    assembler = AssemblerARM(code_size=0x200, endian="little")
    program = assembler.assemble(
        """
        ORG 0000H
        MVN R0, #0
        MOV R1, #1
        ADDS R2, R0, R1
        ADC R3, R3, #0
        SUBS R4, R3, #1
        SBC R5, R4, #0
        END
        """.strip()
    )
    cpu = CPUARM(code_size=0x200, data_size=0x100, endian="little")
    cpu.load_program(program)
    cpu.run(max_steps=16)

    assert cpu.registers[2] == 0
    assert cpu.registers[3] == 1
    assert cpu.registers[4] == 0
    assert cpu.registers[5] == 0
    assert (cpu.flag_n, cpu.flag_z, cpu.flag_c, cpu.flag_v) == (0, 1, 1, 0)


def test_arm_umlal_accumulates_product_into_64bit_register_pair():
    assembler = AssemblerARM(code_size=0x200, endian="little")
    program = assembler.assemble(
        """
        ORG 0000H
        MVN R0, #0
        MOV R1, #0
        MOV R2, #2
        MOV R3, #2
        UMLAL R0, R1, R2, R3
        END
        """.strip()
    )
    cpu = CPUARM(code_size=0x200, data_size=0x100, endian="little")
    cpu.load_program(program)
    cpu.run(max_steps=16)

    assert cpu.registers[0] == 3
    assert cpu.registers[1] == 1


def test_arm_keil_style_source_with_area_entry_literal_loads_and_data_labels_runs():
    assembler = AssemblerARM(code_size=0x400, endian="little")
    program = assembler.assemble(
        """
        AREA PROGRAM, CODE, READONLY
        ENTRY
        MAIN
        LDR R0, =NUM1
        LDR R1, =NUM2
        LDR R2, =RESULT
        LDR R3, [R0]
        LDR R4, [R0, #4]
        LDR R5, [R1]
        LDR R6, [R1, #4]
        UMLAL R3, R4, R5, R6
        STR R3, [R2]
        STR R4, [R2, #4]
        LDR R0, =NUM1
        LDR R1, =NUM2
        LDR R2, =RESULT
        LDR R3, [R0]
        LDR R4, [R1]
        ADDS R5, R3, R4
        STR R5, [R2]
        LDR R6, [R0, #4]
        LDR R7, [R1, #4]
        ADC R8, R6, R7
        STR R8, [R2, #4]
        STOP B STOP

        AREA PROGRAM, DATA, READONLY
        NUM1 DCD 0xFFFFFFFF
             DCD 0x00000001
        NUM2 DCD 0x00000001
             DCD 0x00000002
        RESULT DCD 0x00000000
               DCD 0x00000000
        END
        """.strip()
    )
    cpu = CPUARM(code_size=0x400, data_size=0x400, endian="little")
    cpu.load_program(program)
    result = cpu.run(max_steps=32)
    result_base = program.labels["RESULT"]

    assert program.labels["NUM1"] == 0x100
    assert program.labels["NUM2"] == 0x108
    assert result_base == 0x110
    assert cpu.memory.read32(result_base, space="xram", endian="little") == 0
    assert cpu.memory.read32(result_base + 4, space="xram", endian="little") == 4
    assert any(step.mnemonic.startswith("UMLAL") for step in result.steps)


def test_arm_sum_array_keil_example_runs_with_post_indexed_load():
    assembler = AssemblerARM(code_size=0x400, endian="little")
    program = assembler.assemble(
        """
        AREA    SUM_ARRAY, CODE, READONLY
        ENTRY

START
        LDR     R0, =ARRAY
        MOV     R1, #5
        MOV     R2, #0

LOOP
        CMP     R1, #0
        BEQ     DONE

        LDR     R3, [R0], #4
        ADD     R2, R2, R3

        SUB     R1, R1, #1
        B       LOOP

DONE
STOP
        B STOP

        AREA    DATA, DATA, READWRITE

ARRAY   DCD     10, 20, 30, 40, 50

        END
        """.strip()
    )
    cpu = CPUARM(code_size=0x400, data_size=0x400, endian="little")
    cpu.load_program(program)
    cpu.run(max_steps=64)

    assert program.labels["ARRAY"] == 0x100
    assert cpu.registers[2] == 150
    assert cpu.registers[1] == 0
    assert cpu.pc == program.labels["STOP"]


def test_arm_data_processing_reference_ops_update_results_and_flags():
    assembler = AssemblerARM(code_size=0x200, endian="little")
    program = assembler.assemble(
        """
        ORG 0000H
        MOV R0, #5
        MOV R1, #3
        RSB R2, R0, #10
        BIC R3, R2, #1
        TEQ R3, #4
        CMN R1, #1
        END
        """.strip()
    )
    cpu = CPUARM(code_size=0x200, data_size=0x100, endian="little")
    cpu.load_program(program)
    cpu.run(max_steps=16)

    assert cpu.registers[2] == 5
    assert cpu.registers[3] == 4
    assert cpu.flag_z == 0
    assert cpu.flag_n == 0


def test_arm_shifts_conditional_execution_and_stack_pseudos_work():
    assembler = AssemblerARM(code_size=0x200, endian="little")
    program = assembler.assemble(
        """
        ORG 0000H
        MOV R0, #1
        MOV R1, R0, LSL #3
        MOV R2, R1, LSR #1
        MOV R3, R2, ASR #1
        CMP R3, #2
        MOVEQ R4, #0AAH
        MOVNE R4, #055H
        MOV R5, #3
        MOV R6, R0, LSL R5
        PUSH R6
        MOV R6, #0
        POP R7
        END
        """.strip()
    )
    cpu = CPUARM(code_size=0x200, data_size=0x100, endian="little")
    cpu.load_program(program)
    cpu.run(max_steps=24)

    assert cpu.registers[1] == 8
    assert cpu.registers[2] == 4
    assert cpu.registers[3] == 2
    assert cpu.registers[4] == 0xAA
    assert cpu.registers[6] == 0
    assert cpu.registers[7] == 8


def test_arm_branch_link_and_bx_return_control_flow():
    assembler = AssemblerARM(code_size=0x200, endian="little")
    program = assembler.assemble(
        """
        ORG 0000H
        MOV R0, #1
        BL SUB
        B DONE
        SUB:
        ADD R0, R0, #2
        BX LR
        DONE:
        MOV R1, R0
        END
        """.strip()
    )
    cpu = CPUARM(code_size=0x200, data_size=0x100, endian="little")
    cpu.load_program(program)
    cpu.run(max_steps=16)

    assert cpu.registers[0] == 3
    assert cpu.registers[1] == 3


def test_conditional_breakpoint_stops_only_when_condition_matches():
    assembler = Assembler8051()
    program = assembler.assemble("MOV A,#01H\nINC A\nINC A\nEND")
    cpu = CPU8051()
    cpu.load_program(program)
    cpu.set_breakpoints([{"pc": 0x0002, "condition": "A == 1"}])

    result = cpu.run(max_steps=8)

    assert result.reason == "breakpoint"
    assert cpu.pc == 0x0002
    assert cpu.a == 0x01


def test_register_watchpoint_triggers_on_register_diff():
    assembler = Assembler8051()
    program = assembler.assemble("MOV A,#01H\nEND")
    cpu = CPU8051()
    cpu.load_program(program)
    cpu.set_watchpoints([Watchpoint(target="A", space="register")])

    result = cpu.run(max_steps=4)

    assert result.reason == "watchpoint"
    assert cpu.a == 0x01


class _TestPlugin:
    def register(self, registry):
        class _DummyCPU(BaseCPU):
            def __init__(self):
                super().__init__()
                self.loaded = False

            def reset(self, *, hard: bool = False) -> None:
                self.pc = 0
                self.cycles = 0
                self.halted = not self.loaded

            def load_program(self, program: ProgramImage) -> None:
                self.program = program
                self.loaded = True
                self.halted = False

            def _current_opcode(self) -> int:
                return 0

            def _instruction_length_preview(self, opcode: int) -> int:
                return 1

            def _is_call_opcode(self, opcode: int) -> bool:
                return False

            def _step_impl(self) -> TraceEntry:
                self.halted = True
                return TraceEntry(pc=self.pc, opcode=0, mnemonic="NOP", bytes_=[0], cycles=1, line=None, text=None)

            def snapshot(self) -> dict:
                return {"registers": {"PC": self.pc}, "flags": {}, "cycles": self.cycles, "clock_hz": self.clock_hz, "halted": self.halted, "last_error": self.last_error, "last_interrupt": self.last_interrupt}

            def serialize_state(self) -> dict:
                return {"pc": self.pc, "cycles": self.cycles, "halted": self.halted}

            def load_state(self, state: dict) -> None:
                self.pc = int(state.get("pc", 0))
                self.cycles = int(state.get("cycles", 0))
                self.halted = bool(state.get("halted", True))

            def _restore_register_values(self, values: dict[str, int]) -> None:
                self.pc = int(values.get("PC", self.pc))

        class _DummyAssembler:
            def __init__(self, **_kwargs):
                pass

            def assemble(self, _source: str) -> ProgramImage:
                return ProgramImage(origin=0, rom=bytearray([0]), binary=b"\x00", intel_hex=":0100000000FF\n:00000001FF", listing=[], address_to_line={}, labels={}, size=1)

        registry.register_architecture(
            name="dummy-test",
            cpu_builder=_DummyCPU,
            assembler_builder=_DummyAssembler,
            cpu_model_version="test",
            description="dummy",
        )


def test_plugin_registration_adds_new_architecture():
    register_plugin(_TestPlugin())

    assert "dummy-test" in architecture_metadata()
