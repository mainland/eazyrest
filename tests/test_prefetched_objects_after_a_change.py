"""Prefetched objects belong to the JSON that they were fetched for."""

from __future__ import annotations

from typing import Any

import requests_mock

from eazyrest import API, JSONObject, json_object

api = API("https://prefetch-state.example/")


@json_object
class Member(JSONObject):
    """A related model with a bulk lookup."""

    class_url = "/members/"
    id: int
    name: str

    @classmethod
    def bulk_get_by_pks(  # type: ignore[no-untyped-def]
        cls, pks, *, api, **kwargs
    ):
        """Give each key an object that has its name."""
        return {
            pk: cls(json={"id": pk, "name": f"m{pk}"}, api=api) for pk in pks
        }


@json_object
class Team(JSONObject):
    """A model whose lead is prefetched."""

    class_url = "/teams/"
    id: int
    lead: Member


Member.register_api(api)
Team.register_api(api)


def teams() -> list[Any]:
    """Read two teams whose leads came from one bulk lookup."""
    with requests_mock.Mocker() as mocker:
        mocker.get(
            "https://prefetch-state.example/teams/",
            json=[{"id": 1, "lead": 7}, {"id": 2, "lead": 8}],
        )
        return list(Team.filter(prefetch="lead"))


def test_a_lead_that_a_payload_changes_is_not_the_prefetched_one() -> None:
    """The batch's object for the new key is not read for the field."""
    first, second = teams()
    assert first.lead is not second.lead
    first.update_from_json({"lead": 8})
    assert first.lead.pk == 8
    assert first.lead is not second.lead


def test_a_lead_that_a_payload_repeats_stays_the_prefetched_one() -> None:
    """The batch's object is kept, as any related object is."""
    first, _ = teams()
    lead = first.lead
    first.update_from_json({"lead": 7})
    assert first.lead is lead
