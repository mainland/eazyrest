"""Run each row of the contract table as one test."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import requests_mock as requests_mock_module
from rows import ROWS, Row, World


@pytest.fixture
def world() -> Iterator[World]:
    """Serve the mocked API and record its requests."""
    with requests_mock_module.Mocker() as mocker:
        yield World(mocker)


def _param(row: Row) -> Any:
    marks = []
    if row.pending is not None:
        marks.append(pytest.mark.xfail(reason=row.pending, strict=True))
    return pytest.param(row, id=row.id, marks=marks)


@pytest.mark.parametrize("row", [_param(row) for row in ROWS])
def test_row(row: Row, world: World) -> None:
    """The input of the row has the outcome that the contract gives."""
    row.check(world)
