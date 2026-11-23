"""A prefetch of a model without an API is refused before any request."""

from __future__ import annotations

from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, UsageError, json_object

BASE = "https://loose-prefetch.example/"
api = API(BASE)
other = API("https://loose-other.example/")


@json_object
class Loose(JSONObject):
    """A related model with no registered API."""

    class_url = "/loose/"
    id: int

    @classmethod
    def bulk_get_by_pks(  # type: ignore[no-untyped-def]
        cls, pks, *, api, **kwargs
    ):
        """Give each key an object."""
        return {pk: cls(json={"id": pk}, api=api) for pk in pks}


@json_object
class Holder(JSONObject):
    """A model whose related model has no registered API."""

    class_url = "/holders/"
    id: int
    loose: Loose
    several: list[Loose]


Holder.register_api(api)


@pytest.mark.parametrize("field", ["loose", "several"])
@pytest.mark.parametrize("query", ["filter", "all", "get", "get_page"])
def test_a_query_without_an_api_refuses_the_prefetch_before_its_request(
    query: str, field: str
) -> None:
    """The error names the related model, and nothing is sent."""
    call: Any = {
        "filter": lambda: list(Holder.filter(prefetch=field)),
        "all": lambda: list(Holder.all(prefetch=field)),
        "get": lambda: Holder.get(prefetch=field, id=1),
        "get_page": lambda: Holder.get_page(prefetch=field),
    }[query]
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[])
        with pytest.raises(UsageError, match="No API registered for Loose"):
            call()
        assert mocker.call_count == 0


def test_a_query_with_an_api_prefetches_the_related_model() -> None:
    """The query's ``api=`` is the related model's too."""
    with requests_mock.Mocker() as mocker:
        mocker.get(
            "https://loose-other.example/holders/",
            json=[{"id": 1, "loose": 7, "several": [7]}],
        )
        (holder,) = Holder.filter(prefetch="loose", api=other)
    assert holder.loose.pk == 7


@json_object
class Unresolved(JSONObject):
    """A related model whose key names a class that is not defined."""

    class_url = "/unresolved/"
    id: NotDefinedAnywhere  # type: ignore[name-defined]  # noqa: F821

    @classmethod
    def bulk_get_by_pks(  # type: ignore[no-untyped-def]
        cls, pks, *, api, **kwargs
    ):
        """Give each key an object."""
        return {}


@json_object
class Owned(JSONObject):
    """A model whose related model has a key that cannot be resolved."""

    class_url = "/owned/"
    id: int
    owner: Unresolved


Owned.register_api(api)


@pytest.mark.parametrize("query_api", [None, other], ids=["no_api", "api"])
def test_a_prefetch_with_an_unresolved_related_key_raises_before_a_request(
    query_api: API | None,
) -> None:
    """The key's error comes first, with or without ``api=``."""
    kwargs: Any = {} if query_api is None else {"api": query_api}
    with requests_mock.Mocker() as mocker:
        mocker.get(requests_mock.ANY, json=[{"id": 1, "owner": 7}])
        with pytest.raises(NameError, match="NotDefinedAnywhere"):
            list(Owned.filter(prefetch="owner", **kwargs))
        assert mocker.call_count == 0
