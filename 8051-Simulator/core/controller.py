import re
import inspect

from rich.console import Console

from core.exceptions import OPCODENotFound, SyntaxError
from core.flags import JumpFlag
from core.instruction_set import Instructions
from core.operations import Operations
from core.util import ishex, tohex


class Controller:
    def __init__(self, console=None) -> None:
        self.console = console
        if not console:
            self.console = Console()
        # operations
        self.op = Operations()
        # self.op.super_memory.PC("0x30")  # RAM general scratch pad area
        # instruction set
        self._jump_flag = False
        self._address_jump_flag = None
        self.instruct_set = Instructions(self.op)
        self.instruct_set.controller = self
        self.lookup = {
            name.upper(): call
            for name, call in inspect.getmembers(self.instruct_set, inspect.ismethod)
            if "_" not in name
        }
        # callstack
        self._callstack = []
        self.ready = False
        self._jump_methods = self.op._jump_instructions
        self._wrap_bounceable_methods()
        self._run_idx = 0
        self._labels = {}
        return

    def __repr__(self):
        return f"{self.op.inspect()} \n {self.__callstackrepr__()} \n {self.lookup.keys()}"

    def __callstackrepr__(self) -> str:
        return f"<CallStack calls={len(self._callstack)}>"

    def _wrap_bounceable_methods(self):
        """
        Wrap the jump-ing methods with `Controller._skipper`
        """
        for key in self._jump_methods:
            method = self.lookup.get(key)
            if method:
                self.lookup[key] = self._skipper(method)
        return True

    def _skipper(self, func):
        """
        Wrapper for jump-ing methods
        """

        def _func(*args, **kwargs):
            kwargs["bounce_to_label"] = self._bounce_to_label
            return func(*args, **kwargs)

        return _func

    def _bounce_to_label(self, label):
        _label = str(label)
        idx, _ = self._locate_jump_label(_label)
        if idx is None and ishex(_label):
            pc_target = int(tohex(_label), 16)
            if self._jump_to_pc(pc_target):
                print(f"JUMPING to PC: {label}")
                return True
            idx = pc_target
        if idx is None:
            raise SyntaxError(msg=f"jump target `{label}` not found")
        if idx < 0 or idx > len(self._callstack):
            raise SyntaxError(msg=f"jump target `{label}` out of range")
        print(f"JUMPING to label: {label} index: {idx}")
        self._run_idx = idx
        return True

    def _jump_to_pc(self, target_pc: int) -> bool:
        pc = 0
        for idx, opcodes in enumerate(self.op._internal_PC):
            if pc == target_pc:
                self._run_idx = idx
                return True
            pc += len(opcodes)
        if pc == target_pc:
            self._run_idx = len(self._callstack)
            return True
        return False

    def _sync_PC(self) -> bool:
        for asm_instruct in self.op._internal_PC[self._run_idx - 1]:
            if not asm_instruct:
                return True
            self.op.super_memory.PC.write(*asm_instruct)
            self.console.log(f"Write PC: {asm_instruct}")
        return True

    def _call(self, func, *args, **kwargs) -> bool:
        self._sync_PC()
        return func(*args)

    def _get_jump_flags(self) -> list:
        return [x[2] for x in self._callstack if x[2]]

    def _locate_jump_label(self, label, key="label") -> tuple:
        label = label.upper()
        if key == "label" and label in self._labels:
            return self._labels[label], ("__LABEL__", None, [], {"label": label})
        for idx, x in enumerate(self._callstack):
            key_label = x[3].get(key, None)
            if key_label:
                if key_label == label:
                    return idx, x
        return None, None

    def _target_label(self, label) -> bool:
        self.console.log(f"======={label}========")
        idx_t, x_t = self._locate_jump_label(label, key="target-label")
        idx_l, x_l = self._locate_jump_label(label)
        if x_t and x_l:
            _target_label = x_t[3].get("target-label")
            _assembler = self.op._assembler.get(_target_label._command)
            if _assembler:
                self.op._assembler[_target_label._command] = f"{_assembler} ; -> {label}".strip()
        return

    def inspect(self):
        return self.console.print(self.__repr__())

    def _lookup_opcode_func(self, opcode: str):
        func = self.lookup.get(opcode)
        if func:
            return func
        raise OPCODENotFound(opcode)

    @property
    def callstack(self) -> list:
        return self._callstack

    def _addjob(self, opcode: str, func, args=None, kwargs=None) -> bool:
        if args is None:
            args = []
        if kwargs is None:
            kwargs = {}
        args = list(args)
        jump_target_idx = None
        if self.instruct_set._is_jump_opcode(opcode):
            jump_target_idx = self._jump_target_arg_index(opcode)
        for idx, val in enumerate(args):
            # Preserve jump target tokens as written, even if they look hex-like.
            if jump_target_idx is not None and idx == jump_target_idx:
                continue
            if not self.op.iskeyword(val):
                if ishex(val):
                    args[idx] = tohex(val)
        self._callstack.append((opcode, func, args, kwargs))
        return True

    def _parser(self, command, *args, **kwargs) -> tuple:
        command = command.split(";", 1)[0].strip()
        if not command:
            return None, [], kwargs
        if command[0] == "#":  # Directive
            command = command[1:]
            if not command.strip():
                return None, [], kwargs

        match = re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*:", command)
        if match:
            label = match.group()[:-1]
            kwargs["label"] = JumpFlag(label, self.op.super_memory.PC, command)
            return self._parser(command.replace(f"{label}:", "", 1), *args, **kwargs)

        _proc_command = re.split(r",| ", command)
        for _ in range(_proc_command.count("")):
            _proc_command.remove("")
        opcode = _proc_command[0]
        args = _proc_command[1:]
        return opcode.upper(), args, kwargs

    def _jump_target_arg_index(self, opcode: str) -> int:
        opcode = opcode.upper()
        if opcode == "DJNZ":
            return 1
        if opcode == "CJNE":
            return 2
        if opcode in {"JB", "JNB", "JBC"}:
            return 1
        return 0

    def _jump_needs_offset_placeholder(self, opcode: str) -> bool:
        return opcode.upper() in {"SJMP", "JC", "JNC", "JZ", "JNZ", "DJNZ", "CJNE", "JB", "JNB", "JBC"}

    def parse(self, command, source_line=None):
        self.console.log(command)
        opcode, args, kwargs = self._parser(command)
        kwargs = dict(kwargs)
        label = kwargs.get("label")
        if not opcode:
            if label:
                self._labels[label.upper()] = len(self._callstack)
            return True
        kwargs["_source_line"] = source_line
        kwargs["_source_command"] = command.strip()
        if label:
            self._labels[label.upper()] = len(self._callstack)
        self.console.log(f"opcode: {opcode}; args: {args}; kwargs: {kwargs}")
        if self.instruct_set._is_jump_opcode(opcode) and args:
            print("JUMP instruction")
            target_arg_index = self._jump_target_arg_index(opcode)
            if target_arg_index < len(args):
                target_label = args[target_arg_index]
                if target_label.upper() != "@A+DPTR" and not ishex(target_label):
                    kwargs["target-label"] = JumpFlag(
                        target_label,
                        self.op.super_memory.PC + len(self.op._internal_PC),
                        command,
                    )
            if self._jump_needs_offset_placeholder(opcode):
                args.append("offset")
        opcode_func = self._lookup_opcode_func(opcode)
        self._addjob(opcode, opcode_func, args, kwargs)
        self.op.prepare_operation(command, opcode, *args)
        """
        JNC ZO      ----   Target label
        ...
        ZO: ...     ----    Label


        The function should execute at both commands; if `target-label` or `label` then look for
        `target-label` and `label`;
        if `target-tabel` is found then replace the placeholder obtained using the `PC` in `label`
        """
        _label = kwargs.get("target-label", kwargs.get("label", None))
        if _label:
            self._target_label(_label)
        self.ready = True
        return True

    def parse_all(self, commands):
        for line_no, command in enumerate(commands.split("\n"), start=1):
            proc = command.split(";", 1)[0].strip()
            if not proc:
                continue
            if proc.startswith("#"):
                proc = proc[1:].strip()
            if not proc:
                continue
            if proc.upper().startswith("END"):
                break
            try:
                self.parse(command, source_line=line_no)
            except Exception as exc:
                raise SyntaxError(msg=f"Line {line_no}: `{command.strip()}` -> {exc}")
        return True

    def run_once(self):
        if self._run_idx >= len(self._callstack):
            return {
                "done": True,
                "executed_index": None,
                "next_index": self._run_idx,
                "source_line": None,
                "source_command": None,
                "next_source_line": None,
            }
        try:
            self.console.log(self._callstack[self._run_idx])
            opcode, func, args, kwargs = self._callstack[self._run_idx]
            executed_idx = self._run_idx
            source_line = kwargs.get("_source_line")
            source_command = kwargs.get("_source_command")
            self._run_idx += 1
            print(self._run_idx)
            self._call(func, *args, **kwargs)
            next_source_line = None
            if self._run_idx < len(self._callstack):
                next_kwargs = self._callstack[self._run_idx][3]
                next_source_line = next_kwargs.get("_source_line")
            return {
                "done": self._run_idx >= len(self._callstack),
                "executed_index": executed_idx,
                "next_index": self._run_idx,
                "source_line": source_line,
                "source_command": source_command,
                "next_source_line": next_source_line,
            }
        except StopIteration:
            return {
                "done": self._run_idx >= len(self._callstack),
                "executed_index": None,
                "next_index": self._run_idx,
                "source_line": None,
                "source_command": None,
                "next_source_line": None,
            }
        except Exception as exc:
            if source_line:
                raise SyntaxError(msg=f"Runtime line {source_line}: `{source_command}` -> {exc}")
            raise

    def run(self):
        last_step = None
        while self._run_idx < len(self._callstack):
            last_step = self.run_once()
        if last_step is None:
            last_step = {
                "done": True,
                "executed_index": None,
                "next_index": self._run_idx,
                "source_line": None,
                "source_command": None,
                "next_source_line": None,
            }
        return last_step

    def set_flag(self, key, val):
        self.op.flags[key] = val
        return True

    def set_flags(self, *args, **kwargs):
        return self.op.flags.set_flags(*args, **kwargs)

    def reset(self) -> bool:
        self.__init__(console=self.console)
        return True

    def reset_callstack(self) -> None:
        self._callstack = []
        self._run_idx = 0
        self._labels = {}
        self.op._assembler = {}
        return True

    pass
