"""A ``prefetch_batch_size`` larger than any collection is one batch."""

from __future__ import annotations

from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, json_object

BASE = "https://huge-batch.example/"
api = API(BASE)
lookups: list[set[Any]] = []


@json_object
class Member(JSONObject):
    """A related model with a bulk lookup."""

    class_url = "/members/"
    id: int

    @classmethod
    def bulk_get_by_pks(  # type: ignore[no-untyped-def]
        cls, pks, *, api, **kwargs
    ):
        """Give each key an object, and record the batch."""
        lookups.append(set(pks))
        return {pk: cls(json={"id": pk}, api=api) for pk in pks}


@json_object
class Team(JSONObject):
    """A model whose lead is prefetched."""

    class_url = "/teams/"
    id: int
    lead: Member


Member.register_api(api)
Team.register_api(api)


@pytest.mark.parametrize("size", [10**30, 2**63, 2**63 - 1, None])
def test_a_huge_batch_size_reads_the_collection_as_one_batch(
    size: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One bulk lookup serves every parent, and nothing raises."""
    monkeypatch.setattr(Team, "prefetch_batch_size", size)
    lookups.clear()
    with requests_mock.Mocker() as mocker:
        mocker.get(
            BASE + "teams/",
            json=[{"id": 1, "lead": 7}, {"id": 2, "lead": 8}],
        )
        teams = list(Team.filter(prefetch="lead"))
    assert [team.lead.pk for team in teams] == [7, 8]
    assert lookups == [{7, 8}]
