from abc import abstractmethod
from typing import List, Final, cast
from pyteal.ast.abi.type import BaseType, TypeSpec
from pyteal.ast.abi.uint import Uint, UintTypeSpec, uint_decode, uint_encode
from pyteal.ast.expr import Expr
from pyteal.ast.txn import Txn
from pyteal.errors import TealInputError
from pyteal.types import TealType
#
#
#class ReferenceTypeSpec(TypeSpec):
#    def __init__(self) -> None:
#        super().__init__()
#
#    @abstractmethod
#    def new_instance(self) -> "ReferenceType":
#        pass
#
#    @abstractmethod
#    def annotation_type(self) -> "type[ReferenceType]":
#        pass
#
#    def is_dynamic(self) -> bool:
#        return False
#
#    def byte_length_static(self) -> int:
#        raise TealInputError("Reference Types are not dynamic")
#
#    def storage_type(self) -> TealType:
#        return TealType.uint64 
#
#    def __eq__(self, other: object) -> bool:
#        return type(self) is type(other)
#
#
#ReferenceTypeSpec.__module__ = "pyteal"
#
#class ReferenceType(BaseType):
#
#    @abstractmethod
#    def __init__(self, spec: ReferenceTypeSpec) -> None:
#        super().__init__(spec)
#
#    def type_spec(self) -> ReferenceTypeSpec:
#        return cast(ReferenceTypeSpec, super().type_spec())
#
#    @abstractmethod
#    def get(self) -> Expr:
#        pass 
#
#    def set(self: T, value: Union[int, Expr, "Uint", ComputedValue[T]]) -> Expr:
#        if isinstance(value, ComputedValue):
#            return self._set_with_computed_type(value)
#
#        if isinstance(value, BaseType) and not (
#            isinstance(value.type_spec(), UintTypeSpec)
#            and self.type_spec().bit_size()
#            == cast(UintTypeSpec, value.type_spec()).bit_size()
#        ):
#            raise TealInputError(
#                "Type {} is not assignable to type {}".format(
#                    value.type_spec(), self.type_spec()
#                )
#            )
#        return uint_set(self.type_spec().bit_size(), self.stored_value, value)
#
#    def decode(
#        self,
#        encoded: Expr,
#        *,
#        startIndex: Expr = None,
#        endIndex: Expr = None,
#        length: Expr = None,
#    ) -> Expr:
#        return uint_decode(
#            8,
#            self.stored_value,
#            encoded,
#            startIndex,
#            endIndex,
#            length,
#        )
#
#    def encode(self) -> Expr:
#        return uint_encode(8, self.stored_value)
#


class AccountTypeSpec(UintTypeSpec):
    def __init__(self):
        super().__init__(8)

    def new_instance(self) -> "Account":
        return Account()

    def annotation_type(self) -> "type[Account]":
        return Account

    def __str__(self) -> str:
        return "account"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, AccountTypeSpec)


AccountTypeSpec.__module__ = "pyteal"


class Account(Uint):
    def __init__(self) -> None:
        super().__init__(AccountTypeSpec())

    def get(self) -> Expr:
        return Txn.accounts[self.index()]

    def index(self) -> Expr:
        return self.stored_value.load()


Account.__module__ = "pyteal"


class AssetTypeSpec(UintTypeSpec):
    def __init__(self):
        super().__init__(8)

    def new_instance(self) -> "Asset":
        return Asset()

    def annotation_type(self) -> "type[Asset]":
        return Asset

    def __str__(self) -> str:
        return "asset"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, AssetTypeSpec)


AssetTypeSpec.__module__ = "pyteal"


class Asset(Uint):
    def __init__(self) -> None:
        super().__init__(AssetTypeSpec())

    def get(self) -> Expr:
        return Txn.assets[self.index()]

    def index(self) -> Expr:
        return self.stored_value.load()


Asset.__module__ = "pyteal"


class ApplicationTypeSpec(UintTypeSpec):
    def __init__(self):
        super().__init__(8)

    def new_instance(self) -> "Application":
        return Application()

    def annotation_type(self) -> "type[Application]":
        return Application

    def __str__(self) -> str:
        return "application"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ApplicationTypeSpec)


ApplicationTypeSpec.__module__ = "pyteal"


class Application(Uint):
    def __init__(self) -> None:
        super().__init__(ApplicationTypeSpec())

    def get(self) -> Expr:
        return Txn.applications[self.index()]

    def index(self) -> Expr:
        return self.stored_value.load()


Application.__module__ = "pyteal"


ReferenceTypeSpecs: Final[List[TypeSpec]] = [
    AccountTypeSpec(),
    AssetTypeSpec(),
    ApplicationTypeSpec(),
]
