"""Shared write-mode types and validation helpers."""

from __future__ import annotations

from typing import Literal, cast

WriteMode = Literal["lazy", "eager"]


def validate_write_mode(mode: str) -> WriteMode:
    """Validate and normalize a supported write mode."""
    if mode not in ("lazy", "eager"):
        raise ValueError(
            f"Unsupported write mode {mode!r}; expected 'lazy' or 'eager'"
        )

    return cast(WriteMode, mode)
