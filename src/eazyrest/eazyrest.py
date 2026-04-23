"""Typed JSON object mapping for REST resources."""

from __future__ import annotations

import datetime
import enum
import inspect
import itertools
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
    Literal,
    TypeVar,
    Union,
    cast,
    overload,
)

from typing_extensions import Self

from .api import API
from .dateparse import parse_duration
from .write_mode import WriteMode, validate_write_mode


class DoesNotExist(Exception):
    """Raised when no object matches a query that expects one result."""


class MultipleObjectsReturned(Exception):
    """Raised when a query expected one object but got multiple results."""


class PrefetchNotSupported(Exception):
    """Raised when explicit prefetch is requested for an unsupported model."""


PrefetchMap = dict[str, Mapping[Any, "JSONObject"]]
"""Prefetched related objects keyed by field name and then by primary key."""


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


def _is_collection_type(ty: Any) -> bool:
    """Return whether a type represents a collection."""
    return ty in (Iterable, list, tuple, set, frozenset)


def _is_lazy_collection_type(collection_type: type[Any] | None) -> bool:
    """Return whether a related collection should stay lazy."""
    return collection_type is Iterable


@dataclass(frozen=True)
class TypeClassification:
    """Classify a declared field type for JSON conversion.

    ``value_type`` captures the Python type used for general conversion logic,
    including plain scalar fields such as ``datetime`` and ``timedelta``.

    ``related_type`` is narrower: it is only populated for relationship fields
    and identifies the ``JSONObject`` subclass to instantiate. For related
    fields the two currently match, but for non-related fields only
    ``value_type`` is set.
    """

    kind: Literal["plain", "related_object", "related_collection"] = "plain"
    """High-level conversion category for the declared field type."""

    collection_type: type[Any] | None = None
    """Declared collection class for related collections.

    ``Iterable`` preserves lazy loading. Concrete collection types such as
    ``list`` or ``tuple`` are materialized during JSON conversion.
    """

    is_optional: bool = False
    """Whether the declared field type allows ``None`` values."""

    related_type: type[JSONObject] | None = None
    """Related ``JSONObject`` subclass for relationship fields only."""

    value_type: type[Any] | None = None
    """Python type used by the JSON conversion rules for this field."""


def classify_type(ty: type[Any]) -> TypeClassification:
    """Classify a declared field type.

    Args:
        ty: Declared field type.

    Returns:
        Structured classification of ``ty``.
    """
    ty_origin = typing.get_origin(ty)
    ty_args = typing.get_args(ty)

    if ty_origin in (Union, types.UnionType):
        non_none_args = tuple(arg for arg in ty_args if arg is not type(None))
        if len(non_none_args) == 1 and len(non_none_args) != len(ty_args):
            classification = classify_type(non_none_args[0])
            return TypeClassification(
                kind=classification.kind,
                is_optional=True,
                collection_type=classification.collection_type,
                related_type=classification.related_type,
                value_type=classification.value_type,
            )

    if ty is datetime.datetime:
        return TypeClassification(value_type=ty)

    if ty is datetime.timedelta:
        return TypeClassification(value_type=ty)

    if lenient_issubclass(ty, JSONObject):
        assert issubclass(ty, JSONObject)
        return TypeClassification(
            kind="related_object",
            related_type=ty,
            value_type=ty,
        )

    if (
        _is_collection_type(ty_origin)
        and len(ty_args) == 1
        and lenient_issubclass(ty_args[0], JSONObject)
    ):
        related_type = cast(type[JSONObject], ty_args[0])
        return TypeClassification(
            kind="related_collection",
            collection_type=cast(type[Any], ty_origin),
            related_type=related_type,
            value_type=related_type,
        )

    return TypeClassification(value_type=ty)


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

    Args:
        cls: Class being decorated when used as ``@json_object``.
        pk: Name of the primary-key field in the class.
        field_map: Optional mapping from Python attribute name to JSON key.
        exclude: Set of annotated attribute names to ignore.

    Returns:
        The decorated class, or a decorator function when used with arguments.
    """
    if field_map is None:
        field_map = {}

    def wrap(cls: type[T]) -> type[T]:
        """Install JSON descriptors on a ``JSONObject`` subclass.

        Args:
            cls: Class to modify.

        Returns:
            The same class after descriptor installation.
        """
        fields: MutableSet[str] = set()

        # We only process annotations for this class *without* any annotations
        # for superclasses, so we don't use typing.get_type_hints. We also want
        # to delay resolving references, whereas typing.get_type_hints *does*
        # resolve references.
        for field, ty in inspect.get_annotations(cls, eval_str=False).items():
            if field not in exclude:
                json_field = field_map.get(field, field)

                is_primary_key = field == pk

                prop = JSONProperty(
                    cls, json_field, field, ty, is_primary_key=is_primary_key
                )

                setattr(cls, field, prop)

                fields.add(field)

        # pylint: disable=protected-access
        cls._json_fields = fields

        def _setattr(self: JSONObject, name: str, value: Any) -> None:
            """Restrict arbitrary attribute assignment on JSON-backed models.

            Args:
                self: Instance being modified.
                name: Attribute name being assigned.
                value: Attribute value being assigned.

            Raises:
                AttributeError: If assignment targets an undeclared field.
            """
            if (
                name != "pk"
                and name[0] != "_"
                and name not in cls._json_fields
            ):
                raise AttributeError(
                    f"'{cls.__name__:}' object has no attribute '{name:}'"
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

    _ty: type
    """Field type."""

    json_field: str
    """Name of JSON field corresponding to this property."""

    is_primary_key: bool = False
    """Is this a primary key?"""

    def __init__(
        self,
        cls: type[JSONObject],
        json_field: str,
        field: str,
        ty: type,
        is_primary_key: bool = False,
    ):
        """Initialize a property descriptor.

        Args:
            cls: Model class that owns this descriptor.
            json_field: JSON key backing this attribute.
            field: Python attribute name.
            ty: Declared annotation type.
            is_primary_key: Whether this property stores the primary key.
        """
        self.cls = cls
        self.field = json_field if field is None else field
        self._ty = ty
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
        elif self.json_field not in obj.json:
            raise AttributeError
        else:
            if self.field in obj._field_cache:
                return obj._field_cache[self.field]
            assert self.ty is not None
            value = obj.from_json(
                obj.json[self.json_field],
                self.ty,
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
        elif self.json_field not in obj.json:
            raise AttributeError
        else:
            obj._field_cache.pop(self.field, None)
            obj._prefetched_related.pop(self.field, None)
            assert self.ty is not None
            new_value = obj.to_json(value, self.ty)

            # Access obj.json instead of obj._json to force a load first.
            if obj.json[self.json_field] != new_value:
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

    # pylint: disable=redefined-outer-name
    def __init__(
        self,
        *,
        json: Any = None,
        api: API | None = None,
        write_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Create a JSON-backed object.

        Args:
            json: Optional JSON payload for eager initialization.
            api: Optional API instance overriding the class-level API.
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
        self._json = json

        # Set all attributes passed in as keyword arguments
        for k, v in kwargs.items():
            setattr(self, k, v)

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
        # If the argument is a dict, we treat it as JSON.
        if isinstance(arg, dict):
            return ty(json=arg, api=self.api, write_mode=self.write_mode)
        else:
            assert issubclass(ty, JSONObject)
            kwargs = {ty._pk_json_field: arg}  # pylint: disable=protected-access
            return ty(api=self.api, write_mode=self.write_mode, **kwargs)

    @classmethod
    def related_from_json(
        _cls,
        owner: JSONObject,
        value: Any,
        classification: TypeClassification,
        *,
        field: str | None = None,
    ) -> Any:
        """Convert a related JSON value using overridable default rules.

        Args:
            owner: Object that owns the related field being converted.
            value: Raw JSON value for the related field.
            classification: Classified declared field type.
            field: Optional owning field name for prefetch lookup.

        Returns:
            Converted related object or collection of related objects.

        Raises:
            TypeError: If a related collection value is not iterable.
        """
        assert classification.related_type is not None

        if classification.kind == "related_object":
            return owner.create_related(
                value,
                classification.related_type,
                field=field,
            )

        if classification.kind != "related_collection":
            raise TypeError("related_from_json() requires a related field")

        if not isinstance(value, Iterable) or isinstance(
            value, (str, bytes, dict)
        ):
            raise TypeError(
                "Related collection values must be iterable, "
                f"got {type(value).__name__}"
            )

        related_values = RelatedCollection(
            owner,
            value,
            classification.related_type,
            field=field,
        )

        if _is_lazy_collection_type(classification.collection_type):
            return related_values

        assert classification.collection_type is not None
        return classification.collection_type(related_values)

    def from_json(
        self,
        value: Any,
        ty: type[Any],
        *,
        field: str | None = None,
    ) -> Any:
        """Convert a raw JSON value to a typed Python value.

        Args:
            value: Value read from JSON payload.
            ty: Target Python type.
            field: Optional owning field name for prefetch lookup.

        Returns:
            Converted Python value.
        """
        classification = classify_type(ty)

        if value is None:
            return value
        elif classification.value_type is datetime.datetime:
            return datetime.datetime.fromtimestamp(
                value, datetime.timezone.utc
            )
        elif classification.value_type is datetime.timedelta:
            return parse_duration(value)
        elif lenient_issubclass(classification.value_type, enum.Enum):
            enum_type = classification.value_type
            assert enum_type is not None
            return enum_type(value)
        elif classification.kind in ("related_object", "related_collection"):
            return self.related_from_json(
                self,
                value,
                classification,
                field=field,
            )
        else:
            return value

    @classmethod
    def to_json(cls, value: Any, ty: type[Any]) -> Any:
        """Convert a typed Python value to a JSON-serializable value.

        Args:
            value: Python value to serialize.
            ty: Declared field type.

        Returns:
            JSON-serializable representation of ``value``.
        """
        classification = classify_type(ty)

        if classification.kind == "related_object":
            # If value is an int, assume it is a primary key already
            if isinstance(value, int):
                return value
            else:
                return value.pk
        elif classification.value_type is datetime.datetime:
            return value.timestamp()
        elif classification.value_type is datetime.timedelta:
            return str(value)
        elif lenient_issubclass(classification.value_type, enum.Enum):
            enum_type = classification.value_type
            assert enum_type is not None
            if isinstance(value, enum_type):
                return value.value

            return value
        elif classification.kind == "related_collection":
            return [arg if isinstance(arg, int) else arg.pk for arg in value]
        else:
            return value

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
            self._json = self.object_json(resp.json())
            self._pending_updates.clear()
            self._field_cache.clear()
            self._prefetched_related.clear()
            return

        self.json[json_field] = value
        self._pending_updates[json_field] = value

    def save(self) -> None:
        """Persist all pending lazy field updates.

        In ``"lazy"`` mode, field assignments are accumulated locally and sent
        in one ``PATCH`` request when ``save()`` is called. In ``"eager"``
        mode, assignments are patched immediately and ``save()`` is a no-op
        unless pending updates remain from an earlier lazy mode.
        """
        if len(self._pending_updates) == 0:
            return

        resp = self.api.patch(self.url, json=self._pending_updates)
        self._json = self.object_json(resp.json())
        self._pending_updates.clear()
        self._field_cache.clear()
        self._prefetched_related.clear()

    def update_from_json(self, json: Mapping[str, Any]) -> None:
        """Merge server-provided JSON into this object without a refetch.

        This is useful for out-of-band updates such as websocket messages that
        carry a full or partial object payload. Incoming fields are treated as
        authoritative server state, so any pending local updates for the same
        JSON fields are discarded.

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

    @classmethod
    def register_api(cls, api: API) -> None:
        """Register a default API instance for this model class."""
        _api_registry[cls] = api

    @property
    def pk(self) -> Any:
        """Return this object's primary key value."""
        # If the primary key is another JSON object, return its primary key.
        if isinstance(self._pk, JSONObject):
            return self._pk.pk
        else:
            return self._pk

    @pk.setter
    def pk(self, value: Any) -> None:
        """Set this object's primary key value.

        Args:
            value: Primary-key value or nested ``JSONObject`` used as key.
        """
        # Delegate to the class-installed descriptor instead of shadowing the
        # class variable on this instance.
        cast(JSONProperty, vars(type(self))["_pk"]).__set__(self, value)

    @property
    def url(self) -> str:
        """Return the resource URL for this object.

        Returns:
            Relative URL for this object, always ending with ``/``.
        """
        url = f"{self.class_url.rstrip('/')}/{self.pk}"

        # Ensure url has a trailing slash
        if url[-1] != "/":
            url += "/"

        return url

    @property
    def json(self) -> Any:
        """Return and cache this object's JSON representation.

        Returns:
            JSON payload representing this object.
        """
        if self._json is None:
            self._json = self.object_json(self.api.get(self.url).json())
            self._field_cache.clear()

            # Have have JSON now, so delete _pk_value
            del self._pk_value

        return self._json

    @classmethod
    def create(cls: type[Self], **kwargs: Any) -> Self:
        """Create and return a new remote object.

        Args:
            **kwargs: Field values submitted in Python form for JSON-backed
                fields, or already-JSON values for unknown extra keys.

        Returns:
            Newly created object instance.
        """
        payload: dict[str, Any] = {}

        for field, value in kwargs.items():
            descriptor = getattr(cls, field, None)
            if isinstance(descriptor, JSONProperty):
                payload[descriptor.json_field] = cls.to_json(
                    value,
                    descriptor.ty,
                )
            else:
                payload[field] = value

        resp = cls.api.post(cls.class_url, json=payload)
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
    ) -> tuple[JSONProperty, TypeClassification]:
        """Return descriptor and type info for a prefetchable relation."""
        descriptor = getattr(cls, field, None)
        if not isinstance(descriptor, JSONProperty):
            raise ValueError(
                f"{cls.__name__}.{field} is not a JSON-backed field"
            )

        classification = classify_type(descriptor.ty)
        if classification.kind not in (
            "related_object",
            "related_collection",
        ):
            raise ValueError(
                f"{cls.__name__}.{field} is not a related-object field"
            )

        return descriptor, classification

    @classmethod
    def _apply_prefetch(
        cls: type[Self],
        items: Iterable[Any],
        fields: Sequence[str],
    ) -> PrefetchMap:
        """Bulk-load related objects for a batch of parent JSON items."""
        prefetched_related: PrefetchMap = {}

        for field in fields:
            descriptor, classification = cls._prefetch_info(field)
            assert classification.related_type is not None

            pks: set[Any] = set()

            for item in items:
                if descriptor.json_field not in item:
                    continue

                raw_value = item[descriptor.json_field]

                if raw_value is None:
                    continue

                if classification.kind == "related_object":
                    if not isinstance(raw_value, dict):
                        pks.add(raw_value)
                    continue

                assert classification.kind == "related_collection"
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

            prefetched_related[field] = (
                classification.related_type.bulk_get_by_pks(pks, api=cls.api)
            )

        return prefetched_related

    @classmethod
    def _prefetched_filter(
        cls: type[Self],
        items: Iterable[Any],
        fields: Sequence[str],
    ) -> Iterator[Self]:
        """Yield prefetched objects lazily in batches."""
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
        url: str | None = None,
        *,
        prefetch: Sequence[str] | None = None,
        **kwargs: Any,
    ) -> Iterable[Self]:
        """Query objects matching request parameters.

        Args:
            url: Optional endpoint override; defaults to ``class_url``.
            prefetch: Optional related field names to bulk-load explicitly.
                Prefetched related objects may be shared by identity across
                parent objects within the same batch.
            **kwargs: Query string parameters.

        Returns:
            Iterable of objects built from the response payload.
        """
        if url is None:
            url = cls.class_url

        resp = cls.api.get(url, params=kwargs)
        items = cls.collection_items(resp.json())

        # Treat an empty prefetch list the same as no prefetch at all.
        if not prefetch:
            return (cls(json=item) for item in items)

        return cls._prefetched_filter(items, tuple(prefetch))

    @classmethod
    def all(cls: type[Self]) -> Iterable[Self]:
        """Return all objects for this resource type.

        Returns:
            Iterable of all objects.
        """
        return cls.filter()

    @classmethod
    def get(
        cls: type[Self],
        *,
        prefetch: Sequence[str] | None = None,
        **kwargs: Any,
    ) -> Self:
        """Fetch exactly one object matching query parameters.

        Args:
            prefetch: Optional related field names to bulk-load explicitly.
                Prefetched related objects may be shared by identity across
                parent objects within the same batch.
            **kwargs: Query string parameters.

        Returns:
            Single matching object.

        Raises:
            DoesNotExist: If no objects matched.
            MultipleObjectsReturned: If more than one object matched.
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

        Args:
            defaults: Optional fields applied only when creating a new object.
            **kwargs: Fields used for lookup and included in creation.

        Returns:
            Tuple ``(obj, created)`` where ``created`` indicates whether a new
            object was created.

        Raises:
            MultipleObjectsReturned: If lookup matches multiple objects.
        """
        results = iter(cls.filter(**kwargs))
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
