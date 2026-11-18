"""Tests of the keys that a relation reads, writes, and compares."""

from __future__ import annotations

import enum
from typing import Annotated, Any

import pytest

from eazyrest import (
    API,
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

    assert requests_mock.last_request.qs == {"owner__in": ["1", "2"]}


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
