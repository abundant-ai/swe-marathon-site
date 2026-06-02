"""Excel error sentinels."""
from __future__ import annotations


class XLError(Exception):
    """Raised internally to short-circuit evaluation with a #ERR sentinel."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def is_error(v) -> bool:
    return isinstance(v, str) and v.startswith("#") and v.endswith(("!", "?", "A"))


# Common sentinels
DIV0 = "#DIV/0!"
NAME = "#NAME?"
NA = "#N/A"
NUM = "#NUM!"
REF = "#REF!"
VALUE = "#VALUE!"
NULL = "#NULL!"
SPILL = "#SPILL!"
CIRC = "#CIRC!"
