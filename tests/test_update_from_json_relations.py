"""A merge keeps the related objects whose keys the payload leaves as they are.

A load does, and so does a write's response. A change made in place to a
field's own collection, and a pending update of the field, still go.
"""

from __future__ import annotations

from typing import Any

import requests_mock

from eazyrest import API, JSONObject, json_object

api = API("https://merge-keys.example/")


@json_object
class Member(JSONObject):
    """A related model."""

    class_url = "/members/"
    id: int
    name: str


@json_object
class Team(JSONObject):
    """A model with a single relation, a list, and a set."""

    class_url = "/teams/"
    id: int
    lead: Member | None
    members: list[Member]
    guests: set[Member]


Member.register_api(api)
Team.register_api(api)

BODY: dict[str, Any] = {
    "id": 1,
    "lead": {"id": 7, "name": "a"},
    "members": [{"id": 7, "name": "a"}, {"id": 8, "name": "b"}],
    "guests": [{"id": 9, "name": "c"}],
}


def team() -> Team:
    """Return a loaded team."""
    return Team(json=BODY)


def test_a_single_relation_keeps_its_unsaved_change() -> None:
    """The payload repeats the relation's JSON, so the object stays."""
    obj = team()
    lead = obj.lead
    assert lead is not None
    lead.name = "edited"
    obj.update_from_json({"lead": {"id": 7, "name": "a"}})
    assert obj.lead is lead
    with requests_mock.Mocker() as mocker:
        mocker.patch("https://merge-keys.example/members/7/", json={})
        lead.save()
        assert mocker.last_request.json() == {"name": "edited"}


def test_a_list_and_a_set_keep_their_objects() -> None:
    """The items of a collection keep their unsaved changes too."""
    obj = team()
    first = obj.members[0]
    guest = next(iter(obj.guests))
    first.name = "edited"
    guest.name = "edited"
    obj.update_from_json(
        {"members": BODY["members"], "guests": BODY["guests"]}
    )
    assert obj.members[0] is first
    assert next(iter(obj.guests)) is guest
    assert first.name == "edited"
    assert guest.name == "edited"


def test_a_payload_that_changes_a_key_gives_new_objects() -> None:
    """A changed key is a new object, whatever the field held."""
    obj = team()
    lead = obj.lead
    first = obj.members[0]
    obj.update_from_json(
        {
            "lead": 8,
            "members": [{"id": 9, "name": "c"}, {"id": 8, "name": "b"}],
        }
    )
    assert obj.lead is not lead
    assert obj.lead is not None
    assert obj.lead.pk == 8
    assert obj.members[0] is not first
    assert [m.pk for m in obj.members] == [9, 8]


def test_a_change_in_place_to_a_collection_is_discarded() -> None:
    """The list is read again from the payload, without the added item."""
    obj = team()
    obj.members.append(Member(json={"id": 10, "name": "d"}))
    obj.update_from_json({"members": BODY["members"]})
    assert [m.pk for m in obj.members] == [7, 8]
    with requests_mock.Mocker() as mocker:
        obj.save()
        assert mocker.call_count == 0


def test_a_pending_update_of_the_field_is_discarded() -> None:
    """The payload is the server's state, so the assigned object goes."""
    obj = team()
    obj.lead = Member(json={"id": 11, "name": "e"})
    obj.update_from_json({"lead": BODY["lead"]})
    assert obj.lead is not None
    assert obj.lead.pk == 7
    with requests_mock.Mocker() as mocker:
        obj.save()
        assert mocker.call_count == 0
