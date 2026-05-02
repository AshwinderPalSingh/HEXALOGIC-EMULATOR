import inspect

from core.controller import Controller
from core.instruction_set import Instructions
from core.opcodes import opcodes_lookup
from core.operations import Operations


def _run_code(code: str, setup=None) -> Controller:
    controller = Controller()
    if setup:
        setup(controller)
    controller.parse_all(code.strip())
    controller.run()
    return controller


def test_all_opcodes_have_instruction_handlers():
    operations = Operations()
    instruction_set = Instructions(operations)
    handlers = {
        name.upper()
        for name, call in inspect.getmembers(instruction_set, inspect.ismethod)
        if "_" not in name
    }
    mnemonics = {key.split()[0].upper() for key in opcodes_lookup.keys() if not key.startswith("undefined")}
    missing = sorted(mnemonics - handlers)
    assert missing == []


def test_every_opcode_form_parses():
    sample = {
        "DIRECT": "30H",
        "BIT": "20H",
        "/BIT": "/20H",
        "#IMMED": "#01H",
        "ADDR11": "30H",
        "ADDR16": "1234H",
        "A": "A",
        "AB": "AB",
        "C": "C",
        "DPTR": "DPTR",
        "@R0": "@R0",
        "@R1": "@R1",
        "@A+DPTR": "@A+DPTR",
        "@A+PC": "@A+PC",
        "R0": "R0",
        "R1": "R1",
        "R2": "R2",
        "R3": "R3",
        "R4": "R4",
        "R5": "R5",
        "R6": "R6",
        "R7": "R7",
    }
    forms = sorted({key.upper() for key in opcodes_lookup if key != "undefined"})
    failures = []
    for form in forms:
        opcode, *params = form.split()
        args = [sample.get(param, param) for param in params]
        command = opcode if not args else f"{opcode} {','.join(args)}"
        controller = Controller()
        try:
            controller.parse(command)
        except Exception as exc:  # pragma: no cover - failure path only
            failures.append((form, command, str(exc)))
    assert failures == []


def test_acall_and_ret_return_to_next_instruction():
    controller = _run_code(
        """
        MOV A,#01H
        ACALL SUB
        INC A
        SJMP DONE
        SUB: INC A
        RET
        DONE: NOP
        """
    )

    assert str(controller.op.memory_read("A")) == "0x03"
    assert str(controller.op.memory_read("SP")) == "0x07"


def test_jbc_clears_bit_and_jumps():
    controller = _run_code(
        """
        SETB 20H
        JBC 20H,TAKEN
        MOV A,#00H
        SJMP END
        TAKEN: MOV A,#01H
        END: NOP
        """
    )

    assert str(controller.op.memory_read("A")) == "0x01"
    assert controller.op.bit_read("20H") is False


def test_subb_respects_carry_borrow():
    controller = _run_code(
        """
        MOV A,#03H
        SUBB A,#01H
        """,
        setup=lambda c: setattr(c.op.flags, "CY", True),
    )

    assert str(controller.op.memory_read("A")) == "0x01"
    assert controller.op.flags.CY is False


def test_mov_dptr_keeps_high_low_byte_order():
    controller = _run_code("MOV DPTR,#1234H")

    assert str(controller.op.memory_read("DPH")) == "0x12"
    assert str(controller.op.memory_read("DPL")) == "0x34"
    assert str(controller.op.register_pair_read("DPTR")) == "0x1234"


def test_movc_reads_code_memory_with_a_plus_dptr():
    controller = _run_code(
        """
        MOV DPTR,#0010H
        MOV A,#02H
        MOVC A,@A+DPTR
        """,
        setup=lambda c: c.op.memory_write("0x0012", "0x7a", RAM=False),
    )

    assert str(controller.op.memory_read("A")) == "0x7a"


def test_mov_c_bit_and_mov_bit_c():
    controller = _run_code(
        """
        CLR C
        SETB 20H
        MOV C,20H
        MOV 21H,C
        """
    )

    assert controller.op.bit_read("21H") is True


def test_orl_anl_c_with_complement_bit():
    controller = _run_code(
        """
        CLR C
        CLR 23H
        ORL C,/23H
        MOV 24H,C
        SETB C
        SETB 25H
        ANL C,/25H
        MOV 26H,C
        """
    )

    assert controller.op.bit_read("24H") is True
    assert controller.op.bit_read("26H") is False


def test_mul_and_div_ab_operands():
    controller = _run_code(
        """
        MOV A,#10H
        MOV B,#10H
        MUL AB
        """
    )
    assert str(controller.op.memory_read("A")) == "0x00"
    assert str(controller.op.memory_read("B")) == "0x01"

    controller = _run_code(
        """
        MOV A,#06H
        MOV B,#03H
        DIV AB
        """
    )
    assert str(controller.op.memory_read("A")) == "0x02"
    assert str(controller.op.memory_read("B")) == "0x00"


def test_end_directive_and_label_only_jump_target():
    controller = _run_code(
        """
        MOV A,#01H
        SJMP,HERE
        MOV A,#0FFH
        HERE:
        INC A
        END
        MOV A,#00H
        """
    )

    assert str(controller.op.memory_read("A")) == "0x02"


def test_mov_to_tmod_and_timer_sfr_registers():
    controller = _run_code(
        """
        MOV TMOD,#01H
        MOV TH0,#0FCH
        MOV TL0,#018H
        """
    )

    assert str(controller.op.memory_read("TMOD")) == "0x01"
    assert str(controller.op.memory_read("TH0")) == "0xfc"
    assert str(controller.op.memory_read("TL0")) == "0x18"


def test_djnz_hex_like_label_name_resolves_as_label():
    controller = _run_code(
        """
        MOV R5,#03H
        D1: DJNZ R5,D1
        MOV A,#0AAH
        """
    )

    assert str(controller.op.memory_read("A")) == "0xaa"
    assert str(controller.op.memory_read("R5")) == "0x00"


def test_ei_alias_sets_interrupt_enable_bits():
    controller = _run_code(
        """
        SETB EI.7
        SETB EI.1
        """
    )

    assert str(controller.op.memory_read("IE")) == "0x82"
