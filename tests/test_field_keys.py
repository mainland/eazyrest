"""A field's JSON key and write key are decided once, at decoration.

A field's JSON key is its entry in ``field_map``, or else its name, and its
write key is its entry in ``write_map``, or else its JSON key. A declaration is
refused exactly when the keys that its fields would hold clash, for reads or
for writes, and the descriptors of one that is accepted hold those keys.
"""

from __future__ import annotations

from typing import Any

import pytest

from eazyrest import JSONObject, json_object

DECLARATIONS: list[tuple[dict[str, str], dict[str, str]]] = [
    ({}, {}),
    ({"a": "x"}, {}),
    ({"a": "b"}, {}),
    ({"a": "x"}, {"a": "y"}),
    ({}, {"a": "b"}),
    ({"a": "x"}, {"b": "x"}),
    ({"a": "b", "b": "a"}, {}),
    ({"a": "b", "b": "a"}, {"a": "a"}),
    ({"a": "x", "b": "y"}, {"a": "y", "b": "x"}),
]


def _expected(
    field_map: dict[str, str], write_map: dict[str, str]
) -> dict[str, tuple[str, str]]:
    """Return each field's keys, as the rule says."""
    keys = {}
    for field in ("id", "a", "b"):
        read = field_map.get(field, field)
        keys[field] = (read, write_map.get(field, read))
    return keys


def _declare(
    field_map: dict[str, str],
    write_map: dict[str, str],
    base: type[Any] = JSONObject,
) -> Any:
    @json_object(field_map=field_map, write_map=write_map)
    class Thing(base):  # type: ignore[misc]
        """A model with two fields beside its key."""

        id: int
        a: int
        b: int

    return Thing


@pytest.mark.parametrize(
    ("field_map", "write_map"),
    DECLARATIONS,
    ids=[f"{n}" for n in range(len(DECLARATIONS))],
)
def test_a_declaration_is_refused_exactly_when_its_keys_clash(
    field_map: dict[str, str], write_map: dict[str, str]
) -> None:
    """The descriptors never hold keys that the check would have refused."""
    keys = _expected(field_map, write_map)
    reads = [read for read, _ in keys.values()]
    writes = [write for _, write in keys.values()]
    if len(set(reads)) < 3 or len(set(writes)) < 3:
        with pytest.raises(ValueError, match="JSON key"):
            _declare(field_map, write_map)
        return
    thing = _declare(field_map, write_map)
    for name, (read, write) in keys.items():
        assert getattr(thing, name).json_field == read
        assert getattr(thing, name).write_field == write


@pytest.mark.parametrize(
    ("field_map", "write_map", "refused"),
    [
        ({}, {}, False),
        ({"c": "z"}, {}, False),
        ({}, {"c": "z"}, False),
        ({"c": "x"}, {}, True),
        ({"c": "z"}, {"c": "y"}, True),
        ({"c": "y"}, {}, True),
    ],
    ids=[
        "none",
        "renamed",
        "written",
        "read clash",
        "write clash",
        "write follows read",
    ],
)
def test_a_subclass_is_checked_against_the_keys_it_inherits(
    field_map: dict[str, str], write_map: dict[str, str], refused: bool
) -> None:
    """A field that the subclass adds clashes with an inherited key."""
    parent: Any = _declare({"a": "x"}, {"a": "y"})

    def derive() -> Any:
        @json_object(field_map=field_map, write_map=write_map)
        class Child(parent):  # type: ignore[misc]
            """A model that adds a field."""

            c: int

        return Child

    if refused:
        with pytest.raises(ValueError, match="JSON key"):
            derive()
        return
    child = derive()
    read = field_map.get("c", "c")
    assert child.a.json_field == "x"
    assert child.a.write_field == "y"
    assert child.c.json_field == read
    assert child.c.write_field == write_map.get("c", read)
