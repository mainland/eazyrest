"""Tests of the keys that a relation reads, writes, and compares."""

from __future__ import annotations

import copy
import enum
from collections.abc import Iterable, Mapping
from typing import Annotated, Any, ClassVar, Literal, NewType, TypedDict

import pytest

from eazyrest import (
    API,
    Client,
    Conversion,
    FieldConversionError,
    Hyperlinked,
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
class SubUser(User):
    """Subclass that redeclares the key field with another type."""

    id: str  # type: ignore[assignment]


@json_object
class Post(RelatedBase):
    """Model with a relation and a list of relations."""

    class_url = "/posts/"

    id: int
    owner: User
    readers: list[User]


@json_object
class Feed(RelatedBase):
    """Model whose relations are read lazily and by a bulk hook."""

    class_url = "/feeds/"

    id: int
    readers: Iterable[User]


@json_object
class Note(RelatedBase):
    """Model that names a user by a hyperlink."""

    class_url = "/notes/"

    id: int
    author: Annotated[User, Hyperlinked()]


@json_object(pk="user")
class Account(RelatedBase):
    """Model keyed by a relation."""

    class_url = "/accounts/"

    user: User
    token: str


@json_object
class Me(RelatedBase):
    """Singleton whose class names its URL."""

    id: int

    @property
    def url(self) -> str:
        """Return the fixed URL."""
        return "/me/"


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


class Color(enum.Enum):
    """Enum that a key may be."""

    RED = "red"


@json_object
class Paint(RelatedBase):
    """Model keyed by an enum, whose key can fail to decode."""

    class_url = "/paints/"

    id: Color


@json_object
class Wall(RelatedBase):
    """Model that relates to a paint."""

    class_url = "/walls/"

    id: int
    paint: Paint


@json_object
class Holder(RelatedBase):
    """Model that relates to a singleton."""

    class_url = "/holders/"

    id: int
    me: Me


def _unkeyed(key: Any) -> User:
    """Return a user that the server's JSON gives a key of another type."""
    return User(json={"id": key})


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


@pytest.mark.parametrize("key", BAD_KEYS)
def test_assigning_a_related_object_without_a_key_is_refused(
    key: Any, requests_mock: Any
) -> None:
    """The key is built as a read builds it, and nothing is queued."""
    post = _loaded_post()

    with pytest.raises(FieldConversionError, match=r"Post\.owner"):
        post.owner = _unkeyed(key)

    assert post._pending_updates == {}
    assert requests_mock.call_count == 0


@pytest.mark.parametrize("key", BAD_KEYS)
def test_create_refuses_a_related_object_without_a_key(
    key: Any, requests_mock: Any
) -> None:
    """A key that cannot be built is not sent in the body."""
    posted = requests_mock.post("https://related.example/posts/", json={})

    with pytest.raises(FieldConversionError, match=r"Post\.owner"):
        Post.create(id=2, owner=_unkeyed(key), readers=[])

    assert not posted.called


@pytest.mark.parametrize("key", BAD_KEYS)
def test_a_list_of_relations_refuses_a_related_object_without_a_key(
    key: Any, requests_mock: Any
) -> None:
    """Each item of a list relation is built as a single relation is."""
    posted = requests_mock.post("https://related.example/posts/", json={})

    with pytest.raises(FieldConversionError, match=r"Post\.readers"):
        Post.create(
            id=2, owner=User(id=1), readers=[User(id=3), _unkeyed(key)]
        )

    assert not posted.called


@pytest.mark.parametrize("key", BAD_KEYS)
def test_a_query_refuses_a_related_object_without_a_key(
    key: Any, requests_mock: Any
) -> None:
    """The query sends no key that the related model would not read."""
    requests_mock.get("https://related.example/posts/", json=[])

    with pytest.raises(FieldConversionError, match=r"Post\.owner"):
        list(Post.filter(owner=_unkeyed(key)))

    assert requests_mock.call_count == 0


def test_a_related_object_with_a_key_is_written_as_that_key(
    requests_mock: Any,
) -> None:
    """A related object whose key builds is sent as its wire key."""
    posted = requests_mock.post("https://related.example/posts/", json={})

    Post.create(id=2, owner=User(json={"id": 7}), readers=[User(id=8)])

    assert posted.last_request.json() == {"id": 2, "owner": 7, "readers": [8]}


def test_a_related_object_known_by_its_url_is_checked_after_it_loads(
    requests_mock: Any,
) -> None:
    """Reading the key loads the object, and the loaded key is built."""
    requests_mock.get(
        "https://related.example/notes/1/",
        json={"id": 1, "author": "https://related.example/users/7/"},
    )
    loaded = requests_mock.get(
        "https://related.example/users/7/", json={"id": "7"}
    )
    posted = requests_mock.post("https://related.example/posts/", json={})
    author = Note(id=1).author

    with pytest.raises(FieldConversionError, match=r"Post\.owner"):
        Post.create(id=2, owner=author, readers=[])

    assert loaded.call_count == 1
    assert not posted.called


def test_a_subclass_that_redeclares_the_key_is_refused_by_the_relation(
    requests_mock: Any,
) -> None:
    """The key is built under the relation's model, not the subclass's."""
    posted = requests_mock.post("https://related.example/posts/", json={})
    post = _loaded_post()

    with pytest.raises(FieldConversionError, match=r"Post\.owner"):
        post.owner = SubUser(id="abc")
    with pytest.raises(FieldConversionError, match=r"Post\.owner"):
        Post.create(id=2, owner=SubUser(id="abc"), readers=[])
    with pytest.raises(FieldConversionError, match=r"Post\.readers"):
        Post.create(id=2, owner=User(id=1), readers=[SubUser(id="abc")])
    with pytest.raises(FieldConversionError, match=r"Account\.user"):
        Account(user=SubUser(id="abc"))

    assert post._pending_updates == {}
    assert not posted.called


@pytest.mark.parametrize(
    "key", [{"id": 5}, {"id": {"id": 5}}, {"a": 1}, [1], {"id": 5, "x": 1}]
)
def test_a_key_relation_refuses_a_mapping_and_a_related_key_that_is_one(
    key: Any,
) -> None:
    """A mapping is no wire key, so it is not read as an embedded object."""
    with pytest.raises(FieldConversionError, match=r"Account\.user"):
        Account(user=key)
    with pytest.raises(FieldConversionError, match=r"Account\.user"):
        Account(user=User(json={"id": key}))
    account = Account(json={"token": "t"})
    with pytest.raises(FieldConversionError, match=r"Account\.user"):
        account.user = User(json={"id": key})
    assert account.pk is None


def test_a_key_relation_still_reads_an_embedded_object_from_its_json() -> None:
    """The model's own JSON may embed the related object."""
    account = Account(json={"user": {"id": 5, "name": "Ada"}, "token": "t"})

    assert account.pk == 5
    assert account == Account(user=User(id=5))


def test_a_class_that_overrides_url_refuses_an_assigned_key() -> None:
    """Its next load could name another key, which the object would refuse."""
    me = Me()

    with pytest.raises(AttributeError, match="learns its key"):
        me.id = 7

    assert me.pk is None


def test_a_class_that_overrides_url_refuses_a_key_that_cannot_be_built() -> (
    None
):
    """The key is built first, so the error names what is wrong with it."""
    with pytest.raises(FieldConversionError, match=r"Me\.id"):
        Me().id = "7"  # type: ignore[assignment]


def test_a_class_that_overrides_url_takes_its_key_as_it_is_built() -> None:
    """A URL that is formatted from the key needs the key given at once."""
    slot = Slot(id=3)
    booking = Booking(json={"id": 1, "slot": 4})

    assert slot.url == "/slots/3/detail/"
    assert booking.slot.url == "/slots/4/detail/"


@pytest.mark.parametrize(
    "json",
    [
        pytest.param({"user": [{"id": 1, "token": "s3cr3t"}]}, id="list"),
        pytest.param(
            {"user": {"id": {"token": "s3cr3t"}}}, id="mapping-of-mapping"
        ),
        pytest.param(
            {"user": {"id": [{"token": "s3cr3t"}]}}, id="list-in-map"
        ),
    ],
)
def test_repr_of_a_key_relation_hides_a_key_that_is_not_a_scalar(
    json: Any,
) -> None:
    """A log of the object holds none of the related object's fields."""
    shown = repr(Account(json=json))

    assert shown == "<Account user=...>"
    assert "s3cr3t" not in shown


def test_repr_of_a_key_relation_shows_a_string_or_an_integer_key() -> None:
    """The reduced key is shown, as it is for an embedded object."""
    assert repr(Account(json={"user": {"id": 5, "x": 1}})) == (
        "<Account user=5>"
    )
    assert repr(Account(json={"user": 5})) == "<Account user=5>"


def test_repr_of_a_key_relation_to_an_undefined_model_hides_the_value() -> (
    None
):
    """A key that cannot be reduced yet is not shown as it is."""

    @json_object(pk="later")
    class Waiting(RelatedBase):
        """Model keyed by a relation to a model that is not defined."""

        later: Later  # type: ignore[name-defined]  # noqa: F821

    shown = repr(Waiting(json={"later": {"id": 1, "token": "s3cr3t"}}))

    assert shown == "<Waiting later=...>"


def test_a_key_that_does_not_decode_is_refused_naming_the_relation(
    requests_mock: Any,
) -> None:
    """The error names the field that the program wrote, not the key's."""
    posted = requests_mock.post("https://related.example/walls/", json={})
    wall = Wall(json={"id": 1, "paint": "red"}, write_mode="lazy")
    green = Paint(json={"id": "green"})

    with pytest.raises(FieldConversionError, match=r"Wall\.paint"):
        wall.paint = green
    with pytest.raises(FieldConversionError, match=r"Wall\.paint"):
        Wall.create(id=2, paint=green)
    with pytest.raises(FieldConversionError, match=r"Wall\.paint"):
        list(Wall.filter(paint=green))

    assert requests_mock.call_count == 0
    assert not posted.called


def test_a_hyperlink_to_an_object_without_a_key_names_the_relation(
    requests_mock: Any,
) -> None:
    """The URL of the related object needs its key, and the field is named."""
    posted = requests_mock.post("https://related.example/notes/", json={})

    with pytest.raises(FieldConversionError, match=r"Note\.author"):
        Note.create(id=2, author=_unkeyed("7"))

    assert not posted.called


def test_the_error_of_a_list_relation_names_the_field_once(
    requests_mock: Any,
) -> None:
    """A read, a full save, and a write each name the relation once."""
    put = requests_mock.put("https://related.example/posts/1/", json={})
    post = Post(json={"id": 1, "owner": 2, "readers": [{"id": "7"}]})
    messages = []

    with pytest.raises(FieldConversionError) as read:
        _ = post.readers
    messages.append(str(read.value))
    with pytest.raises(FieldConversionError) as full:
        post.save(full=True)
    messages.append(str(full.value))
    with pytest.raises(FieldConversionError) as created:
        Post.create(id=2, owner=User(id=1), readers=[_unkeyed("7")])
    messages.append(str(created.value))

    assert [message.count("Post.readers") for message in messages] == [1] * 3
    assert not put.called


@pytest.mark.parametrize(
    "lookup", ["owner__in", "owner__id__in", "owner__exact"]
)
def test_a_lookup_refuses_a_related_object_without_a_key(
    lookup: str, requests_mock: Any
) -> None:
    """A lookup on a relation names the relation, as the plain one does."""
    requests_mock.get("https://related.example/posts/", json=[])

    with pytest.raises(FieldConversionError, match=r"Post\.owner"):
        list(Post.filter(params={lookup: [_unkeyed("7")]}))
    with pytest.raises(FieldConversionError, match=r"Post\.owner"):
        list(Post.filter(params={lookup: [SubUser(id="abc")]}))

    assert requests_mock.call_count == 0


def test_a_lookup_sends_the_keys_of_related_objects(
    requests_mock: Any,
) -> None:
    """A lookup writes the wire key of each related object."""
    requests_mock.get("https://related.example/posts/", json=[])

    list(Post.filter(owner__in=[User(id=1), User(json={"id": 2})]))

    assert requests_mock.last_request.qs == {"owner__in": ["1,2"]}


def test_a_singleton_that_has_not_loaded_says_to_load_or_save_it(
    requests_mock: Any,
) -> None:
    """The key of a class that overrides url may be known by loading it."""
    holder = Holder(json={"id": 1, "me": 7}, write_mode="lazy")

    with pytest.raises(FieldConversionError, match="load or save"):
        holder.me = Me()
    with pytest.raises(FieldConversionError, match="load or save"):
        Holder.create(id=2, me=Me())

    assert requests_mock.call_count == 0


@pytest.mark.parametrize("key", [True, 1.5, [1, "s3cr3t"]])
def test_repr_hides_a_key_that_is_not_a_string_or_an_integer(key: Any) -> None:
    """Neither a relation's key nor a model's own shows another JSON value."""
    assert repr(Account(json={"user": key})) == "<Account user=...>"
    assert repr(User(json={"id": key})) == "<User id=...>"


def test_repr_shows_a_missing_key_as_none() -> None:
    """A key that the JSON does not give is not hidden."""
    assert repr(User(json={"id": None})) == "<User id=None>"


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


def test_a_list_relation_reads_an_embedded_object_without_a_key() -> None:
    """A list reads an item as a single relation does, with no key."""
    post = Post(
        json={
            "id": 1,
            "owner": {"x": 1},
            "readers": [{"x": 1}, 3, {"x": 2}],
        }
    )
    feed = Feed(json={"id": 1, "readers": [{"x": 1}, 3]})

    assert repr(post.owner) == "<User id=None>"
    assert [reader.pk for reader in post.readers] == [None, 3, None]
    assert [reader.pk for reader in feed.readers] == [None, 3]


def test_a_keyless_item_of_a_list_does_not_stop_a_save_of_another_field(
    requests_mock: Any,
) -> None:
    """An item that the program did not change is not sent."""
    patch = requests_mock.patch("https://related.example/posts/1/", json={})
    post = Post(
        json={"id": 1, "owner": 2, "readers": [{"x": 1}, 3]},
        write_mode="lazy",
    )
    assert len(post.readers) == 2

    post.owner = User(id=5)
    post.save()

    assert patch.last_request.json() == {"owner": 5}


def test_a_list_that_changed_in_place_refuses_a_keyless_item_when_saved(
    requests_mock: Any,
) -> None:
    """A change to the list sends it, so every item must have a key."""
    patch = requests_mock.patch("https://related.example/posts/1/", json={})
    post = Post(
        json={"id": 1, "owner": 2, "readers": [{"x": 1}, 3]},
        write_mode="lazy",
    )
    post.readers.append(User(id=4))

    with pytest.raises(FieldConversionError, match=r"Post\.readers"):
        post.save()
    del post.readers[0]
    post.save()

    assert patch.call_count == 1
    assert patch.last_request.json() == {"readers": [3, 4]}


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


@pytest.mark.parametrize("key", BAD_BARE_KEYS)
def test_a_query_refuses_a_bare_key_that_cannot_be_built(
    key: Any, requests_mock: Any
) -> None:
    """The query sends no key that the related model would not read."""
    requests_mock.get("https://related.example/posts/", json=[])

    with pytest.raises(FieldConversionError, match=r"Post\.owner"):
        list(Post.filter(owner=[2, key]))

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


@pytest.mark.parametrize("key", ["7", True, 1.5, {"id": 5}])
def test_get_or_create_refuses_a_bare_key_before_its_lookup(
    key: Any, requests_mock: Any
) -> None:
    """A key relation refuses a key that it would refuse to read."""
    with pytest.raises(FieldConversionError, match=r"Account\.user"):
        Account.get_or_create(user=key)

    assert requests_mock.call_count == 0


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
    with pytest.raises(FieldConversionError, match=r"Post\.owner"):
        list(Post.filter(params={lookup: [7, key]}))
    with pytest.raises(FieldConversionError, match=r"Post\.readers"):
        list(Post.filter(readers__in=[key]))
    with pytest.raises(FieldConversionError, match=r"Note\.author"):
        list(Note.filter(author__in=[key]))

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

    list(Post.filter(owner__id__in=["7"], owner__name="x", owner__isnull=True))

    assert requests_mock.last_request.qs == {
        "owner__id__in": ["7"],
        "owner__name": ["x"],
        "owner__isnull": ["true"],
    }


def test_a_filter_on_a_hyperlinked_relation_builds_a_key_as_other_filters_do(
    requests_mock: Any,
) -> None:
    """A key in its field's type is sent, and one given as text is refused."""
    requests_mock.get("https://related.example/notes/", json=[])

    list(Note.filter(author=7))
    list(Note.filter(params={"author": [7]}))
    for key in ("7", True, 1.5):
        with pytest.raises(FieldConversionError, match=r"Note\.author"):
            list(Note.filter(author=key))
        with pytest.raises(FieldConversionError, match=r"Note\.author"):
            list(Note.filter(author=[7, key]))

    assert [r.qs for r in requests_mock.request_history] == [
        {"author": ["7"]},
        {"author": ["7"]},
    ]


def test_a_hyperlinked_relation_refuses_a_subclass_that_redeclares_the_key(
    requests_mock: Any,
) -> None:
    """A write checks the object as a query on the relation does."""
    posted = requests_mock.post("https://related.example/notes/", json={})
    note = Note(
        json={"id": 1, "author": "https://related.example/users/2/"},
        write_mode="lazy",
    )

    with pytest.raises(FieldConversionError, match=r"Note\.author"):
        note.author = SubUser(id="abc")
    with pytest.raises(FieldConversionError, match=r"Note\.author"):
        Note.create(id=2, author=SubUser(id="abc"))

    assert note._pending_updates == {}
    assert not posted.called


@json_object(pk="slug")
class Badge(User):
    """Subclass that chooses another key field, which holds an integer."""

    slug: int


def test_a_hyperlinked_relation_refuses_a_subclass_that_chose_another_key(
    requests_mock: Any,
) -> None:
    """The URL of such an object names no user, though its key would build."""
    posted = requests_mock.post("https://related.example/notes/", json={})
    note = Note(
        json={"id": 1, "author": "https://related.example/users/2/"},
        write_mode="lazy",
    )

    with pytest.raises(FieldConversionError, match=r"Note\.author"):
        note.author = Badge(json={"id": 1, "slug": 7})
    with pytest.raises(FieldConversionError, match=r"Note\.author"):
        Note.create(id=2, author=Badge(json={"id": 1, "slug": 7}))

    assert note._pending_updates == {}
    assert not posted.called


@json_object
class SubNote(RelatedBase):
    """Model that names a user of a subclass that redeclares the key."""

    class_url = "/subnotes/"

    id: int
    author: Annotated[SubUser, Hyperlinked()]
    authors: list[Annotated[SubUser, Hyperlinked()]]


@json_object
class Roster(RelatedBase):
    """Model that names users by hyperlinks in a list."""

    class_url = "/rosters/"

    id: int
    members: list[Annotated[User, Hyperlinked()]]


def test_a_hyperlinked_relation_refuses_a_subclass_known_only_by_its_url(
    requests_mock: Any,
) -> None:
    """The class says its key is not the related model's, so no load."""
    posted = requests_mock.post("https://related.example/notes/", json={})
    listed = requests_mock.post("https://related.example/rosters/", json={})
    sub = SubNote(
        json={
            "id": 1,
            "author": "https://related.example/users/abc/",
            "authors": ["https://related.example/users/abc/"],
        }
    ).author
    note = Note(
        json={"id": 1, "author": "https://related.example/users/2/"},
        write_mode="lazy",
    )

    with pytest.raises(FieldConversionError, match=r"Note\.author"):
        note.author = sub
    with pytest.raises(FieldConversionError, match=r"Note\.author"):
        Note.create(id=2, author=sub)
    with pytest.raises(FieldConversionError, match=r"Roster\.members"):
        Roster.create(id=2, members=[sub])

    assert note._pending_updates == {}
    assert not posted.called
    assert not listed.called
    assert requests_mock.call_count == 0


@pytest.mark.parametrize(
    "url", ["https://elsewhere.example/users/2/", "http://[bad]/users/2/"]
)
def test_a_hyperlinked_relation_refuses_a_url_that_reading_refuses(
    url: Any, requests_mock: Any
) -> None:
    """A string is checked as the field's read checks it, before a request."""
    posted = requests_mock.post("https://related.example/notes/", json={})
    note = Note(
        json={"id": 1, "author": "https://related.example/users/2/"},
        write_mode="lazy",
    )

    with pytest.raises(FieldConversionError, match=r"Note\.author"):
        note.author = url
    with pytest.raises(FieldConversionError, match=r"Note\.author"):
        Note.create(id=2, author=url)
    with pytest.raises(FieldConversionError, match=r"Roster\.members"):
        Roster.create(id=2, members=["https://related.example/users/1/", url])
    with pytest.raises(FieldConversionError, match=r"Note\.author"):
        _ = Note(json={"id": 1, "author": url}).author

    assert note._pending_updates == {}
    assert not posted.called
    assert requests_mock.call_count == 0


def test_a_hyperlinked_relation_writes_a_url_that_reading_accepts(
    requests_mock: Any,
) -> None:
    """A URL of the API's origin is written as given, absolute or relative."""
    posted = requests_mock.post("https://related.example/notes/", json={})

    Note.create(id=2, author="https://related.example/users/3/")
    Note.create(id=3, author="/users/4/")

    assert [r.json()["author"] for r in requests_mock.request_history] == [
        "https://related.example/users/3/",
        "/users/4/",
    ]
    assert posted.call_count == 2


def test_a_hyperlinked_relation_writes_the_url_of_an_object_with_a_key(
    requests_mock: Any,
) -> None:
    """An object that the relation's model reads is written as its URL."""
    posted = requests_mock.post("https://related.example/notes/", json={})

    Note.create(id=2, author=User(id=7))

    assert posted.last_request.json() == {
        "id": 2,
        "author": "https://related.example/users/7/",
    }


def test_a_hyperlinked_relation_writes_an_object_known_only_by_its_url(
    requests_mock: Any,
) -> None:
    """The URL needs no key, so checking the key does not load the object."""
    posted = requests_mock.post("https://related.example/notes/", json={})
    author = Note(
        json={"id": 1, "author": "https://related.example/users/7/"}
    ).author

    Note.create(id=2, author=author)

    assert posted.last_request.json()["author"] == (
        "https://related.example/users/7/"
    )
    assert requests_mock.call_count == 1


hook_calls: list[tuple[set[Any], int]] = []
"""The keys that a bulk hook received, with the decodes done by then."""

decoded_slugs: list[Any] = []
"""The values that the slug conversion decoded."""

Slug = NewType("Slug", str)


def _decode_slug(value: Any, _: Any) -> Slug:
    """Decode a slug, recording that the key was built."""
    decoded_slugs.append(value)
    return Slug(value)


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
        hook_calls.append((set(pks), len(decoded_slugs)))
        return {pk: cls(json={"id": pk}, api=api) for pk in pks}


@json_object
class Label(RelatedBase):
    """Model keyed by a slug whose decodes are counted."""

    class_url = "/labels/"
    conversions: ClassVar[Mapping[Any, Conversion]] = {
        Slug: Conversion(
            decode=_decode_slug, encode=lambda value, _: str(value)
        )
    }

    id: Slug

    @classmethod
    def bulk_get_by_pks(
        cls, pks: set[Any], *, api: Client | None = None, **kwargs: Any
    ) -> dict[Any, Label]:
        """Return a label for each key, after recording the keys."""
        hook_calls.append((set(pks), len(decoded_slugs)))
        return {pk: cls(json={"id": pk}, api=api) for pk in pks}


Mark = NewType("Mark", str)


@json_object
class Stamp(RelatedBase):
    """Model keyed by a mark that its codec encodes as None for one value."""

    class_url = "/stamps/"
    conversions: ClassVar[Mapping[Any, Conversion]] = {
        Mark: Conversion(
            decode=lambda value, _: Mark(value),
            encode=lambda value, _: None if value == "gone" else str(value),
        )
    }

    id: Mark

    @classmethod
    def bulk_get_by_pks(
        cls, pks: set[Any], *, api: Client | None = None, **kwargs: Any
    ) -> dict[Any, Stamp]:
        """Return a stamp for each key, after recording the keys."""
        hook_calls.append((set(pks), len(decoded_slugs)))
        return {pk: cls(json={"id": pk}, api=api) for pk in pks}


class Coords(TypedDict):
    """Mapping that a codec writes as a key."""

    x: int
    y: int


@json_object
class Plot(RelatedBase):
    """Model keyed by a mapping that its codec writes as text."""

    class_url = "/plots/"
    conversions: ClassVar[Mapping[Any, Conversion]] = {
        Coords: Conversion(
            decode=lambda value, _: {
                "x": int(value.split("-")[0]),
                "y": int(value.split("-")[1]),
            },
            encode=lambda value, _: f"{value['x']}-{value['y']}",
        )
    }

    id: Coords


@json_object
class Article(RelatedBase):
    """Model with relations that prefetch loads in bulk."""

    class_url = "/articles/"

    id: int
    writer: Writer
    editors: list[Writer]
    label: Label
    stamp: Stamp
    plot: Plot


@pytest.fixture(autouse=True)
def _clear_records() -> None:
    """Start each test with no recorded hook calls or decodes."""
    hook_calls.clear()
    decoded_slugs.clear()


def _article(**fields: Any) -> dict[str, Any]:
    """Return an article's JSON with valid relations except the given."""
    return {
        "id": 1,
        "writer": 1,
        "editors": [],
        "label": "a",
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

    assert hook_calls == [({1}, 0), ({1, 2, 3}, 0)]
    assert [writer.pk for writer in articles[0].editors] == [2, 1]


def test_prefetch_builds_each_distinct_key_once_before_the_hook(
    requests_mock: Any,
) -> None:
    """The key constructor runs once for each distinct value."""
    requests_mock.get(
        "https://related.example/articles/",
        json=[
            _article(label="a"),
            _article(id=2, label="a"),
            _article(id=3, label="b"),
        ],
    )

    list(Article.filter(prefetch=["label"]))

    assert hook_calls[0][0] == {"a", "b"}
    assert hook_calls[0][1] == 2


@json_object
class Lit(RelatedBase):
    """Model keyed by one of two strings."""

    class_url = "/letters/"

    id: Literal["a", "b"]


@json_object
class LitRef(RelatedBase):
    """Model that relates to a model keyed by a literal."""

    class_url = "/litrefs/"

    id: int
    lit: Lit


@json_object
class Shade(RelatedBase):
    """Model keyed by one member of an enum, and an integer."""

    class_url = "/shades/"

    id: Literal[Color.RED, 3]


def test_a_literal_key_refuses_a_value_that_is_not_a_member() -> None:
    """The key's value names a URL, so it must be one that the type lists."""
    with pytest.raises(FieldConversionError, match=r"Lit\.id"):
        Lit(id="c")
    with pytest.raises(FieldConversionError, match=r"Lit\.id"):
        _ = Lit(json={"id": "c"}).url
    with pytest.raises(TypeError, match="cannot be read from its JSON"):
        hash(Lit(json={"id": "c"}))
    assert Lit(id="a").url == "/letters/a/"
    assert Lit(json={"id": "b"}).url == "/letters/b/"


def test_a_literal_key_checks_the_members_of_an_enum_and_an_integer() -> None:
    """A member is compared by its wire value and its JSON type."""
    assert Shade(json={"id": "red"}).url == "/shades/red/"
    assert Shade(json={"id": 3}).url == "/shades/3/"
    for key in ("blue", 4, "3", True):
        with pytest.raises(FieldConversionError, match=r"Shade\.id"):
            _ = Shade(json={"id": key}).url


def test_a_relation_refuses_a_literal_key_that_is_not_a_member(
    requests_mock: Any,
) -> None:
    """A read, a write, and a query name the relation, before any request."""
    posted = requests_mock.post("https://related.example/litrefs/", json={})
    outsider = Lit(json={"id": "c"})

    with pytest.raises(FieldConversionError, match=r"LitRef\.lit"):
        _ = LitRef(json={"id": 1, "lit": "c"}).lit
    with pytest.raises(FieldConversionError, match=r"LitRef\.lit"):
        LitRef.create(id=1, lit=outsider)
    with pytest.raises(FieldConversionError, match=r"LitRef\.lit"):
        list(LitRef.filter(lit=outsider))
    with pytest.raises(FieldConversionError, match=r"LitRef\.lit"):
        LitRef.create(id=1, lit="c")

    assert requests_mock.call_count == 0
    assert not posted.called


def test_a_relation_writes_a_literal_key_that_is_a_member(
    requests_mock: Any,
) -> None:
    """A member is written as the wire key."""
    posted = requests_mock.post("https://related.example/litrefs/", json={})

    LitRef.create(id=1, lit=Lit(id="a"))
    LitRef.create(id=2, lit="b")

    assert [r.json()["lit"] for r in requests_mock.request_history] == [
        "a",
        "b",
    ]
    assert posted.call_count == 2


def test_prefetch_names_the_field_of_a_value_that_builds_no_key(
    requests_mock: Any,
) -> None:
    """A key that its codec writes as None is no key."""
    requests_mock.get(
        "https://related.example/articles/",
        json=[_article(stamp="gone")],
    )

    with pytest.raises(
        FieldConversionError, match=r"Article\.stamp.*writes no key"
    ):
        list(Article.filter(prefetch=["stamp"]))

    assert hook_calls == []


def test_a_relation_refuses_a_mapping_that_a_key_conversion_would_encode(
    requests_mock: Any,
) -> None:
    """A mapping is no key even for a codec that writes any value as text."""
    posted = requests_mock.post("https://related.example/articles/", json={})

    key = {"x": 1, "y": 2}

    with pytest.raises(FieldConversionError, match=r"Article\.plot"):
        Article.create(id=1, writer=1, editors=[], plot=key)
    with pytest.raises(FieldConversionError, match=r"Article\.plot"):
        list(Article.filter(plot=key))

    assert not posted.called
    assert requests_mock.call_count == 0


UserNumber = NewType("UserNumber", int)


@json_object
class NumberedUser(User):
    """Subclass whose key is a ``NewType`` of the related model's key."""

    id: UserNumber


@json_object
class NotedUser(User):
    """Subclass whose key is an ``Annotated`` of the related model's key."""

    id: Annotated[int, "a note"]


@json_object
class ChosenUser(User):
    """Subclass whose key is a ``Literal`` of keys that ``User`` builds."""

    id: Literal[1, 2]


@json_object
class LabeledUser(User):
    """Subclass whose key is a ``Literal`` that ``User`` cannot build."""

    id: Literal["a"]  # type: ignore[assignment]


@json_object
class Seat(RelatedBase):
    """Model keyed by a ``Literal``."""

    class_url = "/seats/"

    id: Literal[1, 2, 3]


@json_object
class AnySeat(Seat):
    """Subclass whose key allows more than ``Seat``'s."""

    id: Literal[1, 2, 3, 4]  # type: ignore[assignment]


@json_object
class FrontSeat(Seat):
    """Subclass whose key is a ``Literal`` of some of ``Seat``'s members."""

    id: Literal[1, 2]


@json_object
class IntSeat(Seat):
    """Subclass whose key is any int."""

    id: int  # type: ignore[assignment]


@json_object
class NumberedNote(RelatedBase):
    """Models that name each kind of user by a hyperlink."""

    class_url = "/numbered-notes/"

    id: int
    numbered: Annotated[NumberedUser, Hyperlinked()]
    noted: Annotated[NotedUser, Hyperlinked()]
    chosen: Annotated[ChosenUser, Hyperlinked()]
    labeled: Annotated[LabeledUser, Hyperlinked()]
    any_seat: Annotated[AnySeat, Hyperlinked()]
    front_seat: Annotated[FrontSeat, Hyperlinked()]
    int_seat: Annotated[IntSeat, Hyperlinked()]


@json_object
class SeatNote(RelatedBase):
    """Model that names a seat by a hyperlink."""

    class_url = "/seat-notes/"

    id: int
    seat: Annotated[Seat, Hyperlinked()]


def _by_url(field: str, path: str) -> Any:
    """Return an object that is known only by its URL.

    Args:
        field: Field of ``NumberedNote`` that names it.
        path: Path of its URL.

    Returns:
        The object.
    """
    url = f"https://related.example/{path}/"
    return getattr(NumberedNote(json={"id": 1, field: url}), field)


@pytest.mark.parametrize(
    ("field", "path"),
    [
        ("numbered", "users/1"),
        ("noted", "users/1"),
        ("chosen", "users/1"),
    ],
)
def test_a_hyperlinked_write_takes_a_key_type_that_the_related_model_builds(
    field: str, path: str, requests_mock: Any
) -> None:
    """A NewType, an Annotated, or a Literal of keys is written unloaded."""
    posted = requests_mock.post("https://related.example/notes/", json={})
    other = _by_url(field, path)

    note = Note.create(id=2, author=other)

    assert posted.last_request.json() == {
        "id": 2,
        "author": "https://related.example/users/1/",
    }
    assert note is not None
    assert requests_mock.call_count == 1


def test_a_hyperlinked_write_refuses_a_key_type_that_the_model_cannot_build(
    requests_mock: Any,
) -> None:
    """A Literal with a member that the related model refuses stays refused."""
    posted = requests_mock.post("https://related.example/notes/", json={})
    other = _by_url("labeled", "users/a")

    with pytest.raises(FieldConversionError, match=r"Note\.author"):
        Note.create(id=2, author=other)

    assert not posted.called


@pytest.mark.parametrize(
    ("field", "accepted"), [("front_seat", True), ("any_seat", False)]
)
def test_a_hyperlinked_write_checks_each_member_of_a_literal_key(
    field: str, accepted: bool, requests_mock: Any
) -> None:
    """A subclass's Literal is accepted only if every member is built."""
    posted = requests_mock.post("https://related.example/seat-notes/", json={})
    other = _by_url(field, "seats/1")

    if accepted:
        SeatNote.create(id=2, seat=other)
        assert posted.called
    else:
        with pytest.raises(FieldConversionError, match=r"SeatNote\.seat"):
            SeatNote.create(id=2, seat=other)
        assert not posted.called


def test_a_hyperlinked_write_refuses_a_wider_key_than_a_literal_builds(
    requests_mock: Any,
) -> None:
    """An int key is not built by a model whose key is a Literal."""
    posted = requests_mock.post("https://related.example/seat-notes/", json={})

    other = _by_url("int_seat", "seats/1")

    with pytest.raises(FieldConversionError, match=r"SeatNote\.seat"):
        SeatNote.create(id=2, seat=other)

    assert not posted.called


class BareBase(JSONObject):
    """Models with no registered API, used only through ``api=``."""


@json_object
class BareUser(BareBase):
    """Model of a hierarchy that registers no API."""

    class_url = "/users/"

    id: int


@json_object
class BareNote(BareBase):
    """Model that names a user by a hyperlink, in an unregistered hierarchy."""

    class_url = "/notes/"

    id: int
    author: Annotated[BareUser, Hyperlinked()]


TENANT = API("https://tenant.example/")
"""An API on another origin than the registered one."""


def test_a_hyperlink_is_written_back_to_the_api_that_read_it(
    requests_mock: Any,
) -> None:
    """An object loaded with ``api=`` writes the URL its field read."""
    patched = requests_mock.patch("https://tenant.example/notes/1/", json={})
    note = Note(
        json={"id": 1, "author": "https://tenant.example/users/2/"},
        api=TENANT,
        write_mode="lazy",
    )

    note.author = note.author.absolute_url  # type: ignore[assignment]
    note.author = "https://tenant.example/users/3/"  # type: ignore[assignment]
    note.save()

    assert patched.last_request.json() == {
        "author": "https://tenant.example/users/3/"
    }


def test_a_hyperlink_is_checked_against_the_api_of_its_owner(
    requests_mock: Any,
) -> None:
    """The registered API's origin is another origin for such an object."""
    note = Note(
        json={"id": 1, "author": "https://tenant.example/users/2/"},
        api=TENANT,
        write_mode="lazy",
    )

    with pytest.raises(FieldConversionError, match=r"Note\.author"):
        note.author = "https://related.example/users/3/"  # type: ignore[assignment]

    assert note._pending_updates == {}
    assert requests_mock.call_count == 0


def test_create_checks_a_hyperlink_against_the_api_it_is_given(
    requests_mock: Any,
) -> None:
    """``create(api=...)`` writes a URL on that API's origin."""
    posted = requests_mock.post("https://tenant.example/notes/", json={})

    Note.create(id=2, author="https://tenant.example/users/3/", api=TENANT)
    with pytest.raises(FieldConversionError, match=r"Note\.author"):
        Note.create(
            id=3, author="https://related.example/users/3/", api=TENANT
        )

    assert posted.call_count == 1


def test_a_hyperlink_is_written_in_a_hierarchy_with_no_registered_api(
    requests_mock: Any,
) -> None:
    """A relative URL needs the API of the request, not the related one."""
    posted = requests_mock.post("https://tenant.example/notes/", json={})
    patched = requests_mock.patch("https://tenant.example/notes/1/", json={})

    BareNote.create(id=2, author="/users/3/", api=TENANT)
    BareNote.create(id=3, author="https://tenant.example/users/4/", api=TENANT)
    note = BareNote(
        json={"id": 1, "author": "https://tenant.example/users/2/"},
        api=TENANT,
        write_mode="lazy",
    )
    note.author = note.author.absolute_url  # type: ignore[assignment]
    note.author = "/users/5/"  # type: ignore[assignment]
    note.save()

    assert [r.json()["author"] for r in posted.request_history] == [
        "/users/3/",
        "https://tenant.example/users/4/",
    ]
    assert patched.last_request.json() == {"author": "/users/5/"}


def _post_with_keyless_reader() -> Post:
    """Return a lazy post whose first reader has no key."""
    post = Post(
        json={"id": 1, "owner": 2, "readers": [{"name": "x"}, 3]},
        write_mode="lazy",
    )
    assert len(post.readers) == 2
    return post


def test_replacing_a_keyless_item_with_another_is_a_change(
    requests_mock: Any,
) -> None:
    """The new item has no key either, but it is not the old one."""
    requests_mock.patch("https://related.example/posts/1/", json={})
    # An object freed here may give its address to the next one, so repeat.
    for _ in range(200):
        post = Post(
            json={"id": 1, "owner": 2, "readers": [{"name": "x"}]},
            write_mode="lazy",
        )
        post.readers.pop()
        post.readers.append(User(json={"name": "y"}))

        with pytest.raises(FieldConversionError, match=r"Post\.readers"):
            post.save()


def test_an_unchanged_copy_of_an_object_with_a_keyless_item_saves(
    requests_mock: Any,
) -> None:
    """A copy keeps the stand-ins that its snapshot holds."""
    patch = requests_mock.patch("https://related.example/posts/1/", json={})
    post = _post_with_keyless_reader()
    copied = copy.deepcopy(post)

    copied.owner = User(id=5)
    copied.save()

    assert patch.last_request.json() == {"owner": 5}


def test_two_keyless_items_differ_in_the_change_check(
    requests_mock: Any,
) -> None:
    """The stand-in names the object, so swapping two items is a change."""
    requests_mock.patch("https://related.example/posts/1/", json={})
    post = Post(
        json={"id": 1, "owner": 2, "readers": [{"name": "x"}, {"name": "y"}]},
        write_mode="lazy",
    )
    assert len(post.readers) == 2
    post.readers.reverse()

    with pytest.raises(FieldConversionError, match=r"Post\.readers"):
        post.save()
