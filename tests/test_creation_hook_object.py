"""The object that ``create()`` gives ``merge_response()`` has no key."""

from __future__ import annotations

import copy
import pickle
from collections.abc import Callable
from typing import Any

import pytest
import requests_mock

from eazyrest import (
    API,
    JSONObject,
    ResponseShapeError,
    UsageError,
    json_object,
)

api = API("https://hook.example/")
KEPT: list[Any] = []


@json_object
class Hooked(JSONObject):
    """A model whose hook keeps the object that it is given and copies."""

    class_url = "/hooked/"
    id: int
    name: str

    def merge_response(  # type: ignore[no-untyped-def]
        self, resp, current, sent, **kwargs
    ):
        """Keep the hook object and a copy, a deep copy, and a pickle."""
        KEPT[:] = [
            self,
            copy.copy(self),
            copy.deepcopy(self),
            pickle.loads(pickle.dumps(self)),
        ]
        return super().merge_response(resp, current, sent, **kwargs)


Hooked.register_api(api)

USES: dict[str, Callable[[Hooked], Any]] = {
    "pk": lambda o: o.pk,
    "url": lambda o: o.url,
    "hash": hash,
    "refresh": lambda o: o.refresh(),
    "delete": lambda o: o.delete(),
    "filter": lambda o: list(Hooked.filter(id=o)),
}


@pytest.fixture
def hooks() -> list[Hooked]:
    """Return the hook object and its copies, from one creation."""
    with requests_mock.Mocker() as mocker:
        mocker.post(
            "https://hook.example/hooked/", json={"id": 1, "name": "a"}
        )
        Hooked.create(name="a")
    return list(KEPT)


@pytest.mark.parametrize("use", USES.values(), ids=USES.keys())
@pytest.mark.parametrize(
    "which", [1, 2, 3], ids=["copy", "deepcopy", "pickle"]
)
def test_a_copy_of_the_hook_object_has_no_key_either(
    hooks: list[Hooked], which: int, use: Callable[[Hooked], Any]
) -> None:
    """Refuse every use of the key, and send no request."""
    with requests_mock.Mocker() as mocker:
        with pytest.raises(UsageError, match="no key yet"):
            use(hooks[which])
        assert mocker.call_count == 0


def test_the_hook_object_equals_itself_and_refuses_a_comparison() -> None:
    """Only a comparison with another object of its model raises."""
    seen: dict[str, Any] = {}

    @json_object
    class Probe(JSONObject):
        """A keyed model whose hook compares its object."""

        class_url = "/probes/"
        id: int

        def merge_response(  # type: ignore[no-untyped-def]
            self, resp, current, sent, **kwargs
        ):
            """Compare the hook object with itself and with others."""
            seen["itself"] = self == self  # noqa: PLR0124
            seen["another_model"] = self == Hooked(id=1)
            seen["not_a_model"] = self == 1
            with pytest.raises(UsageError, match="no key yet"):
                _ = self == Probe(id=1)
            return super().merge_response(resp, current, sent, **kwargs)

    Probe.register_api(api)
    with requests_mock.Mocker() as mocker:
        mocker.post("https://hook.example/probes/", json={"id": 1})
        Probe.create()
    assert seen == {
        "itself": True,
        "another_model": False,
        "not_a_model": False,
    }


def test_a_hook_reads_the_key_that_the_server_assigns_from_a_header() -> None:
    """A hook that keeps only what it sent still names a key it reads."""

    @json_object
    class Located(JSONObject):
        """A model whose server answers with a status and a Location."""

        class_url = "/located/"
        id: int
        name: str

        def merge_response(  # type: ignore[no-untyped-def]
            self, resp, current, sent, **kwargs
        ):
            """Take the key from the Location header."""
            key = resp.headers["Location"].rstrip("/").rsplit("/", 1)[-1]
            return {**current, **sent, "id": int(key)}

    class Unreading(Located):
        """The hook of the guide's example, which reads no key."""

        def merge_response(  # type: ignore[no-untyped-def]
            self, resp, current, sent, **kwargs
        ):
            """Return only what the request sent."""
            return {**current, **sent}

    Located.register_api(api)
    with requests_mock.Mocker() as mocker:
        mocker.post(
            "https://hook.example/located/",
            json={"status": "ok"},
            headers={"Location": "https://hook.example/located/7/"},
        )
        assert Located.create(name="a").pk == 7
        with pytest.raises(ResponseShapeError):
            json_object(Unreading).create(name="a")
