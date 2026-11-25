"""Tests of the keys that a relation reads, writes, and compares."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Collection, Iterable, Sequence
from typing import Any

import pytest

from eazyrest import (
    API,
    Client,
    FieldConversionError,
    JSONObject,
    json_object,
)


class RelatedBase(JSONObject):
    """Models served by the mocked API."""


RelatedBase.register_api(API("https://related.example/"))


@json_object
class User(RelatedBase):
    """Model keyed by an ``int``."""

    class_url = "/users/"

    id: int


@json_object
class Post(RelatedBase):
    """Model with a relation and a list of relations."""

    class_url = "/posts/"

    id: int
    owner: User
    readers: list[User]


@json_object
class Feed(RelatedBase):
    """Model with a sequence of relations."""

    class_url = "/feeds/"

    id: int
    readers: Sequence[User]


@json_object
class Slot(RelatedBase):
    """Model whose class formats its URL from its key."""

    id: int

    @property
    def url(self) -> str:
        """Return a URL built from the key."""
        return f"/slots/{self.pk}/detail/"


@json_object
class Booking(RelatedBase):
    """Model that relates to a slot."""

    class_url = "/bookings/"

    id: int
    slot: Slot


BAD_KEYS = [
    pytest.param("7", id="string"),
    pytest.param(True, id="bool"),
    pytest.param([1], id="list"),
    pytest.param({"a": 1}, id="mapping"),
]


@pytest.mark.parametrize("key", BAD_KEYS)
def test_full_save_refuses_an_embedded_key_of_another_type(
    key: Any, requests_mock: Any
) -> None:
    """The write reads the field as reading it does, before any request."""
    put = requests_mock.put("https://related.example/posts/1/", json={})
    post = Post(json={"id": 1, "owner": {"id": key}, "readers": []})

    with pytest.raises(FieldConversionError, match=r"Post\.owner"):
        post.save(full=True)

    assert not put.called


@pytest.mark.parametrize("key", BAD_KEYS)
def test_full_save_refuses_an_embedded_key_in_a_list_of_relations(
    key: Any, requests_mock: Any
) -> None:
    """An item of a list relation is checked as a single relation is."""
    put = requests_mock.put("https://related.example/posts/1/", json={})
    post = Post(
        json={"id": 1, "owner": {"id": 2}, "readers": [{"id": 3}, {"id": key}]}
    )

    with pytest.raises(FieldConversionError, match=r"Post\.readers"):
        post.save(full=True)

    assert not put.called


def test_full_save_refuses_a_bare_key_of_another_type_naming_the_field(
    requests_mock: Any,
) -> None:
    """The error names the relation that the program knows."""
    put = requests_mock.put("https://related.example/posts/1/", json={})
    post = Post(json={"id": 1, "owner": "7", "readers": []})

    with pytest.raises(FieldConversionError, match=r"Post\.owner"):
        post.save(full=True)

    assert not put.called


def test_full_save_refuses_what_it_loads_that_a_read_would_refuse(
    requests_mock: Any,
) -> None:
    """The object that the full save loads is checked after it loads."""
    requests_mock.get(
        "https://related.example/posts/1/",
        json={"id": 1, "owner": {"id": "7"}, "readers": []},
    )
    put = requests_mock.put("https://related.example/posts/1/", json={})

    with pytest.raises(FieldConversionError, match=r"Post\.owner"):
        Post(id=1).save(full=True)

    assert not put.called


def _loaded_post() -> Post:
    """Return a loaded post, which an assignment writes without a load."""
    return Post(
        json={"id": 1, "owner": {"id": 2}, "readers": []}, write_mode="lazy"
    )


def test_a_related_object_with_a_key_is_written_as_that_key(
    requests_mock: Any,
) -> None:
    """A related object whose key builds is sent as its wire key."""
    posted = requests_mock.post("https://related.example/posts/", json={})

    Post.create(id=2, owner=User(json={"id": 7}), readers=[User(id=8)])

    assert posted.last_request.json() == {"id": 2, "owner": 7, "readers": [8]}


def test_a_class_that_overrides_url_takes_its_key_as_it_is_built() -> None:
    """A URL that is formatted from the key needs the key given at once."""
    slot = Slot(id=3)
    booking = Booking(json={"id": 1, "slot": 4})

    assert slot.url == "/slots/3/detail/"
    assert booking.slot.url == "/slots/4/detail/"


def test_the_error_of_a_list_relation_names_the_field_once(
    requests_mock: Any,
) -> None:
    """A read and a full save each name the relation once."""
    put = requests_mock.put("https://related.example/posts/1/", json={})
    post = Post(json={"id": 1, "owner": 2, "readers": [{"id": "7"}]})
    messages = []

    with pytest.raises(FieldConversionError) as read:
        _ = post.readers
    messages.append(str(read.value))
    with pytest.raises(FieldConversionError) as full:
        post.save(full=True)
    messages.append(str(full.value))

    assert [message.count("Post.readers") for message in messages] == [1] * 2
    assert not put.called


def test_a_lookup_sends_the_keys_of_related_objects(
    requests_mock: Any,
) -> None:
    """A lookup writes the wire key of each related object."""
    requests_mock.get("https://related.example/posts/", json=[])

    list(Post.filter(owner__in=[User(id=1), User(json={"id": 2})]))

    assert requests_mock.last_request.qs == {"owner__in": ["1,2"]}


def test_the_error_of_a_list_relation_names_the_position_of_the_item() -> None:
    """The index says which item of the server's list is wrong."""
    post = Post(json={"id": 1, "owner": 2, "readers": [3, "x", 4]})
    feed = Feed(json={"id": 1, "readers": [3, 4, "x"]})

    with pytest.raises(FieldConversionError) as eager:
        _ = post.readers
    with pytest.raises(FieldConversionError) as lazy:
        list(feed.readers)

    assert str(eager.value) == (
        "Post.readers: [1]: The key must be a JSON int, not a str"
    )
    assert str(lazy.value) == (
        "Feed.readers: [2]: The key must be a JSON int, not a str"
    )
    assert isinstance(eager.value.__cause__, TypeError)
    assert str(eager.value.__cause__) == (
        "[1]: The key must be a JSON int, not a str"
    )
    assert str(lazy.value.__cause__) == (
        "[2]: The key must be a JSON int, not a str"
    )


def test_a_list_relation_refuses_an_embedded_object_without_a_key() -> None:
    """An item of a collection has a key, as a single relation has."""
    post = Post(json={"id": 1, "owner": {"x": 1}, "readers": [3, {"x": 1}]})
    feed = Feed(json={"id": 1, "readers": [{"id": None}]})

    with pytest.raises(FieldConversionError, match=r"Post\.owner: .*no key"):
        post.owner  # noqa: B018
    with pytest.raises(
        FieldConversionError,
        match=(
            r"Post\.readers: \[1\]: The item holds no key, "
            r"so it names no User"
        ),
    ):
        post.readers  # noqa: B018
    with pytest.raises(FieldConversionError, match=r"Feed\.readers: \[0\]"):
        feed.readers  # noqa: B018


def test_an_unread_list_with_a_keyless_item_does_not_stop_a_save(
    requests_mock: Any,
) -> None:
    """A list that the program did not read is not sent."""
    patch = requests_mock.patch("https://related.example/posts/1/", json={})
    post = Post(
        json={"id": 1, "owner": 2, "readers": [{"x": 1}, 3]},
        write_mode="lazy",
    )

    post.owner = User(id=5)
    post.save()

    assert patch.last_request.json() == {"owner": 5}


BAD_BARE_KEYS = [
    pytest.param("7", id="string"),
    pytest.param(True, id="bool"),
    pytest.param(1.5, id="float"),
    pytest.param([1], id="list"),
    pytest.param({"id": 5}, id="mapping"),
]


@pytest.mark.parametrize("key", BAD_BARE_KEYS)
def test_assigning_a_bare_key_that_cannot_be_built_is_refused(
    key: Any, requests_mock: Any
) -> None:
    """A bare key is built by the related model, as one that is read is."""
    post = _loaded_post()

    with pytest.raises(FieldConversionError, match=r"Post\.owner"):
        post.owner = key
    with pytest.raises(FieldConversionError, match=r"Post\.readers"):
        post.readers = [User(id=3), key]

    assert post._pending_updates == {}
    assert requests_mock.call_count == 0


@pytest.mark.parametrize("key", BAD_BARE_KEYS)
def test_create_refuses_a_bare_key_that_cannot_be_built(
    key: Any, requests_mock: Any
) -> None:
    """The body holds no key that reading the field would refuse."""
    posted = requests_mock.post("https://related.example/posts/", json={})

    with pytest.raises(FieldConversionError, match=r"Post\.owner"):
        Post.create(id=2, owner=key, readers=[])
    with pytest.raises(FieldConversionError, match=r"Post\.readers"):
        Post.create(id=2, owner=1, readers=[3, key])

    assert not posted.called


@pytest.mark.parametrize("key", BAD_BARE_KEYS)
def test_save_refuses_a_bare_key_that_cannot_be_built(
    key: Any, requests_mock: Any
) -> None:
    """A change in place of a list relation is checked when it is saved."""
    patch = requests_mock.patch("https://related.example/posts/1/", json={})
    post = Post(
        json={"id": 1, "owner": 2, "readers": [3]},
        write_mode="lazy",
    )
    post.readers.append(key)

    with pytest.raises(FieldConversionError, match=r"Post\.readers"):
        post.save()

    assert not patch.called


@pytest.mark.parametrize(("key", "same"), [(1, True), (2, 2.0)])
def test_save_refuses_a_key_replaced_by_an_equal_value_that_is_no_key(
    key: Any, same: Any, requests_mock: Any
) -> None:
    """A bare key that is replaced in place by one equal to it is checked."""
    requests_mock.patch("https://related.example/posts/1/", json={})
    post = Post(
        json={"id": 1, "owner": 2, "readers": [key]}, write_mode="lazy"
    )
    post.readers[0] = User(id=key)
    post.save()
    sent = requests_mock.call_count
    post.readers[0] = same

    with pytest.raises(FieldConversionError, match=r"Post\.readers"):
        post.save()

    assert requests_mock.call_count == sent


@pytest.mark.parametrize("key", BAD_BARE_KEYS)
def test_a_query_refuses_a_bare_key_that_cannot_be_built(
    key: Any, requests_mock: Any
) -> None:
    """The query sends no key that the related model would not read."""
    requests_mock.get("https://related.example/posts/", json=[])

    with pytest.raises(FieldConversionError, match=r"Post\.readers"):
        list(Post.filter(readers=[2, key]))

    assert requests_mock.call_count == 0


@pytest.mark.parametrize("key", ["7", True, 1.5, {"id": 5}])
def test_a_query_refuses_a_single_bare_key_that_cannot_be_built(
    key: Any, requests_mock: Any
) -> None:
    """A key given alone is built as one of several is."""
    requests_mock.get("https://related.example/posts/", json=[])

    with pytest.raises(FieldConversionError, match=r"Post\.owner"):
        list(Post.filter(owner=key))

    assert requests_mock.call_count == 0


@pytest.mark.parametrize("method", ["filter", "get", "get_or_create"])
@pytest.mark.parametrize("key", ["7", True, 1.5])
def test_a_query_for_the_key_field_refuses_a_key_that_cannot_be_built(
    method: str, key: Any, requests_mock: Any
) -> None:
    """The key field is compared as every key is, before any request."""
    requests_mock.get("https://related.example/users/", json=[])

    queries: dict[str, Callable[..., Any]] = {
        "filter": lambda **kwargs: list(User.filter(**kwargs)),
        "get": User.get,
        "get_or_create": User.get_or_create,
    }
    query = queries[method]

    with pytest.raises(FieldConversionError, match=r"User\.id"):
        query(id=key)

    assert requests_mock.call_count == 0


@pytest.mark.parametrize(
    ("lookup", "value"),
    [
        ("pk", "7"),
        ("id__exact", "7"),
        ("id__in", ["7"]),
        ("pk__in", ["7"]),
        ("id__gte", "7"),
        ("id__range", ["1", "9"]),
    ],
)
def test_a_lookup_on_the_key_field_refuses_a_key_that_cannot_be_built(
    lookup: str, value: Any, requests_mock: Any
) -> None:
    """Every name of the key, with any value suffix, compares a key."""
    requests_mock.get("https://related.example/users/", json=[])

    with pytest.raises(FieldConversionError, match=r"User\.id"):
        list(User.filter(params={lookup: value}))

    assert requests_mock.call_count == 0


def test_a_query_for_the_key_field_sends_the_built_key(
    requests_mock: Any,
) -> None:
    """A key of the field's type is sent as the key constructor writes it."""
    requests_mock.get("https://related.example/users/", json=[])

    list(User.filter(id=7))
    list(User.filter(pk=7))
    list(User.filter(id__in=[7, 8]))
    list(User.filter(id__range=[1, 9]))

    assert [r.qs for r in requests_mock.request_history] == [
        {"id": ["7"]},
        {"pk": ["7"]},
        {"id__in": ["7,8"]},
        {"id__range": ["1,9"]},
    ]


def test_a_bare_key_that_builds_is_written_as_the_wire_key(
    requests_mock: Any,
) -> None:
    """A valid key is sent as it was, and a key of None still clears."""
    posted = requests_mock.post("https://related.example/posts/", json={})
    requests_mock.get("https://related.example/posts/", json=[])

    Post.create(id=2, owner=7, readers=[8, 9])
    list(Post.filter(owner=7))

    assert posted.last_request.json() == {
        "id": 2,
        "owner": 7,
        "readers": [8, 9],
    }
    assert requests_mock.last_request.qs == {"owner": ["7"]}


@pytest.mark.parametrize("lookup", ["owner__in", "owner__exact"])
@pytest.mark.parametrize("key", ["7", True, 1.5, {"id": 5}])
def test_a_lookup_refuses_a_bare_key_that_cannot_be_built(
    lookup: str, key: Any, requests_mock: Any
) -> None:
    """A lookup that compares the relation's key builds it as a filter does."""
    requests_mock.get("https://related.example/posts/", json=[])

    # A lookup of several values takes a collection, not one key.
    refused = ValueError if lookup == "owner__in" else FieldConversionError
    with pytest.raises(refused, match=r"Post\.owner|owner__in"):
        list(Post.filter(params={lookup: key}))
    if lookup == "owner__in":
        with pytest.raises(FieldConversionError, match=r"Post\.owner"):
            list(Post.filter(params={lookup: [7, key]}))
    with pytest.raises(FieldConversionError, match=r"Post\.readers"):
        list(Post.filter(readers__in=[key]))

    assert requests_mock.call_count == 0


def test_a_lookup_sends_a_bare_key_that_builds(requests_mock: Any) -> None:
    """A key in its field's type is sent, alone or with related objects."""
    requests_mock.get("https://related.example/posts/", json=[])

    list(Post.filter(owner__in=[7, User(id=8)]))
    list(Post.filter(owner__exact=9))

    assert [r.qs for r in requests_mock.request_history] == [
        {"owner__in": ["7,8"]},
        {"owner__exact": ["9"]},
    ]


def test_a_lookup_that_does_not_compare_the_key_is_not_converted(
    requests_mock: Any,
) -> None:
    """Another suffix compares something else, which is sent as given."""
    requests_mock.get("https://related.example/posts/", json=[])

    list(Post.filter(owner__name="x", owner__isnull=True))

    assert requests_mock.last_request.qs == {
        "owner__name": ["x"],
        "owner__isnull": ["true"],
    }


def test_a_bare_key_on_a_path_to_the_key_is_sent_as_the_relation_sends_it(
    requests_mock: Any,
) -> None:
    """A key of the right type is built to its wire value."""
    requests_mock.get("https://related.example/posts/", json=[])

    list(Post.filter(owner__id__in=[7, 8], owner__pk=9))

    assert requests_mock.last_request.qs == {
        "owner__id__in": ["7,8"],
        "owner__pk": ["9"],
    }


@json_object(pk="slug")
class Badge(User):
    """Subclass that chooses another key field, which holds an integer."""

    slug: int


hook_calls: list[set[Any]] = []
"""The keys that a bulk hook received."""


@json_object
class Writer(RelatedBase):
    """Model with a bulk hook that records the keys it gets."""

    class_url = "/writers/"

    id: int

    @classmethod
    def bulk_get_by_pks(
        cls, pks: set[Any], *, api: Client | None = None, **kwargs: Any
    ) -> dict[Any, Writer]:
        """Return a writer for each key, after recording the keys."""
        hook_calls.append(set(pks))
        return {pk: cls(json={"id": pk}, api=api) for pk in pks}


@json_object
class Article(RelatedBase):
    """Model with relations that prefetch loads in bulk."""

    class_url = "/articles/"

    id: int
    writer: Writer
    editors: list[Writer]


@pytest.fixture(autouse=True)
def _clear_records() -> None:
    """Start each test with no recorded hook calls."""
    hook_calls.clear()


def _article(**fields: Any) -> dict[str, Any]:
    """Return an article's JSON with valid relations except the given."""
    return {
        "id": 1,
        "writer": 1,
        "editors": [],
    } | fields


@pytest.mark.parametrize(
    "key",
    [
        pytest.param("7", id="string"),
        pytest.param(True, id="bool"),
        pytest.param(1.0, id="float"),
    ],
)
def test_prefetch_refuses_a_key_that_cannot_be_built_before_the_hook(
    key: Any, requests_mock: Any
) -> None:
    """The hook gets no key that reading the field would refuse."""
    requests_mock.get(
        "https://related.example/articles/",
        json=[_article(writer=key), _article(id=2, writer=2)],
    )

    with pytest.raises(FieldConversionError, match=r"Article\.writer"):
        list(Article.filter(prefetch=["writer"]))
    with pytest.raises(FieldConversionError, match=r"Article\.writer"):
        list(Article.filter(prefetch="writer"))

    assert hook_calls == []


def test_prefetch_refuses_a_key_in_a_list_before_the_hook(
    requests_mock: Any,
) -> None:
    """An item of a list relation is built as a single relation is."""
    requests_mock.get(
        "https://related.example/articles/",
        json=[_article(editors=[1, "7"])],
    )

    with pytest.raises(FieldConversionError, match=r"Article\.editors"):
        list(Article.filter(prefetch=["editors"]))

    assert hook_calls == []


def test_prefetch_keeps_true_apart_from_one(requests_mock: Any) -> None:
    """A valid 1 is not sent as True because a sibling holds true."""
    requests_mock.get(
        "https://related.example/articles/",
        json=[_article(writer=1), _article(id=2, writer=True)],
    )

    with pytest.raises(FieldConversionError, match=r"Article\.writer"):
        list(Article.filter(prefetch=["writer"]))

    assert hook_calls == []


def test_prefetch_hands_the_hook_each_key_once(requests_mock: Any) -> None:
    """Parents that share a key give the hook one key."""
    requests_mock.get(
        "https://related.example/articles/",
        json=[
            _article(writer=1, editors=[2, 1]),
            _article(id=2, writer=1, editors=[3]),
        ],
    )

    articles = list(Article.filter(prefetch=["writer", "editors"]))

    assert hook_calls == [{1}, {1, 2, 3}]
    assert [writer.pk for writer in articles[0].editors] == [2, 1]


@pytest.mark.parametrize("lookup", ["owner__in", "owner__exact"])
def test_a_lookup_on_a_relation_refuses_an_object_of_another_model(
    lookup: str, requests_mock: Any
) -> None:
    """A relation takes the key of its model, not of another."""
    requests_mock.get("https://related.example/posts/", json=[])
    other = Feed(id=4)

    with pytest.raises(FieldConversionError, match=r"Post\.owner"):
        list(
            Post.filter(
                params={lookup: [other] if lookup.endswith("in") else other}
            )
        )

    assert requests_mock.call_count == 0


def test_a_lookup_on_a_relation_refuses_an_object_of_another_key(
    requests_mock: Any,
) -> None:
    """A lookup on a relation refuses what the relation refuses."""
    lookup = "owner__in"
    requests_mock.get("https://related.example/posts/", json=[])
    other = Badge(json={"slug": 4, "id": 9})

    with pytest.raises(FieldConversionError, match=r"Post\.owner"):
        list(
            Post.filter(
                params={lookup: [other] if lookup.endswith("in") else other}
            )
        )

    assert requests_mock.call_count == 0


@json_object
class Team(RelatedBase):
    """Model that a member names."""

    class_url = "/teams/"

    id: int


@json_object
class Member(RelatedBase):
    """Model with a relation to a team besides its own key."""

    class_url = "/members/"

    id: int
    team: Team


@json_object
class Ticket(RelatedBase):
    """Model with a relation to a keyed model and one to a renamed key."""

    class_url = "/tickets/"

    id: int
    owner: Member
    renamed: Renamed


@json_object(pk="uid", field_map={"uid": "id"})
class Renamed(RelatedBase):
    """Model whose key is read from another JSON key than its name."""

    class_url = "/renamed/"

    uid: int


@pytest.mark.parametrize(
    "lookup", ["account__user__in", "account__pk__in", "account__user__id__in"]
)
def test_a_lookup_on_a_path_that_the_class_does_not_know_sends_a_key(
    lookup: str, requests_mock: Any
) -> None:
    """A model object on a path of no known field is sent as its key.

    The class cannot check such a path, so the value is sent as given, except
    that an object of a model is sent as its key.
    """
    requests_mock.get("https://related.example/tickets/", json=[])

    list(Ticket.filter(params={lookup: [User(id=7)]}))

    assert requests_mock.last_request.qs == {lookup: ["7"]}


def test_a_lookup_on_a_field_of_a_related_model_takes_that_models_objects(
    requests_mock: Any,
) -> None:
    """A path that is not the related key compares another model's values."""
    requests_mock.get("https://related.example/tickets/", json=[])

    list(Ticket.filter(owner__team__in=[Team(id=4)]))

    assert requests_mock.last_request.qs == {"owner__team__in": ["4"]}


def test_a_lookup_that_compares_no_value_needs_no_type_of_its_field(
    requests_mock: Any,
) -> None:
    """A field that cannot be resolved yet does not stop such a lookup."""

    @json_object
    class Waiting(RelatedBase):
        """Model whose relation names a class that is not defined."""

        class_url = "/waiting/"

        id: int
        owner: Missing  # type: ignore[name-defined]  # noqa: F821

    requests_mock.get("https://related.example/waiting/", json=[])

    list(Waiting.filter(owner__isnull=True, owner__name="bob"))

    assert requests_mock.last_request.qs == {
        "owner__isnull": ["true"],
        "owner__name": ["bob"],
    }
    with pytest.raises(NameError):
        list(Waiting.filter(owner__in=[7]))


@json_object
class Tag(RelatedBase):
    """Model keyed by a UUID that a bulk hook records."""

    class_url = "/tags/"

    id: uuid.UUID

    @classmethod
    def bulk_get_by_pks(
        cls, pks: set[Any], *, api: Client | None = None, **kwargs: Any
    ) -> dict[Any, Tag]:
        """Return a tag for each key, recording the keys."""
        tag_keys.append(set(pks))
        return {pk: cls(json={"id": pk}, api=api) for pk in pks}


tag_keys: list[set[Any]] = []
"""The keys that ``Tag.bulk_get_by_pks()`` received."""


@json_object
class Tagged(RelatedBase):
    """Model whose relation prefetch loads in bulk."""

    class_url = "/tagged/"

    id: int
    tag: Tag


def test_prefetch_hands_the_hook_the_key_as_the_related_model_writes_it(
    requests_mock: Any,
) -> None:
    """A parent's upper-case UUID is the canonical text for the hook."""
    tag_keys.clear()
    requests_mock.get(
        "https://related.example/tagged/",
        json=[
            {"id": 1, "tag": "5f2a9f5b-0b6f-4f0e-9b7a-3a1c3b8a7c11"},
            {"id": 2, "tag": "5F2A9F5B-0B6F-4F0E-9B7A-3A1C3B8A7C11"},
        ],
    )

    list(Tagged.filter(prefetch=["tag"]))

    assert tag_keys == [{"5f2a9f5b-0b6f-4f0e-9b7a-3a1c3b8a7c11"}]


def test_a_refused_lookup_names_the_field_and_not_the_parameter(
    requests_mock: Any,
) -> None:
    """The error's field is the relation, which a caller can look up."""
    requests_mock.get("https://related.example/posts/", json=[])

    with pytest.raises(FieldConversionError) as info:
        list(Post.filter(owner__in=["7"]))

    assert info.value.field == "owner"
    assert str(info.value).startswith("Post.owner:")


def test_a_model_operand_of_its_own_key_is_sent_as_its_key(
    requests_mock: Any,
) -> None:
    """An object compares as its key, as a related object does."""
    requests_mock.get("https://related.example/users/", json=[{"id": 7}])

    list(User.filter(pk__in=[User(id=7), User(id=8)]))
    User.get(pk=User(id=7))
    list(User.filter(id=User(id=7)))

    assert [r.qs for r in requests_mock.request_history] == [
        {"pk__in": ["7,8"]},
        {"pk": ["7"]},
        {"id": ["7"]},
    ]


def test_a_model_operand_of_its_own_key_is_refused_as_another_model(
    requests_mock: Any,
) -> None:
    """The class check of a related object applies to the key's lookups."""
    requests_mock.get("https://related.example/users/", json=[])

    with pytest.raises(FieldConversionError, match=r"User\.id.*another model"):
        list(User.filter(pk__in=[Team(id=4)]))

    assert requests_mock.call_count == 0


@pytest.mark.parametrize(
    ("model", "params"),
    [
        (User, {"pk": 7, "id": 8}),
        (User, {"id": 8, "pk": 7}),
        (User, {"pk__in": [7], "id__in": [8]}),
        (User, {"pk__gte": 7, "id__gte": 8}),
        (Renamed, {"uid": 7, "pk": 8}),
        (User, {"pk__exact": 7, "id": 8}),
        (User, {"id__exact": 7, "pk": 8}),
    ],
)
def test_two_parameters_that_name_one_key_are_refused(
    model: type[JSONObject], params: dict[str, Any], requests_mock: Any
) -> None:
    """A query that gave the key twice would send two filters on one key."""
    with pytest.raises(ValueError, match="both compare its key") as error:
        list(model.filter(**params))

    assert all(name in str(error.value) for name in params)
    assert requests_mock.call_count == 0


def test_the_json_name_of_the_key_is_an_ordinary_name(
    requests_mock: Any,
) -> None:
    """The JSON name is sent as given, but it compares the key as ``pk``."""
    requests_mock.get("https://related.example/renamed/", json=[])

    list(Renamed.filter(id="7"))

    assert requests_mock.last_request.qs == {"id": ["7"]}
    with pytest.raises(ValueError, match=r"id and pk .* both compare"):
        list(Renamed.filter(id="7", pk=8))


def test_two_parameters_that_send_one_name_are_refused(
    requests_mock: Any,
) -> None:
    """The JSON name of the key and the field's name both send that name."""
    with pytest.raises(ValueError, match=r"uid and id .* both send id"):
        list(Renamed.filter(uid=7, id=8))

    assert requests_mock.call_count == 0


def test_parameters_that_compare_one_key_differently_are_sent(
    requests_mock: Any,
) -> None:
    """Another lookup on the key is another filter."""
    requests_mock.get("https://related.example/users/", json=[])

    list(User.filter(pk=7, id__gte=3, pk__lt=9))

    assert requests_mock.last_request.qs == {
        "pk": ["7"],
        "id__gte": ["3"],
        "pk__lt": ["9"],
    }


@json_object
class Roster(RelatedBase):
    """Model with a list of relations, a set, and a plain field."""

    class_url = "/rosters/"

    id: int
    title: str
    staff: list[User]
    crew: set[User]


def _roster(requests_mock: Any, **over: Any) -> Roster:
    """Return a lazy roster read from JSON, and serve its saves.

    Args:
        requests_mock: The mocked transport.
        **over: Members of the JSON to replace.

    Returns:
        The roster.
    """
    requests_mock.patch("https://related.example/rosters/1/", json={})
    return Roster(
        json={
            "id": 1,
            "title": "t",
            "staff": [1, 2],
            "crew": [5, 6],
            **over,
        },
        write_mode="lazy",
    )


def _sent_bodies(requests_mock: Any) -> list[Any]:
    """Return the JSON body of each write that the transport received."""
    return [
        r.json() for r in requests_mock.request_history if r.method == "PATCH"
    ]


def test_an_item_without_a_key_names_its_position(
    requests_mock: Any,
) -> None:
    """The error names the position, and a save of another field is sent."""
    roster = _roster(requests_mock, staff=[1, {"x": 1}])

    roster.title = "u"
    roster.save()

    assert _sent_bodies(requests_mock) == [{"title": "u"}]
    with pytest.raises(
        FieldConversionError, match=r"Roster\.staff: \[1\]: The item"
    ):
        roster.staff  # noqa: B018


def test_a_saved_list_is_not_sent_with_the_next_save(
    requests_mock: Any,
) -> None:
    """After a save, the list that was sent is the one that the server has."""
    roster = _roster(requests_mock)

    roster.staff.append(User(id=4))
    roster.save()
    roster.title = "z"
    roster.save()

    assert _sent_bodies(requests_mock) == [
        {"staff": [1, 2, 4]},
        {"title": "z"},
    ]


def test_a_set_member_removed_and_added_again_is_not_a_change(
    requests_mock: Any,
) -> None:
    """A set is compared without order, so a new order sends nothing."""
    roster = _roster(requests_mock)

    for key in (5, 6):
        roster.crew.discard(User(id=key))
        roster.crew.add(User(id=key))
    roster.save()

    assert _sent_bodies(requests_mock) == []


@pytest.mark.parametrize(
    ("name", "value"), [("owner__id__isnull", "true"), ("pk__icontains", "7")]
)
def test_a_name_that_is_no_value_lookup_of_a_key_is_sent_as_given(
    name: str, value: str, requests_mock: Any
) -> None:
    """Only a suffix that compares values is built as the key."""
    requests_mock.get("https://related.example/posts/", json=[])

    list(Post.filter(params={name: value}))

    assert requests_mock.last_request.qs == {name: [value]}


@pytest.mark.parametrize(
    "name",
    [
        "id",
        "id__exact",
        "id__gt",
        "id__gte",
        "id__lt",
        "id__lte",
        "pk",
        "pk__exact",
        "pk__gt",
        "owner",
        "owner__exact",
        "owner__gt",
    ],
)
@pytest.mark.parametrize("value", [[7, 8], (7, 8), {7, 8}])
def test_a_lookup_of_one_value_refuses_a_collection(
    name: str, value: Any, requests_mock: Any
) -> None:
    """A server applies one of a repeated parameter, so none is sent."""
    requests_mock.get("https://related.example/posts/", json=[])

    for call in (
        lambda: list(Post.filter(**{name: value})),
        lambda: list(Post.filter(params={name: value})),
        lambda: Post.get(**{name: value}),
    ):
        with pytest.raises(ValueError, match=rf"parameter {name} for Post"):
            call()

    assert requests_mock.call_count == 0


def test_a_lookup_of_several_values_and_a_collection_field_take_a_collection(
    requests_mock: Any,
) -> None:
    """The refusal leaves the parameters that are meant to take several."""
    requests_mock.get("https://related.example/posts/", json=[])

    list(Post.filter(id__in=[7, 8], readers=[1, 2]))

    assert requests_mock.last_request.qs == {
        "id__in": ["7,8"],
        "readers": ["1", "2"],
    }


@pytest.mark.parametrize(
    "annotation",
    [
        list[User | None],
        set[User | None],
        tuple[User | None, ...],
        list[User | None] | None,
    ],
)
def test_a_collection_of_optional_relations_is_refused(
    annotation: Any,
) -> None:
    """Every item has a key, so no item is ``None``."""
    with pytest.raises(TypeError, match=r"Bad\.items.*every item has a key"):
        _declare_items(annotation)


@pytest.mark.parametrize(
    "annotation",
    [
        list[list[User]],
        list[list[list[User]]],
        list[list[set[User]]],
        list[set[User]],
        tuple[list[User], ...],
        Collection[list[User]],
        Iterable[set[User]],
        list[Collection[User]],
        list[list[User] | None],
    ],
)
def test_a_collection_of_relation_collections_is_refused(
    annotation: Any,
) -> None:
    """A collection of relations holds relations and nothing else."""
    with pytest.raises(TypeError, match=r"Bad\.items.*collections of"):
        _declare_items(annotation)


@pytest.mark.parametrize(
    "annotation",
    [
        list[User] | None,
        list[int],
        list[int | None],
        list[list[int]],
        set[str | None],
    ],
)
def test_a_collection_that_holds_no_optional_or_nested_relation_is_admitted(
    annotation: Any,
) -> None:
    """The refusals are of relations, so values and outer optionals stay."""
    _declare_items(annotation)


def _declare_items(annotation: Any) -> Any:
    """Decorate a model whose field ``items`` has the given annotation.

    Args:
        annotation: Type of the field.

    Returns:
        The decorated model.
    """
    return json_object(
        type(
            "Bad",
            (RelatedBase,),
            {
                "class_url": "/bad/",
                "__annotations__": {"id": int, "items": annotation},
            },
        )
    )


def test_a_refused_save_of_a_list_names_the_item_and_a_later_save_sends_it(
    requests_mock: Any,
) -> None:
    """The position of a bad item is named, and a mended list is one PATCH."""
    patch = requests_mock.patch("https://related.example/posts/1/", json={})
    post = Post(
        json={"id": 1, "owner": 1, "readers": [2, 3]}, write_mode="lazy"
    )
    post.readers.append("4")  # type: ignore[arg-type]

    with pytest.raises(FieldConversionError, match=r"Post\.readers: \[2\]"):
        post.save()
    assert not patch.called

    post.readers[2] = User(id=4)
    post.save()

    assert [call.json() for call in patch.request_history] == [
        {"readers": [2, 3, 4]}
    ]


def test_the_error_of_a_creation_with_a_bad_list_item_names_the_field_once(
    requests_mock: Any,
) -> None:
    """The field is named once, at the position of the item."""
    post = requests_mock.post("https://related.example/posts/", json={"id": 2})

    with pytest.raises(FieldConversionError) as error:
        Post.create(id=2, owner=1, readers=["7"])

    assert str(error.value).count("Post.readers") == 1
    assert "[0]" in str(error.value)
    assert not post.called


def test_a_key_given_as_none_through_pk_is_refused() -> None:
    """``pk=None`` names no resource, as ``id=None`` does."""
    for kwargs in ({"pk": None}, {"id": None}):
        with pytest.raises(TypeError, match=r"User\.id"):
            User(**kwargs)
