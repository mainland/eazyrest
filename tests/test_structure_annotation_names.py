"""A string in a structure's member names a type the same way in every form.

A string in the annotation of a dataclass, a ``TypedDict``, or a ``NamedTuple``
member names a type of the structure's module, or the structure itself, whether
the structure is declared in a module or inside a function.
"""

import dataclasses
import datetime
import sys
import types
from collections.abc import Callable
from typing import Any, NamedTuple

import pytest
from typing_extensions import TypedDict

from eazyrest import (
    API,
    FieldConversionError,
    JSONObject,
    encode_json,
    json_object,
)

api = API("https://structure-names.example/")


@dataclasses.dataclass
class ModuleData:
    """A dataclass that names itself."""

    name: str
    children: "list[ModuleData]"


class ModuleDict(TypedDict):
    """A ``TypedDict`` that names itself."""

    name: str
    children: "list[ModuleDict]"


class ModuleTuple(NamedTuple):
    """A ``NamedTuple`` that names itself."""

    name: str
    children: "list[ModuleTuple]"


class Dated(NamedTuple):
    """A ``NamedTuple`` with a field named like the type that it holds."""

    datetime: "datetime.date"


def local_data() -> type[Any]:
    """Return a dataclass that names itself, declared in a function."""

    @dataclasses.dataclass
    class LocalData:
        """A dataclass that names itself."""

        name: str
        children: "list[LocalData]"

    return LocalData


def local_dict() -> type[Any]:
    """Return a ``TypedDict`` that names itself, declared in a function."""

    class LocalDict(TypedDict):
        """A ``TypedDict`` that names itself."""

        name: str
        children: "list[LocalDict]"  # type: ignore[misc]

    return LocalDict


def local_tuple() -> type[Any]:
    """Return a ``NamedTuple`` that names itself, declared in a function."""

    class LocalTuple(NamedTuple):
        """A ``NamedTuple`` that names itself."""

        name: str
        children: "list[LocalTuple]"  # type: ignore[misc]

    return LocalTuple


def local_derived_tuple() -> type[Any]:
    """Return a subclass of a local ``NamedTuple`` that names itself."""

    class Base(NamedTuple):
        """A ``NamedTuple`` that names itself."""

        name: str
        children: "list[Base]"  # type: ignore[misc]

    class Derived(Base):
        """A subclass that annotates no field."""

    return Derived


STRUCTURES: dict[str, Callable[[], type[Any]]] = {
    "module-dataclass": lambda: ModuleData,
    "module-typeddict": lambda: ModuleDict,
    "module-namedtuple": lambda: ModuleTuple,
    "local-dataclass": local_data,
    "local-typeddict": local_dict,
    "local-namedtuple": local_tuple,
    "local-derived-namedtuple": local_derived_tuple,
}


def json_of(structure: type[Any]) -> Any:
    """Return JSON for a structure with one child, in its JSON form."""
    if issubclass(structure, tuple):
        return ["a", [["b", []]]]
    leaf = {"name": "b", "children": []}
    return {"name": "a", "children": [leaf]}


def holder_of(structure: type[Any]) -> Any:
    """Return a model with a field of ``structure``."""

    @json_object
    class Tree(JSONObject):
        """A model with a field that is a structure."""

        class_url = "/trees/"
        id: int
        root: structure  # type: ignore[valid-type]

    Tree.register_api(api)
    return Tree


def children_of(root: Any) -> Any:
    """Return the children of a read structure."""
    return (
        root[1]
        if isinstance(root, tuple)
        else root["children"]
        if isinstance(root, dict)
        else root.children
    )


@pytest.mark.parametrize("name", STRUCTURES)
def test_a_structure_that_names_itself_reads_and_writes(name: str) -> None:
    """A structure reads, and writes as the JSON it read, in every form."""
    structure = STRUCTURES[name]()
    model = holder_of(structure)
    expected = json_of(structure)
    tree = model(json={"id": 1, "root": expected})
    assert len(children_of(tree.root)) == 1
    assert encode_json(tree.root, structure) == expected


@pytest.mark.parametrize("name", STRUCTURES)
def test_a_value_of_the_wrong_shape_is_refused_for_each_form(
    name: str,
) -> None:
    """A structure refuses a number, in every form."""
    structure = STRUCTURES[name]()
    tree = holder_of(structure)(json={"id": 1, "root": 5})
    with pytest.raises(FieldConversionError):
        _ = tree.root


def test_a_field_named_like_a_module_does_not_hide_the_module() -> None:
    """A field does not shadow the module that another annotation names."""
    tree = holder_of(Dated)(json={"id": 1, "root": ["2024-01-02"]})
    assert tree.root.datetime == datetime.date(2024, 1, 2)


def test_a_member_that_names_a_type_of_an_inherited_module() -> None:
    """An inherited member names a type of the module that declares it."""
    module = types.ModuleType("structure_names_base")
    exec(  # noqa: S102
        "import datetime\n"
        "from typing_extensions import TypedDict\n"
        "Stamp = datetime.date\n"
        "class Base(TypedDict):\n"
        "    when: 'Stamp'\n",
        module.__dict__,
    )
    sys.modules["structure_names_base"] = module
    try:

        class Derived(module.Base):  # type: ignore[misc,name-defined]
            """A ``TypedDict`` that inherits a member from another module."""

            extra: int

        tree = holder_of(Derived)(
            json={"id": 1, "root": {"when": "2024-01-02", "extra": 1}}
        )
        assert str(tree.root["when"]) == "2024-01-02"
    finally:
        del sys.modules["structure_names_base"]
