"""Tests for code examples in the documentation."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from eazyrest import BaseType

GUIDE = Path(__file__).resolve().parents[1] / "docs" / "guide.md"

_PYTHON_BLOCK_RE = re.compile(
    r"^```python\n(.*?)^```$", re.MULTILINE | re.DOTALL
)


def run_guide_example(marker: str) -> dict[str, Any]:
    """Run the one Python block in the guide that contains ``marker``.

    Args:
        marker: Text that identifies the block, such as a class statement.

    Returns:
        The global namespace of the block after it runs.
    """
    blocks = [
        block
        for block in _PYTHON_BLOCK_RE.findall(GUIDE.read_text())
        if marker in block
    ]
    assert len(blocks) == 1, f"expected one block with {marker!r}"

    # Do not inherit this module's __future__ imports, so the block runs as
    # it would in a reader's module. The code is this repository's own guide.
    code = compile(blocks[0], str(GUIDE), "exec", dont_inherit=True)
    namespace: dict[str, Any] = {"__name__": "guide_example"}
    exec(code, namespace)  # noqa: S102
    return namespace


def test_decimal_example_converts_only_decimal_fields() -> None:
    """The ``Decimal`` override leaves other scalar fields alone."""
    example = run_guide_example("class APIObject")
    invoice = example["Invoice"](
        json={"id": 1, "total": 1.10, "line_totals": [1, "2.5"]}
    )

    assert invoice.total == Decimal("1.1")
    assert invoice.line_totals == [Decimal(1), Decimal("2.5")]
    assert invoice.from_json(False, BaseType(bool)) is False
    total = invoice.to_json(Decimal("1.1"), BaseType(Decimal))
    assert total == "1.1"
    assert invoice.to_json(5, BaseType(int)) == 5
