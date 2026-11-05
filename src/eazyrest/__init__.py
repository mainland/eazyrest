"""Public package exports for eazyrest."""

from ._errors import (
    EazyrestError,
    PaginationError,
    RedirectError,
    ResponseShapeError,
    UsageError,
)
from ._models import (
    AnalyzedType,
    BaseType,
    CollectionType,
    DoesNotExist,
    FieldConversionError,
    JSONObject,
    MultipleObjectsReturned,
    OptionalType,
    PrefetchNotSupported,
    PydanticType,
    RelatedType,
    json_object,
)
from .api import API
from .dateparse import parse_datetime, parse_duration
from .pagination import follow_link_headers, follow_next_links
from .write_mode import WriteMode

__all__ = [
    "API",
    "AnalyzedType",
    "BaseType",
    "CollectionType",
    "DoesNotExist",
    "EazyrestError",
    "FieldConversionError",
    "JSONObject",
    "MultipleObjectsReturned",
    "OptionalType",
    "PaginationError",
    "PrefetchNotSupported",
    "PydanticType",
    "RedirectError",
    "RelatedType",
    "ResponseShapeError",
    "UsageError",
    "WriteMode",
    "follow_link_headers",
    "follow_next_links",
    "json_object",
    "parse_datetime",
    "parse_duration",
]
