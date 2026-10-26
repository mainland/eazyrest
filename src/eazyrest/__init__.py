"""Public package exports for eazyrest."""

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
    "DoesNotExist",
    "MultipleObjectsReturned",
    "JSONObject",
    "PrefetchNotSupported",
    "json_object",
    "parse_datetime",
    "parse_duration",
]
