"""Typed JSON object mapping for REST resources."""

from __future__ import annotations

import contextlib
import contextvars
import copy
import datetime
import enum
import inspect
import itertools
import re
import sys
import types
import typing
import uuid
from collections.abc import (
    Callable,
    Iterable,
    Iterator,
    Mapping,
    MutableSet,
    Sequence,
)
from collections.abc import Set as AbstractSet
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

import isodate
import requests

from .api import API
from .dateparse import parse_datetime, parse_duration
from .write_mode import WriteMode, validate_write_mode

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

if sys.version_info >= (3, 14):
    from annotationlib import Format


class EazyrestError(Exception):
    """Base class of the exceptions that eazyrest defines."""


class DoesNotExist(EazyrestError):
    """Raised when no object matches a query that expects one result."""


class MultipleObjectsReturned(EazyrestError):
    """Raised when a query expected one object but got multiple results."""


class PrefetchNotSupported(EazyrestError):
    """Raised when explicit prefetch is requested for an unsupported model."""


class FieldConversionError(EazyrestError, TypeError, ValueError):
    """Raised when a field's value cannot be converted to or from JSON.

    The message names the model and field, and the conversion's own error is
    the ``__cause__``. It is both a ``TypeError`` and a ``ValueError``, the
    errors that conversion raises, so code that catches either one still
    catches it.
    """

    model: type
    """Model whose field failed to convert."""

    field: str
    """Python attribute name of the field."""

    def __init__(self, model: type, field: str, error: Exception) -> None:
        """Describe a conversion error of a field.

        Args:
            model: Model whose field failed to convert.
            field: Python attribute name of the field.
            error: Error that the conversion raised.
        """
        super().__init__(f"{model.__name__}.{field}: {error}")
        self.model = model
        self.field = field


@contextlib.contextmanager
def _converting(model: type, field: str) -> Iterator[None]:
    """Report a conversion error of ``field`` as a FieldConversionError.

    Args:
        model: Model class that owns the field.
        field: Name of the field being converted.

    Yields:
        ``None``, while the conversion runs.

    Raises:
        FieldConversionError: If the conversion raises a ``TypeError`` or a
            ``ValueError``. A ``FieldConversionError`` that the conversion
            raises propagates unchanged.
    """
    try:
        yield
    except FieldConversionError:
        raise
    except (TypeError, ValueError) as err:
        raise FieldConversionError(model, field, err) from err


def _describe_lookup(model: type, params: Mapping[str, Any]) -> str:
    """Describe a lookup for an error message, as in ``Todo title='a'``.

    Args:
        model: Model class the lookup is for.
        params: Lookup parameters, by name.

    Returns:
        The model's name, followed by each parameter as ``name=value`` with the
        value's ``repr()``, or the name alone if there are no parameters.
    """
    if not params:
        return model.__name__

    terms = ", ".join(f"{name}={value!r}" for name, value in params.items())
    return f"{model.__name__} {terms}"


PrefetchMap = dict[str, Mapping[Any, "JSONObject"]]
"""Prefetched related objects keyed by field name and then by primary key."""

PrefetchFields: TypeAlias = str | Sequence[str] | None
"""Related field names requested for explicit prefetch."""

_api_registry: dict[type[JSONObject], API] = {}

_query_api: contextvars.ContextVar[tuple[type[JSONObject], API] | None] = (
    contextvars.ContextVar("_query_api", default=None)
)
"""The class and API of a query that passed ``api=``, while it reads items."""


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
        """Resolve and return the API for a model instance or class.

        Args:
            obj: Model instance the API is read from, or ``None`` when it is
                read from the class.
            objtype: Class the API is read from. If omitted, the class of
                ``obj``.

        Returns:
            The instance's own API override, if it has one. Otherwise, the API
            registered for the nearest class in the MRO of ``objtype``.

        Raises:
            RuntimeError: If both ``obj`` and ``objtype`` are ``None``, or no
                API is registered for ``objtype`` or any of its bases.
        """
        if obj is not None and obj._api_override is not None:
            return obj._api_override

        if obj is None and objtype is not None:
            # While a query that passed api= calls collection_items(), that API
            # is the queried class's API, so a hook such as
            # follow_next_links(cls.api, payload) reads later pages with it.
            query = _query_api.get()
            if query is not None and query[0] is objtype:
                return query[1]

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


def _annotations(cls: type) -> dict[str, Any]:
    """Return the annotations made directly on a class.

    The annotations are not evaluated. A field type may refer to a class that
    is not defined yet, including the class being decorated, so
    ``JSONProperty`` resolves field types when they are first used. An
    annotation may therefore be a string or a ``typing.ForwardRef``.

    Args:
        cls: Class whose annotations are read.

    Returns:
        Mapping from each name annotated directly on ``cls`` to its annotation,
        in declaration order.
    """
    if sys.version_info >= (3, 14):
        # Python 3.14 evaluates annotations on demand, and the default VALUE
        # format raises NameError for a forward reference.
        return inspect.get_annotations(cls, format=Format.FORWARDREF)

    return inspect.get_annotations(cls)


# An unevaluated ClassVar annotation, such as "ClassVar[int]" or
# "typing.ClassVar[int]". Like dataclasses, this recognizes ClassVar by name
# instead of evaluating the annotation.
_CLASS_VAR_RE = re.compile(r"\s*(?:\w+\s*\.\s*)*ClassVar\b")


def _is_class_var(annotation: Any) -> bool:
    """Return whether an annotation, evaluated or not, is a ``ClassVar``.

    Args:
        annotation: Annotation to test, as a type, a string, or a
            ``typing.ForwardRef``.

    Returns:
        Whether ``annotation`` is ``ClassVar`` or a parameterized ``ClassVar``.
    """
    if isinstance(annotation, typing.ForwardRef):
        annotation = annotation.__forward_arg__

    if isinstance(annotation, str):
        return _CLASS_VAR_RE.match(annotation) is not None

    return annotation is ClassVar or typing.get_origin(annotation) is ClassVar


def _is_collection_type(ty: Any) -> bool:
    """Return whether a type represents a collection.

    Args:
        ty: Declared type to test.

    Returns:
        Whether ``ty`` is ``Iterable``, ``list``, ``tuple``, ``set``, or
        ``frozenset``.
    """
    return ty in (Iterable, Sequence, list, tuple, set, frozenset)


def _is_collection_value(value: Any) -> bool:
    """Return whether a value is a collection of items.

    A string, bytes, or a mapping is iterable but is not a collection of items.

    Args:
        value: Value to test.

    Returns:
        Whether ``value`` is iterable and is not a string, bytes, or a mapping.
    """
    return isinstance(value, Iterable) and not isinstance(
        value, (str, bytes, Mapping)
    )


def _is_lazy_collection_type(collection_type: type[Any] | None) -> bool:
    """Return whether a related collection should stay lazy.

    Args:
        collection_type: Collection type declared for the related field, or
            ``None``.

    Returns:
        Whether ``collection_type`` is ``Iterable``.
    """
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
    """Remove any optional wrappers from analyzed type metadata.

    Args:
        analyzed_type: Analyzed type metadata, which may be optional.

    Returns:
        The type metadata inside all ``OptionalType`` wrappers.
    """
    while isinstance(analyzed_type, OptionalType):
        analyzed_type = analyzed_type.inner_type

    return analyzed_type


def _decode_uuid(value: Any) -> uuid.UUID:
    """Decode a JSON UUID string.

    Args:
        value: UUID in any form that ``uuid.UUID`` accepts, such as
            ``"6f1c0f5e-0d3c-4bd9-9c3e-2f6a1d1a7b40"``.

    Returns:
        Decoded ``UUID``.

    Raises:
        TypeError: If ``value`` is not a string.
        ValueError: If ``value`` is not a UUID.
    """
    if not isinstance(value, str):
        raise TypeError(
            f"UUID values must be strings, got {type(value).__name__}"
        )

    return uuid.UUID(value)


def _json_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without equating booleans with numbers.

    Args:
        left: First JSON value.
        right: Second JSON value.

    Returns:
        Whether the values are equal. A boolean equals only the same boolean,
        and dicts, lists, and tuples are compared item by item.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right

    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _json_equal(value, right[key]) for key, value in left.items()
        )

    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right, strict=True)
        )

    return bool(left == right)


class _OwnedJSON(dict[str, Any]):
    """JSON object that the library owns, so a constructor need not copy it.

    A mapping passed to ``JSONObject(json=...)`` by a caller is copied deeply,
    because the caller may change it later. Internal construction from a
    response or a parent's JSON wraps the mapping in this class instead.
    """


_UNCHANGEABLE_VALUES = (
    str,
    bytes,
    int,
    float,
    type(None),
    enum.Enum,
    datetime.date,
    datetime.time,
    datetime.timedelta,
    uuid.UUID,
    frozenset,
)
"""Types of field values that cannot be changed in place.

``bool`` is a subclass of ``int`` and ``datetime.datetime`` of
``datetime.date``.
"""

_MISSING = object()
"""Stands for a key that a JSON object lacks."""


def _query_value(value: Any) -> Any:
    """Return an encoded field value as a query string should send it.

    requests converts a query value with ``str()``, which writes Python's
    ``True`` and ``False``. Send JSON's ``true`` and ``false`` instead, as the
    field's value would be written in a request body.

    Args:
        value: Field value, already encoded as JSON.

    Returns:
        ``"true"`` or ``"false"`` for a boolean, a list of converted items for
        a list, and ``value`` unchanged otherwise.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return [_query_value(item) for item in value]

    return value


def _copy_json(value: Any) -> Any:
    """Return a copy of a JSON value that shares no mutable containers.

    Args:
        value: JSON value to copy.

    Returns:
        A deep copy of a dict or list, and ``value`` itself otherwise.
    """
    if isinstance(value, (dict, list)):
        return copy.deepcopy(value)

    return value


def _normalize_prefetch(prefetch: PrefetchFields) -> tuple[str, ...]:
    """Return prefetch field names as a tuple.

    Args:
        prefetch: One field name, an iterable of field names, or ``None``.

    Returns:
        The field names, or an empty tuple if ``prefetch`` is ``None``.
    """
    if prefetch is None:
        return ()
    if isinstance(prefetch, str):
        return (prefetch,)

    return tuple(prefetch)


T = TypeVar("T", bound="JSONObject")


class RelatedCollection(Iterable[T]):
    """Lazy, single-pass iterable of related objects backed by JSON values.

    Each related object is created when iteration reaches it. The collection
    can be iterated only once, so each related object is created only once, and
    changes made to it while iterating are not lost to a second set of objects.
    """

    _owner: JSONObject
    """Object that owns the related field."""

    _values: tuple[Any, ...]
    """Raw JSON values for the related objects."""

    _related_type: type[T]
    """Model type for each related object."""

    _field: str | None
    """Owning field name used to resolve prefetched related objects."""

    _iterated: bool
    """Whether ``__iter__()`` has been called."""

    def __init__(
        self,
        owner: JSONObject,
        values: Iterable[Any],
        related_type: type[T],
        field: str | None = None,
    ) -> None:
        """Initialize a lazy related-object collection.

        Args:
            owner: Object that owns the related field.
            values: Raw JSON values of the related objects.
            related_type: Model class of the related objects.
            field: Name of the owning field, which finds the related objects
                that a prefetch loaded, or ``None`` for a collection without
                one.
        """
        self._owner = owner
        self._values = tuple(values)
        self._related_type = related_type
        self._field = field
        self._iterated = False

    def __iter__(self) -> Iterator[T]:
        """Return an iterator that creates related objects as it advances.

        Returns:
            Iterator over the related objects.

        Raises:
            RuntimeError: If the collection has already been iterated.
        """
        if self._iterated:
            name = type(self._owner).__name__
            if self._field is not None:
                name = f"{name}.{self._field}"

            raise RuntimeError(
                f"{name} is a lazy related collection, which can be iterated "
                "only once. Call list() on it to iterate more than once, or "
                "annotate the field as list[...] or tuple[..., ...] to keep "
                "its related objects."
            )

        self._iterated = True
        return self._related_objects()

    def _related_objects(self) -> Iterator[T]:
        """Yield the related objects without marking the collection used.

        Yields:
            Each related object, created from the next raw JSON value.
        """
        for value in self._values:
            yield self._owner.create_related(
                value,
                self._related_type,
                field=self._field,
            )


def _declared_fields(
    cls: type[JSONObject], exclude: AbstractSet[str]
) -> list[str]:
    """Return the fields that ``cls`` declares, in declaration order.

    Args:
        cls: Model class whose annotations are read.
        exclude: Names to leave out.

    Returns:
        Names annotated directly on ``cls`` that are not ``ClassVar``
        annotations and are not in ``exclude``.
    """
    return [
        field
        for field, annotation in _annotations(cls).items()
        if field not in exclude and not _is_class_var(annotation)
    ]


def _check_json_keys(
    cls: type[JSONObject],
    field_map: Mapping[str, str],
    exclude: AbstractSet[str],
) -> None:
    """Check that no two fields of ``cls`` share a JSON key.

    Each field caches its own converted value, so assigning one of two fields
    backed by the same key would leave the other reading a stale value.

    Args:
        cls: Class being decorated.
        field_map: Mapping from Python attribute name to JSON key.
        exclude: Annotated attribute names to ignore.

    Raises:
        ValueError: If two fields, declared or inherited, map to one key.
    """
    declared = _declared_fields(cls, exclude)
    inherited = sorted(
        field
        for field in getattr(cls, "_json_fields", ())
        if field not in declared
    )

    fields_by_key: dict[str, str] = {}
    for field in [*inherited, *declared]:
        if field in declared:
            json_field = field_map.get(field, field)
        else:
            json_field = getattr(cls, field).json_field

        other = fields_by_key.setdefault(json_field, field)
        if other != field:
            raise ValueError(
                f"Fields {cls.__name__}.{other} and {cls.__name__}.{field} "
                f"both map to the JSON key {json_field!r}"
            )


def _check_options(
    cls: type[JSONObject],
    pk: str | None,
    field_map: Mapping[str, str],
    exclude: AbstractSet[str],
) -> None:
    """Check that ``json_object()`` options name fields of ``cls``.

    A misspelled option would otherwise be ignored, leaving a field mapped to
    the wrong JSON key or a model without the intended primary key.

    Args:
        cls: Class being decorated.
        pk: Explicit primary-key field name, or ``None``.
        field_map: Mapping from Python attribute name to JSON key.
        exclude: Annotated attribute names to ignore.

    Raises:
        ValueError: If an option names no field of ``cls``.
    """
    annotations = _annotations(cls)
    declared = _declared_fields(cls, exclude)

    unknown_excluded = sorted(set(exclude) - set(annotations))
    if unknown_excluded:
        raise ValueError(
            f"json_object(exclude=...) names {unknown_excluded}, which "
            f"{cls.__name__} does not annotate"
        )

    unknown_mapped = sorted(set(field_map).difference(declared))
    if unknown_mapped:
        raise ValueError(
            f"json_object(field_map=...) maps {unknown_mapped}, which are not "
            f"fields declared on {cls.__name__}"
        )

    inherited_pk: JSONProperty | None = getattr(cls, "_pk", None)
    if (
        pk is not None
        and pk not in declared
        and (inherited_pk is None or inherited_pk.field != pk)
    ):
        raise ValueError(
            f"json_object(pk={pk!r}) names no field declared on "
            f"{cls.__name__} and is not the primary key it inherits"
        )


@overload
def json_object(
    cls: type[T],
    /,
    *,
    pk: str | None = None,
    field_map: Mapping[str, str] | None = None,
    exclude: AbstractSet[str] = frozenset(),
) -> type[T]: ...


@overload
def json_object(
    cls: None = None,
    /,
    *,
    pk: str | None = None,
    field_map: Mapping[str, str] | None = None,
    exclude: AbstractSet[str] = frozenset(),
) -> Callable[[type[T]], type[T]]: ...


def json_object(
    cls: type[T] | None = None,
    /,
    *,
    pk: str | None = None,
    field_map: Mapping[str, str] | None = None,
    exclude: AbstractSet[str] = frozenset(),
) -> type[T] | Callable[[type[T]], type[T]]:
    """Decorate a ``JSONObject`` subclass to wire typed JSON fields.

    The decorator transforms annotated attributes into ``JSONProperty``
    descriptors, enabling lazy conversion between JSON payload values and typed
    Python values.

    Fields declared on a decorated base class remain fields of a decorated
    subclass. An attribute annotated with ``ClassVar``, such as
    ``class_url: ClassVar[str] = "/users/"``, is a class attribute rather than
    a field.

    Args:
        cls: Class being decorated when used as ``@json_object``.
        pk: Name of the primary-key field. It must be a field declared on the
            class or the primary key that the class inherits. If omitted, the
            class keeps the primary key it inherits, even if it redeclares that
            field. A class that inherits none uses a field named ``id``
            declared on the class.
        field_map: Optional mapping from Python attribute name to JSON key.
        exclude: Set of annotated attribute names to ignore.

    Returns:
        The decorated class, or a decorator function when used with arguments.

    Raises:
        ValueError: If a field would replace a ``JSONObject`` attribute, such
            as ``url`` or ``json``. Map such a JSON key to a different
            attribute name with ``field_map``. Also raised if ``pk`` names no
            field of the class, if a ``field_map`` key names no field declared
            on the class, if ``exclude`` names no annotated attribute of the
            class, or if two fields, declared or inherited, map to the same
            JSON key.
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
            ValueError: If a field would replace a ``JSONObject`` attribute, if
                an option names no field of the class, or if two fields map to
                the same JSON key.
        """
        reserved = set(dir(JSONObject)).union(_annotations(JSONObject))
        fields: MutableSet[str] = set(getattr(cls, "_json_fields", ()))
        _check_options(cls, pk, field_map, exclude)
        _check_json_keys(cls, field_map, exclude)

        inherited_pk: JSONProperty | None = getattr(cls, "_pk", None)
        if pk is not None:
            pk_field = pk
        elif inherited_pk is not None:
            # A subclass keeps its primary key, also when it redeclares the
            # field, rather than switching to a new field named id.
            pk_field = inherited_pk.field
        else:
            pk_field = "id"

        # We only process annotations for this class *without* any annotations
        # for superclasses, so we don't use typing.get_type_hints. Fields of
        # decorated superclasses already have descriptors. We also want to
        # delay resolving references, whereas typing.get_type_hints *does*
        # resolve references.
        for field, annotation in _annotations(cls).items():
            if field in exclude or _is_class_var(annotation):
                continue

            if field in reserved:
                raise ValueError(
                    f"Field {cls.__name__}.{field} would replace "
                    f"JSONObject.{field}. Use another attribute name and map "
                    f"it to the JSON key {field!r} with field_map."
                )

            json_field = field_map.get(field, field)

            prop = JSONProperty(
                cls,
                json_field,
                field,
                is_primary_key=field == pk_field,
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
    def ty(self) -> Any:
        """Resolve and return the field type for this property.

        Returns:
            Resolved Python type for the owning class attribute.
        """
        # Evaluate only this field's annotation. Another annotation, such as a
        # ClassVar or an excluded field, may name a type that exists only for
        # type checkers, and it must not make this field unreadable.
        holder = type(
            self.cls.__name__,
            (),
            {
                "__module__": self.cls.__module__,
                "__annotations__": {
                    self.field: _annotations(self.cls)[self.field]
                },
            },
        )
        # A class defined inside a function is absent from its module's
        # globals. Keep its own namespace when resolving self-references.
        localns = dict(vars(self.cls))
        localns[self.cls.__name__] = self.cls
        return typing.get_type_hints(holder, localns=localns)[self.field]

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
            # An object created without a key has none until it loads.
            return getattr(obj, "_pk_value", None)

        json = obj._json_with_field(self.json_field, self.field)
        if self.field in obj._field_cache:
            return obj._field_cache[self.field]

        with _converting(type(obj), self.field):
            value = obj.from_json(
                json[self.json_field],
                self.analyzed_type,
                field=self.field,
            )
        obj._field_cache[self.field] = value
        # A related object saves its own changes.
        if not isinstance(
            value, (*_UNCHANGEABLE_VALUES, JSONObject, RelatedCollection)
        ):
            # The caller can change this value in place. Record its encoding,
            # so that save() can detect the change. Encoding the value rather
            # than copying the JSON keeps a different spelling of the same
            # value, such as "Z" for "+00:00", from counting as a change.
            with _converting(type(obj), self.field):
                encoded = obj.to_json(value, self.analyzed_type)
            obj._field_snapshots[self.field] = _copy_json(encoded)

        return value

    def __set__(self, obj: JSONObject, value: Any) -> None:
        """Set a field value using the object's configured write mode.

        Args:
            obj: ``JSONObject`` instance being modified.
            value: New Python value to assign.

        Raises:
            AttributeError: If the backing JSON does not contain this field.
            FieldConversionError: If the value cannot be converted to the
                field's JSON value, or a primary key that initializes an
                unloaded object is not a valid value of the field's type.
        """
        # Only the first key assignment initializes a lazy object's identity.
        # Later assignments must update the resource at its saved key.
        if (
            self.is_primary_key
            and obj._json is None
            and not hasattr(obj, "_pk_value")
        ):
            # Convert the key as if it were read from JSON, so it has the same
            # type before and after the object loads. A related object passed
            # as the key is kept as is.
            if not isinstance(value, JSONObject):
                conversion = self.analyzed_type
                with _converting(type(obj), self.field):
                    value = obj.from_json(
                        obj.to_json(value, conversion), conversion
                    )

            obj._pk_value = value
            return

        json = obj._json_with_field(self.json_field, self.field)
        with _converting(type(obj), self.field):
            new_value = obj.to_json(value, self.analyzed_type)

        if not _json_equal(json[self.json_field], new_value):
            # Store a copy, so that changing the assigned value in place later
            # cannot match the stored JSON and suppress the next update.
            obj._update_json_field(self.json_field, _copy_json(new_value))

        # Drop cached values only now, because the update may read this field.
        obj._prefetched_related.pop(self.field, None)
        if isinstance(value, JSONObject) and isinstance(
            _unwrap_optional_type(self.analyzed_type), RelatedType
        ):
            # Keep the assigned object, so that reading the field returns it.
            # Assigning the object the field already holds must not discard
            # that object's unsaved changes.
            obj._field_cache[self.field] = value
        else:
            obj._field_cache.pop(self.field, None)

        # An assignment replaces any change made in place to the old value.
        obj._field_snapshots.pop(self.field, None)


class JSONObject:
    """Base class for typed REST resources backed by JSON payloads.

    Subclasses usually define annotated attributes and are decorated with
    ``@json_object`` so each field is backed by a ``JSONProperty`` descriptor.

    Reading a field of an object that has not been loaded sends a ``GET``
    request, so field access can raise ``requests`` exceptions. Instances cache
    state without locking and are not safe to share between threads.
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

    _json_fields: ClassVar[AbstractSet[str]]
    """All JSON fields."""

    prefetch_batch_size: ClassVar[int] = 100
    """Maximum number of parent objects to prefetch in one batch."""

    _pk_value: Any
    """Value of the primary key.

    If None, look in JSON
    """

    _saved_pk: Any
    """Primary key of the saved object while a primary-key change is pending.

    Set only while ``_pending_updates`` holds the primary-key JSON field.
    """

    _api_override: API | None
    """Instance-scoped API override."""

    _write_mode: WriteMode
    """Whether field assignment is saved eagerly or lazily."""

    _pending_updates: dict[str, Any]
    """JSON field updates queued for the next ``save()``."""

    _field_cache: dict[str, Any]
    """Converted field values cached by Python attribute name."""

    _field_snapshots: dict[str, Any]
    """Encodings of cached mutable values when they were read.

    A cached value whose encoding differs from its snapshot was changed in
    place, and ``save()`` sends the change.
    """

    _prefetched_related: PrefetchMap
    """Prefetched related objects keyed by field name and primary key."""

    _json: Any
    """Object's JSON representation."""

    _json_is_partial: bool
    """Whether ``_json`` holds only fields from ``update_from_json()``."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Reject a class that inherits fields from separate models.

        A model inherits its fields through a single chain of decorated
        classes. Fields from two decorated classes, neither of which is a
        subclass of the other, would need their field sets and primary keys
        reconciled, so such a class is an error. Undecorated bases, which
        declare no fields, may be mixed in freely.

        Args:
            **kwargs: Keyword arguments for ``object.__init_subclass__()``.

        Raises:
            TypeError: If two decorated ancestors of the class are not
                subclasses of one another.
        """
        super().__init_subclass__(**kwargs)

        # A decorated class records its fields in its own namespace.
        decorated = [
            base for base in cls.__mro__[1:] if "_json_fields" in vars(base)
        ]
        # In a single chain, the MRO lists each decorated ancestor before the
        # decorated ancestors it inherits from.
        for derived, base in itertools.pairwise(decorated):
            if not issubclass(derived, base):
                raise TypeError(
                    f"{cls.__name__} inherits fields from both "
                    f"{derived.__name__} and {base.__name__}, which are not "
                    "subclasses of one another. Declare the shared fields on "
                    "one decorated base class instead."
                )

    # pylint: disable=redefined-outer-name
    def __init__(
        self,
        *,
        json: Mapping[str, Any] | None = None,
        api: API | None = None,
        write_mode: WriteMode | None = None,
        **kwargs: Any,
    ) -> None:
        """Create a JSON-backed object.

        Args:
            json: Optional JSON payload for eager initialization. The object
                keeps a deep copy, so changing the mapping or its nested values
                later does not change the object, and the object's changes do
                not reach the mapping.
            api: Optional API instance overriding the class-level API. Related
                objects created from this object use the same override.
            write_mode: Optional write mode override. When omitted, the
                instance inherits ``api.default_write_mode`` and otherwise
                falls back to ``"lazy"`` when no API is bound. ``"lazy"``
                batches assignments until ``save()``. ``"eager"`` persists each
                assignment immediately.
            **kwargs: Field values, typically including the primary key. The
                primary key is set first. Every other field value is an
                assignment, as after construction: it loads the object if
                ``json`` is omitted, and in ``"eager"`` mode it sends a
                ``PATCH``.
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
        self._field_snapshots = {}
        self._prefetched_related = {}
        if isinstance(json, _OwnedJSON):
            # The library owns this JSON, such as an item of a collection
            # response or an embedded object in a parent's JSON. It never
            # changes nested values in place, so a copy of the top level, which
            # writes replace, keeps the object from modifying it.
            self._json = dict(json)
        elif json is not None:
            # The caller may change its mapping or the values nested in it.
            self._json = copy.deepcopy(dict(json))
        else:
            self._json = None
        self._json_is_partial = False

        # Set the primary key first, because assigning any other field loads
        # the object from its URL.
        pk_names = {"pk"}
        pk_property = getattr(type(self), "_pk", None)
        if pk_property is not None:
            pk_names.add(pk_property.field)

        for name in sorted(kwargs, key=lambda name: name not in pk_names):
            setattr(self, name, kwargs[name])

    def __repr__(self) -> str:
        """Return the class and primary key without sending a request.

        The key is shown as its JSON value, whether or not the object has
        loaded, as in ``<User id=1>``.

        Returns:
            The class name and primary key in angle brackets, or the class name
            alone, as in ``<User>``, if the class has no primary key.
        """
        name = type(self).__name__
        pk_property: JSONProperty | None = getattr(type(self), "_pk", None)
        if pk_property is None:
            return f"<{name}>"

        state = vars(self)
        json = state.get("_json")
        if json is not None:
            value = json.get(pk_property.json_field)
        else:
            value = state.get("_pk_value")
            # A repr must not fail, so show a key that cannot be encoded as is.
            with contextlib.suppress(TypeError, ValueError):
                value = self.to_json(value, pk_property.analyzed_type)

        return f"<{name} {pk_property.field}={value!r}>"

    def _set_prefetched_related(
        self,
        prefetched_related: Mapping[str, Mapping[Any, JSONObject]],
    ) -> None:
        """Attach prefetched related-object mappings to this instance.

        Args:
            prefetched_related: Mapping from each prefetched field name to the
                related objects loaded for it, keyed by primary key.
        """
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
        # Pass on only an instance override. Without one, the related object
        # uses the API registered for its own class.
        api = self._api_override
        # If the argument is a dict, we treat it as JSON.
        if isinstance(arg, dict):
            return ty(
                json=_OwnedJSON(arg), api=api, write_mode=self.write_mode
            )

        related = ty(pk=arg, api=api, write_mode=self.write_mode)
        if field is not None and field in self._prefetched_related:
            pk = related._normalize_pk(arg)
            if pk in self._prefetched_related[field]:
                return cast(T, self._prefetched_related[field][pk])

        return related

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

        Raises:
            TypeError: If ``conversion`` is a collection type and ``value`` is
                not iterable or is a string, bytes, or dict, or if
                ``conversion`` is not a supported analyzed type.
        """
        if value is None:
            return value

        match conversion:
            case OptionalType(inner_type):
                return self.from_json(value, inner_type, field=field)

            case BaseType(base_type):
                if base_type is datetime.datetime:
                    return parse_datetime(value)
                if base_type is datetime.date:
                    return datetime.date.fromisoformat(value)
                if base_type is uuid.UUID:
                    return _decode_uuid(value)
                if base_type is datetime.timedelta:
                    return parse_duration(value)
                if lenient_issubclass(base_type, enum.Enum):
                    return base_type(value)

                # Changing the returned value in place must not change the
                # object's JSON without recording an update.
                return _copy_json(value)

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

                # Abstract collection types read as tuples.
                if collection_type in (Iterable, Sequence):
                    return tuple(converted_items)

                return collection_type(converted_items)

            case _:
                raise TypeError(
                    f"Unsupported analyzed type {type(conversion).__name__}"
                )

    @classmethod
    def to_json(cls, value: Any, conversion: AnalyzedType) -> Any:
        """Convert a typed Python value to a JSON-serializable value.

        A collection converts to a list.

        Args:
            value: Python value to serialize.
            conversion: Analyzed type metadata that guides conversion.

        Returns:
            JSON-serializable representation of ``value``.

        Raises:
            TypeError: If ``conversion`` is a collection type and ``value`` is
                not a collection. A string, bytes, or a mapping is not a
                collection of items. Also raised if ``conversion`` is
                ``datetime.date`` and ``value`` is a ``datetime.datetime``.
        """
        match conversion:
            case OptionalType(inner_type):
                if value is None:
                    return None

                return cls.to_json(value, inner_type)
            case RelatedType(model_type):
                if isinstance(value, JSONObject):
                    model_type = type(value)
                    value = value.pk

                return model_type.to_json(value, model_type._pk.analyzed_type)
            case BaseType(base_type):
                if base_type is datetime.datetime and isinstance(
                    value, datetime.datetime
                ):
                    return value.isoformat()
                if base_type is datetime.date and isinstance(
                    value, datetime.date
                ):
                    if isinstance(value, datetime.datetime):
                        # A datetime is a date, but its ISO 8601 form has a
                        # time, which a date field cannot read back.
                        raise TypeError(
                            "Date field values must be dates, got datetime"
                        )

                    return value.isoformat()
                if base_type is uuid.UUID and isinstance(value, uuid.UUID):
                    return str(value)
                if base_type is datetime.timedelta and isinstance(
                    value, datetime.timedelta
                ):
                    return isodate.duration_isoformat(value)
                if lenient_issubclass(base_type, enum.Enum):
                    if isinstance(value, base_type):
                        return value.value

                    return value

                return value
            case CollectionType(_, item_type):
                if isinstance(value, RelatedCollection):
                    # Encoding the collection must not use up its single
                    # traversal.
                    value = value._related_objects()
                elif not _is_collection_value(value):
                    raise TypeError(
                        "Collection field values must be collections, "
                        f"got {type(value).__name__}"
                    )

                return [cls.to_json(item, item_type) for item in value]
            case _:
                raise TypeError(
                    f"Unsupported analyzed type {type(conversion).__name__}"
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

    def set_write_mode(self, mode: WriteMode) -> None:
        """Set how field assignments are persisted for this object.

        Args:
            mode: ``"lazy"`` to defer writes until ``save()``, or ``"eager"``
                to persist each assignment immediately.
        """
        self._write_mode = validate_write_mode(mode)

    def _update_json_field(self, json_field: str, value: Any) -> None:
        """Apply a JSON field update using the configured write mode.

        Args:
            json_field: Name of the backing JSON field.
            value: JSON-serializable field value.
        """
        if self.write_mode == "eager":
            self._queue_in_place_changes()
            payload = dict(self._pending_updates)
            payload[json_field] = value
            resp = self.api.patch(self.url, json=payload)
            self._apply_patch_response(resp, payload)
            return

        self._queue_update(json_field, value)

    def _queue_update(self, json_field: str, value: Any) -> None:
        """Apply a JSON field update locally and queue it for ``save()``.

        Args:
            json_field: Name of the backing JSON field.
            value: JSON-serializable field value.
        """
        if (
            json_field == getattr(type(self), "_pk_json_field", None)
            and json_field not in self._pending_updates
        ):
            # Until save() sends the new primary key, the object is still saved
            # under the old one.
            self._saved_pk = self.pk

        self.json[json_field] = value
        self._pending_updates[json_field] = value

    def _queue_in_place_changes(self) -> None:
        """Queue updates for cached values that were changed in place."""
        for field, snapshot in self._field_snapshots.items():
            descriptor = getattr(type(self), field)
            with _converting(type(self), field):
                encoded = self.to_json(
                    self._field_cache[field], descriptor.analyzed_type
                )
            if not _json_equal(encoded, snapshot):
                self._queue_update(descriptor.json_field, _copy_json(encoded))
                self._field_snapshots[field] = _copy_json(encoded)

    def _apply_patch_response(
        self,
        resp: requests.Response,
        payload: Mapping[str, Any],
    ) -> None:
        """Update local state after a successful ``PATCH`` request.

        Args:
            resp: Response to the ``PATCH`` request.
            payload: JSON fields sent in the request.

        Raises:
            TypeError: If the response body does not hold a JSON object. The
                object's state, including its pending updates, is unchanged.
        """
        self._replace_json(self.merge_response(resp, self.json, payload))
        self._pending_updates.clear()

    def _replace_json(self, json: dict[str, Any]) -> None:
        """Replace this object's JSON after a write.

        Cached values stay valid for the fields whose JSON the write left
        unchanged, so only the others are dropped. In particular, a related
        object assigned before ``save()`` stays the field's value.

        Args:
            json: The object's new JSON.
        """
        before = self._json if self._json is not None else {}
        self._json = json

        for field in getattr(self, "_json_fields", ()):
            json_field = getattr(type(self), field).json_field
            if not _json_equal(
                before.get(json_field, _MISSING),
                json.get(json_field, _MISSING),
            ):
                self._field_cache.pop(field, None)
                self._field_snapshots.pop(field, None)
                self._prefetched_related.pop(field, None)

    def merge_response(
        self,
        resp: requests.Response,
        current: Mapping[str, Any],
        sent: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Return this object's JSON after a successful write.

        ``save()``, eager field assignments, and ``create()`` call this hook
        once the server has accepted a write, and the result becomes the
        object's JSON. The default applies the sent fields to the current JSON,
        then merges over them the object that the response body holds, if there
        is a body. A body that omits fields, or no body at all, as with
        ``204 No Content``, therefore keeps their values. A field that the
        server computes, such as a modification time, keeps its old value
        unless the body holds it.

        Override this hook for an API whose write responses hold something
        other than the object, such as a status message. Raise before changing
        any state if the response is unusable. ``save()`` then keeps its
        pending updates, although the server may have applied them.

        Args:
            resp: Successful response to the write.
            current: The object's JSON before the write. It is empty for
                ``create()``.
            sent: JSON fields that the write sent. For ``create()``, these are
                only the JSON-backed fields, so that an extra key such as a
                write-only password is not kept in the object's JSON.

        Returns:
            The object's JSON after the write.

        Raises:
            TypeError: If the response body does not hold a JSON object.
        """
        json = {**current, **sent}
        if resp.content:
            json.update(self._object_from_response(resp.json()))

        return json

    def save(self) -> None:
        """Persist all pending lazy field updates.

        In ``"lazy"`` mode, field assignments are accumulated locally and sent
        in one ``PATCH`` request when ``save()`` is called. In ``"eager"``
        mode, assignments are patched immediately and ``save()`` is a no-op
        unless pending updates remain from an earlier lazy mode.

        A value read from a field and then changed in place, such as a list
        appended to, is saved too, in either mode. Such a change is detected
        only when ``save()`` runs, or when an eager assignment sends a
        ``PATCH``, so ``json`` does not show it before then.

        After a successful ``PATCH``, ``merge_response()`` determines the
        object's JSON. By default, the object keeps its JSON, which already
        includes the saved updates, and the object that the response body
        holds, if any, is merged over it.

        Raises:
            TypeError: If the response body does not hold a JSON object. The
                server may have applied the updates, but they remain pending.
        """
        self._queue_in_place_changes()
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
            json: Full or partial JSON object for this instance. The object
                keeps a deep copy, so changing the mapping or its nested values
                later does not change the object.

        Raises:
            ValueError: If the payload refers to a different primary key.
        """
        payload = copy.deepcopy(dict(json))

        existing_pk = self._persisted_pk()
        incoming_pk = payload.get(self._pk_json_field, existing_pk)
        if self._normalize_pk(incoming_pk) != self._normalize_pk(existing_pk):
            raise ValueError(
                "Incoming JSON primary key does not match this object"
            )

        if self._json is None:
            self._json = {
                self._pk_json_field: self.to_json(
                    existing_pk, type(self)._pk.analyzed_type
                )
            }
            self._json_is_partial = True

        self._json.update(payload)

        for field in self._json_fields:
            descriptor = getattr(type(self), field, None)
            if not isinstance(descriptor, JSONProperty):
                continue

            if descriptor.json_field not in payload:
                continue

            self._field_cache.pop(field, None)
            self._field_snapshots.pop(field, None)
            self._prefetched_related.pop(field, None)
            self._pending_updates.pop(descriptor.json_field, None)

    def refresh(self) -> None:
        """Mark this object stale and discard pending local updates."""
        # Store value of primary key
        self._pk_value = self._persisted_pk()
        # Discard unsaved local changes
        self._pending_updates.clear()
        self._field_cache.clear()
        self._field_snapshots.clear()
        self._prefetched_related.clear()
        # Clear JSON
        self._json = None
        self._json_is_partial = False

    @classmethod
    def register_api(cls, api: API) -> None:
        """Register a default API instance for this model class.

        Args:
            api: API that this class, and any subclass that registers none of
                its own, uses by default.
        """
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

    def _persisted_pk(self) -> Any:
        """Return the primary key under which the object is saved.

        This differs from ``pk`` while a lazy primary-key change waits for
        ``save()``.

        Returns:
            The saved primary key while a primary-key change is pending, and
            ``pk`` otherwise.
        """
        if self._pk_json_field in self._pending_updates:
            return self._saved_pk

        return self.pk

    def _normalize_pk(self, value: Any) -> Any:
        """Return a decoded scalar primary key from a Python or JSON value.

        Args:
            value: Primary key, as a Python value or a JSON value.

        Returns:
            The primary key, encoded and decoded by the primary-key field's
            type. A key that decodes to a model instance gives that instance's
            own decoded primary key.
        """
        conversion = type(self)._pk.analyzed_type
        value = self.from_json(self.to_json(value, conversion), conversion)
        if isinstance(value, JSONObject):
            return value._normalize_pk(value.pk)

        return value

    @property
    def url(self) -> str:
        """Return the resource URL for this object.

        The primary key is converted to its JSON value and percent-encoded, so
        it always forms a single path segment. While a primary-key change waits
        for ``save()``, the URL uses the saved primary key, so ``save()`` sends
        the change to the saved object.

        Returns:
            Relative URL for this object. It ends with ``/`` unless the API's
            ``trailing_slash`` is ``False``.

        Raises:
            ValueError: If the primary key is ``None``, empty, or a dot
                segment. The URL would name another resource, such as the
                collection.
        """
        pk = self.to_json(self._persisted_pk(), type(self)._pk.analyzed_type)
        pk_text = str(pk)
        if pk is None or pk_text in ("", ".", ".."):
            raise ValueError(
                f"{type(self).__name__} object has primary key {pk!r}, "
                "so it has no URL"
            )

        url = f"{self.class_url.rstrip('/')}/{quote(pk_text, safe='')}"

        try:
            trailing_slash = self.api.trailing_slash
        except RuntimeError:
            # A model without an API still has a URL, in the default form.
            trailing_slash = True

        return url + "/" if trailing_slash else url

    @property
    def absolute_url(self) -> str:
        """Return the fully resolved resource URL for this object.

        Returns:
            Absolute URL resolved against the object's API base URL.
        """
        return self.api.resolve_url(self.url)

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

            # The JSON now holds the primary key. An object whose subclass
            # overrides url may have loaded without one.
            vars(self).pop("_pk_value", None)

        return self._json

    def _load_json(self) -> None:
        """Load this object's JSON from the API, keeping pending updates.

        Raises:
            TypeError: If the response does not hold a JSON object.
        """
        # Reloading drops cached values, so queue their in-place changes first.
        self._queue_in_place_changes()
        json = self._object_from_response(self.api.get(self.url).json())

        # Some APIs leave the primary key out of a detail response, because the
        # URL holds it. Keep the key that the object was loaded by, so that the
        # object keeps its identity and URL.
        pk_json_field = getattr(type(self), "_pk_json_field", None)
        if pk_json_field is not None and pk_json_field not in json:
            if self._json is not None and pk_json_field in self._json:
                json[pk_json_field] = self._json[pk_json_field]
            elif "_pk_value" in vars(self):
                json[pk_json_field] = self.to_json(
                    self._pk_value, type(self)._pk.analyzed_type
                )

        # Unsaved lazy updates still take precedence over server state.
        json.update(self._pending_updates)
        self._json = json
        self._json_is_partial = False
        self._field_cache.clear()
        self._field_snapshots.clear()

    def _json_with_field(self, json_field: str, field: str) -> Any:
        """Return this object's JSON after checking that it has a field.

        Args:
            json_field: JSON key to look for.
            field: Python attribute name used in the error message.

        Returns:
            JSON payload containing ``json_field``.

        Raises:
            AttributeError: If the loaded JSON does not contain ``json_field``.
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
                with _converting(cls, field):
                    payload[descriptor.json_field] = cls.to_json(
                        value,
                        descriptor.analyzed_type,
                    )
            else:
                payload[field] = value

        return payload

    @classmethod
    def _json_keys(cls) -> set[str]:
        """Return the JSON keys of this class's JSON-backed fields.

        Returns:
            The JSON key of each JSON-backed field, or an empty set if the
            class has none.
        """
        return {
            getattr(cls, field).json_field
            for field in getattr(cls, "_json_fields", ())
        }

    @classmethod
    def _is_single_collection_item(cls, name: str, value: Any) -> bool:
        """Return whether a query value is one item of a collection field.

        Args:
            name: Name of the field the query value is for.
            value: Query value.

        Returns:
            Whether ``name`` is a JSON-backed field of a collection type,
            possibly optional, and ``value`` is neither ``None`` nor a
            collection of items.
        """
        descriptor = getattr(cls, name, None)
        return (
            isinstance(descriptor, JSONProperty)
            and isinstance(
                _unwrap_optional_type(descriptor.analyzed_type), CollectionType
            )
            and value is not None
            and not _is_collection_value(value)
        )

    @classmethod
    def _encode_query(cls, params: Mapping[str, Any]) -> dict[str, Any]:
        """Encode query parameters as ``_encode_fields()`` encodes fields.

        Unlike a field value, a query value for a collection field may be a
        single item, which matches the objects whose collection holds it. A
        boolean field value is sent as ``true`` or ``false``, as in JSON.

        Args:
            params: Query parameter values keyed as for ``_encode_fields()``.

        Returns:
            Query parameters keyed by JSON key.

        Raises:
            ValueError: If a parameter named after a JSON-backed field, or
                after its JSON key, encodes to ``None`` or to an empty
                collection. A query string has no null value or empty list, and
                requests omits such a parameter, so the query would match
                objects whatever their value for that field.
        """
        payload = cls._encode_fields(
            {
                name: value
                for name, value in params.items()
                if not cls._is_single_collection_item(name, value)
            }
        )
        json_keys = cls._json_keys()
        for name, value in params.items():
            if cls._is_single_collection_item(name, value):
                # A single value, such as a query for the objects whose
                # collection holds it, converts as one item.
                descriptor = getattr(cls, name)
                collection = _unwrap_optional_type(descriptor.analyzed_type)
                assert isinstance(collection, CollectionType)
                with _converting(cls, name):
                    payload[descriptor.json_field] = cls.to_json(
                        value, collection.item_type
                    )

        for name, value in params.items():
            descriptor = getattr(cls, name, None)
            if isinstance(descriptor, JSONProperty):
                json_field = descriptor.json_field
            elif name in json_keys:
                json_field = name
            else:
                continue

            encoded = payload[json_field]
            if encoded is None:
                omitted = "is None"
            elif isinstance(encoded, (list, tuple, set, frozenset)) and (
                len(encoded) == 0
            ):
                omitted = "is an empty collection"
            else:
                continue

            raise ValueError(
                f"Query parameter {name}={value!r} for {cls.__name__} cannot "
                f"be sent, because requests omits parameters whose value "
                f"{omitted}"
            )

        for name in params:
            descriptor = getattr(cls, name, None)
            if isinstance(descriptor, JSONProperty):
                payload[descriptor.json_field] = _query_value(
                    payload[descriptor.json_field]
                )

        return payload

    @classmethod
    def create(
        cls: type[Self], *, api: API | None = None, **kwargs: Any
    ) -> Self:
        """Create and return a new remote object.

        The new object's JSON is the result of ``merge_response()``. By
        default, it holds the JSON-backed fields that were sent, with the
        object that the response body holds merged over them. If a field is in
        neither, as when the body holds only the new primary key, reading it
        loads the object.

        Args:
            api: Optional API to send the request with, instead of the API
                registered for the class. The new object keeps it as their API,
                so their later requests use it too.
            **kwargs: Field values submitted in Python form for JSON-backed
                fields, or already-JSON values for unknown extra keys.

        Returns:
            Newly created object instance.

        Raises:
            TypeError: If the response does not hold a JSON object.
        """
        payload = cls._encode_fields(kwargs)
        client = cls.api if api is None else api
        resp = client.post(cls.class_url, json=payload)

        json_keys = cls._json_keys()
        sent = {
            key: value for key, value in payload.items() if key in json_keys
        }
        obj = cls(json={}, api=api)
        obj._json = obj.merge_response(resp, {}, sent)
        # A new object has no earlier state, so a field that neither the
        # payload nor the response holds is unknown rather than absent.
        obj._json_is_partial = not json_keys.issubset(obj._json)
        return obj

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
            api: The API of the query that prefetches, if it passed ``api=``.
                Prefetch passes it only then, and the bulk request should use
                it instead of the class's registered API.

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
    def collection_items_from_response(
        cls, resp: requests.Response
    ) -> Iterable[Any]:
        """Iterate the JSON objects of a collection response.

        ``filter()`` calls this hook with the response to its request. The
        default passes the decoded payload to ``collection_items()``. Override
        this hook instead when the items or the later pages depend on more than
        the payload, such as a ``Link`` header or the URL of the response. As
        in ``collection_items()``, ``cls.api`` is the query's API while
        ``filter()`` calls this hook.

        Args:
            resp: Response to the collection request.

        Returns:
            Iterable of JSON objects used to construct model instances.
        """
        return cls.collection_items(resp.json())

    @classmethod
    def collection_items(cls, payload: Any) -> Iterable[Any]:
        """Iterate JSON objects for a collection query.

        The default implementation yields items from a list response directly.
        Subclasses can override this hook to unwrap envelope responses or
        follow pagination links by issuing additional requests through
        ``cls.api``. When the query passed ``api=``, ``cls.api`` is that API
        while ``filter()`` calls this hook, so read it during the call rather
        than later, while iterating.

        This hook receives only the decoded payload. To read the response's
        headers or URL, override ``collection_items_from_response()``.

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
            MultipleObjectsReturned: If the payload is a list of more than one
                item.
        """
        if isinstance(payload, list):
            if len(payload) == 0:
                raise DoesNotExist(
                    f"{cls.__name__} response is an empty list, not one object"
                )
            if len(payload) > 1:
                raise MultipleObjectsReturned(
                    f"{cls.__name__} response is a list of {len(payload)} "
                    "items, not one object"
                )

            return payload[0]

        return payload

    @classmethod
    def _object_from_response(cls, payload: Any) -> dict[str, Any]:
        """Return the JSON object that a single-object response holds.

        Args:
            payload: Decoded JSON payload of the response.

        Returns:
            A copy of the mapping that ``object_json()`` extracts.

        Raises:
            TypeError: If ``object_json()`` does not return a mapping.
        """
        json = cls.object_json(payload)
        if not isinstance(json, Mapping):
            raise TypeError(
                f"{cls.__name__} response must hold a JSON object, "
                f"got {type(json).__name__}"
            )

        return dict(json)

    @classmethod
    def _prefetch_info(
        cls,
        field: str,
    ) -> tuple[JSONProperty, RelatedType, type[Any] | None]:
        """Return descriptor and type info for a prefetchable relation.

        Args:
            field: Model attribute name to inspect for prefetch support.

        Returns:
            Tuple containing the JSON field descriptor, related-object type
            metadata, and the collection type for to-many relations. The
            collection type is ``None`` for to-one relations.

        Raises:
            ValueError: If ``field`` is not a JSON-backed related-object field.
            PrefetchNotSupported: If the related model does not override
                ``bulk_get_by_pks()``.
        """
        descriptor = getattr(cls, field, None)
        if not isinstance(descriptor, JSONProperty):
            # The field name is the invalid value, so this is a ValueError even
            # though the check is on the descriptor's type.
            raise ValueError(  # noqa: TRY004
                f"{cls.__name__}.{field} is not a JSON-backed field"
            )

        analyzed_type = _unwrap_optional_type(descriptor.analyzed_type)
        collection_type = None
        if isinstance(analyzed_type, CollectionType):
            collection_type = analyzed_type.collection_type
            analyzed_type = _unwrap_optional_type(analyzed_type.item_type)

        if not isinstance(analyzed_type, RelatedType):
            # As above, the field name is the invalid value.
            raise ValueError(  # noqa: TRY004
                f"{cls.__name__}.{field} is not a related-object field"
            )

        model_type = analyzed_type.model_type
        if not model_type._supports_bulk_get():
            raise PrefetchNotSupported(
                f"{cls.__name__}.{field} cannot be prefetched, because "
                f"{model_type.__name__} does not implement bulk_get_by_pks()"
            )

        return descriptor, analyzed_type, collection_type

    @classmethod
    def _supports_bulk_get(cls) -> bool:
        """Return whether this class overrides ``bulk_get_by_pks()``.

        Returns:
            Whether the nearest class in the MRO that defines
            ``bulk_get_by_pks()`` is not ``JSONObject``.
        """
        owner = next(
            base for base in cls.__mro__ if "bulk_get_by_pks" in vars(base)
        )
        return owner is not JSONObject

    @classmethod
    def _apply_prefetch(
        cls: type[Self],
        items: Iterable[Any],
        fields: Sequence[str],
        api: API | None = None,
    ) -> PrefetchMap:
        """Bulk-load related objects for a batch of parent JSON items.

        Args:
            items: Parent JSON objects to scan for related-object primary keys.
            fields: Model attribute names to prefetch.
            api: The API that the query passed as ``api=``, if any.

        Returns:
            Mapping of each prefetched field name to related objects keyed by
            primary key. Fields with no primary keys to fetch are omitted.

        Raises:
            ValueError: If any field is not a JSON-backed related-object field.
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
                    item
                    for item in raw_items
                    if item is not None and not isinstance(item, dict)
                )

            if len(pks) == 0:
                continue

            # The related model loads through its own registered API, unless
            # the query passed api=.
            bulk_kwargs = {} if api is None else {"api": api}
            related_objects = related_type.model_type.bulk_get_by_pks(
                pks, **bulk_kwargs
            )
            prefetched_related[field] = {
                obj._normalize_pk(pk): obj
                for pk, obj in related_objects.items()
            }

        return prefetched_related

    @classmethod
    def _prefetched_filter(
        cls: type[Self],
        items: Iterable[Any],
        fields: Sequence[str],
        api: API | None = None,
    ) -> Iterator[Self]:
        """Yield prefetched objects lazily in batches.

        Args:
            items: Parent JSON objects returned by a collection query.
            fields: Model attribute names to prefetch for each batch.
            api: The API that the query passed as ``api=``, if any.

        Yields:
            Model instances populated from ``items`` with prefetched related
            objects attached for the requested fields.

        Raises:
            ValueError: If any field is not a JSON-backed related-object field.
        """
        iterator = iter(items)

        while True:
            batch_items = list(
                itertools.islice(iterator, cls.prefetch_batch_size)
            )
            if len(batch_items) == 0:
                return

            prefetched_related = cls._apply_prefetch(batch_items, fields, api)
            parents = [
                cls(json=_OwnedJSON(item), api=api) for item in batch_items
            ]

            # Related objects inherit their parent's write mode. A prefetched
            # object is shared by every parent in the batch, so set its mode
            # once, from the mode the parents are created with, rather than
            # when a parent reads it. Reading a relation through one parent
            # must not change how it saves through another.
            for related_objects in prefetched_related.values():
                for related in related_objects.values():
                    related.set_write_mode(parents[0].write_mode)

            for obj in parents:
                obj._set_prefetched_related(prefetched_related)
                yield obj

    @classmethod
    def filter(
        cls: type[Self],
        *,
        prefetch: PrefetchFields = None,
        api: API | None = None,
        **kwargs: Any,
    ) -> Iterator[Self]:
        """Query objects matching request parameters.

        The request is sent when ``filter()`` is called. Objects are created as
        the returned iterator advances.

        Query parameters are encoded the same way as a ``create()`` payload.
        Parameters named after JSON-backed fields are renamed by ``field_map``
        and converted to JSON values, so ``user=User(id=2)`` is sent as
        ``userId=2`` when ``field_map={"user": "userId"}``. Other parameters
        are sent unchanged.

        A parameter named after a JSON-backed field, or after its JSON key,
        must not be ``None`` or an empty collection. A query string has no null
        value or empty list, and requests omits such a parameter, so the query
        would ignore that field.

        Args:
            prefetch: Optional related field name or field names to bulk-load
                explicitly. Prefetched related objects may be shared by
                identity across parent objects within the same batch.
            api: Optional API to send the request with, instead of the API
                registered for the class. Returned objects keep it as their
                API, so their later requests use it too.
            **kwargs: Query string parameters.

        Returns:
            Iterator over objects built from the response payload.

        Raises:
            ValueError: If a ``prefetch`` field is not a JSON-backed
                related-object field, or if a parameter named after a
                JSON-backed field or its JSON key is ``None`` or an empty
                collection.
            PrefetchNotSupported: If the related model of a ``prefetch`` field
                does not implement ``bulk_get_by_pks()``.
        """
        prefetch_fields = _normalize_prefetch(prefetch)
        # Check the fields before the request, so a misspelled field or a model
        # without bulk lookup fails even when the query matches nothing.
        for field in prefetch_fields:
            cls._prefetch_info(field)

        client = cls.api if api is None else api
        resp = client.get(cls.class_url, params=cls._encode_query(kwargs))
        token = _query_api.set((cls, client))
        try:
            items = cls.collection_items_from_response(resp)
        finally:
            _query_api.reset(token)

        # Treat an empty prefetch list the same as no prefetch at all.
        if len(prefetch_fields) == 0:
            return (cls(json=_OwnedJSON(item), api=api) for item in items)

        return cls._prefetched_filter(items, prefetch_fields, api)

    @classmethod
    def all(
        cls: type[Self],
        *,
        prefetch: PrefetchFields = None,
        api: API | None = None,
    ) -> Iterator[Self]:
        """Return all objects for this resource type.

        Args:
            prefetch: Optional related field name or field names to bulk-load
                explicitly. Prefetched related objects may be shared by
                identity across parent objects within the same batch.
            api: Optional API to send the request with, instead of the API
                registered for the class. Returned objects keep it as their
                API, so their later requests use it too.

        Returns:
            Iterator over all objects.

        Raises:
            ValueError: If a ``prefetch`` field is not a JSON-backed
                related-object field.
            PrefetchNotSupported: If the related model of a ``prefetch`` field
                does not implement ``bulk_get_by_pks()``.
        """
        return cls.filter(prefetch=prefetch, api=api)

    @classmethod
    def get(
        cls: type[Self],
        *,
        prefetch: PrefetchFields = None,
        api: API | None = None,
        **kwargs: Any,
    ) -> Self:
        """Fetch exactly one object matching query parameters.

        Query parameters are encoded as in ``filter()``.

        The server must apply every query parameter. If it ignores one, as
        django-filter does for a parameter that names no filter, this method
        can return an object that does not match it. The returned object is not
        checked against the parameters, because the server decides how they
        match, for example case-insensitively.

        Args:
            prefetch: Optional related field name or field names to bulk-load
                explicitly. Prefetched related objects may be shared by
                identity across parent objects within the same batch.
            api: Optional API to send the request with, instead of the API
                registered for the class. The returned object keeps it as their
                API, so their later requests use it too.
            **kwargs: Query string parameters.

        Returns:
            Single matching object.

        Raises:
            DoesNotExist: If no objects matched.
            MultipleObjectsReturned: If more than one object matched.
            ValueError: If a ``prefetch`` field is not a JSON-backed
                related-object field, or if a parameter named after a
                JSON-backed field or its JSON key is ``None`` or an empty
                collection.
            PrefetchNotSupported: If the related model of a ``prefetch`` field
                does not implement ``bulk_get_by_pks()``.
        """
        results = iter(cls.filter(prefetch=prefetch, api=api, **kwargs))
        first = next(results, None)
        if first is None:
            raise DoesNotExist(f"No {_describe_lookup(cls, kwargs)} exists")

        second = next(results, None)
        if second is not None:
            raise MultipleObjectsReturned(
                f"More than one {_describe_lookup(cls, kwargs)} exists"
            )

        return first

    @classmethod
    def get_or_create(
        cls: type[Self],
        defaults: Mapping[str, Any] | None = None,
        *,
        api: API | None = None,
        **kwargs: Any,
    ) -> tuple[Self, bool]:
        """Get one object by fields or create it if absent.

        The lookup query and the creation payload encode ``kwargs`` the same
        way as ``create()``. JSON-backed fields are renamed by ``field_map``
        and converted to JSON values, so ``user=User(id=2)`` is sent as
        ``userId=2`` when ``field_map={"user": "userId"}``. Other keys are sent
        unchanged.

        As in Django, a key that contains ``__`` and does not name a field,
        such as ``name__iexact`` or ``created__gte``, is a lookup rather than a
        field value. It is sent in the query but not in the creation payload.

        As in ``get()``, the server must apply every lookup parameter. If it
        ignores one, this method can return an object that does not match it,
        with ``created`` false. The lookup and the creation are separate
        requests, so two clients can both create the object. A uniqueness
        constraint on the server must prevent that.

        As in ``filter()``, a lookup field must not be ``None`` or an empty
        collection, because the query would ignore it while the creation
        payload sends ``null`` or ``[]``. Such a value in ``defaults`` is sent
        only on creation and is allowed.

        Args:
            defaults: Optional fields applied only when creating a new object.
            api: Optional API to send the lookup and any creation with, instead
                of the API registered for the class. The returned object keeps
                it as its API, so its later requests use it too.
            **kwargs: Fields used for lookup and included in creation.

        Returns:
            Tuple ``(obj, created)`` where ``created`` indicates whether a new
            object was created.

        Raises:
            MultipleObjectsReturned: If lookup matches multiple objects.
            ValueError: If a lookup parameter named after a JSON-backed field
                or its JSON key is ``None`` or an empty collection.
        """
        results = cls.filter(api=api, **kwargs)
        first = next(results, None)
        if first is not None:
            second = next(results, None)
            if second is not None:
                raise MultipleObjectsReturned(
                    f"More than one {_describe_lookup(cls, kwargs)} exists"
                )

            return first, False

        create_kwargs = {
            name: value
            for name, value in kwargs.items()
            if "__" not in name
            or isinstance(getattr(cls, name, None), JSONProperty)
        }
        if defaults is not None:
            create_kwargs.update(defaults)

        obj = cls.create(api=api, **create_kwargs)
        return obj, True
