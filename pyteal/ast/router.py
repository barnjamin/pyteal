from dataclasses import dataclass
from typing import Any, Callable, cast, Optional

import algosdk.abi as sdk_abi

from pyteal.config import METHOD_ARG_NUM_LIMIT
from pyteal.errors import TealInputError
from pyteal.types import TealType
from pyteal.compiler.compiler import compileTeal, DEFAULT_TEAL_VERSION, OptimizeOptions
from pyteal.ir.ops import Mode

from pyteal.ast import abi
from pyteal.ast.subroutine import (
    OutputKwArgInfo,
    SubroutineFnWrapper,
    ABIReturnSubroutine,
)
from pyteal.ast.cond import Cond
from pyteal.ast.expr import Expr
from pyteal.ast.app import OnComplete, EnumInt
from pyteal.ast.int import Int
from pyteal.ast.seq import Seq
from pyteal.ast.methodsig import MethodSignature
from pyteal.ast.naryexpr import And, Or
from pyteal.ast.txn import Txn
from pyteal.ast.return_ import Approve, Reject, Return
from pyteal.ast.global_ import Global


"""
Notes for OC:
- creation conflict with closeout and clear-state
- must check: txn ApplicationId == 0 for creation
- clear-state AST build should be separated with other OC AST build
"""


class OnCompleteActions:
    def __init__(self):
        self.oc_to_action: dict[
            EnumInt, dict[bool, Expr | SubroutineFnWrapper | ABIReturnSubroutine]
        ] = dict()

    def set_action(
        self,
        action: Expr | SubroutineFnWrapper | ABIReturnSubroutine,
        on_complete: EnumInt,
        create: bool = False,
    ) -> "OnCompleteActions":
        if on_complete.name not in self.oc_to_action:
            self.oc_to_action[on_complete] = dict()
        self.oc_to_action[on_complete][create] = action
        return self

    @classmethod
    def template(
        cls,
        *,
        approve_on_noop_create: Optional[bool] = True,
        creator_can_update: Optional[bool] = True,
        creator_can_delete: Optional[bool] = True,
        can_opt_in: Optional[bool] = False,
        can_close_out: Optional[bool] = True,
        can_clear_state: Optional[bool] = True,
    ) -> "OnCompleteActions":
        instance = cls()

        if approve_on_noop_create:
            instance.set_action(Approve(), OnComplete.NoOp, True)

        allow_creator_expr = Return(Txn.sender() == Global.creator_address())
        instance.set_action(
            allow_creator_expr if creator_can_update else Reject(),
            OnComplete.UpdateApplication,
        )
        instance.set_action(
            allow_creator_expr if creator_can_delete else Reject(),
            OnComplete.DeleteApplication,
        )

        instance.set_action(Approve() if can_opt_in else Reject(), OnComplete.OptIn)
        instance.set_action(
            Approve() if can_close_out else Reject(), OnComplete.CloseOut
        )
        instance.set_action(
            Approve() if can_clear_state else Reject(), OnComplete.ClearState
        )

        return instance


OnCompleteActions.__module__ = "pyteal"


@dataclass
class ProgramNode:
    """
    This class contains a condition branch in program AST, with
    - `condition`: logical condition of entering such AST branch
    - `branch`: steps to execute the branch after entering
    - `method_info` (optional): only needed in approval program node, constructed from
        - SDK's method
        - ABIReturnSubroutine's method signature
    """

    condition: Expr
    branch: Expr
    method_info: Optional[sdk_abi.Method]
    ast_order_indicator: "ConflictMapElem"


ProgramNode.__module__ = "pyteal"


@dataclass
class ConflictMapElem:
    is_method_call: bool
    method_name: str
    on_creation: bool

    def __lt__(self, other: "ConflictMapElem"):
        # compare under same oc condition
        # can be used to order AST
        if not isinstance(other, ConflictMapElem):
            raise TealInputError(
                "ConflictMapElem can only check conflict with other ConflictMapElem"
            )

        if self.is_method_call:
            if not other.is_method_call:
                return False
        else:
            if other.is_method_call:
                return True

        # either both is_method_call or not
        # compare on_creation
        if self.on_creation:
            return not other.on_creation
        else:
            return other.on_creation

    def has_conflict_with(self, other: "ConflictMapElem"):
        if not isinstance(other, ConflictMapElem):
            raise TealInputError(
                "ConflictMapElem can only check conflict with other ConflictMapElem"
            )
        if not self.is_method_call and not other.is_method_call:
            if self.method_name == other.method_name:
                raise TealInputError(f"re-registering {self.method_name} under same OC")
            else:
                raise TealInputError(
                    f"re-registering {self.method_name} and {other.method_name} under same OC"
                )


ConflictMapElem.__module__ = "pyteal"


class ASTConflictResolver:
    def __init__(self):
        self.conflict_detect_map: dict[str, list[ConflictMapElem]] = {
            name: list() for name in dir(OnComplete) if not name.startswith("__")
        }

    def add_elem_to(self, oc: str, conflict_map_elem: ConflictMapElem):
        if oc not in self.conflict_detect_map:
            raise TealInputError(
                f"{oc} is not one of the element in conflict map, should be one of the OnCompletes"
            )
        elems_under_oc: list[ConflictMapElem] = self.conflict_detect_map[oc]
        for elem in elems_under_oc:
            if elem.has_conflict_with(conflict_map_elem):
                raise TealInputError(f"{elem} has conflict with {conflict_map_elem}")

        self.conflict_detect_map[oc].append(conflict_map_elem)


ASTConflictResolver.__module__ = "pyteal"


class Router:
    """
    Class that help constructs:
    - an ARC-4 app's approval/clear-state programs
    - and a contract JSON object allowing for easily read and call methods in the contract
    """

    def __init__(
        self,
        name: str,
        bare_calls: OnCompleteActions,
    ) -> None:
        """
        Args:
            name (optional): the name of the smart contract, used in the JSON object.
                Default name is `contract`
        """

        self.name: str = name
        self.approval_if_then: list[ProgramNode] = []
        self.clear_state_if_then: list[ProgramNode] = []
        self.conflict_detect_map: ASTConflictResolver = ASTConflictResolver()
        for oc, action_on_creation in bare_calls.oc_to_action.items():
            for on_create, action in action_on_creation.items():
                self.__add_bare_call(action, on_completes=[oc], creation=on_create)

    @staticmethod
    def parse_conditions(
        method_to_register: Optional[ABIReturnSubroutine],
        on_completes: list[EnumInt],
        creation: bool,
    ) -> tuple[list[Expr], list[Expr]]:
        """This is a helper function in inferring valid approval/clear-state program condition.

        It starts with some initialization check to resolve some conflict:
        - `creation` option is contradicting with OnCompletion.CloseOut and OnCompletion.ClearState
        - if there is `method_to_register` existing, then `method_signature` should appear

        Then this function appends conditions to approval/clear-state program condition:
        - if `creation` is true, then append `Txn.application_id() == 0` to approval conditions
        - if it is handling abi-method, then
          `Txn.application_arg[0] == hash(method_signature) &&
           Txn.application_arg_num == 1 + min(METHOD_ARG_NUM_LIMIT, method's arg num)`
          where `METHOD_ARG_NUM_LIMIT == 15`.
          # TODO wonder if we need to care about arg number, if the arg number is not enough/valid
          # we just directly fail
        - if it is handling conditions for other cases, then `Int(1)` automatically approve

        Args:
            method_to_register: an ABIReturnSubroutine if exists, or None
            on_completes: a list of OnCompletion args
            creation: a boolean variable indicating if this condition is triggered on creation
        Returns:
            approval_conds: A list of exprs for approval program's condition on: creation?, method/bare, Or[OCs]
            clear_state_conds: A list of exprs for clear-state program's condition on: method/bare
        """

        # check that the onComplete has no duplicates
        if len(on_completes) != len(set(on_completes)):
            raise TealInputError(f"input {on_completes} has duplicated on_complete(s)")
        if len(on_completes) == 0:
            raise TealInputError("on complete input should be non-empty list")

        # Check the existence of OC.ClearState (needed later)
        clear_state_exist = any(
            map(lambda x: str(x) == str(OnComplete.ClearState), on_completes)
        )
        oc_other_than_clear_state_exists = any(
            map(lambda x: str(x) != str(OnComplete.ClearState), on_completes)
        )

        # Check:
        # - if current condition is for *ABI METHOD*
        # - *method selector matches* or *BARE APP CALL* (Int(1))
        method_or_bare_condition: Expr
        if method_to_register is not None:
            method_or_bare_condition = (
                MethodSignature(method_to_register.method_signature())
                == Txn.application_args[0]
            )
        else:
            method_or_bare_condition = Int(1)

        # Check if it is a *CREATION*
        approval_conds: list[Expr] = (
            [Txn.application_id() == Int(0)] if creation else []
        )
        clear_state_conds: list[Expr] = []

        if oc_other_than_clear_state_exists:
            approval_conds.append(method_or_bare_condition)

        # if OC.ClearState exists, add method-or-bare-condition since it is only needed in ClearStateProgram
        if clear_state_exist:
            clear_state_conds.append(method_or_bare_condition)

        # Check onComplete conditions for approval_conds, filter out *ClearState*
        approval_oc_conds: list[Expr] = [
            Txn.on_completion() == oc
            for oc in on_completes
            if str(oc) != str(OnComplete.ClearState)
        ]

        # if approval OC condition is not empty, append Or to approval_conds
        if len(approval_oc_conds) > 0:
            approval_conds.append(Or(*approval_oc_conds))

        # what we have here is:
        # list of conds for approval program on one branch: creation?, method/bare, Or[OCs]
        # list of conds for clearState program on one branch: method/bare
        return approval_conds, clear_state_conds

    @staticmethod
    def wrap_handler(
        is_method_call: bool, handler: ABIReturnSubroutine | SubroutineFnWrapper | Expr
    ) -> Expr:
        """This is a helper function that handles transaction arguments passing in bare-app-call/abi-method handlers.

        If `is_abi_method` is True, then it can only be `ABIReturnSubroutine`,
        otherwise:
            - both `ABIReturnSubroutine` and `Subroutine` takes 0 argument on the stack.
            - all three cases have none (or void) type.

        On ABI method case, if the ABI method has more than 15 args, this function manages to de-tuple
        the last (16-th) Txn app-arg into a list of ABI method arguments, and pass in to the ABI method.

        Args:
            is_method_call: a boolean value that specify if the handler is an ABI method.
            handler: an `ABIReturnSubroutine`, or `SubroutineFnWrapper` (for `Subroutine` case), or an `Expr`.
        Returns:
            Expr:
                - for bare-appcall it returns an expression that the handler takes no txn arg and Approve
                - for abi-method it returns the txn args correctly decomposed into ABI variables,
                  passed in ABIReturnSubroutine and logged, then approve.
        """
        if not is_method_call:
            match handler:
                case Expr():
                    if handler.type_of() != TealType.none:
                        raise TealInputError(
                            f"bare appcall handler should be TealType.none not {handler.type_of()}."
                        )
                    return handler if handler.has_return() else Seq(handler, Approve())
                case SubroutineFnWrapper():
                    if handler.type_of() != TealType.none:
                        raise TealInputError(
                            f"subroutine call should be returning none not {handler.type_of()}."
                        )
                    if handler.subroutine.argument_count() != 0:
                        raise TealInputError(
                            f"subroutine call should take 0 arg for bare-app call. "
                            f"this subroutine takes {handler.subroutine.argument_count()}."
                        )
                    return Seq(handler(), Approve())
                case ABIReturnSubroutine():
                    if handler.type_of() != "void":
                        raise TealInputError(
                            f"abi-returning subroutine call should be returning void not {handler.type_of()}."
                        )
                    if handler.subroutine.argument_count() != 0:
                        raise TealInputError(
                            f"abi-returning subroutine call should take 0 arg for bare-app call. "
                            f"this abi-returning subroutine takes {handler.subroutine.argument_count()}."
                        )
                    return Seq(cast(Expr, handler()), Approve())
                case _:
                    raise TealInputError(
                        "bare appcall can only accept: none type Expr, or Subroutine/ABIReturnSubroutine with none return and no arg"
                    )
        else:
            if not isinstance(handler, ABIReturnSubroutine):
                raise TealInputError(
                    f"method call should be only registering ABIReturnSubroutine, got {type(handler)}."
                )
            if not handler.is_abi_routable():
                raise TealInputError(
                    f"method call ABIReturnSubroutine is not registrable"
                    f"got {handler.subroutine.argument_count()} args with {len(handler.subroutine.abi_args)} ABI args."
                )

            arg_type_specs = cast(
                list[abi.TypeSpec], handler.subroutine.expected_arg_types
            )
            if handler.subroutine.argument_count() > METHOD_ARG_NUM_LIMIT:
                last_arg_specs_grouped = arg_type_specs[METHOD_ARG_NUM_LIMIT - 1 :]
                arg_type_specs = arg_type_specs[: METHOD_ARG_NUM_LIMIT - 1]
                last_arg_spec = abi.TupleTypeSpec(*last_arg_specs_grouped)
                arg_type_specs.append(last_arg_spec)

            arg_abi_vars: list[abi.BaseType] = [
                type_spec.new_instance() for type_spec in arg_type_specs
            ]
            decode_instructions: list[Expr] = [
                arg_abi_vars[i].decode(Txn.application_args[i + 1])
                for i in range(len(arg_type_specs))
            ]

            if handler.subroutine.argument_count() > METHOD_ARG_NUM_LIMIT:
                tuple_arg_type_specs: list[abi.TypeSpec] = cast(
                    list[abi.TypeSpec],
                    handler.subroutine.expected_arg_types[METHOD_ARG_NUM_LIMIT - 1 :],
                )
                tuple_abi_args: list[abi.BaseType] = [
                    t_arg_ts.new_instance() for t_arg_ts in tuple_arg_type_specs
                ]
                last_tuple_arg: abi.Tuple = cast(abi.Tuple, arg_abi_vars[-1])
                de_tuple_instructions: list[Expr] = [
                    last_tuple_arg[i].store_into(tuple_abi_args[i])
                    for i in range(len(tuple_arg_type_specs))
                ]
                decode_instructions += de_tuple_instructions
                arg_abi_vars = arg_abi_vars[:-1] + tuple_abi_args

            # NOTE: does not have to have return, can be void method
            if handler.type_of() == "void":
                return Seq(
                    *decode_instructions,
                    cast(Expr, handler(*arg_abi_vars)),
                    Approve(),
                )
            else:
                output_temp: abi.BaseType = cast(
                    OutputKwArgInfo, handler.output_kwarg_info
                ).abi_type.new_instance()
                subroutine_call: abi.ReturnedValue = cast(
                    abi.ReturnedValue, handler(*arg_abi_vars)
                )
                return Seq(
                    *decode_instructions,
                    subroutine_call.store_into(output_temp),
                    abi.MethodReturn(output_temp),
                    Approve(),
                )

    def __append_to_ast(
        self,
        approval_conditions: list[Expr],
        clear_state_conditions: list[Expr],
        branch: Expr,
        ast_order_indicator: ConflictMapElem,
        method_obj: Optional[sdk_abi.Method] = None,
    ) -> None:
        """
        A helper function that appends conditions and execution of branches into AST.

        Args:
            approval_conditions: A list of expressions for approval program's condition on: creation?, method/bare, Or[OCs]
            clear_state_conditions: A list of expressions for clear-state program's condition on: method/bare
            branch: A branch of contract executing the registered method
            method_obj: SDK's Method objects to construct Contract JSON object
        """
        if len(approval_conditions) > 0:
            self.approval_if_then.append(
                ProgramNode(
                    And(*approval_conditions)
                    if len(approval_conditions) > 1
                    else approval_conditions[0],
                    branch,
                    method_obj,
                    ast_order_indicator,
                )
            )
        if len(clear_state_conditions) > 0:
            self.clear_state_if_then.append(
                ProgramNode(
                    And(*clear_state_conditions)
                    if len(clear_state_conditions) > 1
                    else clear_state_conditions[0],
                    branch,
                    method_obj,
                    ast_order_indicator,
                )
            )

    def __add_bare_call(
        self,
        bare_app_call: ABIReturnSubroutine | SubroutineFnWrapper | Expr,
        on_completes: EnumInt | list[EnumInt],
        *,
        creation: bool = False,
    ) -> None:
        """
        Registering a bare-app-call to the router.

        Args:
            bare_app_call: either an `ABIReturnSubroutine`, or `SubroutineFnWrapper`, or `Expr`.
                must take no arguments and evaluate to none (void).
            on_completes: a list of OnCompletion args
            creation: a boolean variable indicating if this condition is triggered on creation
        """
        oc_list: list[EnumInt] = (
            cast(list[EnumInt], on_completes)
            if isinstance(on_completes, list)
            else [cast(EnumInt, on_completes)]
        )
        approval_conds, clear_state_conds = Router.parse_conditions(
            method_to_register=None,
            on_completes=oc_list,
            creation=creation,
        )
        branch = Router.wrap_handler(False, bare_app_call)
        method_name: str
        match bare_app_call:
            case ABIReturnSubroutine():
                method_name = bare_app_call.method_signature()
            case SubroutineFnWrapper():
                method_name = bare_app_call.name()
            case Expr():
                method_name = str(bare_app_call)
            case _:
                raise TealInputError(
                    f"bare app call can only be one of three following cases: "
                    f"{ABIReturnSubroutine, SubroutineFnWrapper, Expr}"
                )

        ast_order_indicator = ConflictMapElem(False, method_name, creation)
        for oc in oc_list:
            self.conflict_detect_map.add_elem_to(oc.name, ast_order_indicator)
        self.__append_to_ast(
            approval_conds, clear_state_conds, branch, ast_order_indicator, None
        )

    def __add_method_handler(
        self,
        method_app_call: ABIReturnSubroutine,
        *,
        on_complete: EnumInt = OnComplete.NoOp,
        creation: bool = False,
    ) -> None:
        """
        Registering an ABI method call to the router.

        Args:
            method_app_call: an `ABIReturnSubroutine` that is registrable
            on_complete: an OnCompletion args
            creation: a boolean variable indicating if this condition is triggered on creation
        """
        oc_list: list[EnumInt] = [on_complete]
        method_signature = method_app_call.method_signature()

        approval_conds, clear_state_conds = Router.parse_conditions(
            method_to_register=method_app_call,
            on_completes=oc_list,
            creation=creation,
        )
        branch = Router.wrap_handler(True, method_app_call)
        ast_order_indicator = ConflictMapElem(
            True, method_app_call.method_signature(), creation
        )
        for oc in oc_list:
            self.conflict_detect_map.add_elem_to(oc.name, ast_order_indicator)
        self.__append_to_ast(
            approval_conds,
            clear_state_conds,
            branch,
            ast_order_indicator,
            sdk_abi.Method.from_signature(method_signature),
        )

    def abi_method(
        self, *, on_complete: EnumInt = OnComplete.NoOp, creation: bool = False
    ) -> Callable[[Callable | ABIReturnSubroutine], ABIReturnSubroutine]:
        """Decorator to register an ABI method call to router.

        Allowing following syntax:

        @router.abi_method(on_complete=OnComplete.OptIn, create=False)
        @router.abi_method(on_complete=OnComplete.NoOp, create=False)
        def echo(a: pt.abi.Uint64, *, output: pt.abi.Uint64) -> pt.Expr:
            ...

        Args:
            on_complete: an OnCompletion args
            creation: a boolean variable indicating if this condition is triggered on creation
        """

        def __method(impl: Callable | ABIReturnSubroutine) -> ABIReturnSubroutine:
            subroutine: ABIReturnSubroutine = (
                impl
                if isinstance(impl, ABIReturnSubroutine)
                else ABIReturnSubroutine(impl)
            )
            self.__add_method_handler(
                subroutine, on_complete=on_complete, creation=creation
            )
            return subroutine

        return __method

    @staticmethod
    def __ast_construct(
        ast_list: list[ProgramNode],
    ) -> Expr:
        """A helper function in constructing approval/clear-state programs.

        It takes a list of `ProgramNode`s, which contains conditions of entering a condition branch
        and the execution of the branch.

        It constructs the program's AST from the list of `ProgramNode`.

        Args:
            ast_list: a non-empty list of `ProgramNode`'s containing conditions of entering such branch
                and execution of the branch.
        Returns:
            program: the Cond AST of (approval/clear-state) program from the list of `ProgramNode`.
        """
        if len(ast_list) == 0:
            raise TealInputError("ABIRouter: Cannot build program with an empty AST")

        sorted(ast_list, key=lambda x: x.ast_order_indicator)

        program: Cond = Cond(*[[node.condition, node.branch] for node in ast_list])

        return program

    def contract_construct(self) -> dict[str, Any]:
        """A helper function in constructing contract JSON object.

        It takes out the method signatures from approval program `ProgramNode`'s,
        and constructs an `Contract` object.

        Returns:
            contract: a dictified `Contract` object constructed from
                approval program's method signatures and `self.name`.
        """
        method_collections = [
            node.method_info for node in self.approval_if_then if node.method_info
        ]
        return sdk_abi.Contract(self.name, method_collections).dictify()

    def build_program(self) -> tuple[Expr, Expr, dict[str, Any]]:
        """
        Constructs ASTs for approval and clear-state programs from the registered methods in the router,
        also generates a JSON object of contract to allow client read and call the methods easily.

        Returns:
            approval_program: AST for approval program
            clear_state_program: AST for clear-state program
            contract: JSON object of contract to allow client start off-chain call
        """
        return (
            Router.__ast_construct(self.approval_if_then),
            Router.__ast_construct(self.clear_state_if_then),
            self.contract_construct(),
        )

    def compile_program(
        self,
        *,
        version: int = DEFAULT_TEAL_VERSION,
        assembleConstants: bool = False,
        optimize: OptimizeOptions = None,
    ) -> tuple[str, str, dict[str, Any]]:
        """
        Combining `build_program` and `compileTeal`, compiles built Approval and ClearState programs
        and returns Contract JSON object for off-chain calling.

        Returns:
            approval_program: compiled approval program
            clear_state_program: compiled clear-state program
            contract: JSON object of contract to allow client start off-chain call
        """
        ap, csp, contract = self.build_program()
        ap_compiled = compileTeal(
            ap,
            Mode.Application,
            version=version,
            assembleConstants=assembleConstants,
            optimize=optimize,
        )
        csp_compiled = compileTeal(
            csp,
            Mode.Application,
            version=version,
            assembleConstants=assembleConstants,
            optimize=optimize,
        )
        return ap_compiled, csp_compiled, contract


Router.__module__ = "pyteal"
