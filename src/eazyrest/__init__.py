"""Public package exports for eazyrest."""

from importlib.metadata import PackageNotFoundError, version

from ._conversion import DEFAULT_CONVERSIONS, Conversion
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
    FieldConversionError,
    Hyperlinked,
    JSONObject,
    encode_json,
    json_object,
)
from ._protocols import Client, Request, Response
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
    "DEFAULT_CONVERSIONS",
    "APIError",
    "Client",
    "Conversion",
    "DoesNotExist",
    "EazyrestError",
    "FieldConversionError",
    "Hyperlinked",
    "JSONObject",
    "LimitExceeded",
    "MultipleObjectsReturned",
    "NotFound",
    "Page",
    "PaginationError",
    "PrefetchNotSupported",
    "RedirectError",
    "Request",
    "Response",
    "ResponseShapeError",
    "Results",
    "UsageError",
    "WriteMode",
    "encode_json",
    "follow_link_headers",
    "follow_next_links",
    "json_object",
    "link_headers_page",
    "next_links_page",
    "parse_datetime",
    "parse_duration",
]
