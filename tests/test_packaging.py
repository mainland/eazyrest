"""Tests for how the test suite imports the package."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import eazyrest

SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.mark.skipif(
    "TOX_ENV_NAME" not in os.environ,
    reason="Only tox installs the built package rather than the source tree",
)
def test_tox_imports_the_installed_package() -> None:
    """Under tox, the tests exercise the package that tox built."""
    module = Path(eazyrest.__file__).resolve()

    assert SRC not in module.parents
