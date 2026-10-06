"""Typed JSON object mapping for REST resources."""

from __future__ import annotations

import datetime
import enum
import inspect
import itertools
import sys
import types
import typing
from collections.abc import (
    Callable,
    Iterable,
    Iterator,
    Mapping,
    MutableSet,
    Sequence,
    Set,
)
from dataclasses import dataclass
from functools import cached_property
from typing import (
    Any,
    ClassVar,
    TypeAlias,
    TypeVar,
    Union,
    cast,
    overload,
)
from urllib.parse import quote

import requests

from .api import API
from .dateparse import parse_duration
from .write_mode import WriteMode, validate_write_mode

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

if sys.version_info >= (3, 14):
    from annotationlib import Format


class DoesNotExist(Exception):
    """Raised when no object matches a query that expects one result."""


class MultipleObjectsReturned(Exception):
    """Raised when a query expected one object but got multiple results."""


class PrefetchNotSupported(Exception):
    """Raised when explicit prefetch is requested for an unsupported model."""


PrefetchMap = dict[str, Mapping[Any, "JSONObject"]]
"""Prefetched related objects keyed by field name and then by primary key."""

PrefetchFields: TypeAlias = str | Sequence[str] | None
"""Related field names requested for explicit prefetch."""


_api_registry: dict[type[JSONObject], API] = {}


class APIDescriptor:
    """Descriptor that resolves APIs from instance overrides or a registry."""

    @overload
    def __get__(
        self, obj: None, objtype: type[JSONObject] | None = None
    ) -> API: ...

    @overload
    def __get__(
        self, obj: JSONObject, objtype: type[JSONObject] | None = None
    ) -> API: ...

    def __get__(
        self,
        obj: JSONObject | None,
        objtype: type[JSONObject] | None = None,
    ) -> API:
        """Resolve and return the API for a model instance or class."""
        if obj is not None and obj._api_override is not None:
            return obj._api_override

        if objtype is None:
            if obj is None:
                raise RuntimeError("Could not resolve API owner class")

            objtype = type(obj)

        for cls in objtype.__mro__:
            if cls in _api_registry:
                return _api_registry[cls]

        raise RuntimeError(f"No API registered for {objtype.__name__}")


#
# Taken from:
#   https://github.com/pydantic/pydantic/blob/main/pydantic/_internal/_utils.py
#
def lenient_issubclass(cls: Any, class_or_tuple: Any) -> bool:
    """Return ``issubclass`` result without raising for non-types.

    Args:
        cls: Candidate class object.
        class_or_tuple: Allowed parent class or tuple of parent classes.

    Returns:
        ``True`` when ``cls`` is a subclass of ``class_or_tuple``, otherwise
        ``False``.
    """
    return isinstance(cls, type) and issubclass(cls, class_or_tuple)


def _annotation_names(cls: type) -> list[str]:
    """Return the names annotated directly on a class.

    The annotations are not evaluated. A field type may refer to a class that
    is not defined yet, including the class being decorated, so
    ``JSONProperty`` resolves field types when they are first used.
    """
    if sys.version_info >= (3, 14):
        # Python 3.14 evaluates annotations on demand, and the default VALUE
        # format raises NameError for a forward reference.
        return list(inspect.get_annotations(cls, format=Format.FORWARDREF))

    return list(inspect.get_annotations(cls))


def _is_collection_type(ty: Any) -> bool:
    """Return whether a type represents a collection."""
    return ty in (Iterable, list, tuple, set, frozenset)


def _is_lazy_collection_type(collection_type: type[Any] | None) -> bool:
    """Return whether a related collection should stay lazy."""
    return collection_type is Iterable


@dataclass(frozen=True)
class BaseType:
    """Leaf type metadata for a non-related Python type."""

    base_type: type[Any]


@dataclass(frozen=True)
class RelatedType:
    """Leaf type metadata for a related ``JSONObject`` type."""

    model_type: type[JSONObject]


@dataclass(frozen=True)
class OptionalType:
    """Wrapper type metadata for optional values."""

    inner_type: AnalyzedType


@dataclass(frozen=True)
class CollectionType:
    """Wrapper type metadata for homogeneous collections."""

    collection_type: type[Any]
    item_type: AnalyzedType


AnalyzedType: TypeAlias = (
    BaseType | RelatedType | OptionalType | CollectionType
)


def analyze_type(ty: type[Any]) -> AnalyzedType:
    """Analyze a declared field type into recursive type metadata.

    Args:
        ty: Declared field type.

    Returns:
        Structured type metadata for ``ty``.
    """
    ty_origin = typing.get_origin(ty)
    ty_args = typing.get_args(ty)

    if ty_origin in (Union, types.UnionType):
        non_none_args = tuple(arg for arg in ty_args if arg is not type(None))
        if len(non_none_args) == 1 and len(non_none_args) != len(ty_args):
            return OptionalType(analyze_type(non_none_args[0]))

    if lenient_issubclass(ty, JSONObject):
        assert issubclass(ty, JSONObject)
        return RelatedType(ty)

    if ty_origin is tuple and len(ty_args) == 2 and ty_args[1] is Ellipsis:
        # tuple[T, ...] is a homogeneous tuple of any length.
        ty_args = ty_args[:1]

    if _is_collection_type(ty_origin) and len(ty_args) == 1:
        return CollectionType(
            collection_type=cast(type[Any], ty_origin),
            item_type=analyze_type(ty_args[0]),
        )

    return BaseType(ty)


def _unwrap_optional_type(analyzed_type: AnalyzedType) -> AnalyzedType:
    """Remove any optional wrappers from analyzed type metadata."""
    while isinstance(analyzed_type, OptionalType):
        analyzed_type = analyzed_type.inner_type

    return analyzed_type


def _normalize_prefetch(prefetch: PrefetchFields) -> tuple[str, ...]:
    """Return prefetch field names as a tuple."""
    if prefetch is None:
        return ()
    if isinstance(prefetch, str):
        return (prefetch,)

    return tuple(prefetch)


T = TypeVar("T", bound="JSONObject")


class RelatedCollection(Iterable[T]):
    """Lazy iterable view over related objects backed by JSON values."""

    _owner: JSONObject
    """Object that owns the related field."""

    _values: tuple[Any, ...]
    """Raw JSON values for the related objects."""

    _related_type: type[T]
    """Model type for each related object."""

    _field: str | None
    """Owning field name used to resolve prefetched related objects."""

    def __init__(
        self,
        owner: JSONObject,
        values: Iterable[Any],
        related_type: type[T],
        field: str | None = None,
    ) -> None:
        """Initialize a lazy related-object collection."""
        self._owner = owner
        self._values = tuple(values)
        self._related_type = related_type
        self._field = field

    def __iter__(self) -> Iterator[T]:
        """Yield related objects lazily as iteration advances."""
        for value in self._values:
            yield self._owner.create_related(
                value,
                self._related_type,
                field=self._field,
            )


@overload
def json_object(
    cls: type[T],
    /,
    *,
    pk: str = "id",
    field_map: Mapping[str, str] | None = None,
    exclude: Set[str] = frozenset(),
) -> type[T]: ...


@overload
def json_object(
    cls: None = None,
    /,
    *,
    pk: str = "id",
    field_map: Mapping[str, str] | None = None,
    exclude: Set[str] = frozenset(),
) -> Callable[[type[T]], type[T]]: ...


def json_object(
    cls: type[T] | None = None,
    /,
    *,
    pk: str = "id",
    field_map: Mapping[str, str] | None = None,
    exclude: Set[str] = frozenset(),
) -> type[T] | Callable[[type[T]], type[T]]:
    """Decorate a ``JSONObject`` subclass to wire typed JSON fields.

    The decorator transforms annotated attributes into ``JSONProperty``
    descriptors, enabling lazy conversion between JSON payload values and typed
    Python values.

    Fields declared on a decorated base class remain fields of a decorated
    subclass.

    Args:
        cls: Class being decorated when used as ``@json_object``.
        pk: Name of the primary-key field in the class.
        field_map: Optional mapping from Python attribute name to JSON key.
        exclude: Set of annotated attribute names to ignore.

    Returns:
        The decorated class, or a decorator function when used with arguments.

    Raises:
        ValueError: If a field would replace a ``JSONObject`` attribute, such
            as ``url`` or ``json``. Map such a JSON key to a different
            attribute name with ``field_map``.
    """
    if field_map is None:
        field_map = {}

    def wrap(cls: type[T]) -> type[T]:
        """Install JSON descriptors on a ``JSONObject`` subclass.

        Args:
            cls: Class to modify.

        Returns:
            The same class after descriptor installation.

        Raises:
            ValueError: If a field would replace a ``JSONObject`` attribute.
        """
        reserved = set(dir(JSONObject)).union(_annotation_names(JSONObject))
        fields: MutableSet[str] = set(getattr(cls, "_json_fields", ()))

        # We only process annotations for this class *without* any annotations
        # for superclasses, so we don't use typing.get_type_hints. Fields of
        # decorated superclasses already have descriptors. We also want to
        # delay resolving references, whereas typing.get_type_hints *does*
        # resolve references.
        for field in _annotation_names(cls):
            if field in exclude:
                continue

            if field in reserved:
                raise ValueError(
                    f"Field {cls.__name__}.{field} would replace "
                    f"JSONObject.{field}. Use another attribute name and map "
                    f"it to the JSON key {field!r} with field_map."
                )

            json_field = field_map.get(field, field)

            prop = JSONProperty(
                cls, json_field, field, is_primary_key=field == pk
            )

            setattr(cls, field, prop)

            fields.add(field)

        # pylint: disable=protected-access
        cls._json_fields = frozenset(fields)

        def _setattr(self: JSONObject, name: str, value: Any) -> None:
            """Restrict arbitrary attribute assignment on JSON-backed models.

            Args:
                self: Instance being modified.
                name: Attribute name being assigned.
                value: Attribute value being assigned.

            Raises:
                AttributeError: If assignment targets an undeclared field.
            """
            # Check the fields of the instance's class, which may be a
            # decorated subclass with more fields than cls.
            if (
                name != "pk"
                and not name.startswith("_")
                and name not in type(self)._json_fields
            ):
                raise AttributeError(
                    f"'{type(self).__name__}' object has no attribute '{name}'"
                )

            super(cls, self).__setattr__(name, value)

        cls.__setattr__ = _setattr  # type: ignore[assignment,method-assign]

        return cls

    # See if we're being called as @json_object or @json_object().
    if cls is None:
        # We're called with parens.
        return wrap

    # We're called as @json_object without parens.
    return wrap(cls)


class JSONProperty:
    """Descriptor that maps a typed class attribute to a JSON field."""

    cls: type[JSONObject]
    """Class to which this JSONProperty belongs."""

    field: str
    """Name of Python field corresponding to this property."""

    json_field: str
    """Name of JSON field corresponding to this property."""

    is_primary_key: bool = False
    """Is this a primary key?"""

    def __init__(
        self,
        cls: type[JSONObject],
        json_field: str,
        field: str,
        is_primary_key: bool = False,
    ):
        """Initialize a property descriptor.

        Args:
            cls: Model class that owns this descriptor.
            json_field: JSON key backing this attribute.
            field: Python attribute name.
            is_primary_key: Whether this property stores the primary key.
        """
        self.cls = cls
        self.field = field
        self.json_field = json_field
        self.is_primary_key = is_primary_key

        # If this property is a primary key, then we store a reference to it in
        # the class's _pk attribute and the JSON field corresponding to the
        # primary key in _pk_json_field.
        if self.is_primary_key:
            cls._pk = self
            cls._pk_json_field = json_field

    @cached_property
    def ty(self) -> type:
        """Resolve and return the field type for this property.

        Returns:
            Resolved Python type for the owning class attribute.
        """
        # Resolve type using typing.get_type_hints
        return typing.get_type_hints(self.cls)[self.field]

    @cached_property
    def analyzed_type(self) -> AnalyzedType:
        """Resolve and cache recursive type metadata."""
        return analyze_type(self.ty)

    def __get__(
        self,
        obj: JSONObject | None,
        objtype: type[JSONObject] | None = None,
    ) -> Any:
        """Read a value from the backing JSON or return the descriptor.

        Args:
            obj: ``JSONObject`` instance being accessed, or ``None`` for class
                access.
            objtype: Owner class.

        Returns:
            Converted Python value for the field, or the descriptor itself when
            accessed on the class.

        Raises:
            AttributeError: If instance access targets a field missing from the
                backing JSON.
        """
        if obj is None:
            return self

        if self.is_primary_key and obj._json is None:
            return obj._pk_value

        json = obj._json_with_field(self.json_field, self.field)
        if self.field in obj._field_cache:
            return obj._field_cache[self.field]

        value = obj.from_json(
            json[self.json_field],
            self.analyzed_type,
            field=self.field,
        )
        obj._field_cache[self.field] = value
        return value

    def __set__(self, obj: JSONObject, value: Any) -> None:
        """Set a field value using the object's configured write mode.

        Args:
            obj: ``JSONObject`` instance being modified.
            value: New Python value to assign.

        Raises:
            AttributeError: If the backing JSON does not contain this field.
        """
        if self.is_primary_key and obj._json is None:
            obj._pk_value = value
            return

        json = obj._json_with_field(self.json_field, self.field)
        obj._field_cache.pop(self.field, None)
        obj._prefetched_related.pop(self.field, None)
        new_value = obj.to_json(value, self.analyzed_type)

        if json[self.json_field] != new_value:
            obj._update_json_field(self.json_field, new_value)


class JSONObject:
    """Base class for typed REST resources backed by JSON payloads.

    Subclasses usually define annotated attributes and are decorated with
    ``@json_object`` so each field is backed by a ``JSONProperty`` descriptor.
    """

    class_url: ClassVar[str]
    """Relative URL for this class."""

    api: ClassVar[API] = cast(Any, APIDescriptor())
    """The API associated with this object."""

    _pk: ClassVar[JSONProperty]
    """The JSONProperty that is the primary key."""

    _pk_json_field: ClassVar[str]
    """The field of the object's JSON representation that holds the primary
    key.
    """

    _json_fields: ClassVar[Set[str]]
    """All JSON fields."""

    prefetch_batch_size: ClassVar[int] = 100
    """Maximum number of parent objects to prefetch in one batch."""

    _pk_value: Any
    """Value of the primary key.

    If None, look in JSON
    """

    _api_override: API | None
    """Instance-scoped API override."""

    _write_mode: WriteMode
    """Whether field assignment is saved eagerly or lazily."""

    _pending_updates: dict[str, Any]
    """JSON field updates queued for the next ``save()``."""

    _field_cache: dict[str, Any]
    """Converted field values cached by Python attribute name."""

    _prefetched_related: PrefetchMap
    """Prefetched related objects keyed by field name and primary key."""

    _json: Any
    """Object's JSON representation."""

    _json_is_partial: bool
    """Whether ``_json`` holds only fields from ``update_from_json()``."""

    # pylint: disable=redefined-outer-name
    def __init__(
        self,
        *,
        json: Mapping[str, Any] | None = None,
        api: API | None = None,
        write_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Create a JSON-backed object.

        Args:
            json: Optional JSON payload for eager initialization. The object
                keeps a copy, so later field assignments do not modify the
                caller's mapping.
            api: Optional API instance overriding the class-level API. Related
                objects created from this object use the same override.
            write_mode: Optional write mode override. When omitted, the
                instance inherits ``api.default_write_mode`` and otherwise
                falls back to ``"lazy"`` when no API is bound. ``"lazy"``
                batches assignments until ``save()``. ``"eager"`` persists
                each assignment immediately.
            **kwargs: Field values, typically including the primary key.
        """
        self._api_override = api
        if write_mode is None:
            try:
                write_mode = self.api.default_write_mode
            except RuntimeError:
                write_mode = "lazy"
        self._write_mode = validate_write_mode(write_mode)
        self._pending_updates = {}
        self._field_cache = {}
        self._prefetched_related = {}
        # Copy the payload so lazy writes do not modify the caller's mapping,
        # which for an embedded related object is part of the parent's JSON.
        self._json = None if json is None else dict(json)
        self._json_is_partial = False

        # Set the primary key first, because assigning any other field loads
        # the object from its URL.
        pk_names = {"pk"}
        pk_property = getattr(type(self), "_pk", None)
        if pk_property is not None:
            pk_names.add(pk_property.field)

        for name in sorted(kwargs, key=lambda name: name not in pk_names):
            setattr(self, name, kwargs[name])

    def _set_prefetched_related(
        self,
        prefetched_related: Mapping[str, Mapping[Any, JSONObject]],
    ) -> None:
        """Attach prefetched related-object mappings to this instance."""
        # Copy the outer mapping so each parent can drop an entire prefetched
        # field cache independently while still sharing the related objects.
        self._prefetched_related = dict(prefetched_related)

    def create_related(
        self,
        arg: Any,
        ty: type[T],
        *,
        field: str | None = None,
    ) -> T:
        """Create a related object from embedded JSON or a foreign key.

        Args:
            arg: Either a JSON mapping for eager object creation or a primary
                key value for lazy lookup.
            ty: Related ``JSONObject`` subclass type.
            field: Optional owning field name for prefetch lookup.

        Returns:
            Instance of ``ty``.
        """
        # If the argument is already an instance of ty, keep it as-is.
        if isinstance(arg, ty):
            return arg
        if (
            field is not None
            and not isinstance(arg, dict)
            and field in self._prefetched_related
            and arg in self._prefetched_related[field]
        ):
            return cast(T, self._prefetched_related[field][arg])
        # Pass on only an instance override. Without one, the related object
        # uses the API registered for its own class.
        api = self._api_override
        # If the argument is a dict, we treat it as JSON.
        if isinstance(arg, dict):
            return ty(json=arg, api=api, write_mode=self.write_mode)
        else:
            return ty(pk=arg, api=api, write_mode=self.write_mode)

    def from_json(
        self,
        value: Any,
        conversion: AnalyzedType,
        *,
        field: str | None = None,
    ) -> Any:
        """Convert a raw JSON value to a typed Python value.

        Args:
            value: Value read from JSON payload.
            conversion: Analyzed type metadata that guides conversion.
            field: Optional owning field name for prefetch lookup.

        Returns:
            Converted Python value.
        """
        if value is None:
            return value

        match conversion:
            case OptionalType(inner_type):
                return self.from_json(value, inner_type, field=field)

            case BaseType(base_type):
                if base_type is datetime.datetime:
                    return datetime.datetime.fromtimestamp(
                        value, datetime.timezone.utc
                    )
                if base_type is datetime.timedelta:
                    return parse_duration(value)
                if lenient_issubclass(base_type, enum.Enum):
                    return base_type(value)

                return value

            case RelatedType(model_type):
                return self.create_related(
                    value,
                    model_type,
                    field=field,
                )

            case CollectionType(collection_type, item_type):
                if not isinstance(value, Iterable) or isinstance(
                    value, (str, bytes, dict)
                ):
                    unwrapped_item_type = _unwrap_optional_type(item_type)
                    if isinstance(unwrapped_item_type, RelatedType):
                        kind = "Related collection"
                    else:
                        kind = "Collection"

                    raise TypeError(
                        f"{kind} values must be iterable, "
                        f"got {type(value).__name__}"
                    )

                if _is_lazy_collection_type(collection_type) and isinstance(
                    item_type, RelatedType
                ):
                    return RelatedCollection(
                        self,
                        value,
                        item_type.model_type,
                        field=field,
                    )

                converted_items = (
                    self.from_json(item, item_type, field=field)
                    for item in value
                )

                if collection_type is Iterable:
                    return tuple(converted_items)

                return collection_type(converted_items)

            case _:
                raise TypeError(
                    "Unsupported analyzed type "
                    f"{type(conversion).__name__}"
                )

    @classmethod
    def to_json(cls, value: Any, conversion: AnalyzedType) -> Any:
        """Convert a typed Python value to a JSON-serializable value.

        Args:
            value: Python value to serialize.
            conversion: Analyzed type metadata that guides conversion.

        Returns:
            JSON-serializable representation of ``value``.
        """
        match conversion:
            case OptionalType(inner_type):
                if value is None:
                    return None

                return cls.to_json(value, inner_type)
            case RelatedType():
                # Any value other than a model instance is already a primary
                # key.
                if isinstance(value, JSONObject):
                    return value.pk

                return value
            case BaseType(base_type):
                if base_type is datetime.datetime:
                    return value.timestamp()
                if base_type is datetime.timedelta:
                    return str(value)
                if lenient_issubclass(base_type, enum.Enum):
                    if isinstance(value, base_type):
                        return value.value

                    return value

                return value
            case CollectionType(_, item_type):
                return [cls.to_json(item, item_type) for item in value]
            case _:
                raise TypeError(
                    "Unsupported analyzed type "
                    f"{type(conversion).__name__}"
                )

    def delete(self, *args: Any, **kwargs: Any) -> None:
        """Delete this object from the remote API.

        Args:
            *args: Positional arguments forwarded to ``API.delete``.
            **kwargs: Keyword arguments forwarded to ``API.delete``.
        """
        self.api.delete(self.url, *args, **kwargs)

    @property
    def write_mode(self) -> WriteMode:
        """Return this object's write mode."""
        return self._write_mode

    def set_write_mode(self, mode: str) -> None:
        """Set how field assignments are persisted for this object.

        Args:
            mode: ``"lazy"`` to defer writes until ``save()``, or
                ``"eager"`` to persist each assignment immediately.
        """
        self._write_mode = validate_write_mode(mode)

    def _update_json_field(self, json_field: str, value: Any) -> None:
        """Apply a JSON field update using the configured write mode.

        Args:
            json_field: Name of the backing JSON field.
            value: JSON-serializable field value.
        """
        if self.write_mode == "eager":
            payload = dict(self._pending_updates)
            payload[json_field] = value
            resp = self.api.patch(self.url, json=payload)
            self._apply_patch_response(resp, payload)
            return

        self.json[json_field] = value
        self._pending_updates[json_field] = value

    def _apply_patch_response(
        self,
        resp: requests.Response,
        payload: Mapping[str, Any],
    ) -> None:
        """Update local state after a successful ``PATCH`` request.

        Args:
            resp: Response to the ``PATCH`` request.
            payload: JSON fields sent in the request.
        """
        if resp.content:
            self._json = self.object_json(resp.json())
            self._json_is_partial = False
            self._field_cache.clear()
            self._prefetched_related.clear()
        else:
            # A response such as 204 No Content has no representation of the
            # object, so apply the patch to the local JSON instead.
            self.json.update(payload)

        self._pending_updates.clear()

    def save(self) -> None:
        """Persist all pending lazy field updates.

        In ``"lazy"`` mode, field assignments are accumulated locally and sent
        in one ``PATCH`` request when ``save()`` is called. In ``"eager"``
        mode, assignments are patched immediately and ``save()`` is a no-op
        unless pending updates remain from an earlier lazy mode.

        When the ``PATCH`` response has a body, it replaces the object's JSON.
        When it has no body, as with ``204 No Content``, the object keeps its
        local JSON, which already includes the saved updates.
        """
        if len(self._pending_updates) == 0:
            return

        resp = self.api.patch(self.url, json=self._pending_updates)
        self._apply_patch_response(resp, self._pending_updates)

    def update_from_json(self, json: Mapping[str, Any]) -> None:
        """Merge server-provided JSON into this object without a refetch.

        This is useful for out-of-band updates such as websocket messages that
        carry a full or partial object payload. Incoming fields are treated as
        authoritative server state, so any pending local updates for the same
        JSON fields are discarded.

        If this object has not been loaded, the payload becomes its JSON
        without a request. Reading a field that the payload lacks then loads
        the full object.

        Args:
            json: Full or partial JSON object for this instance.

        Raises:
            ValueError: If the payload refers to a different primary key.
        """
        payload = dict(json)

        existing_pk = self.pk
        incoming_pk = payload.get(self._pk_json_field, existing_pk)
        if incoming_pk != existing_pk:
            raise ValueError(
                "Incoming JSON primary key does not match this object"
            )

        if self._json is None:
            self._json = {self._pk_json_field: existing_pk}
            self._json_is_partial = True

        self._json.update(payload)

        for field in self._json_fields:
            descriptor = getattr(type(self), field, None)
            if not isinstance(descriptor, JSONProperty):
                continue

            if descriptor.json_field not in payload:
                continue

            self._field_cache.pop(field, None)
            self._prefetched_related.pop(field, None)
            self._pending_updates.pop(descriptor.json_field, None)

    def refresh(self) -> None:
        """Mark this object stale and discard pending local updates."""
        # Store value of primary key
        self._pk_value = self.pk
        # Discard unsaved local changes
        self._pending_updates.clear()
        self._field_cache.clear()
        self._prefetched_related.clear()
        # Clear JSON
        self._json = None
        self._json_is_partial = False

    @classmethod
    def register_api(cls, api: API) -> None:
        """Register a default API instance for this model class."""
        _api_registry[cls] = api

    @property
    def pk(self) -> Any:
        """Return this object's primary key value."""
        value = type(self)._pk.__get__(self, type(self))
        # If the primary key is another JSON object, return its primary key.
        if isinstance(value, JSONObject):
            return value.pk
        else:
            return value

    @pk.setter
    def pk(self, value: Any) -> None:
        """Set this object's primary key value.

        Args:
            value: Primary-key value or nested ``JSONObject`` used as key.
        """
        # Delegate to the class-installed descriptor instead of shadowing the
        # class variable on this instance.
        type(self)._pk.__set__(self, value)

    @property
    def url(self) -> str:
        """Return the resource URL for this object.

        The primary key is percent-encoded, so it always forms a single path
        segment.

        Returns:
            Relative URL for this object, always ending with ``/``.
        """
        url = f"{self.class_url.rstrip('/')}/{quote(str(self.pk), safe='')}"

        # Ensure url has a trailing slash
        if url[-1] != "/":
            url += "/"

        return url

    @property
    def absolute_url(self) -> str:
        """Return the fully resolved resource URL for this object.

        Returns:
            Absolute URL resolved against the object's API base URL.
        """
        return self.api._resolve_url(self.url)  # pylint: disable=protected-access

    @property
    def json(self) -> Any:
        """Return and cache this object's JSON representation.

        After ``update_from_json()`` on an object that was not loaded, this is
        the partial payload received so far.

        Returns:
            JSON payload representing this object.
        """
        if self._json is None:
            self._load_json()

            # We have JSON now, so delete _pk_value
            del self._pk_value

        return self._json

    def _load_json(self) -> None:
        """Load this object's JSON from the API, keeping pending updates."""
        json = dict(self.object_json(self.api.get(self.url).json()))
        # Unsaved lazy updates still take precedence over server state.
        json.update(self._pending_updates)
        self._json = json
        self._json_is_partial = False
        self._field_cache.clear()

    def _json_with_field(self, json_field: str, field: str) -> Any:
        """Return this object's JSON after checking that it has a field.

        Args:
            json_field: JSON key to look for.
            field: Python attribute name used in the error message.

        Returns:
            JSON payload containing ``json_field``.

        Raises:
            AttributeError: If the loaded JSON does not contain
                ``json_field``.
        """
        json = self.json
        if json_field not in json and self._json_is_partial:
            # The partial payload from update_from_json() does not say whether
            # the server has this field, so load the full object.
            self._load_json()
            json = self.json

        if json_field not in json:
            raise AttributeError(
                f"'{type(self).__name__}' object has no attribute '{field}': "
                f"JSON field '{json_field}' is missing"
            )

        return json

    @classmethod
    def _encode_fields(cls, fields: Mapping[str, Any]) -> dict[str, Any]:
        """Encode field values as JSON object members.

        Args:
            fields: Values keyed by Python attribute name for JSON-backed
                fields, or by JSON key for other values.

        Returns:
            JSON values keyed by JSON key. JSON-backed fields are renamed by
            ``field_map`` and converted by ``to_json()``. Other values are
            unchanged.
        """
        payload: dict[str, Any] = {}

        for field, value in fields.items():
            descriptor = getattr(cls, field, None)
            if isinstance(descriptor, JSONProperty):
                payload[descriptor.json_field] = cls.to_json(
                    value,
                    descriptor.analyzed_type,
                )
            else:
                payload[field] = value

        return payload

    @classmethod
    def create(cls: type[Self], **kwargs: Any) -> Self:
        """Create and return a new remote object.

        Args:
            **kwargs: Field values submitted in Python form for JSON-backed
                fields, or already-JSON values for unknown extra keys.

        Returns:
            Newly created object instance.
        """
        resp = cls.api.post(cls.class_url, json=cls._encode_fields(kwargs))
        return cls(json=cls.object_json(resp.json()))

    @classmethod
    def bulk_get_by_pks(
        cls: type[Self],
        pks: set[Any],
        *,
        api: API | None = None,
    ) -> dict[Any, Self]:
        """Bulk-load objects by primary key for explicit prefetch support.

        Args:
            pks: Primary keys to load.
            api: Optional API override for the bulk request.

        Returns:
            Mapping from primary key to loaded object.

        Raises:
            PrefetchNotSupported: Always, unless overridden by a subclass.
        """
        del pks, api
        raise PrefetchNotSupported(
            f"{cls.__name__} does not support bulk prefetch"
        )

    @classmethod
    def collection_items(cls, payload: Any) -> Iterable[Any]:
        """Iterate JSON objects for a collection query.

        The default implementation yields items from a list response directly.
        Subclasses can override this hook to unwrap envelope responses or
        follow pagination links by issuing additional requests through
        ``cls.api``.

        Args:
            payload: Decoded JSON payload returned by a collection request.

        Returns:
            Iterable of JSON objects used to construct model instances.

        Raises:
            TypeError: If the payload is not a supported collection response.
        """
        if isinstance(payload, list):
            return payload

        raise TypeError(
            f"{cls.__name__}.collection_items() expected a list response, "
            f"got {type(payload).__name__}"
        )

    @classmethod
    def object_json(cls, payload: Any) -> Any:
        """Return the JSON object contained in a single-object response.

        Subclasses can override this hook when an API wraps single-object
        responses in an envelope or returns singleton lists.

        Args:
            payload: Decoded JSON payload returned by the object endpoint.

        Returns:
            JSON object used to populate a model instance.

        Raises:
            DoesNotExist: If the payload is an empty list.
        """
        if isinstance(payload, list):
            if len(payload) == 0:
                raise DoesNotExist

            return payload[0]

        return payload

    @classmethod
    def _prefetch_info(
        cls,
        field: str,
    ) -> tuple[JSONProperty, RelatedType, type[Any] | None]:
        """Return descriptor and type info for a prefetchable relation.

        Args:
            field: Model attribute name to inspect for prefetch support.

        Returns:
            Tuple containing the JSON field descriptor, related-object
            type metadata, and the collection type for to-many relations. The
            collection type is ``None`` for to-one relations.

        Raises:
            ValueError: If ``field`` is not a JSON-backed related-object
                field.
        """
        descriptor = getattr(cls, field, None)
        if not isinstance(descriptor, JSONProperty):
            raise ValueError(
                f"{cls.__name__}.{field} is not a JSON-backed field"
            )

        analyzed_type = _unwrap_optional_type(descriptor.analyzed_type)
        if isinstance(analyzed_type, RelatedType):
            return descriptor, analyzed_type, None

        if isinstance(analyzed_type, CollectionType):
            item_type = _unwrap_optional_type(analyzed_type.item_type)
            if isinstance(item_type, RelatedType):
                return (
                    descriptor,
                    item_type,
                    analyzed_type.collection_type,
                )

        raise ValueError(
            f"{cls.__name__}.{field} is not a related-object field"
        )

    @classmethod
    def _apply_prefetch(
        cls: type[Self],
        items: Iterable[Any],
        fields: Sequence[str],
    ) -> PrefetchMap:
        """Bulk-load related objects for a batch of parent JSON items.

        Args:
            items: Parent JSON objects to scan for related-object primary
                keys.
            fields: Model attribute names to prefetch.

        Returns:
            Mapping of each prefetched field name to related objects keyed by
            primary key. Fields with no primary keys to fetch are omitted.

        Raises:
            ValueError: If any field is not a JSON-backed related-object
                field.
        """
        prefetched_related: PrefetchMap = {}

        for field in fields:
            descriptor, related_type, collection_type = cls._prefetch_info(
                field
            )

            pks: set[Any] = set()

            for item in items:
                if descriptor.json_field not in item:
                    continue

                raw_value = item[descriptor.json_field]

                if raw_value is None:
                    continue

                if collection_type is None:
                    if not isinstance(raw_value, dict):
                        pks.add(raw_value)
                    continue

                if not isinstance(raw_value, Iterable) or isinstance(
                    raw_value, (str, bytes, dict)
                ):
                    continue

                raw_items = tuple(raw_value)
                # Explicit prefetch only bulk-loads foreign-key style entries.
                # Embedded related JSON objects do not need to be prefetched.
                pks.update(
                    item for item in raw_items if not isinstance(item, dict)
                )

            if len(pks) == 0:
                continue

            # The related model loads through its own registered API.
            prefetched_related[field] = (
                related_type.model_type.bulk_get_by_pks(pks)
            )

        return prefetched_related

    @classmethod
    def _prefetched_filter(
        cls: type[Self],
        items: Iterable[Any],
        fields: Sequence[str],
    ) -> Iterator[Self]:
        """Yield prefetched objects lazily in batches.

        Args:
            items: Parent JSON objects returned by a collection query.
            fields: Model attribute names to prefetch for each batch.

        Yields:
            Model instances populated from ``items`` with prefetched related
            objects attached for the requested fields.

        Raises:
            ValueError: If any field is not a JSON-backed related-object
                field.
        """
        iterator = iter(items)

        while True:
            batch_items = list(
                itertools.islice(iterator, cls.prefetch_batch_size)
            )
            if len(batch_items) == 0:
                return

            prefetched_related = cls._apply_prefetch(batch_items, fields)
            for item in batch_items:
                obj = cls(json=item)
                obj._set_prefetched_related(prefetched_related)
                yield obj

    @classmethod
    def filter(
        cls: type[Self],
        *,
        prefetch: PrefetchFields = None,
        **kwargs: Any,
    ) -> Iterable[Self]:
        """Query objects matching request parameters.

        Args:
            prefetch: Optional related field name or field names to bulk-load
                explicitly. Prefetched related objects may be shared by
                identity across parent objects within the same batch.
            **kwargs: Query string parameters.

        Returns:
            Iterable of objects built from the response payload.

        Raises:
            ValueError: If a ``prefetch`` field is not a JSON-backed
                related-object field.
        """
        prefetch_fields = _normalize_prefetch(prefetch)
        # Check the fields before the request, so a misspelled field fails even
        # when the query matches nothing.
        for field in prefetch_fields:
            cls._prefetch_info(field)

        resp = cls.api.get(cls.class_url, params=kwargs)
        items = cls.collection_items(resp.json())

        # Treat an empty prefetch list the same as no prefetch at all.
        if len(prefetch_fields) == 0:
            return (cls(json=item) for item in items)

        return cls._prefetched_filter(items, prefetch_fields)

    @classmethod
    def all(
        cls: type[Self],
        *,
        prefetch: PrefetchFields = None,
    ) -> Iterable[Self]:
        """Return all objects for this resource type.

        Args:
            prefetch: Optional related field name or field names to bulk-load
                explicitly. Prefetched related objects may be shared by
                identity across parent objects within the same batch.

        Returns:
            Iterable of all objects.

        Raises:
            ValueError: If a ``prefetch`` field is not a JSON-backed
                related-object field.
        """
        return cls.filter(prefetch=prefetch)

    @classmethod
    def get(
        cls: type[Self],
        *,
        prefetch: PrefetchFields = None,
        **kwargs: Any,
    ) -> Self:
        """Fetch exactly one object matching query parameters.

        Args:
            prefetch: Optional related field name or field names to bulk-load
                explicitly. Prefetched related objects may be shared by
                identity across parent objects within the same batch.
            **kwargs: Query string parameters.

        Returns:
            Single matching object.

        Raises:
            DoesNotExist: If no objects matched.
            MultipleObjectsReturned: If more than one object matched.
            ValueError: If a ``prefetch`` field is not a JSON-backed
                related-object field.
        """
        results = iter(cls.filter(prefetch=prefetch, **kwargs))
        first = next(results, None)
        if first is None:
            raise DoesNotExist

        second = next(results, None)
        if second is not None:
            raise MultipleObjectsReturned

        return first

    @classmethod
    def get_or_create(
        cls: type[Self],
        defaults: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[Self, bool]:
        """Get one object by fields or create it if absent.

        The lookup query and the creation payload encode ``kwargs`` the same
        way as ``create()``. JSON-backed fields are renamed by ``field_map``
        and converted to JSON values, so ``user=User(id=2)`` is sent as
        ``userId=2`` when ``field_map={"user": "userId"}``. Other keys are
        sent unchanged.

        Args:
            defaults: Optional fields applied only when creating a new object.
            **kwargs: Fields used for lookup and included in creation.

        Returns:
            Tuple ``(obj, created)`` where ``created`` indicates whether a new
            object was created.

        Raises:
            MultipleObjectsReturned: If lookup matches multiple objects.
        """
        results = iter(cls.filter(**cls._encode_fields(kwargs)))
        first = next(results, None)
        if first is not None:
            second = next(results, None)
            if second is not None:
                raise MultipleObjectsReturned

            return first, False

        create_kwargs = dict(kwargs)
        if defaults is not None:
            create_kwargs.update(defaults)

        obj = cls.create(**create_kwargs)
        return obj, True
