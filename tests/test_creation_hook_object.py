"""The object that ``create()`` gives ``merge_response()`` has no key."""

from __future__ import annotations

import copy
import pickle
from collections.abc import Callable
from typing import Any

import pytest
import requests_mock

from eazyrest import API, JSONObject, UsageError, json_object

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
