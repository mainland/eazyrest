"""Stateful property tests of an object's local state against a server.

A rule-based state machine applies random sequences of assignments, in- place
changes, saves, refreshes, pushed updates, and write-mode changes to one
object. A fake server records every write, and a model of the server, of the
object's stored JSON, and of the values that reads return predicts the outcome
of each step.
"""

from __future__ import annotations

import copy
import json
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

from eazyrest import API, JSONObject, WriteMode, json_object


class StateBase(JSONObject):
    """Base object for the state machine's model."""


StateBase.register_api(API("https://state.example.com/"))

URL = "https://state.example.com/docs/1/"


@json_object
class Doc(StateBase):
    """Model with scalar, list, and dict fields."""

    class_url = "/docs/"

    id: int
    title: str
    count: int
    tags: list[str]
    meta: dict[str, int]


FIELDS = ("title", "count", "tags", "meta")
"""Fields that the machine reads and writes."""

WORDS = st.text(alphabet="abc", max_size=3)
VALUES = {
    "title": WORDS,
    "count": st.integers(min_value=-3, max_value=3),
    "tags": st.lists(WORDS, max_size=3),
    "meta": st.dictionaries(WORDS, st.integers(0, 3), max_size=3),
}


class ObjectStateMachine(RuleBasedStateMachine):
    """Compare one object's behavior with a model of its state.

    ``server`` is the model of the fake server's JSON. ``stored`` is the
    object's JSON as the model expects it, and ``expected`` holds the values
    that reads should return, which differ from ``stored`` for a value changed
    in place that has not been detected yet. ``pending`` names the fields
    assigned but not saved, and ``in_place`` the fields changed in place but
    not detected.
    """

    def __init__(self) -> None:
        """Start the fake server."""
        super().__init__()
        self.mocker = requests_mock.Mocker()
        self.mocker.start()
        self.mocker.get(URL, text=self.handle_get)
        self.mocker.patch(URL, text=self.handle_patch)
        self.response_mode = "full"

    def teardown(self) -> None:
        """Stop the fake server."""
        self.mocker.stop()

    def handle_get(self, request: Any, context: Any) -> str:
        """Return the fake server's object.

        Args:
            request: Request that the mock received, which is ignored.
            context: Mock response context, which is left unchanged.

        Returns:
            JSON text of the fake server's object, with ``id`` 1.
        """
        return json.dumps({"id": 1, **self.fake})

    def handle_patch(self, request: Any, context: Any) -> str:
        """Apply a PATCH and answer it as ``response_mode`` says.

        Args:
            request: Request that the mock received, whose JSON body holds the
                changed fields.
            context: Mock response context. Its status code becomes 204 when
                ``response_mode`` is ``"empty"``.

        Returns:
            The empty string when ``response_mode`` is ``"empty"``, the JSON
            text of the request body when it is ``"partial"``, and otherwise
            the JSON text of the whole updated object.
        """
        body = request.json()
        self.fake.update(body)
        if self.response_mode == "empty":
            context.status_code = 204
            return ""
        if self.response_mode == "partial":
            return json.dumps(body)
        return json.dumps({"id": 1, **self.fake})

    @initialize(data=st.data())
    def create_object(self, data: st.DataObject) -> None:
        """Create an unloaded object for a server object.

        Args:
            data: Hypothesis data object that draws each field's initial value.
        """
        self.fake: dict[str, Any] = {
            field: data.draw(VALUES[field]) for field in FIELDS
        }
        self.server: dict[str, Any] = copy.deepcopy(self.fake)
        self.stored: dict[str, Any] = copy.deepcopy(self.fake)
        self.expected: dict[str, Any] = copy.deepcopy(self.fake)
        self.pending: set[str] = set()
        self.in_place: set[str] = set()
        self.cached: set[str] = set()
        self.obj = Doc(id=1)

    def detect_in_place_changes(self) -> None:
        """Model the detection of in-place changes as pending updates."""
        for field in self.in_place:
            self.stored[field] = copy.deepcopy(self.expected[field])
            self.pending.add(field)
        self.in_place.clear()

    @rule(field=st.sampled_from(FIELDS))
    def read(self, field: str) -> None:
        """A read returns the expected value.

        Args:
            field: Name of the field to read.
        """
        assert getattr(self.obj, field) == self.expected[field]
        self.cached.add(field)

    @rule(data=st.data(), field=st.sampled_from(FIELDS))
    def assign(self, data: st.DataObject, field: str) -> None:
        """Assign a field in the current write mode.

        Args:
            data: Hypothesis data object that draws the new value.
            field: Name of the field to assign.
        """
        value = data.draw(VALUES[field])
        setattr(self.obj, field, copy.deepcopy(value))
        self.cached.discard(field)
        self.in_place.discard(field)
        self.expected[field] = copy.deepcopy(value)
        if value == self.stored[field]:
            return

        if self.obj.write_mode == "lazy":
            self.stored[field] = copy.deepcopy(value)
            self.pending.add(field)
            return

        # An eager assignment sends earlier changes with its own.
        self.detect_in_place_changes()
        self.stored[field] = copy.deepcopy(value)
        self.pending.add(field)
        self.server.update(
            {name: copy.deepcopy(self.stored[name]) for name in self.pending}
        )
        self.pending.clear()

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

    @rule()
    def save(self) -> None:
        """Save sends every change, including those made in place."""
        self.obj.save()
        self.detect_in_place_changes()
        self.server.update(
            {name: copy.deepcopy(self.stored[name]) for name in self.pending}
        )
        self.pending.clear()

    @rule()
    def refresh(self) -> None:
        """Refresh discards every unsaved change."""
        self.obj.refresh()
        self.stored = copy.deepcopy(self.server)
        self.expected = copy.deepcopy(self.server)
        self.pending.clear()
        self.in_place.clear()
        self.cached.clear()

    @rule(data=st.data(), field=st.sampled_from(FIELDS))
    def push(self, data: st.DataObject, field: str) -> None:
        """A pushed server update overrides local changes to its field.

        Args:
            data: Hypothesis data object that draws the server's new value.
            field: Name of the field that the server updates.
        """
        value = data.draw(VALUES[field])
        self.fake[field] = copy.deepcopy(value)
        self.server[field] = copy.deepcopy(value)
        self.obj.update_from_json({field: copy.deepcopy(value)})
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

    @rule(mode=st.sampled_from(["full", "partial", "empty"]))
    def set_response_mode(self, mode: str) -> None:
        """Change how the server answers a PATCH.

        Args:
            mode: How the server answers, ``"full"`` with the whole object,
                ``"partial"`` with the changed fields, or ``"empty"`` with no
                content.
        """
        self.response_mode = mode

    @invariant()
    def server_matches_model(self) -> None:
        """The fake server holds what the model says it holds."""
        if hasattr(self, "fake"):
            assert self.fake == self.server


ObjectStateMachine.TestCase.settings = settings(
    max_examples=200,
    stateful_step_count=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
test_object_state = ObjectStateMachine.TestCase
