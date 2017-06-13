from angrop import rop_utils

from collections import defaultdict

from functions import Functions
from errors import IdentifierException
from runner import Runner
import angr
from angr.errors import SimEngineError, SimMemoryError
import os

from networkx import NetworkXError

import logging
l = logging.getLogger("identifier.identify")


NUM_TESTS = 5

class FuncInfo(object):
    def __init__(self):
        self.stack_vars = None
        self.stack_var_accesses = None
        self.frame_size = None
        self.pushed_regs = None
        self.stack_args = None
        self.stack_arg_accesses = None
        self.buffers = None
        self.var_args = None
        self.bp_based = None
        self.bp_sp_diff = None
        self.accesses_ret = None
        self.preamble_sp_change = None


class Identifier(object):

    _special_case_funcs = ["free"]

    def __init__(self, project, cfg=None, require_predecessors=True, only_find=None):
        self.project = project
        if cfg is not None:
            self._cfg = cfg
        else:
            self._cfg = project.analyses.CFGFast(resolve_indirect_jumps=True)
        self._runner = Runner(project, self._cfg)

        # only find if in this set
        self.only_find = only_find

        # reg list
        a = self.project.arch
        self._sp_reg = a.register_names[a.sp_offset]
        self._bp_reg = a.register_names[a.bp_offset]
        self._ip_reg = a.register_names[a.ip_offset]
        self._reg_list = a.default_symbolic_registers
        self._reg_list = filter(lambda r: r != self._sp_reg, self._reg_list)
        self._reg_list = filter(lambda r: r != self._ip_reg, self._reg_list)

        self.matches = dict()

        self.callsites = None
        self.inv_callsites = None
        self.func_info = dict()
        self.block_to_func = dict()

        self.map_callsites()

        if self._too_large():
            l.warning("Too large")
            return

        self.base_symbolic_state = rop_utils.make_symbolic_state(self.project, self._reg_list)
        self.base_symbolic_state.options.discard(angr.options.SUPPORT_FLOATING_POINT)
        self.base_symbolic_state.regs.bp = self.base_symbolic_state.se.BVS("sreg_" + "ebp" + "-", self.project.arch.bits)

        for f in self._cfg.functions.values():
            if f.is_syscall:
                continue
            if self.project.is_hooked(f.addr):
                continue

            # skip if no predecessors
            try:
                if require_predecessors and len(self._cfg.functions.callgraph.predecessors(f.addr)) == 0:
                    continue
            except NetworkXError:
                if require_predecessors:
                    continue

            # find the actual vars
            try:
                func_info = self.find_stack_vars_x86(f)
                self.func_info[f] = func_info
            except (SimEngineError, SimMemoryError) as ex:
                l.debug("angr translation error: %s", ex.message)
            except IdentifierException as e:
                l.debug("Identifier Exception: %s", e.message)

    def _too_large(self):
        if len(self._cfg.functions) > 400:
            return True
        return False

    def run(self, only_find=None):
        if only_find is not None:
            self.only_find = only_find

        if self._too_large():
            l.warning("Too large")
            return

        for f in self._cfg.functions.values():
            if f.is_syscall:
                continue
            match = self.identify_func(f)
            if match is not None:
                match_func = match
                match_name = match_func.get_name()
                if f.name is not None:
                    l.debug("Found match for function %s at %#x, %s", f.name, f.addr, match_name)
                else:
                    l.debug("Found match for function %#x, %s", f.addr, match_name)
                self.matches[f] = match_name, match_func
                if match_name != "malloc" and match_name != "free":
                    yield f.addr, match_name
            else:
                if f.name is not None:
                    l.debug("No match for function %s at %#x", f.name, f.addr)
                else:
                    l.debug("No match for function %#x", f.addr)

        # Special case functions
        for name in Identifier._special_case_funcs:
            func = Functions[name]()
            for f in self._cfg.functions.values():
                if f in self.matches:
                    continue
                if f not in self.func_info:
                    continue
                if self.func_info[f] is None:
                    continue
                if len(self.func_info[f].stack_args) != func.num_args():
                    continue
                if self._non_normal_args(self.func_info[f].stack_args):
                    continue
                try:
                    result = func.try_match(f, self, self._runner)
                except IdentifierException as e:
                    l.warning('Encountered IdentifierException trying to analyze %#x, reason: %s',
                            f.addr, e.message)
                    continue
                except angr.SimSegfaultError:
                    continue
                except angr.SimError as e:
                    l.warning("SimError %s", e.message)
                    continue
                except angr.AngrError as e:
                    l.warning("AngrError %s", e.message)
                    continue

                if result:
                    self.matches[f] = func.get_name(), func
                    yield f.addr, func.get_name()

        # fixup malloc/free
        to_remove = []
        for f, (match_name, match_func) in self.matches.items():
            if match_name == "malloc" or match_name == "free":
                if not self.can_call_same_name(f.addr, match_name):
                    yield f.addr, match_func.get_name()
                else:
                    to_remove.append(f)
        for f in to_remove:
            del self.matches[f]

    def can_call_same_name(self, addr, name):
        if addr not in self._cfg.functions.callgraph.nodes():
            return False
        seen = set()
        to_process = [addr]
        while to_process:
            curr = to_process.pop()
            if curr in seen:
                continue
            seen.add(curr)
            succ = list(self._cfg.functions.callgraph.successors(curr))
            for s in succ:
                if s in self._cfg.functions:
                    f = self._cfg.functions[s]
                    if f in self.matches:
                        if self.matches[f][0] == name:
                            return True
            to_process.extend(succ)
        return False

    def get_func_info(self, func):
        if isinstance(func, (int, long)):
            func = self._cfg.functions[func]
        if func not in self.func_info:
            return None
        return self.func_info[func]

    @staticmethod
    def constrain_all_zero(before_state, state, regs):
        for r in regs:
            state.add_constraints(before_state.registers.load(r) == 0)

    def identify_func(self, function):
        l.debug("function at %#x", function.addr)
        if function.is_syscall:
            return None

        func_info = self.get_func_info(function)
        if func_info is None:
            l.debug("func_info is none")
            return None

        l.debug("num args %d", len(func_info.stack_args))

        try:
            calls_other_funcs = len(self._cfg.functions.callgraph.successors(function.addr)) > 0
        except NetworkXError:
            calls_other_funcs = False

        for name, f in Functions.iteritems():
            # check if we should be finding it
            if self.only_find is not None and name not in self.only_find:
                continue
            if name in Identifier._special_case_funcs:
                continue

            # generate an object of the class
            f = f()
            # test it
            if f.num_args() != len(func_info.stack_args) or f.var_args() != func_info.var_args:
                continue
            if calls_other_funcs and not f.can_call_other_funcs():
                continue

            l.debug("testing: %s", name)
            if not self.check_tests(function, f):
                continue
            # match!
            return f

        if len(func_info.stack_args) == 2 and func_info.var_args and len(function.graph.nodes()) < 5:
            match = Functions["fdprintf"]()
            l.warning("%#x assuming fd printf for var_args func with 2 args although we don't really know", function.addr)
            return match

        return None

    def check_tests(self, cfg_func, match_func):
        try:
            if not match_func.pre_test(cfg_func, self._runner):
                return False
            for i in xrange(NUM_TESTS):
                test_data = match_func.gen_input_output_pair()
                if test_data is not None and not self._runner.test(cfg_func, test_data):
                    return False
            return True
        except angr.SimSegfaultError:
            return False
        except angr.SimError as e:
            l.warning("SimError %s", e.message)
            return False
        except angr.AngrError as e:
            l.warning("AngrError %s", e.message)
            return False

    def map_callsites(self):
        callsites = dict()
        for f in self._cfg.functions.values():
            for callsite in f.get_call_sites():
                if f.get_call_target(callsite) is None:
                    continue
                callsites[callsite] = f.get_call_target(callsite)
        self.callsites = callsites

        # create inverse callsite map
        self.inv_callsites = defaultdict(set)
        for c, f in self.callsites.iteritems():
            self.inv_callsites[f].add(c)

        # create map of blocks to the function they reside in
        self.block_to_func = dict()
        for f in self._cfg.functions.values():
            for b in f.graph.nodes():
                self.block_to_func[b.addr] = f

    def do_trace(self, addr_trace, reverse_accesses, func_info):
        # get to the callsite
        s = rop_utils.make_symbolic_state(self.project, self._reg_list, stack_length=200)
        s.options.discard(angr.options.AVOID_MULTIVALUED_WRITES)
        s.options.discard(angr.options.AVOID_MULTIVALUED_READS)
        s.options.add(angr.options.UNDER_CONSTRAINED_SYMEXEC)
        s.options.discard(angr.options.LAZY_SOLVES)

        func_info = self.func_info[self.block_to_func[addr_trace[0]]]
        for i in range(func_info.frame_size/self.project.arch.bytes+5):
            s.stack_push(s.se.BVS("var_" + hex(i), self.project.arch.bits))

        if func_info.bp_based:
            s.regs.bp = s.regs.sp + func_info.bp_sp_diff
        s.regs.ip = addr_trace[0]
        addr_trace = addr_trace[1:]
        p = self.project.factory.path(s)
        while len(addr_trace) > 0:
            p.step()
            stepped = False
            for ss in p.successors:
                # todo could write symbolic data to pointers passed to functions
                if ss.jumpkind == "Ijk_Call":
                    ss.state.regs.eax = ss.state.se.BVS("unconstrained_ret_%#x" % ss.addr, ss.state.arch.bits)
                    ss.state.regs.ip = ss.state.stack_pop()
                    ss.state.scratch.jumpkind = "Ijk_Ret"
                if ss.addr == addr_trace[0]:
                    p = ss
                    stepped = True
            if not stepped:
                if len(p.unconstrained_successors) > 0:
                    p = p.unconstrained_successors[0]
                    if p.jumpkind == "Ijk_Call":
                        p.state.regs.eax = p.state.se.BVS("unconstrained_ret_%#x" % p.addr, p.state.arch.bits)
                        p.state.regs.ip = p.state.stack_pop()
                        p.state.scratch.jumpkind = "Ijk_Ret"
                    p.state.regs.ip = addr_trace[0]
                    stepped = True
            if not stepped:
                raise IdentifierException("could not get call args")
            addr_trace = addr_trace[1:]

        # step one last time to the call
        p.step()
        if len(p.successors) == 0:
            IdentifierException("Didn't succeed call")
        return p.successors[0]

    def get_call_args(self, func, callsite):
        if isinstance(func, (int, long)):
            func = self._cfg.functions[func]
        func_info = self.func_info[func]
        if len(func_info.stack_args) == 0:
            return []

        # get the accesses of calling func
        calling_func = self.block_to_func[callsite]
        reverse_accesses = dict()
        calling_func_info = self.func_info[calling_func]
        stack_var_accesses = calling_func_info.stack_var_accesses
        for stack_var, v in stack_var_accesses.items():
            for addr, type in v:
                reverse_accesses[addr] = (stack_var, type)

        # we need to step back as far as possible
        start = calling_func.get_node(callsite)
        addr_trace = []
        while len(calling_func.transition_graph.predecessors(start)) == 1:
            # stop at a call, could continue farther if no stack addr passed etc
            prev_block = calling_func.transition_graph.predecessors(start)[0]
            addr_trace = [start.addr] + addr_trace
            start = prev_block

        addr_trace = [start.addr] + addr_trace
        succ = None
        while len(addr_trace):
            try:
                succ = self.do_trace(addr_trace, reverse_accesses, calling_func_info)
                break
            except IdentifierException:
                addr_trace = addr_trace[1:]
        if len(addr_trace) == 0:
            return None

        succ_state = succ.state
        arch_bytes = self.project.arch.bytes
        args = []
        for arg in func_info.stack_args:
            arg_addr = succ_state.regs.sp + arg + arch_bytes
            args.append(succ_state.memory.load(arg_addr, arch_bytes, endness=self.project.arch.memory_endness))

        args_as_stack_vars = []
        for a in args:
            if not a.symbolic:
                sp_off = succ_state.se.any_int(a-succ_state.regs.sp-arch_bytes)
                if calling_func_info.bp_based:
                    bp_off = sp_off - calling_func_info.bp_sp_diff
                else:
                    bp_off = sp_off - (calling_func_info.frame_size + self.project.arch.bytes) + self.project.arch.bytes

                if abs(bp_off) < 0x1000:
                    args_as_stack_vars.append(bp_off)
                else:
                    args_as_stack_vars.append(None)
            else:
                args_as_stack_vars.append(None)

        return args, args_as_stack_vars

    @staticmethod
    def get_reg_name(arch, reg_offset):
        """
        :param arch: the architecture
        :param reg_offset: Tries to find the name of a register given the offset in the registers.
        :return: The register name
        """
        # todo does this make sense
        if reg_offset is None:
            return None

        original_offset = reg_offset
        while reg_offset >= 0 and reg_offset >= original_offset - (arch.bits/8):
            if reg_offset in arch.register_names:
                return arch.register_names[reg_offset]
            else:
                reg_offset -= 1
        return None

    @staticmethod
    def _make_regs_symbolic(input_state, reg_list, project):
        """
        converts an input state into a state with symbolic registers
        :return: the symbolic state
        """
        state = input_state.copy()
        # overwrite all registers
        for reg in reg_list:
            state.registers.store(reg, state.se.BVS("sreg_" + reg + "-", project.arch.bits, explicit_name=True))
        # restore sp
        state.regs.sp = input_state.regs.sp
        # restore bp
        state.regs.bp = input_state.regs.bp
        return state

    def _prefilter_floats(self, func):

        # calling _get_block() from `func` respects the size of the basic block
        # in extreme cases (like at the end of a section where VEX cannot disassemble the instruction beyond the
        # section boundary), directly calling project.factory.block() on func.addr may lead to an AngrTranslationError.
        bl = func._get_block(func.addr).vex

        if any(c.type.startswith("Ity_F") for c in bl.all_constants):
            raise IdentifierException("floating const")

    def find_stack_vars_x86(self, func):

        # could also figure out if args are buffers etc
        # doesn't handle dynamically allocated stack, etc

        if isinstance(func, (int, long)):
            func = self._cfg.functions[func]

        self._prefilter_floats(func)

        if func.name is not None:
            l.debug("finding stack vars %s", func.name)
        else:
            l.debug("finding stack vars %#x", func.addr)

        if len(func.block_addrs_set) > 500:
            raise IdentifierException("too many blocks")

        if func.startpoint is None:
            raise IdentifierException("Startpoint is None")

        initial_state = self.base_symbolic_state.copy()

        reg_dict = dict()
        for r in self._reg_list + [self._bp_reg]:
            reg_dict[hash(initial_state.registers.load(r))] = r

        initial_state.regs.ip = func.startpoint.addr
        initial_path = self.project.factory.path(initial_state)

        # find index where stack value is constant
        initial_path.step()
        succ = (initial_path.successors + initial_path.unconstrained_successors)[0]

        if succ.state.scratch.jumpkind == "Ijk_Call":
            goal_sp = succ.state.se.any_int(succ.state.regs.sp + self.project.arch.bytes)
            # could be that this is wrong since we could be pushing args...
            # so let's do a hacky check for pushes after a sub
            bl = self.project.factory.block(func.startpoint.addr)
            # find the sub sp
            for i, insn in enumerate(bl.capstone.insns):
                if str(insn.mnemonic) == "sub" and str(insn.op_str).startswith("esp"):
                    initial_path = self.project.factory.path(initial_state)
                    initial_path.step(num_inst=i+1)
                    succ = (initial_path.successors + initial_path.unconstrained_successors)[0]
                    goal_sp = succ.state.se.any_int(succ.state.regs.sp)

        elif succ.state.scratch.jumpkind == "Ijk_Ret":
            # here we need to know the min sp val
            min_sp = initial_state.se.any_int(initial_state.regs.sp)
            for i in xrange(self.project.factory.block(func.startpoint.addr).instructions):
                test_p = self.project.factory.path(initial_state)
                test_p.step(num_inst=i)
                succ = (test_p.successors + test_p.unconstrained_successors)[0]
                test_sp = succ.state.se.any_int(succ.state.regs.sp)
                if test_sp < min_sp:
                    min_sp = test_sp
                elif test_sp > min_sp:
                    break
            goal_sp = min_sp
        else:
            goal_sp = succ.state.se.any_int(succ.state.regs.sp)

        # find the end of the preamble
        num_preamble_inst = None
        succ = None
        for i in xrange(0, self.project.factory.block(func.startpoint.addr).instructions):
            test_p = self.project.factory.path(initial_state)
            if i == 0:
                succ = test_p
            else:
                test_p.step(num_inst=i)
                succ = (test_p.successors + test_p.unconstrained_successors)[0]
            test_sp = succ.state.se.any_int(succ.state.regs.sp)
            if test_sp == goal_sp:
                num_preamble_inst = i
                break

        # hacky check for mov ebp esp
        # happens when this is after the pushes...
        if num_preamble_inst == 0:
            end_addr = func.startpoint.addr
        else:
            end_addr = func.startpoint.addr + self.project.factory.block(func.startpoint.addr, num_inst=num_preamble_inst).size
        if self._sets_ebp_from_esp(initial_state, end_addr):
            num_preamble_inst += 1
            test_p = self.project.factory.path(initial_state)
            test_p.step(num_inst=num_preamble_inst)
            succ = (test_p.successors + test_p.unconstrained_successors)[0]

        min_sp = goal_sp
        initial_sp = initial_state.se.any_int(initial_state.regs.sp)
        frame_size = initial_sp - min_sp - self.project.arch.bytes
        if num_preamble_inst is None or succ is None:
            raise IdentifierException("preamble checks failed for %#x" % func.startpoint.addr)

        if len(succ.state.se.any_n_int((initial_path.state.regs.sp - succ.state.regs.bp), 2)) == 1:
            bp_based = True
        else:
            bp_based = False

        preamble_sp_change = succ.state.regs.sp - initial_path.state.regs.sp
        if preamble_sp_change.symbolic:
            raise IdentifierException("preamble sp change")
        preamble_sp_change = initial_path.state.se.any_int(preamble_sp_change)

        main_state = self._make_regs_symbolic(succ.state, self._reg_list, self.project)
        if bp_based:
            main_state = self._make_regs_symbolic(main_state, [self._bp_reg], self.project)

        pushed_regs = []
        for a in succ.last_actions:
            if a.type == "mem" and a.action == "write":
                addr = succ.state.se.any_int(a.addr.ast)
                if min_sp <= addr <= initial_sp:
                    if hash(a.data.ast) in reg_dict:
                        pushed_regs.append(reg_dict[hash(a.data.ast)])
        pushed_regs = pushed_regs[::-1]
        # found the preamble

        # find the ends of the function
        ends = set()
        all_end_addrs = set()
        if num_preamble_inst == 0:
            end_preamble = func.startpoint.addr
            preamble_addrs = set()
        else:
            preamble_block = self.project.factory.block(func.startpoint.addr, num_inst=num_preamble_inst)
            preamble_addrs = set(preamble_block.instruction_addrs)
            end_preamble = func.startpoint.addr + preamble_block.vex.size
        for block in func.endpoints:
            addr = block.addr
            if addr in preamble_addrs:
                addr = end_preamble
            if self.project.factory.block(addr).vex.jumpkind == "Ijk_Ret":
                main_state.ip = addr
                test_p = self.project.factory.path(main_state)
                test_p.step()
                for a in (test_p.successors + test_p.unconstrained_successors)[0].last_actions:
                    if a.type == "reg" and a.action == "write":
                        if self.get_reg_name(self.project.arch, a.offset) == self._sp_reg:
                            ends.add(a.ins_addr)
                            all_end_addrs.update(set(self.project.factory.block(a.ins_addr).instruction_addrs))

        bp_sp_diff = None
        if bp_based:
            bp_sp_diff = main_state.se.any_int(main_state.regs.bp - main_state.regs.sp)

        all_addrs = set()
        for bl_addr in func.block_addrs:
            all_addrs.update(set(self._cfg.get_any_node(bl_addr).instruction_addrs))

        sp = main_state.se.BVS("sym_sp", self.project.arch.bits, explicit_name=True)
        main_state.regs.sp = sp
        bp = None
        if bp_based:
            bp = main_state.se.BVS("sym_bp", self.project.arch.bits, explicit_name=True)
            main_state.regs.bp = bp

        stack_vars = set()
        stack_var_accesses = defaultdict(set)
        buffers = set()
        possible_stack_vars = []
        for addr in all_addrs - all_end_addrs - preamble_addrs:
            bl = self.project.factory.block(addr, num_inst=1)
            if self._is_bt(bl):
                continue
            if self._is_jump_or_call(bl):
                continue
            if self._no_sp_or_bp(bl):
                continue
            main_state.ip = addr
            test_p = self.project.factory.path(main_state)
            test_p.step(num_inst=1)
            succ = (test_p.successors + test_p.unconstrained_successors)[0]

            written_regs = set()
            # we can get stack variables via memory actions
            for a in succ.last_actions:
                if a.type == "mem":
                    if "sym_sp" in a.addr.ast.variables or (bp_based and "sym_bp" in a.addr.ast.variables):
                        possible_stack_vars.append((addr, a.addr.ast, a.action))
                if a.type == "reg" and a.action == "write":
                    # stack variables can also be if a stack addr is loaded into a register, eg lea
                    reg_name = self.get_reg_name(self.project.arch, a.offset)
                    # ignore bp if bp_based
                    if reg_name == self._bp_reg and bp_based:
                        continue
                    # ignore weird regs
                    if reg_name not in self._reg_list:
                        continue
                    # check if it was a stack var
                    if "sym_sp" in a.data.ast.variables or (bp_based and "sym_bp" in a.data.ast.variables):
                        possible_stack_vars.append((addr, a.data.ast, "load"))
                    written_regs.add(reg_name)

        for addr, ast, action in possible_stack_vars:
            if "sym_sp" in ast.variables:
                # constrain all to be zero so we detect the base address of buffers
                simplified = succ.state.se.simplify(ast - sp)
                if succ.state.se.symbolic(simplified):
                    self.constrain_all_zero(test_p.state, succ.state, self._reg_list)
                    is_buffer = True
                else:
                    is_buffer = False
                sp_off = succ.state.se.any_int(simplified)
                if sp_off > 2 ** (self.project.arch.bits - 1):
                    sp_off = 2 ** self.project.arch.bits - sp_off

                # get the offsets
                if bp_based:
                    bp_off = sp_off - bp_sp_diff
                else:
                    bp_off = sp_off - (initial_sp-min_sp) + self.project.arch.bytes

                stack_var_accesses[bp_off].add((addr, action))
                stack_vars.add(bp_off)

                if is_buffer:
                    buffers.add(bp_off)
            else:
                simplified = succ.state.se.simplify(ast - bp)
                if succ.state.se.symbolic(simplified):
                    self.constrain_all_zero(test_p.state, succ.state, self._reg_list)
                    is_buffer = True
                else:
                    is_buffer = False
                bp_off = succ.state.se.any_int(simplified)
                if bp_off > 2 ** (self.project.arch.bits - 1):
                    bp_off = -(2 ** self.project.arch.bits - bp_off)
                stack_var_accesses[bp_off].add((addr, action))
                stack_vars.add(bp_off)
                if is_buffer:
                    buffers.add(bp_off)

        stack_args = list()
        stack_arg_accesses = defaultdict(set)
        for v in stack_vars:
            if v > 0:
                stack_args.append(v - self.project.arch.bytes * 2)
                stack_arg_accesses[v - self.project.arch.bytes * 2] = stack_var_accesses[v]
                del stack_var_accesses[v]
        stack_args = sorted(stack_args)
        stack_vars = sorted(stack_vars)

        if len(stack_args) > 0 and any(a[1] == "load" for a in stack_arg_accesses[stack_args[-1]]):
            # print "DETECTED VAR_ARGS"
            var_args = True
            del stack_arg_accesses[stack_args[-1]]
            stack_args = stack_args[:-1]
        else:
            var_args = False

        for v in stack_vars:
            if any(a[1] == "load" for a in stack_var_accesses[v]):
                buffers.add(v)

        if any(v > 0x10000 or v < -0x10000 for v in stack_vars):
            raise IdentifierException("stack seems seems incorrect")

        # return it all in a function info object
        func_info = FuncInfo()
        func_info.preamble_sp_change = preamble_sp_change
        func_info.stack_vars = stack_vars
        func_info.stack_var_accesses = stack_var_accesses
        func_info.frame_size = frame_size
        func_info.pushed_regs = pushed_regs
        func_info.stack_args = stack_args
        func_info.stack_arg_accesses = stack_arg_accesses
        func_info.buffers = buffers
        func_info.var_args = var_args
        func_info.bp_based = bp_based
        if func_info.bp_based:
            func_info.bp_sp_diff = bp_sp_diff

        self._filter_stack_args(func_info)

        return func_info

    def _sets_ebp_from_esp(self, state, addr):
        state = state.copy()
        state.regs.ip = addr
        state.regs.sp = state.se.BVS("sym_sp", 32, explicit_name=True)
        p = self.project.factory.path(state)
        p.step()
        succ = (p.successors + p.unconstrained_successors)[0]

        diff = state.regs.sp - succ.state.regs.bp
        if not diff.symbolic:
            return True
        if len(diff.variables) > 1 or any("ebp" in v for v in diff.variables):
            return False
        if len(succ.state.se.any_n_int((state.regs.sp - succ.state.regs.bp), 2)) == 1:
            return True
        return False

    @staticmethod
    def _is_bt(bl):
        # vex does really weird stuff with bit test instructions
        if bl.bytes.startswith("\x0f\xa3"):
            return True
        return False

    @staticmethod
    def _is_jump_or_call(bl):
        if bl.vex.jumpkind != "Ijk_Boring":
            return True
        if len(bl.vex.constant_jump_targets) != 1:
            return True
        if next(iter(bl.vex.constant_jump_targets)) != bl.addr + bl.size:
            return True

        return False

    @staticmethod
    def get_reg_name(arch, reg_offset):
        """
        :param reg_offset: Tries to find the name of a register given the offset in the registers.
        :return: The register name
        """
        if reg_offset is None:
            return None

        original_offset = reg_offset
        while reg_offset >= 0 and reg_offset >= original_offset - (arch.bits/8):
            if reg_offset in arch.register_names:
                return arch.register_names[reg_offset]
            else:
                reg_offset -= 1
        return None

    def _no_sp_or_bp(self, bl):
        for s in bl.vex.statements:
            for e in [s] + s.expressions:
                if e.tag == "Iex_Get":
                    reg = self.get_reg_name(self.project.arch, e.offset)
                    if reg == "ebp" or reg == "esp":
                        return False
                elif e.tag == "Ist_Put":
                    reg = self.get_reg_name(self.project.arch, e.offset)
                    if reg == "ebp" or reg == "esp":
                        return False
        return True

    @staticmethod
    def _filter_stack_args(func_info):
        if -4 in func_info.stack_args:
            func_info.accesses_ret = True
            func_info.stack_args = [x for x in func_info.stack_args if x != -4]
            del func_info.stack_arg_accesses[-4]
        else:
            func_info.accesses_ret = False

        if any(arg < 0 for arg in func_info.stack_args):
            raise IdentifierException("negative arg")

    @staticmethod
    def _non_normal_args(stack_args):
        for i, arg in enumerate(stack_args):
            if arg != i*4:
                return True
            return False
