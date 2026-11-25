"""One method says how much time remains of a request.

``_Budget.timeout()``, ``_Budget.check_time()``, and the wait between retries
all read the deadline less the clock. They differ only in their floors, since a
socket timeout of 0 does not wait at all.
"""

from __future__ import annotations

import pytest
from urllib3.response import HTTPResponse
from urllib3.util import Retry

from eazyrest import LimitExceeded
from eazyrest._errors import _request_budget
from eazyrest.api import _Budget, _BudgetRetry


class _Clock:
    """A clock that moves only when the test or a sleep moves it."""

    def __init__(self) -> None:
        """Start the clock at zero and record the waits."""
        self.now = 0.0
        self.waits: list[float] = []

    def monotonic(self) -> float:
        """Return the fake time."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Record a wait and advance the fake time by it."""
        self.waits.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """Replace the clock and the sleep that ``eazyrest.api`` reads."""
    fake = _Clock()
    monkeypatch.setattr("eazyrest.api.time.monotonic", fake.monotonic)
    monkeypatch.setattr("eazyrest.api.time.sleep", fake.sleep)
    return fake


ELAPSED = [0.0, 5.0, 9.0, 9.9995, 9.9999, 10.0, 10.5]


@pytest.mark.parametrize("elapsed", ELAPSED)
def test_the_time_that_remains_is_the_deadline_less_the_clock(
    clock: _Clock, elapsed: float
) -> None:
    """The one method gives the same time to every reader."""
    budget = _Budget(None, 10.0)
    clock.now = elapsed

    remaining = budget.remaining()

    assert remaining is not None
    assert remaining == pytest.approx(10.0 - elapsed)


def test_a_budget_without_a_deadline_has_no_time_that_remains() -> None:
    """The request has no limit."""
    assert _Budget(None, None).remaining() is None


@pytest.mark.parametrize("elapsed", ELAPSED)
def test_a_timeout_is_cut_to_the_time_that_remains(
    clock: _Clock, elapsed: float
) -> None:
    """The socket timeout never reaches 0, which would not wait at all."""
    budget = _Budget(None, 10.0)
    clock.now = elapsed

    assert budget.timeout(None) == pytest.approx(max(10.0 - elapsed, 0.001))
    assert budget.timeout(30) == pytest.approx(max(10.0 - elapsed, 0.001))
    assert budget.timeout(0.0004) == 0.0004


@pytest.mark.parametrize("elapsed", ELAPSED)
def test_the_deadline_is_reached_when_no_time_remains(
    clock: _Clock, elapsed: float
) -> None:
    """The check raises exactly when the time that remains is used up."""
    budget = _Budget(None, 10.0)
    clock.now = elapsed

    if elapsed >= 10.0:
        with pytest.raises(LimitExceeded, match="total_timeout"):
            budget.check_time()
    else:
        budget.check_time()


@pytest.mark.parametrize("elapsed", [0.0, 5.0, 9.0, 9.9995, 9.9999])
def test_a_wait_between_retries_is_cut_to_the_time_that_remains(
    clock: _Clock, elapsed: float
) -> None:
    """A wait ends at the deadline, and its floor is 0."""
    retry = _BudgetRetry.wrap(Retry(total=3, backoff_factor=0))
    response = HTTPResponse(headers={"Retry-After": "100"}, status=503)
    token = _request_budget.set(_Budget(None, 10.0))
    clock.now = elapsed
    try:
        with pytest.raises(LimitExceeded, match="total_timeout"):
            retry.sleep(response)
    finally:
        _request_budget.reset(token)

    assert clock.waits == [pytest.approx(10.0 - elapsed)]


def test_a_wait_shorter_than_the_time_that_remains_is_not_cut(
    clock: _Clock,
) -> None:
    """The wait is the server's, and the request goes on."""
    retry = _BudgetRetry.wrap(Retry(total=3))
    response = HTTPResponse(headers={"Retry-After": "2"}, status=503)
    token = _request_budget.set(_Budget(None, 10.0))
    try:
        retry.sleep(response)
    finally:
        _request_budget.reset(token)

    assert clock.waits == [2.0]
