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
from .pagination import follow_next_links
from .write_mode import WriteMode

__all__ = [
    "API",
    "AnalyzedType",
    "BaseType",
    "CollectionType",
    "DoesNotExist",
    "JSONObject",
    "MultipleObjectsReturned",
    "OptionalType",
    "PrefetchNotSupported",
    "RelatedType",
    "WriteMode",
    "follow_next_links",
    "json_object",
    "parse_datetime",
    "parse_duration",
]
