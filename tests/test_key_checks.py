"""Tests of the checks that a primary key and a related key go through."""

from __future__ import annotations

from typing import Any

import pytest

from eazyrest import (
    API,
    Client,
    FieldConversionError,
    JSONObject,
    json_object,
)


class KeyBase(JSONObject):
    """Models served by the mocked API."""


KeyBase.register_api(API("https://keys.example/"))


@json_object
class Account(KeyBase):
    """Model keyed by ``id``."""

    class_url = "/accounts/"

    id: int
    legacy_id: int

    @classmethod
    def bulk_get_by_pks(
        cls, pks: set[Any], *, api: Client | None = None, **kwargs: Any
    ) -> dict[Any, Account]:
        """Return an object of the other key for each key."""
        return {
            pk: LegacyAccount(json={"id": pk, "legacy_id": 900 + pk}, api=api)
            for pk in pks
        }


@json_object(pk="legacy_id")
class LegacyAccount(Account):
    """Subclass that chose another key."""

    class_url = "/legacy-accounts/"

    legacy_id: int


@json_object
class Order(KeyBase):
    """Model that relates to an account."""

    class_url = "/orders/"

    id: int
    account: Account


def _legacy() -> LegacyAccount:
    return LegacyAccount(json={"id": 5, "legacy_id": 900})


def test_relation_assignment_refuses_an_object_of_another_key() -> None:
    """An object whose class is keyed by another field is not written."""
    order = Order(json={"id": 1, "account": 5})

    with pytest.raises(FieldConversionError, match=r"Order\.account"):
        order.account = _legacy()


def test_create_refuses_a_related_object_of_another_key(
    requests_mock: Any,
) -> None:
    """``create()`` refuses it before the request."""
    posted = requests_mock.post("https://keys.example/orders/", json={})

    with pytest.raises(FieldConversionError, match=r"Order\.account"):
        Order.create(id=1, account=_legacy())

    assert not posted.called


def test_query_refuses_a_related_object_of_another_key(
    requests_mock: Any,
) -> None:
    """A query by a related object of another key sends nothing."""
    listed = requests_mock.get("https://keys.example/orders/", json=[])

    with pytest.raises(FieldConversionError, match=r"Order\.account"):
        list(Order.filter(account=_legacy()))

    assert not listed.called


def test_prefetch_refuses_an_object_of_another_key(requests_mock: Any) -> None:
    """A bulk hook's object of another key is not matched to a parent."""
    requests_mock.get(
        "https://keys.example/orders/", json=[{"id": 1, "account": 5}]
    )

    with pytest.raises(TypeError, match=r"Account\.bulk_get_by_pks"):
        list(Order.filter(prefetch="account"))


def test_relation_keyed_by_an_object_of_another_key_is_refused() -> None:
    """A key that is a relation refuses it too."""

    @json_object(pk="account")
    class Profile(KeyBase):
        class_url = "/profiles/"

        account: Account

    with pytest.raises(FieldConversionError, match=r"Profile\.account"):
        Profile(account=_legacy())


def test_relation_accepts_a_subclass_that_keeps_the_key() -> None:
    """A subclass with the inherited key is written by that key."""

    @json_object
    class Plain(Account):
        pass

    order = Order(json={"id": 1, "account": 5})
    order.account = Plain(json={"id": 7, "legacy_id": 1})

    assert order.pk == 1
