"""Public package exports for eazyrest."""

from importlib.metadata import PackageNotFoundError, version

from ._errors import (
    APIError,
    DoesNotExist,
    EazyrestError,
    LimitExceeded,
    MultipleObjectsReturned,
    NotFound,
    PaginationError,
    PrefetchNotSupported,
    RedirectError,
    ResponseShapeError,
    UsageError,
)
from ._models import (
    AnalyzedType,
    BaseType,
    CollectionType,
    FieldConversionError,
    Hyperlinked,
    JSONObject,
    OptionalType,
    PydanticType,
    RelatedType,
    json_object,
)
from ._results import Results
from .api import API
from .dateparse import parse_datetime, parse_duration
from .pagination import (
    Page,
    follow_link_headers,
    follow_next_links,
    link_headers_page,
    next_links_page,
)
from .write_mode import WriteMode

try:
    __version__ = version("eazyrest")
    """Version of the installed package."""
except PackageNotFoundError:  # pragma: no cover - only without metadata
    __version__ = "0+unknown"

__all__ = [
    "API",
    "APIError",
    "AnalyzedType",
    "BaseType",
    "CollectionType",
    "DoesNotExist",
    "EazyrestError",
    "FieldConversionError",
    "Hyperlinked",
    "JSONObject",
    "LimitExceeded",
    "MultipleObjectsReturned",
    "NotFound",
    "OptionalType",
    "Page",
    "PaginationError",
    "PrefetchNotSupported",
    "PydanticType",
    "RedirectError",
    "RelatedType",
    "ResponseShapeError",
    "Results",
    "UsageError",
    "WriteMode",
    "follow_link_headers",
    "follow_next_links",
    "json_object",
    "link_headers_page",
    "next_links_page",
    "parse_datetime",
    "parse_duration",
]
