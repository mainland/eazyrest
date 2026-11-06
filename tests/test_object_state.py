"""Stateful property tests of an object's local state against a server.

A rule-based state machine applies random sequences of assignments, key
changes, in-place changes, saves, full saves, failed and unconfirmed writes,
refreshes, pushed updates, and write-mode changes to one object, which it
loads, creates, or makes partial, and changes the values that the caller
passed to ``create()``.
A fake server records every write, and a model of the server, of the object's
stored JSON, and of the values that reads return predicts the outcome of each
step.

The model has the field kinds that a Django REST Framework client meets: a
related object read nested and written by key with ``write_map``, and a
write-only field that the server accepts but never returns.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

import hypothesis.strategies as st
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
    """Related model, which the machine never loads."""

    class_url = "/owners/"

    id: int


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
    # Read as {"id": 1} and written as owner_id=1.
    owner: Owner
    # Accepted in writes but never returned.
    secret: str


FIELDS = ("title", "count", "tags", "meta", "extra")
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
    "owner": OWNERS,
}

FAILURES: dict[str, type[Exception]] = {
    "error": APIError,
    "redirect": RedirectError,
    "shape": ResponseShapeError,
}
"""How a write fails, and the exception that it raises."""


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
    """

    def __init__(self) -> None:
        """Start the fake server."""
        super().__init__()
        self.mocker = requests_mock.Mocker()
        self.mocker.start()
        self.mocker.get(DOC_URL, text=self.handle_get)
        self.mocker.patch(DOC_URL, text=self.handle_patch)
        self.mocker.put(DOC_URL, text=self.handle_put)
        self.mocker.post(f"{BASE}/docs/", text=self.handle_post)
        # The key of the fake server's object.
        self.server_id = 1
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
            result["owner"] = {"id": result["owner"]}
        return result

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

    def fail(self, context: Any) -> str | None:
        """Answer a write that ``failure`` says fails before it is applied.

        Args:
            context: Mock response context, whose status code and headers are
                set for the failure.

        Returns:
            The response text for an HTTP error or an unfollowed redirect, or
            ``None`` if the write is to be applied.
        """
        if self.failure == "error":
            context.status_code = 500
            return json.dumps({"detail": "Server error"})
        if self.failure == "redirect":
            context.status_code = 307
            context.headers["Location"] = f"{BASE}/docs/{self.server_id}/"
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
            return json.dumps(request.json())
        return json.dumps(
            {"id": self.server_id, **self.representation(self.fake)}
        )

    def handle_patch(self, request: Any, context: Any) -> str:
        """Apply a PATCH and answer it.

        A body that holds the key moves the object to that key.

        Args:
            request: Request that the mock received, whose JSON body holds the
                changed fields.
            context: Mock response context, which ``fail()`` and ``answer()``
                set.

        Returns:
            The response text that ``misaddressed()``, ``fail()``, or
            ``answer()`` returns.
        """
        refused = self.misaddressed(request, context)
        if refused is not None:
            return refused
        failed = self.fail(context)
        if failed is not None:
            return failed
        body = self.accept(request.json())
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
        failed = self.fail(context)
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
        self.fake = {field: data.draw(VALUES[field]) for field in DECLARED}
        self.server: dict[str, Any] = copy.deepcopy(self.fake)
        self.stored: dict[str, Any] = copy.deepcopy(self.fake)
        self.expected: dict[str, Any] = copy.deepcopy(self.fake)
        self.server_secret: str | None = None
        self.pending: set[str] = set()
        self.pending_secret: str | None = None
        self.in_place: set[str] = set()
        self.cached: set[str] = set()
        self.server_key = self.stored_key = self.saved_key = 1
        # The values passed to create(), which the caller still holds.
        self.caller: dict[str, Any] = {}
        if how == "load":
            self.obj = Doc(id=1)
            return
        if how == "pushed":
            self.obj = Doc(id=1)
            fields = data.draw(st.sets(st.sampled_from(DECLARED)))
            self.obj.update_from_json(
                self.representation(
                    {name: copy.deepcopy(self.fake[name]) for name in fields}
                )
            )
            return

        self.create_body = "full" if how == "create-full" else "key"
        self.caller = {
            field: copy.deepcopy(self.fake[field]) for field in FIELDS
        }
        extra: dict[str, Any] = {"owner": Owner(id=self.fake["owner"])}
        if secret is not None:
            extra["secret"] = secret
            self.server_secret = secret
        self.obj = Doc.create(id=1, **self.caller, **extra)

    def detect_in_place_changes(self) -> None:
        """Model the detection of in-place changes as pending updates."""
        for field in self.in_place:
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
            name: copy.deepcopy(self.stored[name]) for name in DECLARED
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

    @rule(field=st.sampled_from(FIELDS))
    def read(self, field: str) -> None:
        """A read returns the expected value.

        Args:
            field: Name of the field to read.
        """
        assert getattr(self.obj, field) == self.expected[field]
        self.cached.add(field)

    @rule()
    def read_owner(self) -> None:
        """A related object read nested has the expected key."""
        assert self.obj.owner.pk == self.expected["owner"]

    @rule(data=st.data(), field=st.sampled_from(FIELDS))
    def assign(self, data: st.DataObject, field: str) -> None:
        """Assign a field in the current write mode.

        Args:
            data: Hypothesis data object that draws the new value.
            field: Name of the field to assign.
        """
        value = data.draw(VALUES[field])
        setattr(self.obj, field, copy.deepcopy(value))
        self.assign_value(field, value, value == self.stored[field])

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
        self.obj.owner = Owner(id=owner)
        self.assign_value("owner", owner, unchanged)

    @rule(secret=WORDS)
    def assign_secret(self, secret: str) -> None:
        """Assign a write-only field, which the object sends but never keeps.

        Args:
            secret: Value to assign.
        """
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

    @rule()
    def save(self) -> None:
        """Save sends every change, including those made in place."""
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
        self.obj.save(full=True)
        self.detect_in_place_changes()
        self.send_full()
        self.pending.clear()
        self.pending_secret = None

    @rule(data=st.data(), full=st.booleans())
    def save_fails(self, data: st.DataObject, full: bool) -> None:
        """A failed or unconfirmed save keeps every change pending.

        An HTTP error and an unfollowed 307 leave the server unchanged. A
        response that is not a JSON object follows an applied write, but the
        object cannot confirm it, so the changes stay pending too.

        An unconfirmed write of a key change moves the server's object while
        the object correctly keeps its saved key, which then names nothing.
        The model cannot follow that, so it draws no such failure while a key
        change is pending.

        Args:
            data: Hypothesis data object that draws how the write fails:
                ``"error"`` for an HTTP error, ``"redirect"`` for a 307 that
                the save does not follow, or ``"shape"`` for a response body
                that is not a JSON object.
            full: Whether to save with ``full=True``.
        """
        failures = sorted(FAILURES)
        if "id" in self.pending:
            failures.remove("shape")
        failure = data.draw(st.sampled_from(failures))
        before = len(self.mocker.request_history)
        options = {"allow_redirects": False} if failure == "redirect" else {}
        self.failure = failure
        try:
            self.obj.save(full=full, request_options=options)
        except FAILURES[failure]:
            raised = True
        else:
            raised = False
        finally:
            self.failure = None

        # A save with nothing pending sends nothing, and so cannot fail.
        sent = [
            request.method
            for request in self.mocker.request_history[before:]
            if request.method in ("PATCH", "PUT")
        ]
        assert raised == bool(sent)
        if full:
            assert sent == ["PUT"]

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
        """Refresh discards every unsaved change."""
        self.obj.refresh()
        self.stored = copy.deepcopy(self.server)
        self.expected = copy.deepcopy(self.server)
        self.stored_key = self.saved_key
        self.pending.clear()
        self.pending_secret = None
        self.in_place.clear()
        self.cached.clear()

    @rule(data=st.data(), field=st.sampled_from(DECLARED))
    def push(self, data: st.DataObject, field: str) -> None:
        """A pushed server update overrides local changes to its field.

        Args:
            data: Hypothesis data object that draws the server's new value.
            field: Name of the field that the server updates.
        """
        value = data.draw(VALUES[field])
        self.fake[field] = copy.deepcopy(value)
        self.server[field] = copy.deepcopy(value)
        self.obj.update_from_json(
            self.representation({field: copy.deepcopy(value)})
        )
        self.stored[field] = copy.deepcopy(value)
        self.expected[field] = copy.deepcopy(value)
        self.pending.discard(field)
        self.in_place.discard(field)
        self.cached.discard(field)

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
        for field in FIELDS:
            if field in state and field not in self.in_place:
                assert state[field] == self.stored[field]


ObjectStateMachine.TestCase.settings = settings(
    max_examples=200,
    stateful_step_count=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
test_object_state = ObjectStateMachine.TestCase
