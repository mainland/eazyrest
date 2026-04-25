"""Public package exports for eazyrest."""

from .api import API
from .dateparse import parse_datetime, parse_duration
from .eazyrest import (
    AnalyzedType,
    BaseType,
    CollectionType,
    DoesNotExist,
    JSONObject,
    MultipleObjectsReturned,
    OptionalType,
    PrefetchNotSupported,
    RelatedType,
    json_object,
)

__all__ = [
    "API",
    "AnalyzedType",
    "BaseType",
    "CollectionType",
    "DoesNotExist",
    "MultipleObjectsReturned",
    "JSONObject",
    "OptionalType",
    "PrefetchNotSupported",
    "RelatedType",
    "json_object",
    "parse_datetime",
    "parse_duration",
]
