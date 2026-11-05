"""Tests of the release workflow's version gate."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

release_version = runpy.run_path(
    str(
        Path(__file__).resolve().parents[1]
        / ".github/scripts/release_version.py"
    )
)["release_version"]


@pytest.mark.parametrize(
    "version",
    ["0.9.0", "1.0.0", "1.0.0a1", "1.0.0b2", "1.0.0rc1", "1.0.0.post1"],
)
def test_public_release_versions(version: str) -> None:
    """Canonical final, prerelease, and postrelease tags are publishable."""
    assert release_version("v" + version) == version


@pytest.mark.parametrize(
    "tag",
    [
        "v1.0.0+build",
        "v1.0.0.dev1",
        "v1.0",
        "v01.0.0",
        "v1.0.0rc01",
        "v1!1.0.0",
        "v1.0.0\n",
        "v1.0.0;anything",
        "1.0.0",
    ],
)
def test_unpublishable_or_noncanonical_tags(tag: str) -> None:
    """Reject tags before building artifacts or contacting an index."""
    with pytest.raises(ValueError, match="Expected"):
        release_version(tag)
