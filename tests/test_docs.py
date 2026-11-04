"""Tests for code examples in the documentation."""

from __future__ import annotations

import datetime
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from eazyrest import API, BaseType, JSONObject, json_object

DOCS = Path(__file__).resolve().parents[1] / "docs"
GUIDE = DOCS / "guide.md"
DRF = DOCS / "drf.md"

_PYTHON_BLOCK_RE = re.compile(
    r"^```python\n(.*?)^```$", re.MULTILINE | re.DOTALL
)


def run_guide_example(
    marker: str, *, doc: Path = GUIDE, **names: Any
) -> dict[str, Any]:
    """Run the one Python block in a document that contains ``marker``.

    Args:
        marker: Text that identifies the block, such as a class statement.
        doc: Markdown document that holds the block.
        **names: Names that the block uses but an earlier block defines.

    Returns:
        The global namespace of the block after it runs.
    """
    blocks = [
        block
        for block in _PYTHON_BLOCK_RE.findall(doc.read_text())
        if marker in block
    ]
    assert len(blocks) == 1, f"expected one block with {marker!r}"

    # Do not inherit this module's __future__ imports, so the block runs as it
    # would in a reader's module. The code is this repository's own guide.
    code = compile(blocks[0], str(doc), "exec", dont_inherit=True)
    namespace: dict[str, Any] = {"__name__": "guide_example", **names}
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


def test_timestamp_example_encodes_datetimes_as_timestamps() -> None:
    """The ``TimestampObject`` override encodes only datetimes."""
    example = run_guide_example("class TimestampObject")
    timestamp_object = example["TimestampObject"]
    when = datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc)

    encoded = timestamp_object.to_json(when, BaseType(datetime.datetime))
    assert encoded == when.timestamp()
    assert timestamp_object.to_json("text", BaseType(str)) == "text"


def test_drf_bulk_example_uses_the_query_api(requests_mock: Any) -> None:
    """The DRF bulk lookup loads users through the API it is given."""

    class DRFObject(JSONObject):
        """Base model registered with an API other than the override."""

    DRFObject.register_api(API("https://registered.example.com/"))
    example = run_guide_example(
        "cls.filter(id=sorted(pks)",
        doc=DRF,
        json_object=json_object,
        DRFObject=DRFObject,
    )
    override = API("https://override.example.com/")
    lookup = requests_mock.get(
        "https://override.example.com/users/", json=[{"id": 2, "name": "Ada"}]
    )

    users = example["User"].bulk_get_by_pks({2}, api=override)

    assert lookup.last_request.qs == {"id": ["2"]}
    assert users[2].name == "Ada"
    assert users[2].api is override
