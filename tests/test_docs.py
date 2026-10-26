"""Tests for code examples in the documentation."""

from __future__ import annotations

import re
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

GUIDE = Path(__file__).resolve().parents[1] / "docs" / "guide.md"

_PYTHON_BLOCK_RE = re.compile(
    r"^```python\n(.*?)^```$", re.MULTILINE | re.DOTALL
)


def run_guide_example(marker: str) -> dict[str, Any]:
    """Run the one Python block in the guide that contains ``marker``.

    Args:
        marker: Text that identifies the block, such as a class statement.

    Returns:
        The global namespace of the block after it runs.
    """
    blocks = [
        block
        for block in _PYTHON_BLOCK_RE.findall(GUIDE.read_text())
        if marker in block
    ]
    assert len(blocks) == 1, f"expected one block with {marker!r}"

    # Do not inherit this module's __future__ imports, so the block runs as it
    # would in a reader's module. The code is this repository's own guide.
    code = compile(blocks[0], str(GUIDE), "exec", dont_inherit=True)
    namespace: dict[str, Any] = {"__name__": "guide_example"}
    exec(code, namespace)  # noqa: S102
    return namespace


def test_address_example_converts_only_address_fields() -> None:
    """The ``IPv4Address`` conversion converts addresses, in lists too."""
    example = run_guide_example("class APIObject")
    host = example["Host"](
        json={"id": 1, "address": "10.0.0.1", "allowed": ["10.0.0.2"]}
    )

    assert host.address == IPv4Address("10.0.0.1")
    assert host.allowed == [IPv4Address("10.0.0.2")]
    host.address = IPv4Address("10.0.0.3")
    host.allowed = [IPv4Address("10.0.0.4")]
    assert host.json["address"] == "10.0.0.3"
    assert host.json["allowed"] == ["10.0.0.4"]
