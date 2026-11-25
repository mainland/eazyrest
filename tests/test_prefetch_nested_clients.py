"""Objects that a bulk hook reads keep none of the query's request options.

A hook builds its objects with the client that it receives, which carries the
query's request options. When it reads a relation of such an object, the
related object is built with the same client and cached. The objects that the
query returns do not keep the options, so neither may the objects that they
hold, whose later loads and writes would otherwise send the query's headers.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, UsageError, json_object

BASE = "https://nested-clients.example/"
api = API(BASE)
OPTIONS = {"headers": {"X-Query": "token-of-the-query"}}


class Base(JSONObject):
    """Models served by the mocked API."""


Base.register_api(api)


@json_object
class Group(Base):
    """A model that a hook reads through the objects that it builds."""

    class_url = "/groups/"
    id: int
    name: str


@json_object
class Member(Base):
    """A model whose bulk lookup reads its relations."""

    class_url = "/members/"
    id: int
    name: str
    group: Group
    groups: list[Group]

    @classmethod
    def bulk_get_by_pks(
        cls, pks: set[Any], *, api: Any, **kwargs: Any
    ) -> dict[Any, Member]:
        """Build the members and read their relations."""
        found = {}
        for item in api.json(api.get(cls.class_url)):
            member = cls(json=item, api=api)
            # Reading a relation caches the related object.
            assert member.group.pk is not None
            assert [group.pk for group in member.groups] == [3, 4]
            found[member.pk] = member
        return found


@json_object
class Task(Base):
    """A model that prefetches its member."""

    class_url = "/tasks/"
    id: int
    member: Member


def nested(task: Task) -> list[Any]:
    """Return the related objects that the hook read."""
    member = task.member
    return [member.group, *member.groups]


def serve(mocker: Any) -> None:
    """Answer every request that the queries make."""
    mocker.get(BASE + "tasks/", json=[{"id": 1, "member": 7}])
    mocker.get(
        BASE + "members/",
        json=[
            {
                "id": 7,
                "name": "a",
                "group": 3,
                "groups": [3, 4],
            }
        ],
    )
    for number in (3, 4):
        mocker.get(
            BASE + f"groups/{number}/", json={"id": number, "name": "g"}
        )
        mocker.patch(BASE + f"groups/{number}/", json={})
    mocker.patch(BASE + "members/7/", json={})


@pytest.mark.parametrize("given", [None, "own"], ids=["registered", "given"])
def test_a_related_object_of_a_hook_keeps_no_request_options(
    given: str | None,
) -> None:
    """A later load and write of each related object send no option."""
    own = API(BASE) if given else None
    with requests_mock.Mocker() as mocker:
        serve(mocker)
        task = next(
            iter(
                Task.filter(
                    prefetch="member", api=own, request_options=OPTIONS
                )
            )
        )
        after_query = mocker.call_count
        for related in nested(task):
            assert related.api is (own if own is not None else api)
            related.name = "h"
            related.save()
        assert mocker.call_count > after_query
        for request in mocker.request_history[after_query:]:
            assert "X-Query" not in request.headers
        task.member.name = "b"
        task.member.save()
        assert "X-Query" not in mocker.last_request.headers


def test_the_query_itself_still_sends_its_options() -> None:
    """The options go with the requests of the query, the hook's included."""
    with requests_mock.Mocker() as mocker:
        serve(mocker)
        list(Task.filter(prefetch="member", request_options=OPTIONS))
        assert [r.path for r in mocker.request_history] == [
            "/tasks/",
            "/members/",
        ]
        for request in mocker.request_history:
            assert request.headers["X-Query"] == "token-of-the-query"


OTHER = API(BASE)


@json_object
class Pinned(Base):
    """A member that the hook builds with a client of its own."""

    class_url = "/pinned/"
    id: int
    group: Group

    @classmethod
    def bulk_get_by_pks(
        cls, pks: set[Any], *, api: Any, **kwargs: Any
    ) -> dict[Any, Pinned]:
        """Build a member with another client and read its relation."""
        member = cls(json={"id": 7, "group": 3}, api=OTHER)
        assert member.group.pk == 3
        return {7: member}


@json_object
class Job(Base):
    """A model that prefetches its pinned member."""

    class_url = "/jobs/"
    id: int
    pinned: Pinned


def test_an_object_that_the_hook_built_with_another_api_is_refused() -> None:
    """The error names the hook, and the objects are not used."""
    with requests_mock.Mocker() as mocker:
        mocker.get(BASE + "jobs/", json=[{"id": 1, "pinned": 7}])
        with pytest.raises(UsageError, match=r"Pinned\.bulk_get_by_pks\(\)"):
            next(iter(Job.filter(prefetch="pinned", request_options=OPTIONS)))
