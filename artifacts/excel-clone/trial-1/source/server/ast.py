"""AST node types for parsed formulas."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class Num:
    value: float | int

@dataclass
class Str:
    value: str

@dataclass
class Bool:
    value: bool

@dataclass
class Err:
    value: str

@dataclass
class Ref:
    sheet: Optional[str]
    col: int
    row: int
    col_abs: bool
    row_abs: bool

@dataclass
class RangeRef:
    sheet: Optional[str]
    c0: int
    r0: int
    c1: int
    r1: int
    c0_abs: bool
    r0_abs: bool
    c1_abs: bool
    r1_abs: bool

@dataclass
class Name:
    """Bare identifier — could be a defined-name reference."""
    name: str

@dataclass
class BinOp:
    op: str
    left: Any
    right: Any

@dataclass
class UnaryOp:
    op: str
    operand: Any

@dataclass
class PercentOp:
    operand: Any

@dataclass
class Call:
    name: str
    args: list

@dataclass
class CallExpr:
    """Apply an arbitrary expression as a function (lambda value)."""
    fn: Any
    args: list

@dataclass
class ArrayLit:
    """{1,2,3; 4,5,6} array literal."""
    rows: list  # list of lists of expr nodes
