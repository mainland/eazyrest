"""Stateful property test of the laws of equality by key.

A rule-based state machine builds objects of models with keys of several types,
and loads them, refreshes them, merges pushed payloads into them, tries to
change their keys by assignment and in place, and saves them. A fake server
answers at the key that an object is saved under and spells the key it returns
in more than one way. After every step, each object must agree with a model
that moves its saved key only when the object records the key of server state,
and these laws must hold:

- If two objects are equal, they have the same ``url``.
- The equalities and the hash of an object, and its ``url``, change only when
  it records server state that names another key. A spelling of the same key
  does not change them, and neither does an attempt to change the key, which
  is refused and sends nothing.
- Comparing and hashing send no request.
"""

from __future__ import annotations

import datetime
import decimal
import itertools
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import hypothesis.strategies as st
import pytest
import requests_mock
from hypothesis import HealthCheck, settings
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    rule,
)
from test_eazyrest import (
    Cell,
    Grid,
    GridProfile,
    PriceKeyed,
    TimeKeyed,
    User,
    UserProfile,
    test_api,
)

from eazyrest import JSONObject, UsageError, WriteMode

UTC = datetime.UTC


@dataclass(frozen=True)
class Kind:
    """A model with one type of key, and how to spell and give its keys.

    A key is named by a small value ``n``, which the model of the machine
    compares in place of the object's own key.
    """

    cls: type[JSONObject]
    key_field: str
    name_field: str
    keys: tuple[Any, ...]
    given: Callable[[Any], Any]
    """The key as a caller gives it to the constructor or assigns it."""
    spell: Callable[[Any, str], Any]
    """The key's JSON in the spelling ``"plain"`` or ``"alt"``."""
    changed: Callable[[Any], Callable[[], None]] | None = None
    """Change a key in place, and return the function that changes it back."""


def _change_cell(key: Cell) -> Callable[[], None]:
    key.value += 10

    def undo() -> None:
        key.value -= 10

    return undo


def _day(n: int) -> datetime.datetime:
    return datetime.datetime(2026, 1, n, tzinfo=UTC)


KINDS: dict[str, Kind] = {
    "int": Kind(User, "id", "name", (1, 2, 3), lambda n: n, lambda n, v: n),
    "cell": Kind(
        Grid,
        "id",
        "name",
        (1, 2, 3),
        Cell,
        lambda n, v: n,
        _change_cell,
    ),
    "decimal": Kind(
        PriceKeyed,
        "id",
        "name",
        ("1.0", "1.00", "2"),
        decimal.Decimal,
        lambda n, v: n,
    ),
    "datetime": Kind(
        TimeKeyed,
        "id",
        "name",
        (1, 2, 3),
        _day,
        lambda n, v: (
            f"2026-01-0{n}T00:00:00" + ("Z" if v == "plain" else "+00:00")
        ),
    ),
    "grid": Kind(
        GridProfile,
        "grid",
        "bio",
        (1, 2, 3),
        lambda n: Grid(id=Cell(n)),
        lambda n, v: n if v == "plain" else {"id": n, "name": "e"},
    ),
    "user": Kind(
        UserProfile,
        "user",
        "bio",
        (1, 2, 3),
        lambda n: User(id=n),
        lambda n, v: n if v == "plain" else {"id": n, "name": "e"},
    ),
}


def twin(kind: str, n: Any) -> JSONObject:
    """Build an object of a kind that is known only by a given key.

    Args:
        kind: Name of the kind.
        n: The key's name.

    Returns:
        A new object that is saved under the key.
    """
    spec = KINDS[kind]
    return spec.cls(**{spec.key_field: spec.given(n)})


@dataclass
class Subject:
    """An object of the machine, and the key that the model expects."""

    kind: str
    obj: Any
    saved: Any
    """The key that the object is saved under, by the model."""
    mode: WriteMode = "lazy"


class KeyIdentityMachine(RuleBasedStateMachine):
    """Check that objects compare, hash, and form URLs by their saved key."""

    def __init__(self) -> None:
        """Start the fake server."""
        super().__init__()
        for spec in KINDS.values():
            spec.cls.register_api(test_api)
        self.mocker = requests_mock.Mocker()
        self.mocker.start()
        self.mocker.register_uri(
            requests_mock.ANY,
            re.compile(r"https://example\.com/v1/.*"),
            text=self.handle,
        )
        self.paths: dict[str, tuple[str, Any]] = {}
        self.subjects: list[Subject] = []
        self.variant = "plain"
        self.next_key: Any = None
        self.violations: list[str] = []

    def teardown(self) -> None:
        """Stop the fake server."""
        self.mocker.stop()

    def path(self, kind: str, n: Any) -> str:
        """Return the path of the resource that a key names, and know it.

        Args:
            kind: Name of the kind.
            n: The key's name.

        Returns:
            The path of the resource.
        """
        path = "/v1" + twin(kind, n).url
        self.paths[path] = (kind, n)
        return path

    def record(self, kind: str, n: Any) -> dict[str, Any]:
        """Return the JSON that the server holds for a key.

        Args:
            kind: Name of the kind.
            n: The key's name.

        Returns:
            The record, with its key spelled as the machine says.
        """
        spec = KINDS[kind]
        return {
            spec.key_field: spec.spell(n, self.variant),
            spec.name_field: "s",
        }

    def handle(self, request: Any, context: Any) -> str:
        """Answer a request at a resource, which never sends a key.

        Args:
            request: The request.
            context: The response to fill.

        Returns:
            The response body.
        """
        entry = self.paths.get(urlparse(request.url).path)
        if entry is None or request.method not in ("GET", "PATCH"):
            self.violations.append(f"{request.method} {request.url}")
            context.status_code = 404
            return "{}"
        kind, n = entry
        if request.method == "GET":
            named = n if self.next_key is None else self.next_key
            self.next_key = None
            return json.dumps(self.record(kind, named))
        if KINDS[kind].key_field in request.json():
            self.violations.append(f"{request.method} {request.url} sends key")
        return json.dumps(self.record(kind, n))

    def act(self, subject: Subject, action: Callable[[], Any]) -> Any:
        """Run an action, and check that it sends requests only to the key.

        Args:
            subject: The object that the action is for.
            action: The action.

        Returns:
            What the action returns.
        """
        expected = self.path(subject.kind, subject.saved)
        before = len(self.mocker.request_history)
        result = action()
        for request in self.mocker.request_history[before:]:
            assert urlparse(request.url).path == expected
        return result

    @rule(
        kind=st.sampled_from(sorted(KINDS)),
        data=st.data(),
        form=st.sampled_from(["given", "json"]),
        variant=st.sampled_from(["plain", "alt"]),
        mode=st.sampled_from(["lazy", "eager"]),
    )
    def build(
        self,
        kind: str,
        data: st.DataObject,
        form: str,
        variant: str,
        mode: WriteMode,
    ) -> None:
        """Build an object from its key or from JSON that spells it.

        Args:
            kind: Kind of object to build.
            data: Draws the key.
            form: Whether to give the key or the JSON.
            variant: How the JSON spells the key.
            mode: The object's write mode.
        """
        spec = KINDS[kind]
        n = data.draw(st.sampled_from(spec.keys))
        self.path(kind, n)
        if form == "given":
            obj = spec.cls(**{spec.key_field: spec.given(n)}, write_mode=mode)
        else:
            self.variant = variant
            obj = spec.cls(json=self.record(kind, n), write_mode=mode)
        self.subjects.append(Subject(kind, obj, n, mode=mode))

    def pick(
        self,
        data: st.DataObject,
        only: Callable[[Subject], bool] = lambda subject: True,
    ) -> Subject | None:
        """Draw one of the objects.

        Args:
            data: Draws the object.
            only: Which objects can be drawn.

        Returns:
            The object's subject, or ``None`` if no object can be drawn.
        """
        found = [subject for subject in self.subjects if only(subject)]
        return data.draw(st.sampled_from(found)) if found else None

    @rule(data=st.data())
    def refresh(self, data: st.DataObject) -> None:
        """Refresh an object, which discards what it has not saved."""
        subject = self.pick(data)
        if subject is None:
            return
        self.act(subject, subject.obj.refresh)

    @rule(
        data=st.data(),
        variant=st.sampled_from(["plain", "alt"]),
        other=st.booleans(),
    )
    def load(self, data: st.DataObject, variant: str, other: bool) -> None:
        """Refresh an object and load it, perhaps as another key."""
        subject = self.pick(data)
        if subject is None:
            return
        spec = KINDS[subject.kind]
        self.variant = variant
        subject.obj.refresh()
        named = subject.saved
        if other:
            named = data.draw(st.sampled_from(spec.keys))
            self.path(subject.kind, named)
            self.next_key = named
        self.act(subject, lambda: getattr(subject.obj, spec.name_field))
        subject.saved = named

    @rule(
        data=st.data(),
        how=st.sampled_from(["own", "other", "nokey"]),
        variant=st.sampled_from(["plain", "alt"]),
    )
    def update_from_json(
        self, data: st.DataObject, how: str, variant: str
    ) -> None:
        """Merge a pushed payload that names the key, another key, or none."""
        subject = self.pick(data)
        if subject is None:
            return
        spec = KINDS[subject.kind]
        payload: dict[str, Any] = {spec.name_field: "u"}
        if how == "own":
            payload[spec.key_field] = spec.spell(subject.saved, variant)
        elif how == "other":
            others = [k for k in spec.keys if k != subject.saved]
            payload[spec.key_field] = spec.spell(
                data.draw(st.sampled_from(others)), variant
            )
        try:
            self.act(subject, lambda: subject.obj.update_from_json(payload))
        except ValueError:
            assert how == "other"
            return
        assert how != "other"

    @rule(data=st.data())
    def assign_key(self, data: st.DataObject) -> None:
        """Assign a key, which is refused unless it is the object's own."""
        subject = self.pick(data)
        if subject is None:
            return
        spec = KINDS[subject.kind]
        n = data.draw(st.sampled_from(spec.keys))
        self.path(subject.kind, n)
        calls = self.mocker.call_count

        def assign() -> None:
            setattr(subject.obj, spec.key_field, spec.given(n))

        if n == subject.saved:
            assign()
        else:
            with pytest.raises(AttributeError, match="cannot change"):
                assign()
        assert self.mocker.call_count == calls

    @rule(data=st.data())
    def change_key_in_place(self, data: st.DataObject) -> None:
        """Change the key that the object returns in place, and try to save.

        The write is refused before anything is sent, and the object is as it
        was once the key is changed back.
        """
        subject = self.pick(data, lambda s: bool(KINDS[s.kind].changed))
        if subject is None:
            return
        change = KINDS[subject.kind].changed
        assert change is not None
        key = self.act(subject, lambda: subject.obj.pk)
        calls = self.mocker.call_count
        undo = change(key)
        with pytest.raises(UsageError, match="cannot change"):
            subject.obj.save()
        assert self.mocker.call_count == calls
        undo()

    @rule(data=st.data())
    def save(self, data: st.DataObject) -> None:
        """Save an object, which has no key to send."""
        subject = self.pick(data)
        if subject is None:
            return
        self.act(subject, subject.obj.save)

    @rule(data=st.data(), variant=st.sampled_from(["plain", "alt"]))
    def read(self, data: st.DataObject, variant: str) -> None:
        """Read another field, which loads an object that has not loaded."""
        subject = self.pick(data)
        if subject is None:
            return
        self.variant = variant
        name_field = KINDS[subject.kind].name_field
        self.act(subject, lambda: getattr(subject.obj, name_field))

    @rule(data=st.data())
    def rename(self, data: st.DataObject) -> None:
        """Assign another field, which an eager object sends at once."""
        subject = self.pick(data)
        if subject is None:
            return
        spec = KINDS[subject.kind]
        self.act(subject, lambda: setattr(subject.obj, spec.name_field, "w"))

    @invariant()
    def laws_hold(self) -> None:
        """Each object agrees with the model, and the laws hold."""
        assert not self.violations
        calls = self.mocker.call_count
        for subject in self.subjects:
            mirror = twin(subject.kind, subject.saved)
            obj = subject.obj
            assert obj == mirror
            assert mirror == obj
            assert hash(obj) == hash(mirror)
            assert obj.url == mirror.url
        for a, b in itertools.product(self.subjects, repeat=2):
            equal = (a.kind, a.saved) == (b.kind, b.saved)
            assert (a.obj == b.obj) == equal
            if a.obj == b.obj:
                assert a.obj.url == b.obj.url
                assert hash(a.obj) == hash(b.obj)
        assert self.mocker.call_count == calls


KeyIdentityMachine.TestCase.settings = settings(
    max_examples=300,
    stateful_step_count=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
test_key_identity = KeyIdentityMachine.TestCase
