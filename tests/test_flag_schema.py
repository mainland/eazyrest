"""The schema of a ``Flag`` field accepts the combinations that it writes."""

from __future__ import annotations

import enum
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from eazyrest import JSONObject, json_object


class Perm(enum.Flag):
    """A flag whose members are bits."""

    READ = 1
    WRITE = 2


class Mode(enum.IntFlag):
    """An integer flag."""

    READ = 1
    WRITE = 2


@json_object
class File(JSONObject):
    """A model with flag fields."""

    class_url = "/files/"
    id: int
    perm: Perm
    mode: Mode


@pytest.mark.parametrize("mode", ["read", "write"])
@pytest.mark.parametrize("field", ["perm", "mode"])
def test_a_flag_field_has_an_integer_schema(mode: Any, field: str) -> None:
    """The schema is an integer, not the values of the single members."""
    schema = File.json_schema(mode)
    assert schema["properties"][field] == {"type": "integer"}
    validator = Draft202012Validator(schema)
    body = {"id": 1, "perm": 3, "mode": 3}
    assert validator.is_valid(body)


def test_a_flag_field_writes_and_reads_a_combination() -> None:
    """The combination that the schema accepts is what the field exchanges."""
    obj = File(json={"id": 1, "perm": 1, "mode": 1})
    obj.perm = Perm.READ | Perm.WRITE
    assert obj._pending_updates == {"perm": 3}
    assert File(json={"id": 1, "perm": 3, "mode": 3}).perm == (
        Perm.READ | Perm.WRITE
    )
