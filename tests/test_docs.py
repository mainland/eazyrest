"""Tests for code examples in the documentation."""

from __future__ import annotations

import datetime
import re
from ipaddress import IPv4Address
from pathlib import Path
from typing import Annotated, Any

import pytest

from eazyrest import (
    API,
    APIError,
    DoesNotExist,
    Hyperlinked,
    JSONObject,
    json_object,
)

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


def test_address_example_converts_only_address_fields() -> None:
    """The ``IPv4Address`` conversion converts addresses, in lists too."""
    example = run_guide_example("class APIObject")
    host = example["Host"](
        json={"id": 1, "address": "10.0.0.1", "allowed": ["10.0.0.2"]}
    )

    assert host.address == IPv4Address("10.0.0.1")
    assert host.allowed == [IPv4Address("10.0.0.2")]
    host.address = IPv4Address("10.0.0.3")
    host.allowed = [IPv4Address("10.0.0.4")]
    assert host.json["address"] == "10.0.0.3"
    assert host.json["allowed"] == ["10.0.0.4"]


def test_timestamp_example_encodes_datetimes_as_timestamps() -> None:
    """The ``TimestampObject`` conversion writes datetimes as timestamps."""
    base: Any = run_guide_example("class TimestampObject")["TimestampObject"]

    @json_object
    class Event(base):  # type: ignore[misc]
        class_url = "/events/"

        id: int
        when: datetime.datetime
        note: str

    when = datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc)
    event = Event(json={"id": 1, "when": when.timestamp(), "note": "a"})

    assert event.when == when
    event.when = when
    assert event.json["when"] == when.timestamp()
    assert event.note == "a"


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


class _RecipeBase(JSONObject):
    """Base model for the DRF notes' bulk lookup recipes."""


_RecipeBase.register_api(API("https://api.example.com/"))
_BulkByPks = run_guide_example(
    "cls.filter(id=sorted(pks)",
    doc=DRF,
    json_object=json_object,
    DRFObject=_RecipeBase,
)["User"]


@json_object
class _LinkedUser(_BulkByPks):  # type: ignore[misc,valid-type]
    """User with the notes' hook for hyperlinked relations."""

    bulk_get_by_urls = run_guide_example(
        "def bulk_get_by_urls(cls, urls, *, api, **kwargs):", doc=DRF
    )["bulk_get_by_urls"]


@json_object
class _OwnedTodo(_RecipeBase):
    """Todo whose owner is a hyperlink."""

    class_url = "/todos/"

    id: int
    owner: Annotated[_LinkedUser, Hyperlinked()]


def test_drf_hyperlink_recipe_skips_urls_outside_class_url(
    requests_mock: Any,
) -> None:
    """URLs outside class_url prefetch nothing and load lazily."""
    requests_mock.get(
        "https://api.example.com/todos/",
        json=[{"id": 1, "owner": "https://api.example.com/members/7/"}],
    )
    requests_mock.get(
        "https://api.example.com/members/7/", json={"id": 7, "name": "Ada"}
    )

    todos = list(_OwnedTodo.filter(prefetch="owner"))

    assert todos[0].owner.name == "Ada"
    assert [r.path for r in requests_mock.request_history] == [
        "/todos/",
        "/members/7/",
    ]


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


def test_merge_response_example_ignores_status_bodies(
    requests_mock: Any,
) -> None:
    """The ``merge_response()`` example keeps the sent fields."""
    example = run_guide_example("class StatusObject")
    base: type[JSONObject] = example["StatusObject"]

    @json_object
    class Ticket(base):  # type: ignore[valid-type,misc]
        class_url = "/tickets/"

        id: int
        title: str

    Ticket.register_api(API("https://api.example.com/"))
    requests_mock.patch(
        "https://api.example.com/tickets/1/", json={"status": "ok"}
    )
    ticket = Ticket(json={"id": 1, "title": "old"})

    ticket.title = "new"
    ticket.save()

    assert ticket.json == {"id": 1, "title": "new"}


@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        (400, {"email": ["This field must be unique."]}, "DRFAPIError"),
        (404, {"detail": "Not found."}, "DRFNotFound"),
    ],
)
def test_drf_error_example_keeps_eazyrest_exceptions(
    requests_mock: Any, status: int, body: dict[str, Any], kind: str
) -> None:
    """The DRF error example raises its exceptions as eazyrest's subclasses.

    Args:
        status: Status of the error response.
        body: DRF error response body.
        kind: Name of the example's exception that the response raises.
    """
    example = run_guide_example("class DRFAPIError", doc=DRF)
    api = example["DRFAPI"]("https://api.example.com/")
    requests_mock.get(
        "https://api.example.com/users/9/", status_code=status, json=body
    )

    with pytest.raises(example[kind]) as info:
        api.get("/users/9/")

    error: Any = info.value
    assert isinstance(error, APIError)
    assert isinstance(error, DoesNotExist) is (status == 404)
    assert error.data == body
    assert error.response.status_code == status
    # The message is eazyrest's, which does not quote the body.
    assert "unique" not in str(error)
    assert "Not found." not in str(error)
    if status == 404:
        assert info.value.detail == "Not found."
        assert info.value.field_errors == {}
    else:
        assert info.value.detail is None
        assert info.value.field_errors == body


def test_fields_never_sent_reports_only_fields_that_no_object_has() -> None:
    """The drift recipe reports a field only when every object lacks it."""
    fields_never_sent = run_guide_example("def fields_never_sent")[
        "fields_never_sent"
    ]

    @json_object
    class Machine(JSONObject):
        class_url = "/machines/"

        id: int
        state: str
        deployed: bool

    objects = [
        Machine(json={"id": 1, "state": "ready"}),
        Machine(json={"id": 2}),
    ]

    assert fields_never_sent(Machine, objects) == {"deployed"}
