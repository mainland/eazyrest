"""Regression tests for fixed defects."""

from __future__ import annotations

import datetime
import io
from typing import Annotated, Any

import pytest

from eazyrest import (
    API,
    APIError,
    FieldConversionError,
    Hyperlinked,
    JSONObject,
    LimitExceeded,
    RedirectError,
    ResponseShapeError,
    WriteMode,
    json_object,
)


class MockedBase(JSONObject):
    """Models served by the mocked API."""


MockedBase.register_api(API("https://mocked.example/"))


@json_object
class MockedUser(MockedBase):
    """Related resource with explicit bulk lookup support."""

    class_url = "/users/"

    id: int
    name: str

    @classmethod
    def bulk_get_by_pks(
        cls, pks: set[Any], *, api: API | None = None
    ) -> dict[Any, MockedUser]:
        """Return loaded users for the requested keys.

        Args:
            pks: Keys to load.
            api: Query API override.

        Returns:
            Users keyed by their primary keys.
        """
        return {pk: cls(json={"id": pk, "name": "Ada"}, api=api) for pk in pks}


@json_object(write_only={"password"}, write_map={"owner": "owner_id"})
class MockedDoc(MockedBase):
    """Resource combining the new write and relation features."""

    class_url = "/docs/"

    id: int
    title: str
    body: str
    tags: list[str]
    owner: MockedUser
    linked: Annotated[MockedUser, Hyperlinked()]
    password: str


DOC_URL = "https://mocked.example/docs/1/"
DOC_JSON = {
    "id": 1,
    "title": "old",
    "body": "keep",
    "tags": ["a"],
    "owner": 2,
    "linked": "https://mocked.example/users/2/",
}


@pytest.mark.parametrize("source", ["push", "create", "write-only"])
def test_full_save_completes_partial_state(
    requests_mock: Any, source: str
) -> None:
    """Replacement writes load fields unknown to a partial object.

    Args:
        source: Operation that established the partial state.
    """
    detail = requests_mock.get(
        DOC_URL,
        json={**DOC_JSON, "title": "new" if source == "create" else "old"},
    )
    put = requests_mock.put(DOC_URL, status_code=204)
    if source == "create":
        requests_mock.post("https://mocked.example/docs/", json={"id": 1})
        doc = MockedDoc.create(title="new")
    else:
        doc = MockedDoc(id=1)
        if source == "push":
            doc.update_from_json({"title": "old"})
            doc.title = "new"
        else:
            requests_mock.patch(DOC_URL, json={"id": 1})
            doc.password = "dummy"
            doc.save()

    doc.save(full=True)

    assert detail.call_count == 1
    assert put.last_request.json() == {
        "id": 1,
        "title": "old" if source == "write-only" else "new",
        "body": "keep",
        "tags": ["a"],
        "owner_id": 2,
        "linked": "https://mocked.example/users/2/",
    }
    assert doc._pending_updates == {}


@pytest.mark.parametrize("operation", ["create", "save", "full", "delete"])
@pytest.mark.parametrize("status", [300, 307, 308])
def test_model_writes_reject_unconfirmed_redirects(
    requests_mock: Any, operation: str, status: int
) -> None:
    """An unfollowed response cannot acknowledge a model write.

    Args:
        operation: Model write to perform.
        status: Non-success final status.
    """
    method = {
        "create": "POST",
        "save": "PATCH",
        "full": "PUT",
        "delete": "DELETE",
    }[operation]
    url = "https://mocked.example/docs/" if operation == "create" else DOC_URL
    requests_mock.register_uri(
        method, url, status_code=status, headers={"Location": "/elsewhere/"}
    )
    doc = MockedDoc(json=DOC_JSON)
    doc.title = "new"
    options = {"allow_redirects": False}

    def write() -> None:
        """Perform the requested operation."""
        if operation == "create":
            MockedDoc.create(title="new", request_options=options)
        elif operation == "delete":
            doc.delete(request_options=options)
        else:
            doc.save(full=operation == "full", request_options=options)

    with pytest.raises(RedirectError):
        write()

    assert doc._pending_updates == {"title": "new"}
    assert requests_mock.call_count == 1


@pytest.mark.parametrize("operation", ["create", "save", "delete"])
def test_model_writes_refuse_streaming(
    requests_mock: Any, operation: str
) -> None:
    """Model operations consume responses themselves and must keep limits.

    Args:
        operation: Model write to perform.
    """
    doc = MockedDoc(json=DOC_JSON)
    doc.title = "new"

    def write() -> None:
        """Perform the requested operation."""
        if operation == "create":
            MockedDoc.create(title="new", request_options={"stream": True})
        elif operation == "delete":
            doc.delete(request_options={"stream": True})
        else:
            doc.save(request_options={"stream": True})

    with pytest.raises(TypeError, match="stream"):
        write()

    assert not requests_mock.called
    assert doc._pending_updates == {"title": "new"}


def test_empty_multipart_list_is_rejected_before_sending(
    requests_mock: Any,
) -> None:
    """An empty list cannot silently disappear from a multipart update."""
    doc = MockedDoc(json=DOC_JSON)
    doc.tags = []

    with pytest.raises(TypeError, match="tags"):
        doc.save(files={"image": ("dummy.txt", b"dummy")})

    assert not requests_mock.called
    assert doc._pending_updates == {"tags": []}


@pytest.mark.parametrize("status", [307, 308])
def test_multipart_redirect_resends_the_prepared_body(
    requests_mock: Any, status: int
) -> None:
    """Redirects preserve uploads prepared from a file's current position."""
    requests_mock.post(
        "https://mocked.example/docs/",
        status_code=status,
        headers={"Location": "/created/"},
    )
    requests_mock.post("https://mocked.example/created/", json={"id": 1})
    upload = io.BytesIO(b"skipHELLO")
    upload.seek(4)

    MockedDoc.create(title="new", files={"image": ("test.txt", upload)})

    first, redirected = requests_mock.request_history
    assert first.body == redirected.body
    assert b"HELLO" in redirected.body
    assert b"skipHELLO" not in redirected.body


def test_create_rejects_colliding_write_names(requests_mock: Any) -> None:
    """Mapped fields and raw wire names must not overwrite one another."""
    with pytest.raises(TypeError, match="owner_id"):
        MockedDoc.create(owner=MockedUser(id=2), owner_id=3)

    assert not requests_mock.called


def test_query_rejects_colliding_read_names(requests_mock: Any) -> None:
    """A raw alias must not replace a converted query constraint."""

    @json_object(field_map={"owner": "owner_id"})
    class Mapped(MockedDoc):
        owner: MockedUser

    with pytest.raises(TypeError, match="owner_id"):
        Mapped.filter(owner=MockedUser(id=2), owner_id=3)

    assert not requests_mock.called


@pytest.mark.parametrize("value", [2, "2", MockedUser(id=2)])
def test_hyperlinked_queries_use_primary_keys(
    requests_mock: Any, value: Any
) -> None:
    """DRF filters use keys independently of hyperlinked write values.

    Args:
        value: Key or related resource to filter on.
    """
    query = requests_mock.get("https://mocked.example/docs/", json=[])

    list(MockedDoc.filter(linked=value))

    assert query.last_request.qs == {"linked": ["2"]}


def test_non_field_queries_encode_model_keys(requests_mock: Any) -> None:
    """Lookup syntax must not send a model's display representation."""
    query = requests_mock.get("https://mocked.example/docs/", json=[])

    list(MockedDoc.filter(owner__in=[MockedUser(id=2), MockedUser(id=3)]))

    assert query.last_request.qs == {"owner__in": ["2", "3"]}


def test_redeclared_fields_keep_their_options() -> None:
    """Narrowing or repeating an annotation preserves its wire policy."""

    @json_object(
        field_map={"owner": "user"},
        write_map={"owner": "user_id"},
        write_only={"password"},
    )
    class Parent(MockedBase):
        id: int
        owner: MockedUser
        password: str

    @json_object
    class Child(Parent):
        owner: MockedUser
        password: str

    child = Child(json={"id": 1, "user": 2, "password": "dummy"})

    assert child.owner.pk == 2
    assert Child.owner.json_field == "user"  # type: ignore[attr-defined]
    assert Child.owner.write_field == "user_id"  # type: ignore[attr-defined]
    assert "password" not in child.json
    with pytest.raises(AttributeError, match="write-only"):
        _ = child.password


def test_redeclared_field_can_explicitly_replace_its_policy() -> None:
    """An explicit set or mapping overrides a redeclared field's policy."""

    @json_object(write_only=set(), write_map={"owner": "owner"})
    class Child(MockedDoc):
        password: str
        owner: MockedUser

    child = Child(json={**DOC_JSON, "password": "dummy"})

    assert child.password == "dummy"
    assert Child.owner.write_field == "owner"  # type: ignore[attr-defined]


def test_read_only_fields_are_omitted_from_full_updates(
    requests_mock: Any,
) -> None:
    """File URLs and computed values remain readable without being resent."""

    @json_object(read_only={"image", "id"})
    class Image(MockedBase):
        class_url = "/images/"

        id: int
        title: str
        image: str

    payload = {"id": 1, "title": "old", "image": "/media/file.txt"}
    requests_mock.get("https://mocked.example/images/1/", json=payload)
    put = requests_mock.put(
        "https://mocked.example/images/1/", status_code=204
    )
    image = Image(id=1)

    image.save(full=True)

    assert put.last_request.json() == {"title": "old"}
    assert image.image == "/media/file.txt"
    with pytest.raises(AttributeError, match="read-only"):
        image.image = "/media/other.txt"
    with pytest.raises(AttributeError, match="read-only"):
        Image.create(image="/media/other.txt")

    requests_mock.patch("https://mocked.example/images/1/", status_code=204)
    image.save(files={"image": ("new.txt", b"upload")})
    assert b"upload" in requests_mock.last_request.body


def test_read_only_value_changed_in_place_is_not_sent(
    requests_mock: Any,
) -> None:
    """A read-only value changed in place does not block later writes.

    It used to be queued, and then every ``save()`` raised
    ``AttributeError``, whatever else it sent.
    """

    @json_object(read_only={"tags"})
    class Tagged(MockedBase):
        class_url = "/tagged/"

        id: int
        title: str
        tags: list[str]

    patch = requests_mock.patch(
        "https://mocked.example/tagged/1/", status_code=204
    )
    tagged = Tagged(json={"id": 1, "title": "old", "tags": ["a"]})

    tagged.tags.append("b")
    tagged.save()
    tagged.title = "new"
    tagged.save()

    assert patch.call_count == 1
    assert patch.last_request.json() == {"title": "new"}


@pytest.mark.parametrize("mode", ["lazy", "eager"])
def test_hyperlink_key_assignment_updates_existing_identity(
    requests_mock: Any, mode: WriteMode
) -> None:
    """A URL identifies an existing resource before its key is loaded.

    Args:
        mode: Write mode of the parent and related user.
    """
    requests_mock.get(
        "https://mocked.example/users/2/", json={"id": 2, "name": "Ada"}
    )
    patch = requests_mock.patch(
        "https://mocked.example/users/2/", status_code=204
    )
    user = MockedDoc(json=DOC_JSON, write_mode=mode).linked

    user.id = 3
    user.save()

    assert patch.last_request.json() == {"id": 3}
    assert user.pk == 3
    assert user.absolute_url == "https://mocked.example/users/3/"


def test_hyperlink_accepts_pushed_identity_without_loading(
    requests_mock: Any,
) -> None:
    """An authoritative push can establish a URL-only object's key."""
    user = MockedDoc(json=DOC_JSON).linked

    user.update_from_json({"id": 2, "name": "Ada"})

    assert user.pk == 2
    assert user.name == "Ada"
    assert not requests_mock.called


@pytest.mark.parametrize(
    "source", ["constructor", "push", "load", "create", "save"]
)
def test_write_only_values_never_enter_json(
    requests_mock: Any, source: str
) -> None:
    """Both response and caller JSON exclude declared write-only values.

    Args:
        source: Source of JSON containing a write-only member.
    """
    payload = {**DOC_JSON, "password": "dummy"}
    if source == "constructor":
        doc = MockedDoc(json=payload)
    elif source == "create":
        requests_mock.post("https://mocked.example/docs/", json=payload)
        doc = MockedDoc.create(title="old", password="dummy")
    elif source == "load":
        requests_mock.get(DOC_URL, json=payload)
        doc = MockedDoc(id=1)
    else:
        doc = MockedDoc(json=DOC_JSON)
        if source == "push":
            doc.update_from_json(payload)
        else:
            requests_mock.patch(DOC_URL, json=payload)
            doc.title = "new"
            doc.save()

    assert "password" not in doc.json
    assert "password" in payload


@pytest.mark.parametrize(
    "body", ["NaN", "Infinity", "-Infinity", "1e999", "9" * 5000]
)
def test_unrepresentable_json_keeps_the_error_boundary(
    requests_mock: Any, body: str
) -> None:
    """Invalid numeric responses use the public response/status errors.

    Args:
        body: Number that cannot be decoded as supported JSON.
    """
    requests_mock.get(DOC_URL, text='{"id":1,"tags":[' + body + "]}")
    with pytest.raises(ResponseShapeError):
        _ = MockedDoc(id=1).tags

    requests_mock.get(DOC_URL, status_code=400, text='{"error":' + body + "}")
    with pytest.raises(APIError) as info:
        MockedDoc.api.get(DOC_URL)
    assert info.value.data is None


@pytest.mark.parametrize("prefetch", [None, "owner"])
@pytest.mark.parametrize("item", [1, "ab", [["id", 1]]])
def test_collection_items_must_be_objects(
    requests_mock: Any, prefetch: str | None, item: Any
) -> None:
    """A collection cannot coerce a scalar or pairs into a resource.

    Args:
        prefetch: Whether to prefetch a related field.
        item: Malformed collection member.
    """
    requests_mock.get("https://mocked.example/docs/", json=[item])

    with pytest.raises(ResponseShapeError):
        list(MockedDoc.filter(prefetch=prefetch))


def test_unhashable_prefetch_key_is_a_shape_error(requests_mock: Any) -> None:
    """Malformed related keys fail at the response boundary."""
    requests_mock.get(
        "https://mocked.example/docs/", json=[{"id": 1, "owner": [2]}]
    )

    with pytest.raises(ResponseShapeError):
        list(MockedDoc.filter(prefetch="owner"))


def test_error_body_depth_limit_is_not_silently_lost(
    requests_mock: Any,
) -> None:
    """HTTP error parsing preserves an explicit depth-limit violation."""
    api = API("https://mocked.example/")
    api.max_json_depth = 1
    requests_mock.get(DOC_URL, status_code=400, json={"field": ["bad"]})

    with pytest.raises(LimitExceeded, match="max_json_depth"):
        api.get(DOC_URL)


@pytest.mark.parametrize(
    "value", ["20260401", "2026-W14-3", "2026-04-01T00:00:00"]
)
def test_date_strings_use_the_same_format_on_every_python(value: str) -> None:
    """Basic and week dates cannot depend on the interpreter's parser."""

    @json_object
    class Dated(MockedBase):
        id: int
        date: datetime.date

    dated = Dated(json={"id": 1, "date": value})

    with pytest.raises(FieldConversionError):
        _ = dated.date


def test_hyperlink_refresh_does_not_load_unknown_identity(
    requests_mock: Any,
) -> None:
    """Discarding a URL-only object's cache never requires a request."""
    user = MockedDoc(json=DOC_JSON).linked

    user.refresh()

    assert user.absolute_url == "https://mocked.example/users/2/"
    assert not requests_mock.called

    user.update_from_json({"id": 2, "name": "Ada"})

    assert user.pk == 2
    assert not requests_mock.called


def test_hyperlink_refresh_after_a_load_without_the_key(
    requests_mock: Any,
) -> None:
    """A URL-only object loaded from JSON without its key can refresh.

    A hyperlinked serializer sends ``url`` in place of the key.
    """
    url = "https://mocked.example/users/2/"
    requests_mock.get(url, json={"url": url, "name": "Ada"})
    user = MockedDoc(json=DOC_JSON).linked
    assert user.name == "Ada"

    user.refresh()

    assert user.absolute_url == url
    assert user.name == "Ada"
    assert requests_mock.call_count == 2


def test_mapped_write_only_field_stays_out_of_full_save_state(
    requests_mock: Any,
) -> None:
    """Full and eager writes keep a write-only value only until it is sent."""

    @json_object(write_only={"password"}, write_map={"password": "secret"})
    class Account(MockedBase):
        class_url = "/accounts/"

        id: int
        name: str
        password: str

    url = "https://mocked.example/accounts/1/"
    put = requests_mock.put(url, json={"id": 1, "password": "echo"})
    account = Account(json={"id": 1, "name": "Ada"})
    account.password = "dummy"

    account.save(full=True)

    assert put.last_request.json() == {
        "id": 1,
        "name": "Ada",
        "secret": "dummy",
    }
    assert "password" not in account.json
    assert account._pending_updates == {}

    patch = requests_mock.patch(url, status_code=204)
    account.set_write_mode("eager")
    account.password = "next"

    assert patch.last_request.json() == {"secret": "next"}
    assert "password" not in account.json


def test_file_aliases_cannot_overwrite_each_other(requests_mock: Any) -> None:
    """File names are checked after write mappings have been applied."""
    with pytest.raises(TypeError, match="owner_id"):
        MockedDoc.create(files={"owner": ("a", b"a"), "owner_id": ("b", b"b")})

    assert not requests_mock.called


def test_public_json_helper_keeps_limits_in_response_hooks(
    requests_mock: Any,
) -> None:
    """Custom response decoders can use the same policy as model reads."""
    api = API("https://mocked.example/")
    api.max_json_depth = 1
    requests_mock.get(DOC_URL, json={"field": ["nested"]})
    response = api.get(DOC_URL)

    with pytest.raises(LimitExceeded, match="max_json_depth"):
        api.json(response)
