"""Cell model: input + parsed formula + evaluated value."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Cell:
    sheet: str
    ref: str  # canonical A1 (no $)

    # user input — string or None for empty
    raw_input: Optional[str] = None

    # if raw_input begins with `=`, parsed AST — None on parse failure
    formula: Any = None
    is_formula: bool = False

    # last evaluated value: number / str / bool / error sentinel / None
    value: Any = None

    # spill: if this anchor produced a 2D array, store it; ghosts reference anchor
    spill_array: Any = None  # 2D list of values for the anchor
    spill_anchor: Optional[tuple] = None  # (sheet, ref) for ghosts only

    # presentation
    format: Optional[str] = None
    style: Optional[dict] = None

    def is_empty(self) -> bool:
        return self.raw_input in (None, "") and self.spill_anchor is None

    def display_value(self):
        """Value used for downstream formula references."""
        return self.value
