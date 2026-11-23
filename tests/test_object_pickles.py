"""The state that a keyed object is restored from holds its key."""

from __future__ import annotations

import pickle

import pytest

from eazyrest import API, JSONObject, UsageError, json_object

api = API("https://pickle.example/")


@json_object
class Keyed(JSONObject):
    """A model with a key."""

    class_url = "/keyed/"
    id: int
    name: str


@json_object
class Keyless(JSONObject):
    """A model without a key."""

    name: str


@json_object
class Member(JSONObject):
    """A model with a set relation to its own model."""

    class_url = "/members/"
    id: int
    friends: set[Member]


Keyed.register_api(api)
Member.register_api(api)


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_a_pickle_of_a_keyed_object_round_trips_at_every_protocol(
    protocol: int,
) -> None:
    """The restored object has the key that was pickled."""
    restored = pickle.loads(pickle.dumps(Keyed(id=3), protocol))
    assert restored == Keyed(id=3)
    assert restored.pk == 3
    assert restored != Keyed(id=4)


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_a_pickle_of_a_keyless_object_round_trips_at_every_protocol(
    protocol: int,
) -> None:
    """The restored object holds its JSON."""
    obj = Keyless(json={"name": "a"})
    restored = pickle.loads(pickle.dumps(obj, protocol))
    assert restored.name == "a"


def test_an_object_made_without_its_constructor_has_no_key() -> None:
    """No default key stands in for the one that the constructor builds."""
    bare = Keyed.__new__(Keyed)
    with pytest.raises(AttributeError):
        _ = bare.pk
    with pytest.raises(AttributeError):
        _ = hash(bare)


def test_state_without_a_key_is_refused_for_a_keyed_model() -> None:
    """State without the key is not restored, so no object has key None."""
    state = dict(vars(Keyed(json={"id": 1, "name": "a"})))
    del state["_wire_key"]
    bare = Keyed.__new__(Keyed)
    with pytest.raises(UsageError, match="Keyed"):
        bare.__setstate__(state)


def test_a_pickle_written_without_the_key_does_not_load() -> None:
    """A pickle from before objects kept their key raises on load."""
    state = dict(vars(Keyed(json={"id": 1, "name": "a"})))
    del state["_wire_key"]

    class Carrier:
        """An object that pickles as a bare keyed object with this state."""

        def __reduce__(self) -> tuple[object, ...]:
            return (Keyed.__new__, (Keyed,), state)

    with pytest.raises(UsageError, match="Keyed"):
        pickle.loads(pickle.dumps(Carrier()))


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_objects_that_hold_each_other_in_sets_load_from_a_pickle(
    protocol: int,
) -> None:
    """A set is rebuilt after the objects in it have their keys."""
    first = Member(json={"id": 1, "friends": [2, 3]})
    second = Member(json={"id": 2, "friends": [1]})
    first.friends = {second, Member(id=3)}
    second.friends = {first}
    restored = pickle.loads(pickle.dumps(first, protocol))
    assert restored == first
    assert {friend.pk for friend in restored.friends} == {2, 3}
    (back,) = next(f for f in restored.friends if f.pk == 2).friends
    assert back is restored
