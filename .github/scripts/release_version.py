"""Validate the canonical public version of a release tag."""

from __future__ import annotations

import re
import sys

_NUMBER = r"(?:0|[1-9][0-9]*)"
_TAG = re.compile(
    rf"v{_NUMBER}\.{_NUMBER}\.{_NUMBER}"
    rf"(?:(?:a|b|rc){_NUMBER})?(?:\.post{_NUMBER})?"
)


def release_version(tag: str) -> str:
    """Return the version of a canonical three-component public release tag.

    Args:
        tag: Tag such as ``v1.0.0`` or ``v1.0.0rc1``.

    Returns:
        The public version without its ``v`` prefix.

    Raises:
        ValueError: If the tag is not a supported release tag. Local and
            development versions cannot be published by this workflow.
    """
    if _TAG.fullmatch(tag) is None:
        raise ValueError(
            "Expected vMAJOR.MINOR.PATCH, optionally followed by aN, bN, "
            "rcN, or .postN, with no leading zeros"
        )
    return tag[1:]


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: release_version.py TAG")
    try:
        print(release_version(sys.argv[1]))
    except ValueError as err:
        sys.exit(str(err))
