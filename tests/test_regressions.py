"""Regression tests for fixed defects."""

from __future__ import annotations

import datetime
import io
from typing import Annotated, Any

import pytest
import requests
from typing_extensions import TypeAliasType

from eazyrest import (
    API,
    APIError,
    Client,
    FieldConversionError,
    Hyperlinked,
    JSONObject,
    LimitExceeded,
    RedirectError,
    Response,
    ResponseShapeError,
    UsageError,
    WriteMode,
    follow_next_links,
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
        cls, pks: set[Any], *, api: Client | None = None, **kwargs: Any
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


def test_mapped_write_only_field_is_created_under_its_write_key(
    requests_mock: Any,
) -> None:
    """Creation sends a mapped write-only value but never keeps it."""

    @json_object(write_only={"password"}, write_map={"password": "secret"})
    class Account(MockedBase):
        class_url = "/accounts/"

        id: int
        name: str
        password: str

    post = requests_mock.post(
        "https://mocked.example/accounts/",
        json={"id": 1, "name": "Ada", "secret": "echo", "password": "echo"},
    )

    account = Account.create(name="Ada", password="dummy")

    assert post.last_request.json() == {"name": "Ada", "secret": "dummy"}
    assert "password" not in account.json
    with pytest.raises(AttributeError, match="write-only"):
        _ = account.password


class TenantBase(JSONObject):
    """Models whose hyperlinks may also name a second, allowed origin."""


tenant_default = API("https://mocked.example/v1/")
tenant_default.redirect_origins = ("https://files.mocked.example",)
TenantBase.register_api(tenant_default)


@json_object
class LinkedUser(TenantBase):
    """Related resource that hyperlinks name."""

    class_url = "/users/"

    id: int
    name: str


@json_object
class Note(TenantBase):
    """Resource with a hyperlink and a whole hyperlinked collection."""

    class_url = "/notes/"

    id: int
    author: Annotated[LinkedUser, Hyperlinked()]
    readers: Annotated[list[LinkedUser], Hyperlinked()]


@json_object
class Draft(Note):
    """Subclass that inherits the hyperlinked fields."""

    class_url = "/drafts/"

    title: str


def test_hyperlink_may_name_an_allowed_origin(requests_mock: Any) -> None:
    """An origin in ``redirect_origins`` is followed like the API's own."""
    requests_mock.get(
        "https://files.mocked.example/users/2/", json={"id": 2, "name": "Ada"}
    )
    note = Note(
        json={
            "id": 1,
            "author": "https://files.mocked.example/users/2/",
            "readers": [],
        }
    )

    author = note.author

    assert repr(author) == (
        "<LinkedUser url='https://files.mocked.example/users/2/'>"
    )
    assert author.name == "Ada"
    assert author.pk == 2


@pytest.mark.parametrize(
    "url", ["https://mocked.example:port/users/2/", "http://[::1/users/2/"]
)
def test_malformed_hyperlink_is_a_conversion_error(
    requests_mock: Any, url: str
) -> None:
    """A hyperlink that is not a URL fails at the field, sending nothing.

    Args:
        url: Malformed hyperlink.
    """
    note = Note(json={"id": 1, "author": url, "readers": []})

    with pytest.raises(FieldConversionError, match="valid"):
        _ = note.author

    assert not requests_mock.called


def test_hyperlinked_collection_and_inherited_fields_use_urls(
    requests_mock: Any,
) -> None:
    """``Hyperlinked()`` applies to a whole list and to inherited fields."""
    patch = requests_mock.patch(
        "https://mocked.example/v1/drafts/1/", status_code=204
    )
    draft = Draft(
        json={
            "id": 1,
            "title": "t",
            "author": "/v1/users/2/",
            "readers": ["/v1/users/3/", "https://mocked.example/v1/users/4/"],
        }
    )

    assert draft.author.url == "https://mocked.example/v1/users/2/"
    assert [reader.url for reader in draft.readers] == [
        "https://mocked.example/v1/users/3/",
        "https://mocked.example/v1/users/4/",
    ]

    draft.author = LinkedUser(id=7)
    draft.readers = [LinkedUser(id=8)]
    draft.save()

    assert patch.last_request.json() == {
        "author": "https://mocked.example/v1/users/7/",
        "readers": ["https://mocked.example/v1/users/8/"],
    }


def test_query_api_reaches_hyperlinked_objects(requests_mock: Any) -> None:
    """A tenant API passed to a query checks and loads its hyperlinks."""
    tenant = API("https://tenant.example/v1/")
    requests_mock.get(
        "https://tenant.example/v1/notes/",
        json=[
            {
                "id": 1,
                "author": "https://tenant.example/v1/users/2/",
                "readers": [],
            }
        ],
    )
    requests_mock.get(
        "https://tenant.example/v1/users/2/", json={"id": 2, "name": "T"}
    )

    (note,) = Note.filter(api=tenant)

    assert note.author.api is tenant
    assert note.author.name == "T"
    assert {r.hostname for r in requests_mock.request_history} == {
        "tenant.example"
    }

    foreign = Note(
        json={
            "id": 2,
            "author": "https://mocked.example/v1/users/2/",
            "readers": [],
        },
        api=tenant,
    )
    with pytest.raises(FieldConversionError, match="origin"):
        _ = foreign.author


class PagedMockedBase(MockedBase):
    """Models whose collection hook reads DRF pages from the response."""

    @classmethod
    def collection_items_from_response(
        cls, resp: Response, *, api: Client, **kwargs: Any
    ) -> Any:
        """Read every page, resolving links against the response's URL.

        Args:
            resp: Response for the collection's first page.
            api: The query's client.

        Returns:
            An iterator over the items of every page, with their count.
        """
        return follow_next_links(api, api.json(resp), url=resp.url)


@json_object
class PagedDoc(PagedMockedBase):
    """Paginated resource with a prefetchable relation."""

    class_url = "/paged-docs/"
    prefetch_batch_size = 2

    id: int
    owner: MockedUser


def test_count_survives_hooks_cross_page_prefetch_and_api(
    requests_mock: Any,
) -> None:
    """``count``, a response hook, prefetch, and ``api=`` work together."""
    tenant = API("https://tenant.example/")
    requests_mock.get(
        "https://tenant.example/paged-docs/",
        json={
            "count": 3,
            "next": "?page=2",
            "results": [{"id": 1, "owner": 10}],
        },
    )
    requests_mock.get(
        "https://tenant.example/paged-docs/?page=2",
        json={
            "count": 3,
            "next": None,
            "results": [{"id": 2, "owner": 20}, {"id": 3, "owner": 10}],
        },
    )

    docs = PagedDoc.filter(prefetch="owner", api=tenant)

    assert docs.count == 3
    loaded = list(docs)
    assert [doc.id for doc in loaded] == [1, 2, 3]
    assert [doc.owner.pk for doc in loaded] == [10, 20, 10]
    assert all(doc.api is tenant and doc.owner.api is tenant for doc in loaded)
    assert requests_mock.call_count == 2
    assert {r.hostname for r in requests_mock.request_history} == {
        "tenant.example"
    }


def test_request_hooks_run_for_model_writes(requests_mock: Any) -> None:
    """A write's own response hooks run, and the redirect guard still runs."""
    seen: list[tuple[str, int]] = []

    def record(resp: requests.Response, **kwargs: Any) -> None:
        seen.append((resp.request.method or "", resp.status_code))

    requests_mock.post("https://mocked.example/docs/", json={"id": 1})
    doc = MockedDoc.create(
        title="new", request_options={"hooks": {"response": [record]}}
    )
    requests_mock.patch(
        DOC_URL, status_code=307, headers={"Location": "https://evil.example/"}
    )
    doc.title = "next"

    with pytest.raises(RedirectError):
        doc.save(request_options={"hooks": {"response": record}})

    assert seen == [("POST", 200), ("PATCH", 307)]
    assert doc._pending_updates == {"title": "next"}
    assert requests_mock.call_count == 2


@pytest.mark.parametrize("location", ["/n\x85x", "http://[bad/", "//[::1"])
def test_write_refuses_an_unparsable_location_that_it_does_not_follow(
    requests_mock: Any, location: str
) -> None:
    """A ``Location`` that cannot be parsed raises ``RedirectError``.

    Args:
        location: The redirect's ``Location`` header.
    """
    requests_mock.post(
        "https://mocked.example/docs/",
        status_code=302,
        headers={"Location": location},
    )

    with pytest.raises(RedirectError, match="cannot be parsed"):
        MockedDoc.create(
            title="new", request_options={"allow_redirects": False}
        )


def test_unfollowed_redirects_allow_a_completed_write(
    requests_mock: Any,
) -> None:
    """With ``allow_redirects=False``, a 2xx write is still acknowledged."""
    patch = requests_mock.patch(DOC_URL, status_code=204)
    doc = MockedDoc(json=DOC_JSON)
    doc.title = "new"

    doc.save(request_options={"allow_redirects": False})

    assert patch.last_request.json() == {"title": "new"}
    assert doc._pending_updates == {}


@json_object
class CountingUser(MockedBase):
    """Related resource whose bulk lookup omits one key."""

    class_url = "/counting-users/"

    id: int
    name: str

    @classmethod
    def bulk_get_by_pks(
        cls, pks: set[Any], *, api: Client | None = None, **kwargs: Any
    ) -> dict[Any, CountingUser]:
        """Return loaded users for every key except 99.

        Args:
            pks: Keys to load.
            api: Query API override.

        Returns:
            Users keyed by their primary keys.
        """
        return {
            pk: cls(json={"id": pk, "name": "Ada"}, api=api)
            for pk in pks
            if pk != 99
        }


@json_object(pk="user")
class Profile(MockedBase):
    """Resource whose primary key is a related object."""

    class_url = "/profiles/"

    user: CountingUser
    bio: str

    @classmethod
    def bulk_get_by_pks(
        cls, pks: set[Any], *, api: Client | None = None, **kwargs: Any
    ) -> dict[Any, Profile]:
        """Return loaded profiles for the requested keys.

        Args:
            pks: Keys to load.
            api: Query API override.

        Returns:
            Profiles keyed by their primary keys.
        """
        return {pk: cls(json={"user": pk, "bio": "b"}, api=api) for pk in pks}


@json_object
class Post(MockedBase):
    """Resource relating to a model-keyed resource and to a user."""

    class_url = "/posts/"

    id: int
    profile: Profile
    author: CountingUser


def test_model_valued_primary_key_identifies_by_its_key() -> None:
    """A related object as primary key gives its own key and URL."""
    profile = Profile(user=CountingUser(id=3))

    assert profile.pk == 3
    assert profile.url == "/profiles/3/"
    assert profile._normalize_pk(CountingUser(id=3)) == 3
    assert profile._normalize_pk(3) == 3


def test_prefetch_matches_model_keys_and_loads_misses(
    requests_mock: Any,
) -> None:
    """Prefetched model-keyed objects are found, and missing keys load."""
    requests_mock.get(
        "https://mocked.example/posts/",
        json=[{"id": 1, "profile": 3, "author": 99}],
    )
    missing = requests_mock.get(
        "https://mocked.example/counting-users/99/",
        json={"id": 99, "name": "Zed"},
    )

    (post,) = Post.filter(prefetch=["profile", "author"])

    assert post.profile.bio == "b"
    assert not missing.called
    assert post.author.name == "Zed"
    assert missing.call_count == 1


def test_api_descriptor_resolves_without_an_owner_class() -> None:
    """The descriptor falls back to the instance's class, or refuses."""
    descriptor = vars(JSONObject)["api"]
    doc = MockedDoc(json=DOC_JSON)

    assert descriptor.__get__(doc, None) is MockedBase.api
    with pytest.raises(UsageError, match="owner class"):
        descriptor.__get__(None, None)


def test_plain_collection_must_be_iterable() -> None:
    """A non-related collection field rejects a scalar JSON value."""
    doc = MockedDoc(json={**DOC_JSON, "tags": 5})

    with pytest.raises(
        FieldConversionError, match=r"^MockedDoc\.tags: Collection values"
    ):
        _ = doc.tags


Stamps = TypeAliasType("Stamps", list[datetime.date])


@json_object
class AliasedDoc(MockedBase):
    """Resource whose aliases sit inside ``Annotated`` and a union."""

    id: int
    annotated: Annotated[Stamps, "metadata"]
    optional: list[Stamps] | None


def test_aliases_expand_inside_annotated_and_unions() -> None:
    """A named alias converts within ``Annotated`` and an ``X | Y`` union."""
    doc = AliasedDoc(
        json={
            "id": 1,
            "annotated": ["2024-04-01"],
            "optional": [["2024-04-02"]],
        }
    )

    assert doc.annotated == [datetime.date(2024, 4, 1)]
    assert doc.optional == [[datetime.date(2024, 4, 2)]]


def test_single_collection_item_cannot_share_a_raw_key(
    requests_mock: Any,
) -> None:
    """A one-item collection query and a raw key cannot both send ``t``."""

    @json_object(field_map={"tags": "t"})
    class Tagged(MockedBase):
        class_url = "/tagged/"

        id: int
        tags: list[str]

    with pytest.raises(FieldConversionError, match="JSON key t"):
        Tagged.filter(tags="a", t="b")

    assert not requests_mock.called


def test_non_field_lookup_encodes_a_single_model_key(
    requests_mock: Any,
) -> None:
    """A model instance as a lookup value is sent as its primary key."""
    query = requests_mock.get("https://mocked.example/docs/", json=[])

    list(MockedDoc.filter(owner__exact=MockedUser(id=2)))

    assert query.last_request.qs == {"owner__exact": ["2"]}


def test_redirect_guard_runs_without_session_response_hooks(
    requests_mock: Any,
) -> None:
    """A session whose hooks lack a response entry is still guarded."""
    api = API("https://mocked.example/")
    del api.session.hooks["response"]
    requests_mock.get(
        "https://mocked.example/a",
        status_code=302,
        headers={"Location": "https://evil.example/"},
    )

    with pytest.raises(RedirectError):
        api.get("/a")

    assert requests_mock.call_count == 1


def test_write_only_save_with_empty_body_leaves_object_unloaded(
    requests_mock: Any,
) -> None:
    """Saving only a secret needs no load, and a later read loads."""
    patch = requests_mock.patch(DOC_URL, status_code=204)
    detail = requests_mock.get(DOC_URL, json=DOC_JSON)
    doc = MockedDoc(id=1)
    doc.password = "dummy"

    doc.save()

    assert patch.last_request.json() == {"password": "dummy"}
    assert not detail.called
    assert doc.title == "old"
    assert detail.call_count == 1
    assert "password" not in doc.json


def test_url_only_object_loads_a_body_without_its_key(
    requests_mock: Any,
) -> None:
    """A detail body may omit the key, since the URL identifies it."""
    requests_mock.get(
        "https://mocked.example/v1/users/2/", json={"name": "Ada"}
    )
    note = Note(
        json={
            "id": 1,
            "author": "https://mocked.example/v1/users/2/",
            "readers": [],
        }
    )
    author = note.author

    assert author.name == "Ada"
    assert author.url == "https://mocked.example/v1/users/2/"
    assert requests_mock.call_count == 1


@json_object
class Team(MockedBase):
    """Resource with a to-one and a to-many prefetchable relation."""

    class_url = "/teams/"

    id: int
    lead: MockedUser
    members: list[MockedUser]


def test_prefetch_skips_embedded_objects(requests_mock: Any) -> None:
    """An embedded related object is used as is, not looked up again."""
    requests_mock.get(
        "https://mocked.example/teams/",
        json=[{"id": 1, "lead": {"id": 5, "name": "Eve"}, "members": [2]}],
    )

    (team,) = Team.filter(prefetch=["lead", "members"])

    assert team.lead.name == "Eve"
    assert [member.name for member in team.members] == ["Ada"]
    assert requests_mock.call_count == 1


def test_unhashable_collection_prefetch_key_is_a_shape_error(
    requests_mock: Any,
) -> None:
    """A malformed key inside a related list fails at the response."""
    requests_mock.get(
        "https://mocked.example/teams/",
        json=[{"id": 1, "lead": 2, "members": [[2]]}],
    )

    with pytest.raises(ResponseShapeError, match=r"Team\.members"):
        list(Team.filter(prefetch="members"))


@json_object
class Diary(MockedBase):
    """Resource with two collection fields that can be changed in place."""

    class_url = "/diaries/"

    id: int
    tags: list[str]
    days: list[datetime.date]


@pytest.mark.parametrize("bad_first", [True, False])
def test_failed_in_place_encoding_queues_nothing(
    requests_mock: Any, bad_first: bool
) -> None:
    """A save that cannot encode one change queues none of them.

    Args:
        bad_first: Whether the unencodable change is made before the other.
    """
    patch = requests_mock.patch(
        "https://mocked.example/diaries/1/", status_code=204
    )
    diary = Diary(json={"id": 1, "tags": ["a"], "days": ["2024-01-01"]})
    tags, days = diary.tags, diary.days

    def bad() -> None:
        """Append a datetime, which a date field cannot write."""
        days.append(datetime.datetime(2024, 1, 2, 3))  # noqa: DTZ001

    if bad_first:
        bad()
    tags.append("b")
    if not bad_first:
        bad()

    with pytest.raises(FieldConversionError, match=r"Diary\.days"):
        diary.save()

    assert diary._pending_updates == {}
    assert not patch.called

    days.pop()
    diary.save()

    assert patch.last_request.json() == {"tags": ["a", "b"]}
    assert diary._pending_updates == {}
