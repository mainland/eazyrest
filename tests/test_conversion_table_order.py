"""A subclass's entry for a type replaces its base's entry for that type."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from eazyrest import Conversion, JSONObject, json_object


class Tag:
    """A class of the program that models convert in their own ways."""

    def __init__(self, text: str) -> None:
        """Hold the text."""
        self.text = text


def conversion(prefix: str) -> Conversion:
    """Return a conversion that reads and writes text with a prefix."""
    return Conversion(
        decode=lambda value, cls: cls(prefix + value),
        encode=lambda value, cls: value.text.removeprefix(prefix),
    )


class Base(JSONObject):
    """A base whose entry for ``Tag`` reads it with ``base:``."""

    conversions: ClassVar[Mapping[Any, Conversion]] = {
        Tag: conversion("base:")
    }


@json_object
class First(Base):
    """A model that keeps its base's entry."""

    class_url = "/firsts/"
    id: int
    tag: Tag


@json_object
class Second(Base):
    """A model that replaces its base's entry for ``Tag``."""

    conversions: ClassVar[Mapping[Any, Conversion]] = {
        Tag: conversion("second:")
    }

    class_url = "/seconds/"
    id: int
    tag: Tag


def test_a_subclass_reads_by_its_own_entry() -> None:
    """The nearest entry in the order of bases decides."""
    assert First(json={"id": 1, "tag": "a"}).tag.text == "base:a"
    assert Second(json={"id": 1, "tag": "a"}).tag.text == "second:a"


def test_a_subclass_writes_by_its_own_entry() -> None:
    """The write and the read use the same entry."""
    second = Second(json={"id": 1, "tag": "a"})
    second.tag = Tag("second:b")
    assert second.json["tag"] == "b"
