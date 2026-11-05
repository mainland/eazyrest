"""Tests for code examples in the documentation."""

from __future__ import annotations

import datetime
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

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


def test_enum_fallback_example_maps_unknown_values() -> None:
    """The ``_missing_()`` example maps values the enum lacks."""
    example = run_guide_example("def _missing_")
    status = example["TicketStatus"]

    assert status("open") is status.OPEN
    assert status("archived") is status.UNKNOWN


def test_pydantic_example_converts_fields(requests_mock: Any) -> None:
    """The Pydantic example reads, writes, and saves changes in place."""
    pytest.importorskip("pydantic")
    example = run_guide_example("class Logo")
    logo_type = example["Logo"]
    logo_type.register_api(API("https://api.example.com/"))
    patch = requests_mock.patch(
        "https://api.example.com/logos/1/", status_code=204
    )
    logo = logo_type(
        json={
            "id": 1,
            "image": "aGVsbG8=",
            "rendering": {
                "width": 640,
                "height": 480,
                "timeout": "0 00:01:30",
                "dpi": 300,
            },
            "anchors": [{"x": 0, "y": 0, "label": "a"}],
            "shape": "round",
            "published": 1711972800,
        }
    )

    assert logo.image == b"hello"
    assert logo.rendering["timeout"] == datetime.timedelta(seconds=90)
    assert logo.rendering["dpi"] == 300
    assert logo.published == datetime.datetime(
        2024, 4, 1, 12, tzinfo=datetime.timezone.utc
    )

    logo.anchors[0].x = 2
    logo.image = b"bye"
    logo.save()

    assert patch.last_request.json() == {
        "image": "Ynll",
        "anchors": [{"x": 2.0, "y": 0.0, "label": "a"}],
    }


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


README = DOCS.parent / "README.md"
QUICKSTART = DOCS / "quickstart.md"

_DEMO = "https://jsonplaceholder.typicode.com"
_DEMO_USER = {
    "id": 1,
    "name": "Leanne Graham",
    "username": "Bret",
    "email": "leanne@example.com",
    "address": {},
    "phone": "1-770-736-8031",
    "website": "example.com",
    "company": {},
}
_DEMO_TODOS = [
    {"userId": 1, "id": 1, "title": "delectus aut autem", "completed": False},
    {"userId": 1, "id": 2, "title": "quis ut est", "completed": False},
]


def _mock_demo_api(requests_mock: Any) -> Any:
    """Serve the JSONPlaceholder requests that the examples send.

    Args:
        requests_mock: The ``requests_mock`` fixture.

    Returns:
        The mocked ``PATCH`` endpoint of the first todo.
    """
    requests_mock.get(f"{_DEMO}/todos/1/", json=_DEMO_TODOS[0])
    requests_mock.get(f"{_DEMO}/users/1/", json=_DEMO_USER)
    requests_mock.get(f"{_DEMO}/todos/?userId=1", json=_DEMO_TODOS)
    return requests_mock.patch(
        f"{_DEMO}/todos/1/", json={**_DEMO_TODOS[0], "completed": True}
    )


@pytest.mark.parametrize(
    ("doc", "marker"),
    [(README, "with API("), (QUICKSTART, "with API(")],
)
def test_front_page_examples_run(
    requests_mock: Any,
    capsys: pytest.CaptureFixture[str],
    doc: Path,
    marker: str,
) -> None:
    """The README and quickstart examples run against the demo API's shape.

    Args:
        doc: Document that holds the example.
        marker: Text that identifies the example's block.
    """
    patch = _mock_demo_api(requests_mock)

    run_guide_example(marker, doc=doc)

    output = capsys.readouterr().out.splitlines()
    assert output[0].startswith("delectus aut autem")
    assert "Leanne Graham" in output[1]
    assert "2" in output
    assert patch.call_count == 1
    assert patch.last_request.json() == {"completed": True}
