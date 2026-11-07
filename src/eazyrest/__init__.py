"""Public package exports for eazyrest."""

from ._conversion import DEFAULT_CONVERSIONS, Conversion
from .api import API
from .dateparse import parse_datetime, parse_duration
from .eazyrest import (
    DoesNotExist,
    JSONObject,
    MultipleObjectsReturned,
    PrefetchNotSupported,
    json_object,
)

__all__ = [
    "API",
    "DEFAULT_CONVERSIONS",
    "Conversion",
    "DoesNotExist",
    "JSONObject",
    "MultipleObjectsReturned",
    "PrefetchNotSupported",
    "json_object",
    "parse_datetime",
    "parse_duration",
]
