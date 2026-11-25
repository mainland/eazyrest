"""``max_items`` bounds a traversal whichever hook gave the items.

``filter()`` counts the items it yields before prefetch reads ahead of them,
and refuses a sized result over the bound before it yields any. A hook's count
of the collection's size is kept only if it is a valid size.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterable
from typing import Any, cast

import pytest

from eazyrest import (
    API,
    Client,
    JSONObject,
    LimitExceeded,
    Page,
    Response,
    Results,
    follow_next_links,
    json_object,
    next_links_page,
)

BASE = "https://example.com/v1/"
ITEMS = [{"id": 1}, {"id": 2}, {"id": 3}]


@json_object
class Owner(JSONObject):
    """Related model that prefetch reads in bulk."""

    class_url = "/owners/"

    id: int

    @classmethod
    def bulk_get_by_pks(
        cls, pks: set[Any], *, api: Any, **kwargs: Any
    ) -> dict[Any, Owner]:
        """Read the owners with one request.

        Args:
            pks: Keys to read.
            api: The query's client.
            **kwargs: Context that later releases may pass.

        Returns:
            The owners by key.
        """
        return {
            item["id"]: cls(json=item, api=api)
            for item in api.json(api.get(cls.class_url))
        }


Owner.register_api(API(BASE))


def _declare(
    hook: Callable[..., Iterable[Any]] | None, name: str
) -> type[Any]:
    """Return a model whose collection is read through one hook."""
    namespace: dict[str, Any] = {
        "__annotations__": {"id": int, "owner": Owner},
        "class_url": f"/{name}/",
    }
    if hook is not None:
        namespace[hook.__name__] = classmethod(hook)
    model: type[Any] = json_object(type(name, (JSONObject,), namespace))
    model.register_api(API(BASE))
    return model


def collection_items(
    cls: Any, payload: Any, *, api: Client, **kwargs: Any
) -> Iterable[Any]:
    """Return the guide's envelope example, a list."""
    return cast("Iterable[Any]", payload["results"])


def collection_items_from_response(
    cls: Any, resp: Response, *, api: Client, **kwargs: Any
) -> Iterable[Any]:
    """Return the items of a response as a list."""
    return list(api.json(resp)["results"])


def a_generator(
    cls: Any, resp: Response, *, api: Client, **kwargs: Any
) -> Iterable[Any]:
    """Return the items of a response as a generator."""
    yield from api.json(resp)["results"]


def _generated(
    cls: Any, resp: Response, *, api: Client, **kwargs: Any
) -> Iterable[Any]:
    """Return a generator, which has no size."""
    return a_generator(cls, resp, api=api)


_generated.__name__ = "collection_items_from_response"

HOOKS: dict[str, Callable[..., Iterable[Any]] | None] = {
    "collection_items": collection_items,
    "collection_items_from_response, a list": (collection_items_from_response),
    "collection_items_from_response, a generator": _generated,
}


def _serve(requests_mock: Any, name: str, count: int) -> None:
    """Serve ``count`` items of ``ITEMS`` in one page."""
    items = [{"id": number, "owner": 1} for number in range(1, count + 1)]
    requests_mock.get(f"{BASE}{name}/", json={"results": items})
    requests_mock.get(f"{BASE}owners/", json=[{"id": 1}])


_counter = itertools.count()


def _hooked(hook: Callable[..., Iterable[Any]] | None) -> type[Any]:
    """Return a new model that one hook reads."""
    return _declare(hook, f"hooked{next(_counter)}")


by_hook = pytest.mark.parametrize("hook", HOOKS.values(), ids=HOOKS)


@by_hook
@pytest.mark.parametrize("method", ["filter", "all"])
@pytest.mark.parametrize(("limit", "count"), [(3, 3), (2, 2), (5, 3)])
def test_items_within_max_items_are_yielded(
    requests_mock: Any,
    hook: Callable[..., Iterable[Any]],
    method: str,
    limit: int,
    count: int,
) -> None:
    """At the bound, and below it, every item is yielded."""
    model = _hooked(hook)
    _serve(requests_mock, model.class_url.strip("/"), count)
    api = API(BASE)
    api.max_items = limit

    assert [o.id for o in getattr(model, method)(api=api)] == list(
        range(1, count + 1)
    )


@by_hook
@pytest.mark.parametrize("method", ["filter", "all"])
def test_items_past_max_items_raise(
    requests_mock: Any, hook: Callable[..., Iterable[Any]], method: str
) -> None:
    """One item past the bound raises, whichever hook gave the items."""
    model = _hooked(hook)
    _serve(requests_mock, model.class_url.strip("/"), 3)
    api = API(BASE)
    api.max_items = 2

    with pytest.raises(LimitExceeded, match="more than 2 items") as raised:
        list(getattr(model, method)(api=api))

    assert raised.value.limit == "max_items"


def test_a_sized_result_over_the_bound_yields_nothing(
    requests_mock: Any,
) -> None:
    """A list over the bound raises before the query returns any item."""
    model = _declare(collection_items, "sized")
    _serve(requests_mock, "sized", 3)
    api = API(BASE)
    api.max_items = 2

    with pytest.raises(LimitExceeded, match="more than 2 items"):
        model.filter(api=api)


def test_an_unsized_result_yields_up_to_the_bound(requests_mock: Any) -> None:
    """A generator yields the items within the bound, then raises."""
    model = _declare(_generated, "unsized")
    _serve(requests_mock, "unsized", 3)
    api = API(BASE)
    api.max_items = 2

    items = model.filter(api=api)

    assert [next(items).pk, next(items).pk] == [1, 2]
    with pytest.raises(LimitExceeded, match="more than 2 items"):
        next(items)


@pytest.mark.parametrize("hook", [collection_items, _generated])
def test_the_bound_applies_before_prefetch_reads_ahead(
    requests_mock: Any, hook: Callable[..., Iterable[Any]]
) -> None:
    """No related object is requested for items past the bound."""
    model = _declare(hook, "prefetched" + hook.__name__)
    _serve(requests_mock, model.class_url.strip("/"), 3)
    owners = requests_mock.get(f"{BASE}owners/", json=[{"id": 1}])
    api = API(BASE)
    api.max_items = 2

    with pytest.raises(LimitExceeded, match="more than 2 items"):
        list(model.filter(prefetch="owner", api=api))

    assert not owners.called


def test_the_default_hooks_bound_pages_and_a_page_alike(
    requests_mock: Any,
) -> None:
    """A page over the bound raises for ``get_page()`` as for ``filter()``."""
    model = _declare(None, "plain")
    _serve(requests_mock, "plain", 3)
    api = API(BASE)
    api.max_items = 2

    for read in (model.filter, model.all, model.get_page):
        with pytest.raises(LimitExceeded, match="more than 2 items"):
            list(read(api=api))


@pytest.mark.parametrize(
    ("given", "kept"),
    [(5, 5), (0, 0), (-1, None), ("5", None), (True, None), (5.0, None)],
    ids=["five", "zero", "-1", "'5'", "True", "5.0"],
)
class TestACountThatAHookReports:
    """A hook's count is a size only if it is a non-negative integer."""

    def test_filter_keeps_a_valid_count(
        self, requests_mock: Any, given: Any, kept: int | None
    ) -> None:
        """``filter().count`` is the count, or ``None`` for an invalid one."""

        def collection_items_from_response(
            cls: Any, resp: Response, *, api: Client, **kwargs: Any
        ) -> Results[Any]:
            return Results([], count=given)

        model = _declare(
            collection_items_from_response, "counted" + repr(given)
        )
        requests_mock.get(f"{BASE}counted{given!r}/", json={"results": []})

        assert model.filter().count == kept

    def test_get_page_keeps_a_valid_count(
        self, requests_mock: Any, given: Any, kept: int | None
    ) -> None:
        """``get_page().count`` is the count, or ``None`` if it is invalid."""

        def collection_page(
            cls: Any, resp: Response, *, api: Client, **kwargs: Any
        ) -> Page[Any]:
            return Page([], url=resp.url, count=given)

        model = _declare(collection_page, "paged" + repr(given))
        requests_mock.get(f"{BASE}paged{given!r}/", json={"results": []})

        assert model.get_page().count == kept

    def test_a_page_in_a_payload_keeps_a_valid_count(
        self, requests_mock: Any, given: Any, kept: int | None
    ) -> None:
        """The default hooks read the payload's count the same way."""
        model = _declare(None, "payload" + repr(given))
        requests_mock.get(
            f"{BASE}payload{given!r}/",
            json={"results": [], "count": given},
        )

        assert model.filter().count == kept
        assert model.get_page().count == kept


def test_a_page_and_results_built_by_hand_keep_any_count() -> None:
    """The constructors keep what they are given."""
    assert Page([], url=BASE, count=-1).count == -1
    assert Results([], count=-1).count == -1


@pytest.mark.parametrize("method", ["filter", "all"])
def test_a_page_over_the_bound_is_refused_before_any_item_is_yielded(
    requests_mock: Any, method: str
) -> None:
    """The default hooks read a page whole, and refuse it with its first."""
    model = _declare(None, "firstpage")
    _serve(requests_mock, "firstpage", 3)
    api = API(BASE)
    api.max_items = 2

    with pytest.raises(LimitExceeded, match="more than 2 items"):
        next(getattr(model, method)(api=api))


def test_the_default_hooks_refuse_a_later_page_before_yielding_its_items(
    requests_mock: Any,
) -> None:
    """A page that would pass the bound adds none of its items."""
    model = _declare(None, "twopages")
    requests_mock.get(
        f"{BASE}twopages/",
        json={"results": [{"id": 1}, {"id": 2}], "next": "?page=2"},
    )
    requests_mock.get(
        f"{BASE}twopages/?page=2",
        json={"results": [{"id": 3}, {"id": 4}], "next": None},
    )
    api = API(BASE)
    api.max_items = 3

    items = model.filter(api=api)

    assert [next(items).pk, next(items).pk] == [1, 2]
    with pytest.raises(LimitExceeded, match="more than 3 items"):
        next(items)


def test_a_helper_counts_the_items_of_every_page(requests_mock: Any) -> None:
    """``follow_next_links()`` refuses a page over the bound whole."""
    requests_mock.get(
        f"{BASE}p/?page=2", json={"results": [{"id": 3}, {"id": 4}]}
    )
    first = {"results": [{"id": 1}, {"id": 2}], "next": "?page=2"}
    api = API(BASE)
    api.max_items = 3

    items = follow_next_links(api, first, url=f"{BASE}p/")

    assert [next(items), next(items)] == [{"id": 1}, {"id": 2}]
    with pytest.raises(LimitExceeded, match="more than 3 items"):
        next(items)
    api.max_items = 4
    assert len(list(follow_next_links(api, first, url=f"{BASE}p/"))) == 4


@pytest.mark.parametrize(
    ("given", "kept"),
    [(5, 5), (0, 0), (-1, None), ("5", None), (True, None), (5.0, None)],
    ids=["five", "zero", "-1", "'5'", "True", "5.0"],
)
def test_the_page_helpers_keep_a_valid_count_of_the_payload(
    requests_mock: Any, given: Any, kept: int | None
) -> None:
    """The helpers read the payload's count by the one rule."""
    payload = {"results": [], "count": given}
    requests_mock.get(f"{BASE}counted/", json=payload)
    api = API(BASE)

    assert follow_next_links(api, payload).count == kept
    assert next_links_page(api, api.get("counted/")).count == kept
