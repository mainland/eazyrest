"""Stateful property tests of an object's local state against a server.

A rule-based state machine applies random sequences of assignments, key
changes, in-place changes, saves, full saves, failed and unconfirmed writes,
refreshes, pushed updates, and write-mode changes to one object, which it
loads, creates, or makes partial, and changes the values that the caller
passed to ``create()``. Some assigned and in-place values cannot be encoded,
and every write must then fail before it sends anything. A related object
changed but not saved must survive a load that leaves its JSON as it was. A
save with a file sends the changes as form fields, which the fake server
parses as Django REST Framework would.

A second machine follows a related object that a note names by its URL, as
``Hyperlinked()`` reads one: an object that knows only its URL, its load, its
assignment by object or URL, pushed URLs, and loads of the note.
A fake server records every write, and a model of the server, of the object's
stored JSON, and of the values that reads return predicts the outcome of each
step.

The model has the field kinds that a Django REST Framework client meets: a
related object read nested and written by key with ``write_map``, and a
write-only field that the server accepts but never returns.
"""

from __future__ import annotations

import copy
import datetime
import email
import email.message
import json
import re
from collections.abc import Callable
from typing import Annotated, Any, ClassVar
from urllib.parse import urljoin

import hypothesis.strategies as st
import pytest
import requests_mock
from hypothesis import HealthCheck, settings
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from eazyrest import (
    API,
    APIError,
    FieldConversionError,
    Hyperlinked,
    JSONObject,
    RedirectError,
    ResponseShapeError,
    WriteMode,
    json_object,
)


class StateBase(JSONObject):
    """Base object for the state machine's model."""


BASE = "https://state.example.com"
StateBase.register_api(API(f"{BASE}/"))

DOC_URL = re.compile(r"https://state\.example\.com/docs/(\d+)/(\?.*)?$")
"""URL of a server object, whose key the fake server checks."""


@json_object
class Owner(StateBase):
    """Related model, which the machine changes but never saves."""

    class_url = "/owners/"

    id: int
    name: str


OWNER_URL = re.compile(r"https://state\.example\.com/owners/(\d+)/$")

OWNER_NAMES = {1: "one", 2: "two", 3: "three"}
"""The name of each related object, which the server never changes."""


@json_object(write_only={"secret"}, write_map={"owner": "owner_id"})
class Doc(StateBase):
    """Model with scalar, list, dict, related, and write-only fields."""

    class_url = "/docs/"

    id: int
    title: str
    count: int
    tags: list[str]
    meta: dict[str, int]
    # A bare dict is not converted, so its value is passed through as it is.
    extra: dict  # type: ignore[type-arg]
    # Converted, so a value can fail to encode.
    days: list[datetime.date]
    # Read as {"id": 1} and written as owner_id=1.
    owner: Owner
    # Accepted in writes but never returned.
    secret: str


FIELDS = ("title", "count", "tags", "meta", "extra", "days")
"""Plain fields that the machine reads and writes."""

DECLARED = (*FIELDS, "owner")
"""Every field that the server returns, other than the key."""

WORDS = st.text(alphabet="abc", max_size=3)
OWNERS = st.integers(min_value=1, max_value=3)
KEYS = st.integers(min_value=1, max_value=4)
VALUES = {
    "title": WORDS,
    "count": st.integers(min_value=-3, max_value=3),
    "tags": st.lists(WORDS, max_size=3),
    "meta": st.dictionaries(WORDS, st.integers(0, 3), max_size=3),
    "extra": st.dictionaries(
        WORDS, st.lists(st.integers(0, 3), max_size=2), max_size=2
    ),
    "days": st.lists(
        st.dates(datetime.date(2024, 1, 1), datetime.date(2024, 1, 9)).map(
            datetime.date.isoformat
        ),
        max_size=2,
    ),
    "owner": OWNERS,
}
"""JSON values of each field, which the model holds."""

BAD_VALUES: dict[str, Any] = {
    "tags": 5,
    "days": [datetime.datetime(2024, 1, 2, 3)],  # noqa: DTZ001
}
"""A value of each converted collection field that cannot be encoded."""


def python_value(field: str, value: Any) -> Any:
    """Return the Python value whose JSON is a field's JSON value.

    Args:
        field: Name of the field.
        value: JSON value of the field.

    Returns:
        The value, with the ISO 8601 strings of ``days`` as dates.
    """
    if field == "days":
        return [datetime.date.fromisoformat(day) for day in value]
    return copy.deepcopy(value)


FORM_INTEGERS = frozenset({"id", "count", "owner_id"})
"""Form fields whose text the server reads as an integer."""

FORM_LISTS = frozenset({"tags", "days"})
"""Form fields that the server reads as a list of their repeated values."""


def sent_fields(request: Any) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Return the fields and files of a write, as the server reads them.

    A JSON body's members are its fields. A multipart body's form fields
    are text, which the server reads back by each field's type, as Django
    REST Framework's serializers do: a repeated field is a list, and the
    key, count, and owner are integers.

    Args:
        request: Request that the mock received.

    Returns:
        The write's fields, and the content of each uploaded file by name.
    """
    content_type = request.headers.get("Content-Type", "")
    if not content_type.startswith("multipart/form-data"):
        return request.json(), {}

    message = email.message_from_bytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + request.body
    )
    payload = message.get_payload()
    assert isinstance(payload, list)
    text: dict[str, list[str]] = {}
    files: dict[str, bytes] = {}
    for part in payload:
        assert isinstance(part, email.message.Message)
        name = str(part.get_param("name", header="content-disposition"))
        content = part.get_payload(decode=True)
        assert isinstance(content, bytes)
        if part.get_filename() is not None:
            files[name] = content
        else:
            text.setdefault(name, []).append(content.decode())

    fields: dict[str, Any] = {}
    for name, values in text.items():
        if name in FORM_LISTS:
            fields[name] = values
        elif name in FORM_INTEGERS:
            fields[name] = int(values[-1])
        else:
            fields[name] = values[-1]
    return fields, files


FAILURES: dict[str, type[Exception]] = {
    "error": APIError,
    "redirect": RedirectError,
    "shape": ResponseShapeError,
}
"""How a write fails, and the exception that it raises."""

REDIRECT_STATUSES = [300, 301, 302, 303, 307, 308, 307, 308]
"""Statuses of a redirect that answers a write, 307 and 308 twice as often."""


class ObjectStateMachine(RuleBasedStateMachine):
    """Compare one object's behavior with a model of its state.

    ``server`` is the model of the fake server's JSON, with the owner as its
    key, and ``server_secret`` the write-only value that the server last
    accepted. ``stored`` is the object's JSON as the model expects it, with
    the owner as its key, and ``expected`` holds the values that reads should
    return, which differ from ``stored`` for a value changed in place that has
    not been detected yet. ``pending`` names the fields assigned but not saved,
    ``pending_secret`` is the write-only value assigned but not saved, and
    ``in_place`` names the fields changed in place but not detected.

    The object's key can change too. ``server_key`` is the key under which
    the model says the server holds the object, ``stored_key`` the key in the
    object's JSON, and ``saved_key`` the key under which the object is saved,
    which differs from ``stored_key`` while a key change is pending. The fake
    server answers only at its object's current key.

    A subclass can model a subclass of ``Doc`` with more fields by setting
    the class attributes, adding rules for the new fields, and extending
    ``python_value()``.
    """

    MODEL: ClassVar[type[Doc]] = Doc
    """Model of the object, ``Doc`` or a decorated subclass of it."""

    FIELDS: ClassVar[tuple[str, ...]] = FIELDS
    """Fields that the server returns, other than the key and the owner."""

    DECLARED: ClassVar[tuple[str, ...]] = DECLARED
    """Every field that the server returns, other than the key."""

    VALUES: ClassVar[dict[str, st.SearchStrategy[Any]]] = VALUES
    """JSON values of each field in ``DECLARED``."""

    OWNED: ClassVar[tuple[str, ...]] = (
        "title",
        "count",
        "tags",
        "meta",
        "extra",
    )
    """Fields whose JSON values a caller passes to ``create()`` as they are."""

    def python_value(self, field: str, value: Any) -> Any:
        """Return the Python value whose JSON is a field's JSON value.

        Args:
            field: Name of the field.
            value: JSON value of the field.

        Returns:
            The value, as reading the field returns it.
        """
        return python_value(field, value)

    def __init__(self) -> None:
        """Start the fake server."""
        super().__init__()
        self.mocker = requests_mock.Mocker()
        self.mocker.start()
        self.mocker.get(DOC_URL, text=self.handle_get)
        self.mocker.patch(DOC_URL, text=self.handle_patch)
        self.mocker.put(DOC_URL, text=self.handle_put)
        self.mocker.post(f"{BASE}/docs/", text=self.handle_post)
        self.mocker.register_uri(
            requests_mock.ANY, OWNER_URL, text=self.handle_owner
        )
        self.mocker.register_uri(
            requests_mock.ANY,
            re.compile(r"https://evil\.example/"),
            text=self.handle_foreign,
        )
        # The status of a redirect that fails a write, and whether its
        # Location is the object's own URL ("same") or another origin.
        self.redirect: tuple[int, str] = (307, "same")
        # The key of the fake server's object.
        self.server_id = 1
        # The content of each file that a write uploaded, by form name.
        self.uploads: dict[str, bytes] = {}
        # A related object whose name was changed but not saved, and the name.
        self.dirty_owner: Owner | None = None
        self.dirty_name = ""
        self.response_mode = "full"
        # How the next write fails, or None for a successful write.
        self.failure: str | None = None
        # Messages describing requests that the server should never receive.
        self.violations: list[str] = []
        self.fake: dict[str, Any] = {}
        # What the server answers to create(), "full" or "key".
        self.create_body = "full"
        self.fake_secret: str | None = None

    def teardown(self) -> None:
        """Stop the fake server."""
        self.mocker.stop()

    def representation(self, fields: dict[str, Any]) -> dict[str, Any]:
        """Return fields of the server's object as the server reads them.

        Args:
            fields: Fields of the server's object, with the owner as its key.

        Returns:
            The fields, with the owner nested as DRF nests a related object.
        """
        result = dict(fields)
        if "owner" in result:
            key = result["owner"]
            result["owner"] = {"id": key, "name": OWNER_NAMES[key]}
        return result

    def handle_foreign(self, request: Any, context: Any) -> str:
        """Record a request to another origin, which must never be sent.

        Args:
            request: Request that the mock received.
            context: Mock response context, whose status code becomes 500.

        Returns:
            An empty body.
        """
        self.violations.append(f"{request.method} sent to {request.url}")
        context.status_code = 500
        return ""

    def handle_owner(self, request: Any, context: Any) -> str:
        """Return a related object, and refuse any write to one.

        The parent's writes send only the related object's key, and the
        machine never saves a related object, so no write reaches one.

        Args:
            request: Request that the mock received.
            context: Mock response context, whose status code becomes 405
                for a write.

        Returns:
            JSON text of the related object, or an error for a write.
        """
        match = OWNER_URL.match(request.url)
        assert match is not None
        if request.method != "GET":
            self.violations.append(f"{request.method} sent to {request.url}")
            context.status_code = 405
            return json.dumps({"detail": "Method not allowed."})
        key = int(match.group(1))
        return json.dumps({"id": key, "name": OWNER_NAMES[key]})

    def accept(self, body: dict[str, Any]) -> dict[str, Any]:
        """Check a write's keys and return its fields as the server keeps them.

        A write must send the owner under its write key and never under its
        read key. The write-only value is kept apart from the object.

        Args:
            body: JSON body of a write.

        Returns:
            The fields that the write sets, with the owner as its key and
            without the write-only value.
        """
        body = dict(body)
        if "owner" in body:
            self.violations.append(f"owner sent under its read key: {body}")
        if "owner_id" in body:
            body["owner"] = body.pop("owner_id")
        if "secret" in body:
            self.fake_secret = body.pop("secret")
        return body

    def misaddressed(self, request: Any, context: Any) -> str | None:
        """Refuse a request that names a key other than the object's.

        Args:
            request: Request that the mock received.
            context: Mock response context, whose status code becomes 404
                for a request to another key.

        Returns:
            The response text of a 404, or ``None`` if the request names the
            server object's current key.
        """
        match = DOC_URL.match(request.url)
        assert match is not None
        if int(match.group(1)) == self.server_id:
            return None
        self.violations.append(f"{request.method} sent to {request.url}")
        context.status_code = 404
        return json.dumps({"detail": "Not found."})

    def handle_get(self, request: Any, context: Any) -> str:
        """Return the fake server's object.

        Args:
            request: Request that the mock received.
            context: Mock response context, set only for a 404.

        Returns:
            JSON text of the fake server's object, with its key.
        """
        refused = self.misaddressed(request, context)
        if refused is not None:
            return refused
        return json.dumps(
            {"id": self.server_id, **self.representation(self.fake)}
        )

    def fail(self, request: Any, context: Any) -> str | None:
        """Answer a write that ``failure`` says fails before it is applied.

        A redirect names the object's own URL, with ``?moved=1``, or another
        origin, as ``redirect`` says. The redirected request is applied.

        Args:
            request: Request that the mock received.
            context: Mock response context, whose status code and headers are
                set for the failure.

        Returns:
            The response text for an HTTP error or a redirect, or ``None`` if
            the write is to be applied.
        """
        if self.failure == "error":
            context.status_code = 500
            return json.dumps({"detail": "Server error"})
        if self.failure == "redirect" and "moved=1" not in request.url:
            status, target = self.redirect
            context.status_code = status
            context.headers["Location"] = (
                f"{BASE}/docs/{self.server_id}/?moved=1"
                if target == "same"
                else "https://evil.example/docs/1/"
            )
            return ""
        return None

    def answer(self, request: Any, body: dict[str, Any], context: Any) -> str:
        """Answer an applied write as ``failure`` and ``response_mode`` say.

        Args:
            request: Request that the mock received.
            body: Fields that the write set, as ``accept()`` returns them.
            context: Mock response context. Its status code becomes 204 when
                ``response_mode`` is ``"empty"``.

        Returns:
            A JSON array when ``failure`` is ``"shape"``, the empty string when
            ``response_mode`` is ``"empty"``, the JSON text of the fields that
            the write set when it is ``"partial"``, the request's own body,
            write-only value included, when it is ``"echo"``, and otherwise
            the JSON text of the whole updated object.
        """
        if self.failure == "shape":
            return "[1]"
        if self.response_mode == "empty":
            context.status_code = 204
            return ""
        if self.response_mode == "partial":
            return json.dumps(self.representation(body))
        if self.response_mode == "echo":
            return json.dumps(sent_fields(request)[0])
        return json.dumps(
            {"id": self.server_id, **self.representation(self.fake)}
        )

    def handle_patch(self, request: Any, context: Any) -> str:
        """Apply a PATCH and answer it.

        A body that holds the key moves the object to that key. A multipart
        body's form fields are applied as a JSON body's members are, and its
        files are recorded in ``uploads``.

        Args:
            request: Request that the mock received, whose JSON or multipart
                body holds the changed fields.
            context: Mock response context, which ``fail()`` and ``answer()``
                set.

        Returns:
            The response text that ``misaddressed()``, ``fail()``, or
            ``answer()`` returns.
        """
        refused = self.misaddressed(request, context)
        if refused is not None:
            return refused
        failed = self.fail(request, context)
        if failed is not None:
            return failed
        fields, files = sent_fields(request)
        self.uploads.update(files)
        body = self.accept(fields)
        key = body.pop("id", None)
        if key is not None:
            self.server_id = key
        self.fake.update(body)
        if key is not None:
            body["id"] = key
        return self.answer(request, body, context)

    def handle_put(self, request: Any, context: Any) -> str:
        """Replace the whole object with a PUT and answer it.

        A field that the body lacks is removed, as an API that replaces the
        whole object on ``PUT`` would clear it, and the body's key moves the
        object to that key.

        Args:
            request: Request that the mock received, whose JSON body holds the
                whole object.
            context: Mock response context, which ``fail()`` and ``answer()``
                set.

        Returns:
            The response text that ``misaddressed()``, ``fail()``, or
            ``answer()`` returns.
        """
        refused = self.misaddressed(request, context)
        if refused is not None:
            return refused
        failed = self.fail(request, context)
        if failed is not None:
            return failed
        body = self.accept(request.json())
        self.server_id = body.pop("id", self.server_id)
        self.fake = body
        return self.answer(request, {"id": self.server_id, **body}, context)

    def handle_post(self, request: Any, context: Any) -> str:
        """Answer the creation of the server's object.

        Args:
            request: Request that the mock received, whose JSON body holds the
                created fields.
            context: Mock response context, whose status code becomes 201.

        Returns:
            JSON text of the whole object when ``create_body`` is ``"full"``,
            and otherwise of its key only.
        """
        body = self.accept(request.json())
        self.server_id = body.pop("id")
        if body != self.fake:
            self.violations.append(f"create() sent {request.json()}")
        context.status_code = 201
        if self.create_body == "full":
            return json.dumps(
                {"id": self.server_id, **self.representation(self.fake)}
            )
        return json.dumps({"id": self.server_id})

    @initialize(
        data=st.data(),
        how=st.sampled_from(["load", "create-full", "create-key", "pushed"]),
        secret=st.none() | WORDS,
    )
    def create_object(
        self, data: st.DataObject, how: str, secret: str | None
    ) -> None:
        """Make an object for a server object.

        Args:
            data: Hypothesis data object that draws each field's initial value
                and, for ``"pushed"``, the fields pushed.
            how: ``"load"`` for an unloaded object, ``"create-full"`` or
                ``"create-key"`` for one that ``create()`` makes, with a
                response holding the whole object or only its key, or
                ``"pushed"`` for an unloaded object whose JSON is a pushed
                subset of the server's fields, so that it is partial.
            secret: Write-only value to pass to ``create()``, if any.
        """
        self.fake = {
            field: data.draw(self.VALUES[field]) for field in self.DECLARED
        }
        self.server: dict[str, Any] = copy.deepcopy(self.fake)
        self.stored: dict[str, Any] = copy.deepcopy(self.fake)
        self.expected: dict[str, Any] = copy.deepcopy(self.fake)
        self.server_secret: str | None = None
        self.pending: set[str] = set()
        self.pending_secret: str | None = None
        self.in_place: set[str] = set()
        self.cached: set[str] = set()
        self.server_key = self.stored_key = self.saved_key = 1
        # Whether the cached days hold a value changed in place that cannot
        # be encoded, so that every write fails before sending anything.
        self.poisoned = False
        # The values passed to create(), which the caller still holds.
        self.caller: dict[str, Any] = {}
        if how == "load":
            self.obj = self.MODEL(id=1)
            return
        if how == "pushed":
            self.obj = self.MODEL(id=1)
            fields = data.draw(st.sets(st.sampled_from(self.DECLARED)))
            pushed = self.representation(
                {name: copy.deepcopy(self.fake[name]) for name in fields}
            )
            # As for a later push, a related object may be named by its key.
            if "owner" in pushed and data.draw(st.booleans()):
                pushed["owner"] = self.fake["owner"]
            self.obj.update_from_json(pushed)
            return

        self.create_body = "full" if how == "create-full" else "key"
        # The caller passes the fields that need no conversion as the JSON
        # values themselves, so that changing them afterwards would change
        # the object's JSON if create() kept them.
        self.caller = {
            field: copy.deepcopy(self.fake[field]) for field in self.OWNED
        }
        converted = {
            field: self.python_value(field, copy.deepcopy(self.fake[field]))
            for field in self.FIELDS
            if field not in self.OWNED
        }
        extra: dict[str, Any] = {"owner": Owner(id=self.fake["owner"])}
        if secret is not None:
            extra["secret"] = secret
            self.server_secret = secret
        self.obj = self.MODEL.create(id=1, **self.caller, **converted, **extra)

    def detect_in_place_changes(self) -> None:
        """Model the detection of in-place changes as pending updates.

        A change that leaves the value as it was, such as setting a key to
        the value that it already has, is no change.
        """
        for field in self.in_place:
            if self.expected[field] != self.stored[field]:
                self.stored[field] = copy.deepcopy(self.expected[field])
                self.pending.add(field)
        self.in_place.clear()

    def send_pending(self) -> None:
        """Model a PATCH of every pending update.

        A pending key change moves the server's object, and the object is
        saved under its new key from then on.
        """
        self.server.update(
            {
                name: copy.deepcopy(self.stored[name])
                for name in self.pending - {"id"}
            }
        )
        if "id" in self.pending:
            self.server_key = self.saved_key = self.stored_key
        if self.pending_secret is not None:
            self.server_secret = self.pending_secret

    def send_full(self) -> None:
        """Model a PUT of the whole object with its pending updates."""
        self.server = {
            name: copy.deepcopy(self.stored[name]) for name in self.DECLARED
        }
        self.server_key = self.saved_key = self.stored_key
        if self.pending_secret is not None:
            self.server_secret = self.pending_secret

    def assign_value(self, field: str, value: Any, unchanged: bool) -> None:
        """Model an assignment in the current write mode.

        Args:
            field: Name of the field assigned.
            value: JSON value of the assignment, as ``stored`` holds it.
            unchanged: Whether the object sees no change, and so sends
                nothing.
        """
        self.cached.discard(field)
        self.in_place.discard(field)
        self.expected[field] = copy.deepcopy(value)
        if unchanged:
            return

        self.stored[field] = copy.deepcopy(value)
        self.pending.add(field)
        if self.obj.write_mode == "lazy":
            return

        # An eager assignment sends earlier changes with its own.
        self.detect_in_place_changes()
        self.send_pending()
        self.pending.clear()
        self.pending_secret = None

    def refused(self, step: Callable[[], object]) -> None:
        """Check that a step fails on the poisoned days and sends nothing.

        Every write first detects the values changed in place, and the
        poisoned days cannot be encoded, so the write raises before it sends
        anything and leaves every change as it was.

        Args:
            step: Step that would send a write.
        """
        before = len(self.mocker.request_history)
        with pytest.raises(FieldConversionError, match=r"Doc\.days"):
            step()
        assert len(self.mocker.request_history) == before

    def sends(self, unchanged: bool = False) -> bool:
        """Return whether an assignment sends a write at once.

        Args:
            unchanged: Whether the assignment leaves the object's JSON as it
                is, so that it sends nothing.

        Returns:
            Whether the object is in eager mode and the assignment changes it.
        """
        return self.obj.write_mode == "eager" and not unchanged

    @rule(field=st.sampled_from(FIELDS))
    def read(self, field: str) -> None:
        """A read returns the expected value.

        Args:
            field: Name of the field to read.
        """
        value = getattr(self.obj, field)
        if not (field == "days" and self.poisoned):
            assert value == self.python_value(field, self.expected[field])
        self.cached.add(field)

    @rule()
    def read_owner(self) -> None:
        """A related object read nested has the expected key."""
        assert self.obj.owner.pk == self.expected["owner"]

    @rule(name=WORDS)
    def modify_owner(self, name: str) -> None:
        """Change the related object's name without saving it.

        Args:
            name: New name of the related object.
        """
        owner = self.obj.owner
        owner.set_write_mode("lazy")
        owner.name = name
        self.dirty_owner = owner
        self.dirty_name = name

    @precondition(
        lambda self: (
            not self.poisoned
            and vars(self.obj).get("_json_is_partial", False)
            and any(
                field not in vars(self.obj)["_json"] for field in self.FIELDS
            )
        )
    )
    @rule(name=st.none() | WORDS)
    def load_through_partial_read(self, name: str | None) -> None:
        """A load keeps a related object whose JSON it does not change.

        Reading a field that a partial object lacks loads the object. A
        related object read before the load is kept, with any change made to
        it, exactly when the load leaves the related object's JSON as it was.

        Args:
            name: New name to give the related object before the load, or
                ``None`` to leave it unchanged.
        """
        state = vars(self.obj)["_json"]
        field = next(field for field in self.FIELDS if field not in state)
        owner = None
        if "owner" in state:
            owner = self.obj.owner
            if name is not None:
                self.modify_owner(name)
        owner_json = copy.deepcopy(state.get("owner"))

        self.read(field)

        if owner is None:
            return
        kept = self.obj._field_cache.get("owner") is owner
        assert kept == (vars(self.obj)["_json"].get("owner") == owner_json)

    @invariant()
    def dirty_owner_keeps_its_change(self) -> None:
        """A changed related object that the object holds keeps its change."""
        owner = self.dirty_owner
        if owner is None:
            return
        held = self.obj._field_cache.get("owner") is owner
        if held:
            assert owner._pending_updates == {"name": self.dirty_name}

    @rule(data=st.data(), field=st.sampled_from(FIELDS))
    def assign(self, data: st.DataObject, field: str) -> None:
        """Assign a field in the current write mode.

        Args:
            data: Hypothesis data object that draws the new value.
            field: Name of the field to assign.
        """
        value = data.draw(self.VALUES[field])
        unchanged = value == self.stored[field]
        if self.poisoned and self.sends(unchanged):
            self.refused(
                lambda: setattr(
                    self.obj, field, self.python_value(field, value)
                )
            )
            return

        setattr(self.obj, field, self.python_value(field, value))
        self.assign_value(field, value, unchanged)
        # The assigned value replaces the one changed in place.
        if field == "days":
            self.poisoned = False

    @rule(owner=OWNERS)
    def assign_owner(self, owner: int) -> None:
        """Assign a related object, which a write sends by its key.

        As for any field, an assignment that leaves the object's JSON
        unchanged sends nothing. A load or a response holds the related
        object nested, which differs from its key, so the assignment of the
        same key is unchanged only after an assignment has stored the key.

        Args:
            owner: Key of the related object to assign.
        """
        state = vars(self.obj).get("_json")
        unchanged = state is not None and state.get("owner") == owner
        if self.poisoned and self.sends(unchanged):
            self.refused(lambda: setattr(self.obj, "owner", Owner(id=owner)))
            return

        self.obj.owner = Owner(id=owner)
        self.assign_value("owner", owner, unchanged)

    @rule(secret=WORDS)
    def assign_secret(self, secret: str) -> None:
        """Assign a write-only field, which the object sends but never keeps.

        Args:
            secret: Value to assign.
        """
        if self.poisoned and self.sends():
            self.refused(lambda: setattr(self.obj, "secret", secret))
            return

        self.obj.secret = secret
        if self.obj.write_mode == "lazy":
            self.pending_secret = secret
            return

        self.detect_in_place_changes()
        self.pending_secret = secret
        self.send_pending()
        self.pending.clear()
        self.pending_secret = None

    @rule(key=KEYS)
    def change_key(self, key: int) -> None:
        """Assign a new primary key, which a write sends to the saved key.

        Until the change is sent, the object is saved under its old key, so a
        write reaches the server's object, which then moves to the new key.

        Args:
            key: New primary key.
        """
        if self.poisoned and self.sends(key == self.stored_key):
            self.refused(lambda: setattr(self.obj, "id", key))
            return

        self.obj.id = key
        if key == self.stored_key:
            return

        self.stored_key = key
        self.pending.add("id")
        if self.obj.write_mode == "lazy":
            return

        self.detect_in_place_changes()
        self.send_pending()
        self.pending.clear()
        self.pending_secret = None

    @precondition(lambda self: "tags" in self.cached)
    @rule(word=WORDS)
    def append_tag(self, word: str) -> None:
        """Change a cached list in place.

        Args:
            word: Tag to append to the object's ``tags`` list.
        """
        self.obj.tags.append(word)
        self.expected["tags"].append(word)
        self.in_place.add("tags")

    @precondition(lambda self: "meta" in self.cached)
    @rule(key=WORDS, value=st.integers(0, 3))
    def set_meta(self, key: str, value: int) -> None:
        """Change a cached dict in place.

        Args:
            key: Key to set in the object's ``meta`` dict.
            value: Value to store under ``key``.
        """
        self.obj.meta[key] = value
        self.expected["meta"][key] = value
        self.in_place.add("meta")

    @precondition(lambda self: bool(self.caller))
    @rule(word=WORDS, key=WORDS)
    def change_caller_values(self, word: str, key: str) -> None:
        """Changing the values passed to ``create()`` changes nothing else.

        Args:
            word: Tag to append to the caller's ``tags`` list.
            key: Key to set in the caller's ``meta`` and ``extra`` dicts.
        """
        self.caller["tags"].append(word)
        self.caller["meta"][key] = 99
        self.caller["extra"][key] = [99]
        for items in self.caller["extra"].values():
            items.append(98)

    def form_ready(self) -> bool:
        """Return whether every pending value can be sent as a form field.

        A form field holds text, and a list as repeated fields, so a mapping
        and an empty list cannot be sent.

        Returns:
            Whether a multipart write can hold every pending update.
        """
        values = [self.stored[name] for name in self.pending - {"id"}]
        return not any(
            isinstance(value, dict) or value == [] for value in values
        )

    @rule(content=st.binary(max_size=8))
    def save_with_file(self, content: bytes) -> None:
        """Save with a file, which sends the changes as form fields.

        The save detects in-place changes first, as every save does, and
        raises ``TypeError`` before sending anything if a pending value
        cannot be a form field. The changes then stay pending.

        Args:
            content: Content of the uploaded file.
        """
        upload = {"upload": ("upload.bin", content)}
        if self.poisoned:
            self.refused(lambda: self.obj.save(files=upload))
            return

        before = len(self.mocker.request_history)
        self.detect_in_place_changes()
        if not self.form_ready():
            with pytest.raises(TypeError, match="form field"):
                self.obj.save(files=upload)
            assert len(self.mocker.request_history) == before
            return

        self.obj.save(files=upload)
        assert self.uploads.pop("upload") == content
        self.send_pending()
        self.pending.clear()
        self.pending_secret = None

    @rule(field=st.sampled_from(sorted(BAD_VALUES)))
    def assign_bad(self, field: str) -> None:
        """Assigning a value that cannot be encoded changes nothing.

        The assignment may load the object first, which the model already
        expects, so only an object that holds the field is compared.

        Args:
            field: Name of the converted collection field to assign.
        """
        state = vars(self.obj).get("_json")
        before = copy.deepcopy(state) if state and field in state else None
        pending = copy.deepcopy(self.obj._pending_updates)
        cached = dict(self.obj._field_cache)
        with pytest.raises(FieldConversionError, match=rf"Doc\.{field}"):
            setattr(self.obj, field, BAD_VALUES[field])

        # A load queues in-place changes before it replaces the JSON, so
        # pending updates are unchanged only when no load happened.
        if before is not None:
            assert vars(self.obj)["_json"] == before
            assert self.obj._field_cache.keys() == cached.keys()
            assert all(
                self.obj._field_cache[name] is value
                for name, value in cached.items()
            )
            assert self.obj._pending_updates == pending

    @precondition(
        lambda self: (
            "days" in self.cached
            and not self.poisoned
            and not vars(self.obj).get("_json_is_partial", False)
        )
    )
    @rule()
    def poison_days(self) -> None:
        """Change the cached days in place to a value that cannot be encoded.

        A datetime is a date, so the list accepts it, but a date field cannot
        write it. Only a complete object is poisoned, so that no read loads
        it, because a load detects the change too.
        """
        self.obj.days.append(datetime.datetime(2024, 1, 2, 3))  # noqa: DTZ001
        self.poisoned = True

    @rule()
    def save(self) -> None:
        """Save sends every change, including those made in place."""
        if self.poisoned:
            self.refused(self.obj.save)
            return

        self.obj.save()
        self.detect_in_place_changes()
        self.send_pending()
        self.pending.clear()
        self.pending_secret = None

    @rule()
    def save_full(self) -> None:
        """A full save replaces the server's object with the whole object.

        The object may be unloaded or partial, so the save must load it first
        and keep its pending changes, or the server loses the fields that the
        object did not know.
        """
        if self.poisoned:
            self.refused(lambda: self.obj.save(full=True))
            return

        self.obj.save(full=True)
        self.detect_in_place_changes()
        self.send_full()
        self.pending.clear()
        self.pending_secret = None

    @rule(data=st.data(), full=st.booleans())
    def save_fails(self, data: st.DataObject, full: bool) -> None:
        """A failed or unconfirmed save keeps every change pending.

        An HTTP error and a redirect that does not deliver the write leave the
        server unchanged. Only a 307 or 308 to the object's own origin, which
        the save follows, delivers the write, with its method and body, and
        the save then succeeds. Any other redirect raises ``RedirectError``
        with no further request: a 300, which requests does not follow, a
        301, 302, or 303, which would change the method or drop the body, one
        to another origin, and any redirect that ``allow_redirects=False``
        leaves unfollowed. A response that is not a JSON object follows an
        applied write, but the object cannot confirm it, so the changes stay
        pending too.

        An unconfirmed write of a key change moves the server's object while
        the object correctly keeps its saved key, which then names nothing.
        The model cannot follow that, so it draws no such failure while a key
        change is pending.

        Args:
            data: Hypothesis data object that draws how the write fails:
                ``"error"`` for an HTTP error, ``"redirect"`` for a redirect,
                with its status, its target, and whether the save follows
                redirects, or ``"shape"`` for a response body that is not a
                JSON object.
            full: Whether to save with ``full=True``.
        """
        failures = sorted(FAILURES)
        if "id" in self.pending:
            failures.remove("shape")
        failure = data.draw(st.sampled_from(failures))
        options: dict[str, Any] = {}
        delivered = False
        if failure == "redirect":
            # Weighted toward the one delivered case, which is the rarest.
            status = data.draw(st.sampled_from(REDIRECT_STATUSES))
            target = data.draw(st.sampled_from(["same", "same", "other"]))
            follow = data.draw(st.booleans())
            self.redirect = (status, target)
            if not follow:
                options["allow_redirects"] = False
            delivered = follow and status in (307, 308) and target == "same"
        before = len(self.mocker.request_history)
        if self.poisoned:
            self.refused(
                lambda: self.obj.save(full=full, request_options=options)
            )
            return

        self.failure = failure
        try:
            self.obj.save(full=full, request_options=options)
        except FAILURES[failure]:
            raised = True
        else:
            raised = False
        finally:
            self.failure = None

        # A save with nothing pending sends nothing, and so cannot fail. A
        # full save of an unloaded or partial object loads it first.
        requests = self.mocker.request_history[before:]
        writes = [
            index
            for index, request in enumerate(requests)
            if request.method in ("PATCH", "PUT")
        ]
        if not writes:
            assert not raised
            self.detect_in_place_changes()
            return
        if full:
            assert requests[writes[0]].method == "PUT"
        if delivered:
            # The redirected request is the only one after the write.
            assert not raised
            assert len(requests) == writes[0] + 2
            self.detect_in_place_changes()
            if full:
                self.send_full()
            else:
                self.send_pending()
            self.pending.clear()
            self.pending_secret = None
            return

        # A failed write is the save's last request.
        assert raised
        assert len(requests) == writes[0] + 1

        # The save detects in-place changes before it sends anything.
        self.detect_in_place_changes()
        if failure != "shape":
            return
        if full:
            self.send_full()
        else:
            self.send_pending()

    @rule()
    def refresh(self) -> None:
        """Refresh discards every unsaved change, encodable or not."""
        self.obj.refresh()
        self.stored = copy.deepcopy(self.server)
        self.expected = copy.deepcopy(self.server)
        self.stored_key = self.saved_key
        self.pending.clear()
        self.pending_secret = None
        self.in_place.clear()
        self.cached.clear()
        self.poisoned = False

    @rule(data=st.data(), field=st.sampled_from(DECLARED))
    def push(self, data: st.DataObject, field: str) -> None:
        """A pushed server update overrides local changes to its field.

        A push may name the related object by its key, which a later load
        replaces with the nested object, as a server's messages may differ
        from its responses.

        Args:
            data: Hypothesis data object that draws the server's new value
                and whether a related object is pushed as its key.
            field: Name of the field that the server updates.
        """
        value = data.draw(self.VALUES[field])
        self.fake[field] = copy.deepcopy(value)
        self.server[field] = copy.deepcopy(value)
        pushed = self.representation({field: copy.deepcopy(value)})
        if field == "owner" and data.draw(st.booleans()):
            pushed["owner"] = value
        self.obj.update_from_json(pushed)
        self.stored[field] = copy.deepcopy(value)
        self.expected[field] = copy.deepcopy(value)
        self.pending.discard(field)
        self.in_place.discard(field)
        self.cached.discard(field)
        # The pushed value replaces the one changed in place.
        if field == "days":
            self.poisoned = False

    @rule(mode=st.sampled_from(["lazy", "eager"]))
    def set_write_mode(self, mode: WriteMode) -> None:
        """Switching modes sends nothing by itself.

        Args:
            mode: Write mode to switch the object to, ``"lazy"`` or
                ``"eager"``.
        """
        self.obj.set_write_mode(mode)

    @rule(mode=st.sampled_from(["full", "partial", "echo", "empty"]))
    def set_response_mode(self, mode: str) -> None:
        """Change how the server answers a write.

        Args:
            mode: How the server answers, ``"full"`` with the whole object,
                ``"partial"`` with the fields that the write set, ``"echo"``
                with the request's body, which holds any write-only value, or
                ``"empty"`` with no content.
        """
        self.response_mode = mode

    @invariant()
    def server_matches_model(self) -> None:
        """The fake server holds what the model says it holds."""
        assert not self.violations
        if hasattr(self, "server"):
            assert self.fake == self.server
            assert self.fake_secret == self.server_secret
            assert self.server_id == self.server_key

    @invariant()
    def key_matches_model(self) -> None:
        """The object reads its new key, and is saved under its saved key.

        Neither reads loads the object.
        """
        if not hasattr(self, "obj"):
            return
        before = len(self.mocker.request_history)
        assert self.obj.pk == self.stored_key
        assert self.obj.url == f"/docs/{self.saved_key}/"
        assert len(self.mocker.request_history) == before

    @invariant()
    def json_matches_model(self) -> None:
        """The object's JSON agrees with the model and has no write-only value.

        The JSON is read without loading the object, so a partial object may
        lack fields. A related object may be nested or a key, so only the
        plain fields are compared. A load detects in-place changes before it
        replaces the JSON, so a field changed in place may already hold its
        new value, and is not compared.
        """
        if not hasattr(self, "obj"):
            return
        state = vars(self.obj).get("_json")
        if state is None:
            return
        assert "secret" not in state
        for field in self.FIELDS:
            if field in state and field not in self.in_place:
                assert state[field] == self.stored[field]


ObjectStateMachine.TestCase.settings = settings(
    max_examples=200,
    stateful_step_count=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
test_object_state = ObjectStateMachine.TestCase


@json_object
class Person(StateBase):
    """Related model that a note names by its URL."""

    class_url = "/people/"

    id: int
    name: str


@json_object
class LinkedNote(StateBase):
    """Model whose related object is a hyperlink, as DRF serializes one."""

    class_url = "/notes/"

    id: int
    title: str
    author: Annotated[Person, Hyperlinked()]


NOTE_URL = f"{BASE}/notes/1/"
PERSON_URL = re.compile(r"https://state\.example\.com/people/(\d+)/$")
PEOPLE = {1: "ada", 2: "bob", 3: "cy"}
"""The name of each person, which the server never changes."""


def person_url(key: int, form: str) -> str:
    """Return a person's URL in the form that a server or a caller uses.

    Args:
        key: The person's primary key.
        form: ``"absolute"`` or ``"relative"``.

    Returns:
        The person's URL, absolute or as a path.
    """
    path = f"/people/{key}/"
    return f"{BASE}{path}" if form == "absolute" else path


class LinkedStateMachine(RuleBasedStateMachine):
    """Compare a note's hyperlinked author with a model of its identity.

    ``server_author`` is the key of the person that the server's note names,
    and ``link_form`` how the server writes the URL. ``author_json`` is the
    URL string that the note's JSON holds, or ``None`` when the note has no
    JSON for the author, and ``pending`` the URL string assigned but not
    saved.
    """

    def __init__(self) -> None:
        """Start the fake server."""
        super().__init__()
        self.mocker = requests_mock.Mocker()
        self.mocker.start()
        self.mocker.get(NOTE_URL, text=self.handle_get)
        self.mocker.patch(NOTE_URL, text=self.handle_patch)
        self.mocker.register_uri(
            requests_mock.ANY, PERSON_URL, text=self.handle_person
        )
        self.violations: list[str] = []
        self.server_author = 1
        self.link_form = "absolute"
        self.author_json: str | None = None
        self.pending: str | None = None
        # A person whose name was changed but not saved, and the name.
        self.dirty: Person | None = None
        self.dirty_name = ""

    def teardown(self) -> None:
        """Stop the fake server."""
        self.mocker.stop()

    def served(self) -> str:
        """Return the URL of the server's author as the server writes it.

        Returns:
            The URL, in the form that ``link_form`` names.
        """
        return person_url(self.server_author, self.link_form)

    def handle_get(self, request: Any, context: Any) -> str:
        """Return the server's note.

        Args:
            request: Request that the mock received, which is ignored.
            context: Mock response context, which is left unchanged.

        Returns:
            JSON text of the note.
        """
        return json.dumps({"id": 1, "title": "t", "author": self.served()})

    def handle_patch(self, request: Any, context: Any) -> str:
        """Apply a PATCH, which names the author by its URL.

        An assigned object is written as its absolute URL, but an assigned
        URL string as it was given, so the server resolves a path against its
        own origin, as Django REST Framework's hyperlinked fields do.

        Args:
            request: Request that the mock received.
            context: Mock response context, which is left unchanged.

        Returns:
            JSON text of the updated note.
        """
        body = request.json()
        if "author" in body:
            match = PERSON_URL.match(urljoin(f"{BASE}/", body["author"]))
            assert match is not None
            self.server_author = int(match.group(1))
        return self.handle_get(request, context)

    def handle_person(self, request: Any, context: Any) -> str:
        """Return a person, and refuse any write to one.

        Args:
            request: Request that the mock received.
            context: Mock response context, whose status code becomes 405
                for a write.

        Returns:
            JSON text of the person, or an error for a write.
        """
        if request.method != "GET":
            self.violations.append(f"{request.method} sent to {request.url}")
            context.status_code = 405
            return ""
        match = PERSON_URL.match(request.url)
        assert match is not None
        key = int(match.group(1))
        return json.dumps({"id": key, "name": PEOPLE[key]})

    @initialize(
        how=st.sampled_from(["load", "json", "pushed"]),
        form=st.sampled_from(["absolute", "relative"]),
    )
    def create_note(self, how: str, form: str) -> None:
        """Make a note that is unloaded, loaded, or partial.

        Args:
            how: ``"load"`` for an unloaded note, ``"json"`` for one built
                from the server's JSON, or ``"pushed"`` for one whose JSON
                holds only a pushed author, so that it is partial.
            form: How the server writes the author's URL.
        """
        self.link_form = form
        if how == "load":
            self.note = LinkedNote(id=1)
        elif how == "json":
            self.note = LinkedNote(
                json={"id": 1, "title": "t", "author": self.served()}
            )
            self.author_json = self.served()
        else:
            self.note = LinkedNote(id=1)
            self.note.update_from_json({"author": self.served()})
            self.author_json = self.served()

    def loaded(self) -> None:
        """Model a load, which keeps a pending author."""
        self.author_json = (
            self.pending if self.pending is not None else self.served()
        )

    def expected_key(self) -> int:
        """Return the key of the person that the note's JSON names.

        Returns:
            The key that ``author_json`` names.
        """
        assert self.author_json is not None
        match = PERSON_URL.match(urljoin(f"{BASE}/", self.author_json))
        assert match is not None
        return int(match.group(1))

    @rule()
    def read_author(self) -> None:
        """Reading the author gives an object that knows its URL.

        A person read from a URL loads only when a field other than its URL
        is read, so neither its URL nor its ``repr`` sends a request.
        """
        if self.author_json is None:
            self.loaded()
        author = self.note.author
        before = len(self.mocker.request_history)
        assert author.absolute_url == person_url(
            self.expected_key(), "absolute"
        )
        repr(author)
        assert len(self.mocker.request_history) == before

    @rule()
    def load_author(self) -> None:
        """A URL-only person loads from its URL and then knows its key."""
        if self.author_json is None:
            self.loaded()
        author = self.note.author
        key = self.expected_key()
        if author is not self.dirty:
            assert author.name == PEOPLE[key]
        assert author.pk == key

    @rule(
        key=st.sampled_from(sorted(PEOPLE)),
        how=st.sampled_from(["object", "absolute", "relative"]),
    )
    def assign_author(self, key: int, how: str) -> None:
        """Assign the author by an object or by a URL.

        An object is written as its absolute URL, and a URL as it is given,
        so the model compares the written string with the note's JSON.

        Args:
            key: Key of the person to assign.
            how: ``"object"`` for a person object, or the form of a URL.
        """
        if self.author_json is None:
            self.loaded()
        value: Person | str
        if how == "object":
            value = Person(id=key)
            written = person_url(key, "absolute")
        else:
            value = written = person_url(key, how)
        unchanged = written == self.author_json
        # A hyperlinked field also accepts a URL, which its type omits.
        self.note.author = value  # type: ignore[assignment]
        if unchanged:
            return
        self.author_json = written
        self.pending = written
        if self.note.write_mode == "eager":
            self.sent()

    def sent(self) -> None:
        """Model a successful write of the pending author."""
        assert self.pending is not None
        match = PERSON_URL.match(urljoin(f"{BASE}/", self.pending))
        assert match is not None
        self.server_author = int(match.group(1))
        self.pending = None
        # The response holds the server's form of the URL.
        self.author_json = self.served()

    @rule()
    def save(self) -> None:
        """Saving sends a pending author, which the server then names."""
        self.note.save()
        if self.pending is not None:
            self.sent()

    @rule(
        key=st.sampled_from(sorted(PEOPLE)),
        form=st.sampled_from(["absolute", "relative"]),
    )
    def push_author(self, key: int, form: str) -> None:
        """A pushed URL replaces the author and any pending one.

        Args:
            key: Key of the person that the server now names.
            form: Form of the pushed URL.
        """
        self.server_author = key
        url = person_url(key, form)
        self.note.update_from_json({"author": url})
        self.author_json = url
        self.pending = None

    @rule(name=WORDS)
    def modify_author(self, name: str) -> None:
        """Change the author's name without saving it.

        Args:
            name: New name of the author.
        """
        if self.author_json is None:
            self.loaded()
        author = self.note.author
        author.set_write_mode("lazy")
        author.name = name
        self.dirty = author
        self.dirty_name = name

    @precondition(
        lambda self: (
            vars(self.note).get("_json") is not None
            and "title" not in vars(self.note)["_json"]
        )
    )
    @rule(
        name=st.none() | WORDS, form=st.sampled_from(["absolute", "relative"])
    )
    def load_partial_note(self, name: str | None, form: str) -> None:
        """A load keeps an author whose URL string it does not change.

        The same person's URL in another form is another string, so the load
        then replaces the author.

        Args:
            name: New name to give the author before the load, or ``None`` to
                leave it unchanged.
            form: How the server writes the author's URL in the load.
        """
        author = self.note.author
        if name is not None:
            self.modify_author(name)
        self.link_form = form
        before = self.author_json

        assert self.note.title == "t"
        self.loaded()

        kept = self.note._field_cache.get("author") is author
        assert kept == (self.author_json == before)

    @rule()
    def refresh(self) -> None:
        """Refresh discards the author and any pending change."""
        self.note.refresh()
        self.author_json = None
        self.pending = None

    @precondition(lambda self: vars(self.note).get("_json") is None)
    @rule(
        key=st.sampled_from(sorted(PEOPLE)),
        form=st.sampled_from(["absolute", "relative"]),
    )
    def push_into_unloaded(self, key: int, form: str) -> None:
        """A push into an unloaded note makes it partial.

        Args:
            key: Key of the person that the server now names.
            form: Form of the pushed URL.
        """
        self.push_author(key, form)

    @rule(form=st.sampled_from(["absolute", "relative"]))
    def set_link_form(self, form: str) -> None:
        """Change how the server writes the author's URL.

        Args:
            form: ``"absolute"`` or ``"relative"``.
        """
        self.link_form = form

    @rule(mode=st.sampled_from(["lazy", "eager"]))
    def set_write_mode(self, mode: WriteMode) -> None:
        """Switching modes sends nothing by itself.

        Args:
            mode: Write mode to switch the note to.
        """
        self.note.set_write_mode(mode)

    @invariant()
    def json_matches_model(self) -> None:
        """The note's JSON holds the URL string that the model expects."""
        assert not self.violations
        if not hasattr(self, "note"):
            return
        state = vars(self.note).get("_json")
        if state is None or "author" not in state:
            assert self.author_json is None or state is not None
            return
        assert state["author"] == self.author_json

    @invariant()
    def dirty_author_keeps_its_change(self) -> None:
        """A changed author that the note holds keeps its change."""
        author = self.dirty
        if author is None:
            return
        held = self.note._field_cache.get("author") is author
        if held:
            assert author._pending_updates == {"name": self.dirty_name}


LinkedStateMachine.TestCase.settings = settings(
    max_examples=100,
    stateful_step_count=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
test_linked_state = LinkedStateMachine.TestCase
