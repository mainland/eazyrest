"""Public package exports for eazyrest."""

from importlib.metadata import PackageNotFoundError, version

from ._conversion import DEFAULT_CONVERSIONS, Conversion
from ._errors import (
    EazyrestError,
    PaginationError,
    RedirectError,
    ResponseShapeError,
    UsageError,
)
from ._models import (
    DoesNotExist,
    FieldConversionError,
    JSONObject,
    MultipleObjectsReturned,
    PrefetchNotSupported,
    json_object,
)
from .api import API
from .dateparse import parse_datetime, parse_duration
from .pagination import follow_link_headers, follow_next_links
from .write_mode import WriteMode

try:
    __version__ = version("eazyrest")
    """Version of the installed package."""
except PackageNotFoundError:  # pragma: no cover - only without metadata
    __version__ = "0+unknown"

__all__ = [
    "API",
    "DEFAULT_CONVERSIONS",
    "Conversion",
    "DoesNotExist",
    "EazyrestError",
    "FieldConversionError",
    "JSONObject",
    "MultipleObjectsReturned",
    "PaginationError",
    "PrefetchNotSupported",
    "RedirectError",
    "ResponseShapeError",
    "UsageError",
    "WriteMode",
    "follow_link_headers",
    "follow_next_links",
    "json_object",
    "parse_datetime",
    "parse_duration",
]
