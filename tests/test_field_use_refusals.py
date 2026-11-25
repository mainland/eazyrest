"""A field whose type cannot be used yet breaks nothing but itself.

A field's type may be admitted when its class is decorated and still be
unusable when the field is first used, because it names something that is not
defined yet or something eazyrest refuses only then. Reading the field raises
an error of the field, and every other field of the object still reads and
writes. Each kind of failure is crossed with each entry point that can reach
the field, so that a new entry point that skips the one access point fails
here.
"""

import collections
import dataclasses
import types
from collections.abc import Callable, Collection
from typing import Any, NamedTuple

import pytest
from typing_extensions import TypeAliasType, TypedDict

from eazyrest import (
    API,
    Conversion,
    FieldConversionError,
    JSONObject,
    json_object,
)

BASE = "https://field-use.example.com/"
_LATER_MODULE = types.ModuleType("_field_use_later_module")

_OTHERS = {"first": "a", "second": "b", "third": 1}
"""The values of the fields that work, in the JSON that the server sends."""


@json_object
class Friend(JSONObject):
    """Related model that works, so that a prefetch has a relation to load."""

    class_url = "/friends/"

    id: int
    name: str

    @classmethod
    def bulk_get_by_pks(
        cls, pks: Any, *, api: Any = None, **kwargs: Any
    ) -> dict[Any, "Friend"]:
        """Return friends without a request.

        Args:
            pks: Keys of the friends.
            api: Unused.
            **kwargs: Unused.

        Returns:
            One friend for each key, by key.
        """
        return {pk: cls(json={"id": pk, "name": "f"}) for pk in pks}


class _Closed(TypedDict, closed=True):
    """``TypedDict`` that eazyrest does not convert yet."""

    a: int


@dataclasses.dataclass(frozen=True)
class Kind:
    """One way in which a field's type cannot be used yet.

    Attributes:
        error: The class of the error that using the field raises.
        fixable: Whether defining a name makes the field work.
    """

    error: type[Exception]
    fixable: bool


KINDS = {
    "undefined_name": Kind(NameError, True),
    "related_key_name": Kind(NameError, True),
    "related_key_attribute": Kind(AttributeError, True),
    "dotted_attribute": Kind(FieldConversionError, True),
    "member_for_type_checkers": Kind(FieldConversionError, True),
    "alias_string": Kind(FieldConversionError, True),
    "closed_typed_dict": Kind(FieldConversionError, False),
    "collection_of_relations": Kind(FieldConversionError, False),
    "refused_member": Kind(FieldConversionError, False),
}


class Scenario:
    """A model with one field that cannot be used yet, and its fixtures.

    Attributes:
        kind: The kind of failure.
        model: The model, with the fields ``first``, ``second``, and ``third``,
            the relation ``friend``, and the field ``bad``.
        bad_json: What the server sends for ``bad``.
        other_json: Another thing that the server sends for ``bad``.
        assigned: A value to assign to ``bad``.
        fixed: What ``bad`` reads as once the name is defined.
    """

    def __init__(
        self, kind: str, monkeypatch: pytest.MonkeyPatch, session: Any
    ) -> None:
        """Build the model, and decorate it before the name is defined.

        Args:
            kind: Key of ``KINDS``.
            monkeypatch: Defines the names and the model's name for pickling.
            session: Mock of the transport.
        """
        self.kind = KINDS[kind]
        self.name = kind
        self._monkeypatch = monkeypatch
        self.session = session
        self.bad_json: Any = 5
        self.other_json: Any = 6
        self.assigned: Any = 7
        self._define: Callable[[], None] = lambda: None
        self._after_decoration: Callable[[], None] = lambda: None
        self.decode: Callable[[Any], Any] = lambda value: value
        hint = getattr(self, f"_{kind}")()
        self.model = self._decorate(hint)
        self._after_decoration()

    def _decorate(self, hint: Any) -> Any:
        """Decorate the model that holds the field.

        Args:
            hint: Annotation of ``bad``.

        Returns:
            The decorated class, with an API registered, which pickle finds by
            its name in this module.
        """
        name = f"Thing_{self.name}"
        model: Any = json_object(
            type(
                name,
                (JSONObject,),
                {
                    "__module__": __name__,
                    "class_url": "/things/",
                    "__annotations__": {
                        "id": int,
                        "first": str,
                        "second": str,
                        "third": int,
                        "bad": hint,
                        "friend": Friend,
                    },
                },
            )
        )
        api = API(BASE)
        model.register_api(api)
        Friend.register_api(api)
        self._monkeypatch.setitem(globals(), name, model)
        return model

    def _name(self, name: str) -> None:
        """Arrange for a global name to be defined later.

        Args:
            name: The name, which the module does not have yet.
        """
        self._monkeypatch.delitem(globals(), name, raising=False)
        self._define = lambda: self._monkeypatch.setitem(globals(), name, int)

    def _undefined_name(self) -> Any:
        self._name("_UndefinedName")
        return "_UndefinedName"

    def _keyed(self, annotation: str) -> Any:
        """Build a model whose key names something that is not defined yet.

        Args:
            annotation: The key's annotation.

        Returns:
            The model, whose objects cannot be built yet.
        """
        keyed: Any = json_object(
            type(
                f"Keyed_{self.name}",
                (JSONObject,),
                {
                    "__module__": __name__,
                    "class_url": "/keyed/",
                    "bulk_get_by_pks": classmethod(
                        lambda cls, pks, **kwargs: {
                            pk: cls(id=pk) for pk in pks
                        }
                    ),
                    "__annotations__": {"id": annotation},
                },
            )
        )
        self.decode = lambda value: keyed(id=value)
        return keyed

    def _related_key_name(self) -> Any:
        self._name("_RelatedKeyName")
        return self._keyed("_RelatedKeyName")

    def _related_key_attribute(self) -> Any:
        self._monkeypatch.delattr(_LATER_MODULE, "RelatedKey", raising=False)
        self._monkeypatch.setitem(
            globals(), "_field_use_later_module", _LATER_MODULE
        )
        self._define = lambda: self._monkeypatch.setattr(
            _LATER_MODULE, "RelatedKey", int, raising=False
        )
        return self._keyed("_field_use_later_module.RelatedKey")

    def _dotted_attribute(self) -> Any:
        self._monkeypatch.delattr(_LATER_MODULE, "Thing", raising=False)
        self._monkeypatch.setitem(
            globals(), "_field_use_later_module", _LATER_MODULE
        )
        self._define = lambda: self._monkeypatch.setattr(
            _LATER_MODULE, "Thing", int, raising=False
        )
        return "_field_use_later_module.Thing"

    def _member_for_type_checkers(self) -> Any:
        self._name("_ForTypeCheckers")
        member = dataclasses.make_dataclass(
            "Member", [("inner", "_ForTypeCheckers")]
        )
        member.__module__ = __name__
        self.bad_json = {"inner": 5}
        self.other_json = {"inner": 6}
        self.assigned = member(inner=7)
        self.decode = lambda value: member(**value)
        return member

    def _alias_string(self) -> Any:
        self._name("_AliasMember")
        self.bad_json = [5]
        self.other_json = [6]
        self.assigned = [7]
        return TypeAliasType("LaterAlias", "list[_AliasMember]")  # noqa: F821

    def _collection_of_relations(self) -> Any:
        """Declare a collection that is refused once its model is defined.

        Decoration cannot tell that the items are relations while the name is
        undefined, so the refusal waits for the field's first use.
        """
        name = "_LateRelated"
        self._monkeypatch.delitem(globals(), name, raising=False)
        self._after_decoration = lambda: self._monkeypatch.setitem(
            globals(), name, Friend
        )
        self.bad_json = [5]
        self.other_json = [6]
        self.assigned = [Friend(id=7)]
        return Collection["_LateRelated"]  # type: ignore[name-defined]  # noqa: F821

    def _closed_typed_dict(self) -> Any:
        self.bad_json = {"a": 5}
        self.other_json = {"a": 6}
        self.assigned = {"a": 7}
        return _Closed

    def _refused_member(self) -> Any:
        member = dataclasses.make_dataclass(
            "Refused", [("inner", dict[int, str])]
        )
        member.__module__ = __name__
        self.bad_json = {"inner": {}}
        self.other_json = {"inner": {}}
        self.assigned = member(inner={})
        return member

    def json(self, bad: Any = None, **fields: Any) -> dict[str, Any]:
        """Return the server's JSON for object 1.

        Args:
            bad: What the server sends for ``bad``, or ``bad_json``.
            **fields: Values that replace those of the other fields.

        Returns:
            The JSON object.
        """
        return {
            "id": 1,
            **_OTHERS,
            "friend": 9,
            "bad": self.bad_json if bad is None else bad,
            **fields,
        }

    def loaded(self, **fields: Any) -> Any:
        """Return an object that holds its JSON.

        Args:
            **fields: Values that replace those of the other fields.

        Returns:
            The object.
        """
        return self.model(json=self.json(**fields))

    def define(self) -> None:
        """Define what the field names, so that it can be used."""
        self._define()

    def refused(self) -> Any:
        """Return the context manager that expects the field's error.

        Returns:
            The context manager of ``pytest.raises()``, whose value holds the
            error.
        """
        return pytest.raises(self.kind.error)

    def check_error(self, err: Exception) -> None:
        """Check that an error names the field, if it can.

        Args:
            err: The error that using the field raised.
        """
        if isinstance(err, FieldConversionError):
            assert err.field == "bad"
            assert err.model.__name__ == self.model.__name__
            assert err.__cause__ is not None

    def check_intact(
        self, obj: Any, *, bad: Any = None, define: bool = True, **fields: Any
    ) -> None:
        """Check that every other field reads, and the field raises still.

        Once the name is defined, the field reads, if defining makes it work.

        Args:
            obj: The object.
            bad: What the server sent for ``bad``, or ``bad_json``.
            define: Whether to define the name, if that makes the field work.
            **fields: Values that replace those of the other fields.
        """
        expected = {**_OTHERS, **fields}
        assert obj.first == expected["first"]
        assert obj.second == expected["second"]
        assert obj.third == expected["third"]
        for _ in range(2):
            with self.refused() as caught:
                _ = obj.bad
            self.check_error(caught.value)
        assert not hasattr(obj, "never_a_field")
        if not (self.kind.fixable and define):
            return

        self.define()
        assert obj.bad == self.decode(self.bad_json if bad is None else bad)
        assert obj.first == expected["first"]


@pytest.fixture(params=KINDS)
def scenario(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
) -> Scenario:
    """Build a model whose field cannot be used yet, for each kind.

    Args:
        request: The fixture's request, which holds the kind.
        monkeypatch: Defines the names that the field waits for.
        requests_mock: Mock of the transport.

    Returns:
        The scenario.
    """
    return Scenario(request.param, monkeypatch, requests_mock)


def _bodies(session: Any, method: str) -> list[Any]:
    """Return the JSON bodies of the requests of a method.

    Args:
        session: Mock of the transport.
        method: HTTP method.

    Returns:
        Each body, in order.
    """
    return [
        request.json()
        for request in session.request_history
        if request.method == method
    ]


def _methods(session: Any) -> list[str]:
    """Return the methods of the requests sent, in order.

    Args:
        session: Mock of the transport.

    Returns:
        The methods.
    """
    return [request.method for request in session.request_history]


def test_the_first_read_of_an_unloaded_object_loads_it(
    scenario: Scenario,
) -> None:
    """The load succeeds, and only the field raises."""
    session = scenario.session
    session.get(BASE + "things/1/", json=scenario.json())
    obj = scenario.model(id=1)

    assert obj.first == "a"

    assert _methods(session) == ["GET"]
    scenario.check_intact(obj)
    assert _methods(session) == ["GET"]


def test_the_load_of_a_partial_object_replaces_every_cached_value(
    scenario: Scenario,
) -> None:
    """No field keeps a value that the load replaced."""
    session = scenario.session
    session.get(
        BASE + "things/1/",
        json=scenario.json(
            scenario.other_json, first="loaded", second="y", third=3
        ),
    )
    obj = scenario.model(id=1)
    obj.update_from_json({"first": "old", "second": "z"})
    assert (obj.first, obj.second) == ("old", "z")

    assert obj.third == 3

    scenario.check_intact(
        obj, bad=scenario.other_json, first="loaded", second="y", third=3
    )
    assert _methods(session) == ["GET"]


def test_a_lazy_save_sends_the_other_updates_and_records_the_response(
    scenario: Scenario,
) -> None:
    """A response that changes the field's JSON leaves the others coherent."""
    session = scenario.session
    session.patch(
        BASE + "things/1/",
        json=scenario.json(
            scenario.other_json, first="server", second="y", third=6
        ),
    )
    obj = scenario.loaded()
    assert obj.first == "a"
    obj.first = "new"
    obj.third = 5

    obj.save()

    assert _bodies(session, "PATCH") == [{"first": "new", "third": 5}]
    scenario.check_intact(
        obj, bad=scenario.other_json, first="server", second="y", third=6
    )
    obj.save()
    assert _methods(session) == ["PATCH"]


def test_an_eager_assignment_sends_the_value_and_records_the_response(
    scenario: Scenario,
) -> None:
    """The write succeeds, and the response leaves the others coherent."""
    session = scenario.session
    session.patch(
        BASE + "things/1/",
        json=scenario.json(
            scenario.other_json, first="server", second="y", third=6
        ),
    )
    obj = scenario.loaded()
    obj.set_write_mode("eager")
    assert obj.first == "a"

    obj.first = "new"

    assert _bodies(session, "PATCH") == [{"first": "new"}]
    scenario.check_intact(
        obj, bad=scenario.other_json, first="server", second="y", third=6
    )


def test_a_lazy_assignment_of_the_field_raises_and_queues_nothing(
    scenario: Scenario,
) -> None:
    """The other pending updates stay, and a later save sends only them."""
    session = scenario.session
    session.patch(BASE + "things/1/", json=scenario.json(first="new"))
    obj = scenario.loaded()
    obj.first = "new"

    with scenario.refused() as caught:
        obj.bad = scenario.assigned

    scenario.check_error(caught.value)
    assert not session.called
    obj.save()
    assert _bodies(session, "PATCH") == [{"first": "new"}]
    scenario.check_intact(obj, first="new")


def test_an_eager_assignment_of_the_field_raises_before_any_request(
    scenario: Scenario,
) -> None:
    """Nothing is sent, and the pending updates stay."""
    session = scenario.session
    session.patch(BASE + "things/1/", json=scenario.json(third=2))
    obj = scenario.loaded()
    obj.third = 2
    obj.set_write_mode("eager")

    with scenario.refused() as caught:
        obj.bad = scenario.assigned

    scenario.check_error(caught.value)
    assert not session.called
    obj.save()
    assert _bodies(session, "PATCH") == [{"third": 2}]


def test_a_full_save_of_an_object_that_holds_the_field_raises_before_sending(
    scenario: Scenario,
) -> None:
    """The other pending updates stay, and the save works once it can."""
    session = scenario.session
    session.put(BASE + "things/1/", json=scenario.json(first="new"))
    session.patch(BASE + "things/1/", json=scenario.json(first="new"))
    obj = scenario.loaded()
    obj.first = "new"

    with scenario.refused() as caught:
        obj.save(full=True)

    scenario.check_error(caught.value)
    assert not session.called
    assert obj.first == "new"
    if scenario.kind.fixable:
        scenario.define()
        obj.save(full=True)
        assert _methods(session) == ["PUT"]
        assert _bodies(session, "PUT")[0]["first"] == "new"
        assert "bad" in _bodies(session, "PUT")[0]
    else:
        obj.save()
        assert _bodies(session, "PATCH") == [{"first": "new"}]


def test_a_full_save_of_an_object_without_the_field_sends_the_others(
    scenario: Scenario,
) -> None:
    """The field is not used, so it raises nothing."""
    session = scenario.session
    session.put(BASE + "things/1/", json={"id": 1, **_OTHERS, "first": "new"})
    json = scenario.json()
    del json["bad"]
    obj = scenario.model(json=json)
    obj.first = "new"

    obj.save(full=True)

    body = _bodies(session, "PUT")[0]
    assert body["first"] == "new"
    assert "bad" not in body


def test_update_from_json_merges_the_others_and_drops_their_caches(
    scenario: Scenario,
) -> None:
    """A payload that changes the field's JSON leaves the others coherent."""
    obj = scenario.loaded()
    assert (obj.first, obj.second) == ("a", "b")
    obj.third = 3

    obj.update_from_json(
        {"first": "x", "second": "y", "bad": scenario.other_json}
    )

    assert not scenario.session.called
    scenario.check_intact(
        obj, bad=scenario.other_json, first="x", second="y", third=3
    )


def test_update_from_json_of_an_unloaded_object_keeps_the_field_unread(
    scenario: Scenario,
) -> None:
    """The payload becomes the object's JSON, and nothing is requested."""
    obj = scenario.model(id=1)

    obj.update_from_json(
        {"first": "x", "second": "y", "third": 4, "bad": scenario.bad_json}
    )

    assert not scenario.session.called
    scenario.check_intact(obj, first="x", second="y", third=4)


def _list(scenario: Scenario, **query: Any) -> Any:
    """Query with ``filter()``.

    Args:
        scenario: The scenario.
        **query: The lookup.

    Returns:
        The objects.
    """
    return list(scenario.model.filter(**query))


def _all(scenario: Scenario, **query: Any) -> Any:
    return list(scenario.model.all())


def _get(scenario: Scenario, **query: Any) -> Any:
    return [scenario.model.get(**query)]


def _get_page(scenario: Scenario, **query: Any) -> Any:
    return list(scenario.model.get_page(**query).items)


QUERIES = {"filter": _list, "get": _get, "get_page": _get_page}


@pytest.mark.parametrize("entry_point", [*QUERIES, "all"])
def test_a_query_builds_objects_whose_other_fields_read(
    scenario: Scenario, entry_point: str
) -> None:
    """The objects hold a field that cannot be used yet, and read the rest."""
    scenario.session.get(
        BASE + "things/", json=[scenario.json(), scenario.json(id=2)][:1]
    )
    query = {} if entry_point == "all" else {"first": "a"}
    run = _all if entry_point == "all" else QUERIES[entry_point]

    (obj,) = run(scenario, **query)

    scenario.check_intact(obj)


@pytest.mark.parametrize("entry_point", QUERIES)
def test_a_query_by_the_field_raises_before_any_request(
    scenario: Scenario, entry_point: str
) -> None:
    """A lookup that compares the field's value needs its type."""
    scenario.session.get(BASE + "things/", json=[scenario.json()])

    with scenario.refused() as caught:
        QUERIES[entry_point](scenario, bad=scenario.assigned)

    scenario.check_error(caught.value)
    assert not scenario.session.called


def test_a_prefetch_of_another_field_loads_objects_that_hold_the_field(
    scenario: Scenario,
) -> None:
    """The prefetch uses the relation, not the field."""
    session = scenario.session
    session.get(BASE + "things/", json=[scenario.json()])
    session.get(BASE + "friends/", json=[{"id": 9, "name": "f"}])
    session.get(BASE + "friends/9/", json={"id": 9, "name": "f"})

    (obj,) = list(scenario.model.filter(prefetch="friend"))

    assert obj.friend.name == "f"
    scenario.check_intact(obj)


def test_a_prefetch_of_the_field_raises_before_any_request_for_it(
    scenario: Scenario,
) -> None:
    """The field is used to find the relation, so its error is raised."""
    session = scenario.session
    session.get(BASE + "things/", json=[scenario.json()])

    with scenario.refused() as caught:
        list(scenario.model.filter(prefetch="bad"))

    scenario.check_error(caught.value)
    assert _methods(session) in ([], ["GET"])


def test_create_of_the_others_builds_an_object_that_holds_the_field(
    scenario: Scenario,
) -> None:
    """The response's JSON holds the field, and the object reads the rest."""
    session = scenario.session
    session.post(BASE + "things/", json=scenario.json())

    obj = scenario.model.create(first="a", second="b", third=1)

    assert _bodies(session, "POST") == [
        {"first": "a", "second": "b", "third": 1}
    ]
    scenario.check_intact(obj)


def test_create_with_the_field_raises_before_any_request(
    scenario: Scenario,
) -> None:
    """The value of the field cannot be encoded yet."""
    scenario.session.post(BASE + "things/", json=scenario.json())

    with scenario.refused() as caught:
        scenario.model.create(first="a", bad=scenario.assigned)

    scenario.check_error(caught.value)
    assert not scenario.session.called


def test_get_or_create_finds_an_object_that_holds_the_field(
    scenario: Scenario,
) -> None:
    """The lookup does not use the field."""
    scenario.session.get(BASE + "things/", json=[scenario.json()])

    obj, created = scenario.model.get_or_create(first="a")

    assert not created
    scenario.check_intact(obj)


def test_get_or_create_with_the_field_raises_before_any_request(
    scenario: Scenario,
) -> None:
    """The lookup compares the field's value."""
    scenario.session.get(BASE + "things/", json=[])
    scenario.session.post(BASE + "things/", json=scenario.json())

    with scenario.refused() as caught:
        scenario.model.get_or_create(first="a", bad=scenario.assigned)

    scenario.check_error(caught.value)
    assert not scenario.session.called


@pytest.mark.parametrize("mode", ["read", "write"])
def test_json_schema_raises_the_error_of_the_field(
    scenario: Scenario, mode: str
) -> None:
    """The schema is built once the field can be used."""
    with scenario.refused() as caught:
        scenario.model.json_schema(mode)

    scenario.check_error(caught.value)
    if scenario.kind.fixable:
        scenario.define()
        assert "bad" in scenario.model.json_schema(mode)["properties"]


Point = collections.namedtuple("Point", "x y")


class Annotated2D(NamedTuple):
    """``NamedTuple`` that annotates its fields."""

    x: int
    y: int


class SubPoint(Point):
    """Subclass of a ``namedtuple()`` class, whose fields stay unannotated."""


def _declare(hint: Any, **namespace: Any) -> Any:
    """Decorate a model with one field ``value`` of a type.

    Args:
        hint: Annotation of the field.
        **namespace: Attributes of the class, such as its conversions.

    Returns:
        The decorated class.
    """
    return json_object(
        type(
            "Located",
            (JSONObject,),
            {
                "__module__": __name__,
                "class_url": "/located/",
                "__annotations__": {"id": int, "value": hint},
                **namespace,
            },
        )
    )


@pytest.mark.parametrize(
    "hint",
    [Point, list[Point], Point | None, dict[str, Point], SubPoint],
    ids=["direct", "list", "optional", "mapping", "subclass"],
)
def test_a_tuple_class_without_annotations_is_refused_when_decorated(
    hint: Any,
) -> None:
    """The message names the field and the class, and says what to declare.

    Args:
        hint: Annotation of the field.
    """
    name = "SubPoint" if hint is SubPoint else "Point"
    with pytest.raises(
        TypeError, match=rf"Located\.value: {name} has the fields x, y without"
    ) as caught:
        _declare(hint)

    assert not isinstance(caught.value, FieldConversionError)


def test_a_tuple_class_with_annotations_is_not_refused() -> None:
    """A ``NamedTuple`` reads and writes by its annotations."""
    model = _declare(Annotated2D)

    obj = model(json={"id": 1, "value": [1, 2]})

    assert obj.value == Annotated2D(1, 2)


def test_a_conversion_of_a_tuple_class_without_annotations_is_used() -> None:
    """A class that a conversion converts is not entered."""
    model = _declare(
        Point,
        conversions={
            Point: Conversion(
                decode=lambda value, _: Point(*value),
                encode=lambda value, _: list(value),
            )
        },
    )

    assert model(json={"id": 1, "value": [1, 2]}).value == Point(1, 2)
