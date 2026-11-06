"""Typed JSON object mapping for REST resources."""

from __future__ import annotations

import contextlib
import contextvars
import copy
import datetime
import decimal
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
    Collection,
    Generator,
    Iterable,
    Iterator,
    Mapping,
    MutableSet,
    Sequence,
)
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from functools import cached_property
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Literal,
    TypeAlias,
    TypeVar,
    Union,
    cast,
    overload,
)
from urllib.parse import quote, urljoin

import requests

from ._conversion import (
    Conversion,
    _active_table,
    _conversion_of,
    _final_supertype,
    _is_instance,
    _table_of,
)
from ._errors import (
    DoesNotExist,
    EazyrestError,
    MultipleObjectsReturned,
    PrefetchNotSupported,
    RedirectError,
    ResponseShapeError,
    UsageError,
)
from ._results import Results, _count_of
from .api import API, _origin
from .write_mode import WriteMode, validate_write_mode

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

if sys.version_info >= (3, 14):
    from annotationlib import Format

if TYPE_CHECKING:
    from pydantic import TypeAdapter


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
def _converting(model: type, field: str) -> Generator[None, None, None]:
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
    """Describe a lookup for an error message, as in ``Todo matching title``.

    The description names the parameters but not their values, which may be
    secrets such as a password or a token.

    Args:
        model: Model class the lookup is for.
        params: Lookup parameters, by name.

    Returns:
        The model's name, followed by the parameter names, or the model's name
        alone if there are no parameters.
    """
    if not params:
        return model.__name__

    return f"{model.__name__} matching {', '.join(params)}"


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
            UsageError: If both ``obj`` and ``objtype`` are ``None``, or no
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
                raise UsageError("Could not resolve API owner class")

            objtype = type(obj)

        for cls in objtype.__mro__:
            if cls in _api_registry:
                return _api_registry[cls]

        raise UsageError(f"No API registered for {objtype.__name__}")


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
    else:
        # Mypy skips the branch a version check rules out, but it would report
        # code after the check as unreachable on Python 3.14.
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
    hyperlinked: bool = False
    """Whether the JSON value is the related object's URL, not its key."""


@dataclass(frozen=True)
class Hyperlinked:
    """Annotation metadata for a relation whose JSON value is a URL.

    Django REST Framework's ``HyperlinkedRelatedField`` and
    ``HyperlinkedModelSerializer`` represent a related object by its URL,
    such as ``"https://api.example.com/users/2/"``, rather than its primary
    key. Annotate such a field as ``Annotated[User, Hyperlinked()]``, or a
    collection as ``list[Annotated[User, Hyperlinked()]]``. Reading it gives a
    related object that loads from that URL, and writing it sends the related
    object's absolute URL.

    A URL is followed only if its origin is the related model's API's, or one
    of that API's ``redirect_origins``. Otherwise reading the field raises
    ``FieldConversionError``, so a server cannot point the client, with its
    credentials, at another host.
    """


@dataclass(frozen=True)
class OptionalType:
    """Wrapper type metadata for optional values."""

    inner_type: AnalyzedType


@dataclass(frozen=True)
class CollectionType:
    """Wrapper type metadata for homogeneous collections."""

    collection_type: type[Any]
    item_type: AnalyzedType


@dataclass(frozen=True)
class PydanticType:
    """Leaf type metadata for a type that Pydantic converts.

    A type that mentions Pydantic, such as a Pydantic model or ``Annotated[int,
    pydantic.Field(gt=0)]``, converts with a Pydantic ``TypeAdapter``.
    """

    annotation: Any
    """Declared type, with its ``Annotated`` metadata."""

    @cached_property
    def adapter(self) -> TypeAdapter[Any]:
        """Return the ``TypeAdapter`` that converts values of the type.

        The adapter converts ``datetime``, ``date``, ``timedelta``, ``UUID``,
        and enum types that the annotation names as eazyrest does. Types inside
        a class that the annotation names, such as the fields of a Pydantic
        model, follow that class's rules.

        Raises:
            TypeError: If Pydantic is not installed, or it cannot convert the
                type.
        """
        try:
            from .pydantic import _type_adapter
        except ImportError as err:
            raise TypeError(
                f"Converting {self.annotation!r} needs Pydantic, which the "
                "eazyrest[pydantic] extra installs"
            ) from err

        return _type_adapter(self.annotation)


AnalyzedType: TypeAlias = (
    BaseType | RelatedType | OptionalType | CollectionType | PydanticType
)


def _is_pydantic_metadata(metadata: Any) -> bool:
    """Return whether ``Annotated`` metadata is for Pydantic.

    Constraints from ``annotated_types``, which other libraries also use, are
    not.

    Args:
        metadata: One item of ``Annotated`` metadata.

    Returns:
        Whether ``metadata`` has Pydantic's schema hook or its type comes from
        the ``pydantic`` package.
    """
    return (
        hasattr(metadata, "__get_pydantic_core_schema__")
        or type(metadata).__module__.partition(".")[0] == "pydantic"
    )


def _mentions_pydantic(ty: Any) -> bool:
    """Return whether a type, or a type inside it, is for Pydantic.

    A class with Pydantic's schema hook, such as a Pydantic model, a class with
    a Pydantic config, such as a ``TypedDict`` decorated with
    ``pydantic.with_config()``, and ``Annotated`` metadata for Pydantic are for
    Pydantic.

    Args:
        ty: Type to inspect. The values of a ``Literal`` are not inspected.

    Returns:
        Whether ``ty``, or any type argument inside it, is for Pydantic.
    """
    origin = typing.get_origin(ty)
    args = typing.get_args(ty)
    if origin is Annotated:
        return _mentions_pydantic(args[0]) or any(
            _is_pydantic_metadata(metadata) for metadata in args[1:]
        )

    # A generic alias, such as Spec[int], does not forward these attributes to
    # the generic class.
    for candidate in (ty, origin):
        if hasattr(candidate, "__get_pydantic_core_schema__") or hasattr(
            candidate, "__pydantic_config__"
        ):
            return True

    if origin is Literal:
        return False

    return any(_mentions_pydantic(arg) for arg in args)


def _is_named_tuple(ty: Any) -> bool:
    """Return whether a type is a ``NamedTuple`` class.

    Args:
        ty: Type to test.

    Returns:
        Whether ``ty`` is a tuple class with named fields.
    """
    return (
        isinstance(ty, type)
        and issubclass(ty, tuple)
        and hasattr(ty, "_fields")
    )


def _is_type_alias(ty: Any) -> bool:
    """Return whether a type is a named type alias.

    A named alias is a ``typing.TypeAliasType``, which the ``type`` statement
    creates, or its ``typing_extensions`` backport. An ordinary assignment, as
    in ``Points = list[Point]``, binds the type itself and is not one.

    Args:
        ty: Type to test.

    Returns:
        Whether ``ty`` is a named type alias.
    """
    return type(ty).__name__ == "TypeAliasType" and hasattr(ty, "__value__")


def _expand_alias(
    alias: Any, args: tuple[Any, ...], expanding: tuple[Any, ...]
) -> Any:
    """Return the type that a named alias, with type arguments, stands for.

    Args:
        alias: Named type alias.
        args: Type arguments for the alias's type parameters, already
            expanded.
        expanding: Aliases whose expansion is in progress.

    Returns:
        The alias's value, with ``args`` in place of its type parameters and
        every named alias inside it expanded.

    Raises:
        TypeError: If the alias refers to itself, or its type parameters are
            not type variables matching ``args``.
    """
    if alias in expanding:
        raise TypeError(
            f"Type alias {alias.__name__} refers to itself, which eazyrest "
            "cannot convert"
        )

    params = alias.__type_params__
    if len(params) != len(args) or not all(
        isinstance(param, TypeVar) for param in params
    ):
        raise TypeError(
            f"Type alias {alias.__name__} needs one type argument for each of "
            "its type parameters, which must be type variables"
        )

    return _expand_aliases(
        _resolve_alias_value(alias),
        dict(zip(params, args, strict=True)),
        (*expanding, alias),
    )


def _has_forward_refs(ty: Any) -> bool:
    """Return whether a type names a type by a string, at any depth.

    Args:
        ty: Type to examine.

    Returns:
        Whether ``ty``, or a type argument within it, is a string or a
        ``ForwardRef``.
    """
    if isinstance(ty, (str, typing.ForwardRef)):
        return True
    if typing.get_origin(ty) is Literal:
        return False
    return any(_has_forward_refs(arg) for arg in typing.get_args(ty))


def _as_forward_refs(ty: Any) -> Any:
    """Return a type with each string in it made a ``ForwardRef``.

    Before Python 3.11, ``typing.get_type_hints()`` resolves a string inside a
    built-in generic, as in ``list["datetime.datetime"]``, only if it is a
    ``ForwardRef``.

    Args:
        ty: Type to convert.

    Returns:
        ``ty`` with each string, also as a type argument, replaced by a
        ``ForwardRef`` to it.
    """
    if isinstance(ty, str):
        return typing.ForwardRef(ty)
    origin = typing.get_origin(ty)
    args = typing.get_args(ty)
    if not args or origin is Literal:
        return ty

    new_args = tuple(_as_forward_refs(arg) for arg in args)
    if all(new is old for new, old in zip(new_args, args, strict=True)):
        return ty
    if origin is Annotated:
        params = new_args
        return Annotated[params]
    if origin is types.UnionType:
        origin = Union

    return origin[new_args]


def _resolve_alias_value(alias: Any) -> Any:
    """Return the value of a named alias with its string references resolved.

    A string inside an alias's value, as in ``list["datetime.datetime"]``,
    names a type in the module that defines the alias. It is resolved there,
    as ``typing.get_type_hints()`` resolves an annotation, so that the alias
    converts as the type it names rather than as an unconverted value.

    Args:
        alias: Named type alias.

    Returns:
        The alias's value, with each string reference replaced by the type it
        names.

    Raises:
        TypeError: If a string reference names nothing in the alias's module.
    """
    value = alias.__value__
    if not _has_forward_refs(value):
        return value

    holder = type(
        alias.__name__,
        (),
        {
            "__module__": alias.__module__,
            "__annotations__": {"value": _as_forward_refs(value)},
        },
    )
    try:
        return typing.get_type_hints(holder, include_extras=True)["value"]
    except Exception as error:
        raise TypeError(
            f"Type alias {alias.__name__} names a type by a string that "
            f"cannot be resolved in module {alias.__module__}"
        ) from error


def _expand_aliases(
    ty: Any,
    substitutions: Mapping[Any, Any] | None = None,
    expanding: tuple[Any, ...] = (),
) -> Any:
    """Return a type with every named type alias in it expanded.

    Args:
        ty: Type to expand.
        substitutions: Types to put in place of type variables, while the
            value of a parameterized alias expands.
        expanding: Aliases whose expansion is in progress.

    Returns:
        ``ty`` with each named alias, also as a type argument, replaced by the
        type it stands for, or ``ty`` itself if it has none.

    Raises:
        TypeError: If an alias cannot be expanded.
    """
    if substitutions and isinstance(ty, TypeVar) and ty in substitutions:
        return substitutions[ty]
    if _is_type_alias(ty):
        return _expand_alias(ty, (), expanding)

    origin = typing.get_origin(ty)
    args = typing.get_args(ty)
    if _is_type_alias(origin):
        args = tuple(
            _expand_aliases(arg, substitutions, expanding) for arg in args
        )
        return _expand_alias(origin, args, expanding)
    if not args or origin is Literal:
        return ty

    new_args = tuple(
        _expand_aliases(arg, substitutions, expanding) for arg in args
    )
    if all(new is old for new, old in zip(new_args, args, strict=True)):
        return ty
    if origin is Annotated:
        params = new_args
        return Annotated[params]
    if origin is types.UnionType:
        origin = Union

    return origin[new_args]


def _hyperlinked(analyzed: AnalyzedType) -> AnalyzedType:
    """Return type metadata whose related types are hyperlinked.

    Args:
        analyzed: Metadata of the type that ``Hyperlinked()`` annotates.

    Returns:
        ``analyzed`` with each related type, also an optional one or the item
        type of a collection, marked as hyperlinked.

    Raises:
        TypeError: If ``analyzed`` holds no related type.
    """
    match analyzed:
        case RelatedType(model_type):
            return RelatedType(model_type, hyperlinked=True)
        case OptionalType(inner_type):
            return OptionalType(_hyperlinked(inner_type))
        case CollectionType(collection_type, item_type):
            return CollectionType(collection_type, _hyperlinked(item_type))
    raise TypeError("Hyperlinked() applies only to a related model type")


def analyze_type(ty: type[Any]) -> AnalyzedType:
    """Analyze a declared field type into recursive type metadata.

    ``Annotated[T, ...]`` analyzes as ``T``, unless its metadata is for
    Pydantic. Then, and for a type below eazyrest's structure of related
    objects, optional values, and collections that mentions Pydantic, Pydantic
    converts the whole type.

    A named type alias, such as one that the ``type`` statement creates,
    analyzes as the type it stands for.

    Args:
        ty: Declared field type.

    Returns:
        Structured type metadata for ``ty``.

    Raises:
        TypeError: If ``ty`` holds a named type alias that refers to itself, or
            whose type parameters cannot be substituted.
    """
    ty = _expand_aliases(ty)
    ty_origin = typing.get_origin(ty)
    ty_args = typing.get_args(ty)

    if ty_origin is Annotated:
        if any(_is_pydantic_metadata(metadata) for metadata in ty_args[1:]):
            return PydanticType(ty)

        inner = analyze_type(ty_args[0])
        if any(isinstance(metadata, Hyperlinked) for metadata in ty_args[1:]):
            return _hyperlinked(inner)
        return inner

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

    if _mentions_pydantic(ty):
        return PydanticType(ty)

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

    def __init__(self, value: Mapping[str, Any]) -> None:
        """Validate an object response before taking ownership of it.

        Args:
            value: JSON object from a response or collection hook.

        Raises:
            ResponseShapeError: If the value is not a mapping.
        """
        if not isinstance(value, Mapping):
            raise ResponseShapeError(
                "Collection items must be JSON objects, "
                f"got {type(value).__name__}"
            )
        super().__init__(value)


_UNCHANGEABLE_VALUES = (
    str,
    bytes,
    int,
    float,
    decimal.Decimal,
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


def _query_params(
    params: Mapping[str, Any] | None,
    kwargs: Mapping[str, Any],
    kind: str = "Query parameter",
    argument: str = "params",
) -> dict[str, Any]:
    """Return the parameters given as a mapping and as keywords.

    Args:
        params: Parameters passed as a mapping, whose names may be anything,
            including the names that the methods reserve.
        kwargs: Parameters passed as keyword arguments.
        kind: What a parameter is, for the error message.
        argument: Name of the mapping argument, for the error message.

    Returns:
        Every parameter, keyed by name.

    Raises:
        TypeError: If a name is given both ways.
    """
    if params is None:
        return dict(kwargs)
    both = sorted(set(params) & set(kwargs))
    if both:
        raise TypeError(
            f"{kind} {both[0]} is given both as a keyword argument and in "
            f"{argument}"
        )
    return {**params, **kwargs}


# Keyword arguments that the query methods reserve without yet taking them, so
# that taking them later does not change what an existing call sends.
_RESERVED_QUERY_KEYWORDS = frozenset({"request_options"})


def _check_query_keywords(kwargs: Mapping[str, Any]) -> None:
    """Reject query keyword arguments that the query methods reserve.

    Args:
        kwargs: Keyword arguments passed to a query method.

    Raises:
        TypeError: If a keyword argument is reserved.
    """
    reserved = sorted(_RESERVED_QUERY_KEYWORDS.intersection(kwargs))
    if reserved:
        raise TypeError(
            f"The query methods reserve the keyword argument {reserved[0]}. "
            "Pass a query parameter of that name in params."
        )


# Request arguments that eazyrest sets itself for a write.
_WRITE_BODY_ARGUMENTS = frozenset({"json", "data", "files"})


def _request_options(options: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the request options of a write, checked.

    Args:
        options: Keyword arguments for the request, such as ``headers`` or
            ``params``, as ``requests.Session.request()`` takes them.

    Returns:
        A copy of ``options``, or an empty dict.

    Raises:
        TypeError: If ``options`` sets the request body, which the write
            builds itself, or enables streaming of the response.
    """
    if options is None:
        return {}
    if options.get("stream"):
        raise TypeError(
            "Model writes cannot stream their responses. Use an API method "
            "directly to read a streamed response."
        )
    body = sorted(_WRITE_BODY_ARGUMENTS.intersection(options))
    if body:
        raise TypeError(
            f"request_options cannot set {', '.join(body)}, because the write "
            "builds the request body itself"
        )
    return dict(options)


def _form_value(name: str, value: Any) -> Any:
    """Return a JSON value as a multipart form field's value.

    Args:
        name: Name of the form field, for the error message.
        value: JSON value of the field.

    Returns:
        A string, or a list of strings for a list of scalars, which requests
        sends as repeated fields. Booleans become ``true`` and ``false``.

    Raises:
        TypeError: If the value is ``None``, a mapping, an empty list, or a
            list that holds anything but scalars, which a form field cannot
            express.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    if (
        isinstance(value, list)
        and value
        and all(isinstance(item, (str, int, float)) for item in value)
    ):
        return [_form_value(name, item) for item in value]
    raise TypeError(
        f"Field {name} cannot be sent in a multipart request with files, "
        f"because its value is {type(value).__name__}, which a form field "
        "cannot express. Save it in a separate write without files."
    )


def _check_model_write(resp: requests.Response) -> requests.Response:
    """Require a completed write before acknowledging a model change.

    Args:
        resp: Response that the API accepted.

    Returns:
        The response when no redirect remains to be followed.

    Raises:
        RedirectError: If the response is a final 3xx status.
    """
    if 300 <= resp.status_code < 400:
        resp.close()
        raise RedirectError(
            f"{resp.request.method} {resp.url} returned status "
            f"{resp.status_code}, so the model write was not confirmed",
            response=resp,
        )
    return resp


def _query_type(conversion: AnalyzedType) -> AnalyzedType:
    """Use primary keys for related query values, including hyperlinks.

    Args:
        conversion: Field's read/write conversion.

    Returns:
        The same conversion with each hyperlink represented by a key.
    """
    match conversion:
        case RelatedType(model_type):
            return RelatedType(model_type)
        case OptionalType(inner_type):
            return OptionalType(_query_type(inner_type))
        case CollectionType(collection_type, item_type):
            return CollectionType(collection_type, _query_type(item_type))
    return conversion


def _materialize_query_value(value: Any) -> Any:
    """Return a query value with any collection read into a list.

    requests iterates every value that is iterable and is not a string or
    bytes, so an iterator, a ``deque``, a ``range``, or a dictionary view is a
    collection to it, as a list is. Reading the collection once lets the value
    be checked and then sent without iterating it again.

    Args:
        value: Query value, already encoded as JSON where it names a field.

    Returns:
        A list of the items of a collection that is not a mapping, and
        ``value`` itself otherwise.
    """
    if _is_collection_value(value) and not isinstance(value, list):
        return list(value)
    return value


def _unsendable_query_value(value: Any) -> str | None:
    """Return why requests cannot send a value as a query parameter.

    requests omits a parameter whose value is ``None`` or an empty collection,
    and it omits a ``None`` item of a collection. It sends a mapping as its
    keys, and it flattens a collection nested in another into the outer one's
    items. A query that loses or changes a constraint this way matches objects
    it should not.

    Args:
        value: Query value, as ``_materialize_query_value()`` returns it.

    Returns:
        A clause that completes "because its value", or ``None`` if requests
        sends ``value`` intact.
    """
    if value is None:
        return "is None, and requests omits it"
    if isinstance(value, Mapping):
        return "is a mapping, and requests sends only its keys"
    if not _is_collection_value(value):
        return None
    if not value:
        return "is an empty collection, and requests omits it"
    if any(item is None for item in value):
        return "contains None, and requests omits that item"
    if any(
        isinstance(item, Mapping) or _is_collection_value(item)
        for item in value
    ):
        return "contains a collection, and requests flattens it"

    return None


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


def _checked_hyperlink(api: API, url: str) -> str:
    """Return a hyperlink as an absolute URL, after checking its origin.

    Args:
        api: API that the related object uses.
        url: URL that the JSON holds, absolute or relative to the API's base
            URL.

    Returns:
        The absolute URL.

    Raises:
        ValueError: If the URL's origin is neither the API's nor one of its
            ``redirect_origins``. The message does not quote the URL.
    """
    allowed = {_origin(api.base_url), *map(_origin, api.redirect_origins)}
    # urljoin() also rejects some URLs, such as one whose host is "[bad]", with
    # a message that quotes part of the URL.
    try:
        absolute = urljoin(api.base_url, url)
        origin = _origin(absolute)
    except (ValueError, requests.RequestException) as err:
        raise ValueError("Hyperlinked field value is not a valid URL") from err
    if origin not in allowed:
        raise ValueError(
            "Hyperlinked field value names an origin other than the API's. "
            "Add the origin to API.redirect_origins to follow it."
        )
    return absolute


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
        hyperlinked: bool = False,
    ) -> None:
        """Initialize a lazy related-object collection.

        Args:
            owner: Object that owns the related field.
            values: Raw JSON values of the related objects.
            related_type: Model class of the related objects.
            field: Name of the owning field, which finds the related objects
                that a prefetch loaded, or ``None`` for a collection without
                one.
            hyperlinked: Whether the raw values are the related objects'
                URLs.
        """
        self._owner = owner
        self._values = tuple(values)
        self._related_type = related_type
        self._field = field
        self._hyperlinked = hyperlinked
        self._iterated = False

    def __iter__(self) -> Iterator[T]:
        """Return an iterator that creates related objects as it advances.

        Returns:
            Iterator over the related objects.

        Raises:
            UsageError: If the collection has already been iterated.
        """
        if self._iterated:
            name = type(self._owner).__name__
            if self._field is not None:
                name = f"{name}.{self._field}"

            raise UsageError(
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
            yield self._owner._create_related(
                value,
                self._related_type,
                field=self._field,
                hyperlinked=self._hyperlinked,
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
    write_map: Mapping[str, str] | None = None,
) -> None:
    """Check that no two fields of ``cls`` share a JSON key.

    Each field caches its own converted value, so assigning one of two fields
    backed by the same key would leave the other reading a stale value. Two
    fields written under one key would overwrite each other in a write.

    Args:
        cls: Class being decorated.
        field_map: Mapping from Python attribute name to JSON key.
        exclude: Annotated attribute names to ignore.
        write_map: Mapping from Python attribute name to the JSON key that
            writes use.

    Raises:
        ValueError: If two fields, declared or inherited, map to one key, for
            reads or for writes.
    """
    write_map = write_map or {}
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

    write_fields_by_key: dict[str, str] = {}
    for field in [*inherited, *declared]:
        if field in declared:
            write_field = write_map.get(field, field_map.get(field, field))
        else:
            write_field = getattr(cls, field).write_field

        other = write_fields_by_key.setdefault(write_field, field)
        if other != field:
            raise ValueError(
                f"Fields {cls.__name__}.{other} and {cls.__name__}.{field} "
                f"are both written under the JSON key {write_field!r}"
            )


def _check_options(
    cls: type[JSONObject],
    pk: str | None,
    field_map: Mapping[str, str],
    exclude: AbstractSet[str],
    write_only: AbstractSet[str] = frozenset(),
    write_map: Mapping[str, str] | None = None,
    read_only: AbstractSet[str] = frozenset(),
) -> None:
    """Check that ``json_object()`` options name fields of ``cls``.

    A misspelled option would otherwise be ignored, leaving a field mapped to
    the wrong JSON key or a model without the intended primary key.

    Args:
        cls: Class being decorated.
        pk: Explicit primary-key field name, or ``None``.
        field_map: Mapping from Python attribute name to JSON key.
        exclude: Annotated attribute names to ignore.
        write_only: Names of fields that are written but never read.
        write_map: Mapping from Python attribute name to the JSON key that
            writes use.
        read_only: Names of fields whose Python values cannot be written.

    Raises:
        ValueError: If an option names no field of ``cls``, or the primary key
            is write-only.
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

    unknown_write_mapped = sorted(set(write_map or {}).difference(declared))
    if unknown_write_mapped:
        raise ValueError(
            f"json_object(write_map=...) maps {unknown_write_mapped}, which "
            f"are not fields declared on {cls.__name__}"
        )

    unknown_write_only = sorted(set(write_only).difference(declared))
    if unknown_write_only:
        raise ValueError(
            f"json_object(write_only=...) names {unknown_write_only}, which "
            f"are not fields declared on {cls.__name__}"
        )

    unknown_read_only = sorted(set(read_only).difference(declared))
    if unknown_read_only:
        raise ValueError(
            f"json_object(read_only=...) names {unknown_read_only}, which "
            f"are not fields declared on {cls.__name__}"
        )
    if read_only & write_only:
        raise ValueError("A field cannot be both read-only and write-only")

    inherited_pk: JSONProperty | None = getattr(cls, "_pk", None)
    key = pk or (inherited_pk.field if inherited_pk is not None else "id")
    if key in write_only:
        raise ValueError(
            f"json_object(write_only=...) names {key!r}, the primary key of "
            f"{cls.__name__}, which must be readable"
        )
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
    write_only: AbstractSet[str] | None = None,
    write_map: Mapping[str, str] | None = None,
    read_only: AbstractSet[str] | None = None,
) -> type[T]: ...


@overload
def json_object(
    cls: None = None,
    /,
    *,
    pk: str | None = None,
    field_map: Mapping[str, str] | None = None,
    exclude: AbstractSet[str] = frozenset(),
    write_only: AbstractSet[str] | None = None,
    write_map: Mapping[str, str] | None = None,
    read_only: AbstractSet[str] | None = None,
) -> Callable[[type[T]], type[T]]: ...


def json_object(
    cls: type[T] | None = None,
    /,
    *,
    pk: str | None = None,
    field_map: Mapping[str, str] | None = None,
    exclude: AbstractSet[str] = frozenset(),
    write_only: AbstractSet[str] | None = None,
    write_map: Mapping[str, str] | None = None,
    read_only: AbstractSet[str] | None = None,
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
        write_map: Optional mapping from Python attribute name to the JSON
            key that writes use, for a field that the API returns under one
            key and accepts under another. Django REST Framework, for
            example, often returns a related object nested under ``user`` and
            accepts its primary key as ``user_id``. Reads, queries, and the
            object's JSON use the field's key. ``create()``, ``save()``, and
            eager assignments send the field under its write key.
        write_only: Names of fields that the API accepts in writes but never
            returns, such as a password. Such a field can be passed to
            ``create()`` and assigned, and the next write sends it, but it is
            not kept in the object's JSON, and reading it raises
            ``AttributeError``.
        read_only: Names of fields whose Python values cannot be assigned or
            passed to ``create()`` and are omitted from full saves. An initial
            primary key can still identify a lazy object. Files passed in
            ``files=`` are separate upload values and may name these fields.

    A redeclared field inherits its read and write keys unless a mapping
    explicitly replaces them. Omitted ``write_only`` and ``read_only`` options
    inherit those policies too. An explicit set applies to every redeclared
    field, so an empty set removes its inherited policy.

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
    if write_map is None:
        write_map = {}

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
        read_keys = dict(field_map)
        write_keys = dict(write_map)
        hidden = set(write_only or ())
        readonly = set(read_only or ())
        for field in _declared_fields(cls, exclude):
            # Look the attribute up without running descriptors. A field named
            # like the class's ``api`` would otherwise resolve the API, which
            # fails before one is registered, instead of reaching the check
            # that refuses the name.
            inherited = inspect.getattr_static(cls, field, None)
            if not isinstance(inherited, JSONProperty):
                continue
            read_keys.setdefault(field, inherited.json_field)
            if inherited.write_field != inherited.json_field:
                write_keys.setdefault(field, inherited.write_field)
            if write_only is None and inherited.write_only:
                hidden.add(field)
            if read_only is None and inherited.read_only:
                readonly.add(field)
        _check_options(
            cls, pk, read_keys, exclude, hidden, write_keys, readonly
        )
        _check_json_keys(cls, read_keys, exclude, write_keys)

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

            json_field = read_keys.get(field, field)

            prop = JSONProperty(
                cls,
                json_field,
                field,
                is_primary_key=field == pk_field,
                write_only=field in hidden,
                write_field=write_keys.get(field),
                read_only=field in readonly,
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

    write_only: bool = False
    """Whether the API accepts this field in writes but never returns it."""

    read_only: bool = False
    """Whether the Python field value cannot be written."""

    write_field: str
    """JSON key that writes send this field under, usually ``json_field``."""

    is_primary_key: bool = False
    """Whether this property is the primary key of the class that owns it.

    A subclass can choose another key, so this does not say whether the
    property is the key of an instance of a subclass.
    """

    def __init__(
        self,
        cls: type[JSONObject],
        json_field: str,
        field: str,
        is_primary_key: bool = False,
        write_only: bool = False,
        write_field: str | None = None,
        read_only: bool = False,
    ):
        """Initialize a property descriptor.

        Args:
            cls: Model class that owns this descriptor.
            json_field: JSON key backing this attribute.
            field: Python attribute name.
            is_primary_key: Whether this property stores the primary key.
            write_only: Whether the API accepts this field in writes but never
                returns it.
            write_field: JSON key that writes send this field under, if not
                ``json_field``.
            read_only: Whether the Python field value cannot be written.
        """
        self.cls = cls
        self.field = field
        self.json_field = json_field
        self.is_primary_key = is_primary_key
        self.write_only = write_only
        self.read_only = read_only
        self.write_field = json_field if write_field is None else write_field

        # If this property is a primary key, then we store a reference to it in
        # the class's _pk attribute and the JSON field corresponding to the
        # primary key in _pk_json_field.
        if self.is_primary_key:
            cls._pk = self
            cls._pk_json_field = json_field

    def _is_key_of(self, obj: JSONObject) -> bool:
        """Return whether this property is the primary key of ``obj``.

        A subclass can choose another field as its key, so a property that is
        its own class's key need not be the key of an instance of a subclass.

        Args:
            obj: Instance of the class that owns this property, or of a
                subclass.

        Returns:
            Whether this property is the primary key of ``obj``'s class.
        """
        return type(obj)._pk is self

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
        # globals. Keep its own namespace when resolving self-references, but
        # without the fields, which json_object() put there as descriptors.
        # In a plain class a field without a default is absent from the
        # namespace, so a string annotation such as "date" in ``date: date``
        # names the module's date, not the field.
        localns = {
            name: value
            for name, value in vars(self.cls).items()
            if not isinstance(value, JSONProperty)
        }
        localns[self.cls.__name__] = self.cls
        # Keep Annotated metadata, which analyze_type() interprets.
        return typing.get_type_hints(
            holder, localns=localns, include_extras=True
        )[self.field]

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
                backing JSON, or a write-only field.
        """
        if obj is None:
            return self

        if self.write_only:
            raise AttributeError(
                f"{type(obj).__name__}.{self.field} is write-only, so it "
                "cannot be read"
            )

        if (
            self._is_key_of(obj)
            and obj._url_override is None
            and (
                obj._json is None
                or (obj._json_is_partial and self.json_field not in obj._json)
            )
        ):
            # An object created without a key has none until it loads, and so
            # has one whose partial JSON, from a push or a write response,
            # lacks the key. Loading it would need its URL, which needs the
            # key.
            return getattr(obj, "_pk_value", None)

        json = obj._json_with_field(self.json_field, self.field)
        if self.field in obj._field_cache:
            return obj._field_cache[self.field]

        with _converting(type(obj), self.field):
            value = obj._from_json(
                json[self.json_field],
                self.analyzed_type,
                field=self.field,
            )
        # A related object saves its own changes. A read-only value cannot be
        # sent, so a change made to it in place is not tracked, and it would
        # otherwise make every later write fail.
        changeable = not self.read_only and not isinstance(
            value, (*_UNCHANGEABLE_VALUES, JSONObject, RelatedCollection)
        )
        if changeable:
            # The caller can change this value in place. Record its encoding,
            # so that save() can detect the change. Encoding the value rather
            # than copying the JSON keeps a different spelling of the same
            # value, such as "Z" for "+00:00", from counting as a change.
            with _converting(type(obj), self.field):
                encoded = obj._to_json(value, self.analyzed_type)

        # Cache the value only once its encoding is known, so that a field
        # whose value cannot be encoded fails on every read, rather than
        # returning a value whose changes save() would miss.
        obj._field_cache[self.field] = value
        if changeable:
            obj._field_snapshots[self.field] = _copy_json(encoded)

        return value

    def __set__(self, obj: JSONObject, value: Any) -> None:
        """Set a field value using the object's configured write mode.

        Args:
            obj: ``JSONObject`` instance being modified.
            value: New Python value to assign.

        A write-only field's value is sent by the next write, as any other
        assignment is, but it is not kept in the object's JSON, and assigning
        it does not load the object.

        Raises:
            AttributeError: If the backing JSON does not contain this field.
            FieldConversionError: If the value cannot be converted to the
                field's JSON value, or a primary key that initializes an
                unloaded object is not a valid value of the field's type.
        """
        if self.write_only:
            with _converting(type(obj), self.field):
                encoded = obj._to_json(value, self.analyzed_type)
            obj._update_write_only_field(self.json_field, _copy_json(encoded))
            return

        # Only the first key assignment initializes a lazy object's identity.
        # Later assignments must update the resource at its saved key.
        if (
            self._is_key_of(obj)
            and obj._json is None
            and not hasattr(obj, "_pk_value")
            and obj._url_override is None
        ):
            # Convert the key as if it were read from JSON, so it has the same
            # type before and after the object loads. A related object passed
            # as the key is kept as is.
            if not isinstance(value, JSONObject):
                conversion = self.analyzed_type
                with _converting(type(obj), self.field):
                    value = obj._from_json(
                        obj._to_json(value, conversion), conversion
                    )

            obj._pk_value = value
            return

        if self.read_only:
            raise AttributeError(
                f"{type(obj).__name__}.{self.field} is read-only"
            )
        json = obj._json_with_field(self.json_field, self.field)
        with _converting(type(obj), self.field):
            new_value = obj._to_json(value, self.analyzed_type)

        if not _json_equal(json[self.json_field], new_value):
            # Store a copy, so that changing the assigned value in place later
            # cannot match the stored JSON and suppress the next update.
            obj._update_json_field(self.json_field, _copy_json(new_value))

        # Drop cached values only now, because the update may read this field.
        # An assignment also replaces any change made in place to the old
        # value.
        obj._prefetched_related.pop(self.field, None)
        obj._field_cache.pop(self.field, None)
        obj._field_snapshots.pop(self.field, None)

        # An eager write's response can name other related objects, which
        # then replace the assigned ones, as after save().
        final = (obj._json or {}).get(self.json_field, _MISSING)
        if not _json_equal(final, new_value):
            return

        kept = self._assigned_related(value)
        if kept is not _MISSING:
            # Keep the assigned related objects, so that reading the field
            # returns them. Assigning the objects the field already holds
            # must not discard their unsaved changes.
            obj._field_cache[self.field] = kept
            if not isinstance(kept, JSONObject):
                # The caller can change a collection in place, as one read
                # from JSON.
                obj._field_snapshots[self.field] = _copy_json(new_value)

    def _assigned_related(self, value: Any) -> Any:
        """Return the field value that keeps assigned related objects.

        Args:
            value: Value assigned to this field.

        Returns:
            ``value``, if it is a related object for a related field. For a
            collection of related objects that is not read lazily, a new
            collection of the field's type that holds the same objects.
            ``_MISSING`` for any other value, which the field reads from its
            JSON instead.
        """
        conversion = _unwrap_optional_type(self.analyzed_type)
        if isinstance(conversion, RelatedType):
            return value if isinstance(value, JSONObject) else _MISSING

        if not isinstance(conversion, CollectionType):
            return _MISSING

        collection_type = conversion.collection_type
        item_type = conversion.item_type
        if (
            not isinstance(_unwrap_optional_type(item_type), RelatedType)
            or (
                _is_lazy_collection_type(collection_type)
                and isinstance(item_type, RelatedType)
            )
            # A one-shot iterable was used up by encoding it.
            or not isinstance(value, Collection)
            or isinstance(value, (str, bytes, Mapping, RelatedCollection))
            or not all(
                item is None or isinstance(item, JSONObject) for item in value
            )
        ):
            return _MISSING

        # Abstract collection types read as tuples, as in _from_json().
        if collection_type in (Iterable, Sequence):
            return tuple(value)

        return collection_type(value)


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

    conversions: ClassVar[Mapping[Any, Conversion]] = MappingProxyType({})
    """Conversions of field types, keyed by the type they convert.

    A class's entries are merged over its bases', and theirs over the
    default ones for ``datetime``, ``date``, ``time``, ``timedelta``,
    ``UUID``, ``Decimal``, and enums. A field type converts by the entry
    for exactly that type, else by the entry of the nearest class in its
    MRO, else, for a ``NewType``, as its supertype. A key cannot be a
    model, or a type that eazyrest converts itself: ``object``,
    ``bool``, ``int``, ``float``, ``str``, ``None``'s type, or a
    collection type such as ``list`` or ``dict``. Declare the mapping in
    the class body: the class merges and checks its table when it is
    defined.
    """

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
    """Whether ``_json`` holds only some of the object's fields."""

    _url_override: str | None = None
    """The URL of an object that a hyperlinked relation named, if any."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Check a class's conversions and the models it inherits fields from.

        The class's conversion table is merged and checked now, so that a
        bad entry fails when the class is defined rather than when a field
        first converts.

        A model inherits its fields through a single chain of decorated
        classes. Fields from two decorated classes, neither of which is a
        subclass of the other, would need their field sets and primary keys
        reconciled, so such a class is an error. Undecorated bases, which
        declare no fields, may be mixed in freely.

        Args:
            **kwargs: Keyword arguments for ``object.__init_subclass__()``.

        Raises:
            TypeError: If the class's ``conversions`` or a base's has a bad
                entry, such as one keyed by a model, or if two decorated
                ancestors of the class are not subclasses of one another.
        """
        super().__init_subclass__(**kwargs)

        for base in cls.__mro__:
            own = vars(base).get("conversions")
            if not isinstance(own, Mapping):
                continue
            for key in own:
                if lenient_issubclass(_final_supertype(key), JSONObject):
                    raise TypeError(
                        f"{base.__name__}.conversions has the key "
                        f"{key.__name__}, a model or a NewType of one. A "
                        "field of a model's type is a relation, which a "
                        "conversion cannot replace."
                    )
        _table_of(cls)

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
        if self._json is not None:
            self._json = self._readable_json(self._json)
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
                value = self._to_json(value, pk_property.analyzed_type)

        if value is None and self._url_override is not None:
            return f"<{name} url={self._url_override!r}>"
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

    def _create_related(
        self,
        arg: Any,
        ty: type[T],
        *,
        field: str | None = None,
        hyperlinked: bool = False,
    ) -> T:
        """Create a related object from embedded JSON, a key, or a URL.

        Args:
            arg: Either a JSON mapping for eager object creation, a primary
                key value for lazy lookup, or, for a hyperlinked relation, the
                related object's URL.
            ty: Related ``JSONObject`` subclass type.
            field: Optional owning field name for prefetch lookup.
            hyperlinked: Whether a string value is the related object's URL.

        Returns:
            Instance of ``ty``.

        Raises:
            TypeError: If a hyperlinked value is neither a URL nor a mapping.
            ValueError: If a URL's origin is not one that the related model's
                API allows.
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

        if hyperlinked:
            if not isinstance(arg, str):
                raise TypeError(
                    "Hyperlinked field values must be URLs or objects, got "
                    f"{type(arg).__name__}"
                )
            related = ty(api=api, write_mode=self.write_mode)
            related._url_override = _checked_hyperlink(related.api, arg)
            return related

        related = ty(pk=arg, api=api, write_mode=self.write_mode)
        if field is not None and field in self._prefetched_related:
            pk = related._normalize_pk(arg)
            if pk in self._prefetched_related[field]:
                return cast(T, self._prefetched_related[field][pk])

        return related

    def _from_json(
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
                return self._from_json(value, inner_type, field=field)

            case BaseType(base_type):
                found = _conversion_of(base_type, _table_of(type(self)))
                if found is not None:
                    return found.entry.decode(value, found.declared)

                # Changing the returned value in place must not change the
                # object's JSON without recording an update.
                return _copy_json(value)

            case PydanticType():
                # Building the adapter reports a missing Pydantic.
                adapter = conversion.adapter
                from .pydantic import _decode

                # Pydantic does not copy nested values of type Any, which the
                # returned value would then share with the JSON.
                with _active_table(_table_of(type(self))):
                    return _decode(adapter, _copy_json(value))

            case RelatedType(model_type, hyperlinked):
                return self._create_related(
                    value,
                    model_type,
                    field=field,
                    hyperlinked=hyperlinked,
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
                        hyperlinked=item_type.hyperlinked,
                    )

                converted_items = (
                    self._from_json(item, item_type, field=field)
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
    def _to_json(cls, value: Any, conversion: AnalyzedType) -> Any:
        """Convert a typed Python value to a JSON-serializable value.

        A collection converts to a list. ``None`` converts to ``None``, which
        JSON writes as ``null``, whatever the type, as ``_from_json()`` reads
        ``null``.

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
        if value is None:
            return None

        match conversion:
            case OptionalType(inner_type):
                return cls._to_json(value, inner_type)
            case RelatedType(model_type, hyperlinked=True):
                if isinstance(value, JSONObject):
                    return value.absolute_url
                if isinstance(value, str):
                    return value
                raise TypeError(
                    "Hyperlinked field values must be objects or URLs, got "
                    f"{type(value).__name__}"
                )
            case RelatedType(model_type):
                if isinstance(value, JSONObject):
                    model_type = type(value)
                    value = value.pk

                return model_type._to_json(value, model_type._pk.analyzed_type)
            case BaseType(base_type):
                found = _conversion_of(base_type, _table_of(cls))
                # A subclass of the key reads as the key's type, such as a
                # plain datetime for a subclass of datetime, which must write
                # back.
                if found is not None and _is_instance(value, found.key):
                    return found.entry.encode(value, found.declared)

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

                return [cls._to_json(item, item_type) for item in value]
            case PydanticType():
                from .pydantic import _encode

                with _active_table(_table_of(cls)):
                    return _encode(conversion.adapter, value)
            case _:
                raise TypeError(
                    f"Unsupported analyzed type {type(conversion).__name__}"
                )

    def delete(
        self, *, request_options: Mapping[str, Any] | None = None
    ) -> None:
        """Delete this object from the remote API.

        Args:
            request_options: Optional keyword arguments for the request, such
                as ``headers={"If-Match": etag}``, as for ``save()``. They
                cannot set the body or enable response streaming.

        Raises:
            TypeError: If ``request_options`` sets the body or streams the
                response.
            APIError: If the server returns an HTTP error status.
            RedirectError: If a redirect is unsafe or the final status is 3xx.
            LimitExceeded: If a request or its JSON exceeds the API's limits.
        """
        options = _request_options(request_options)
        _check_model_write(self.api.delete(self.url, **options))

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
            resp = self.api.patch(self.url, json=self._to_wire(payload))
            self._apply_patch_response(resp, payload)
            return

        self._queue_update(json_field, value)

    def _update_write_only_field(self, json_field: str, value: Any) -> None:
        """Send or queue a write-only field's value, as the write mode says.

        Args:
            json_field: JSON key of the write-only field.
            value: JSON-serializable field value.
        """
        if self.write_mode == "eager":
            self._queue_in_place_changes()
            payload = dict(self._pending_updates)
            payload[json_field] = value
            resp = self.api.patch(self.url, json=self._to_wire(payload))
            self._apply_patch_response(resp, payload)
            return

        self._pending_updates[json_field] = value

    @classmethod
    def _to_wire(cls, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Return a write's JSON with each field under its write key.

        Args:
            payload: JSON members keyed by the fields' JSON keys, as the
                object's JSON is, or other keys, which are kept.

        Returns:
            ``payload`` with each field that ``write_map`` names under its
            write key.
        """
        write_keys = {
            descriptor.json_field: descriptor.write_field
            for field in getattr(cls, "_json_fields", ())
            if (descriptor := getattr(cls, field)).write_field
            != descriptor.json_field
        }
        wire = {}
        for key, value in payload.items():
            target = write_keys.get(key, key)
            if target in wire:
                raise TypeError(f"Multiple fields write the JSON key {target}")
            wire[target] = value
        return wire

    @classmethod
    def _write_body(
        cls, payload: Mapping[str, Any], files: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        """Return the request arguments that send a write's body.

        Args:
            payload: JSON members of the write, keyed by read key.
            files: Files to upload, keyed by field name or form field name,
                as ``requests`` takes them, or ``None``.

        Returns:
            ``{"json": ...}`` without files. With files, ``{"data": ...,
            "files": ...}`` for a multipart request, whose form fields hold
            the payload.

        Raises:
            TypeError: If a payload value cannot be a form field.
        """
        wire = cls._to_wire(payload)
        if not files:
            return {"json": wire}
        form = {name: _form_value(name, value) for name, value in wire.items()}
        uploads = {}
        for name, file in files.items():
            descriptor = getattr(cls, name, None)
            key = (
                descriptor.write_field
                if isinstance(descriptor, JSONProperty)
                else name
            )
            if key in uploads:
                raise TypeError(f"Multiple files write the form key {key}")
            uploads[key] = file
        return {"data": form, "files": uploads}

    @classmethod
    def _write_only_keys(cls) -> frozenset[str]:
        """Return the JSON keys of this class's write-only fields.

        Returns:
            The JSON key of each write-only field.
        """
        return frozenset(
            descriptor.json_field
            for field in getattr(cls, "_json_fields", ())
            if (descriptor := getattr(cls, field)).write_only
        )

    @classmethod
    def _readable_json(cls, json: Mapping[str, Any]) -> dict[str, Any]:
        """Exclude write-only values from stored JSON.

        Args:
            json: Response or caller-provided object payload.

        Returns:
            JSON members other than declared write-only fields.
        """
        hidden = cls._write_only_keys()
        return {key: value for key, value in json.items() if key not in hidden}

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
                encoded = self._to_json(
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
        """Update local state after a successful ``PATCH`` or ``PUT`` request.

        Args:
            resp: Response to the request.
            payload: JSON fields sent in the request.

        Raises:
            ResponseShapeError: If the response body does not hold a JSON
                object. The object's state, including its pending updates, is
                unchanged.
        """
        _check_model_write(resp)
        # The object never keeps a write-only field's value.
        write_only = self._write_only_keys()
        sent = {k: v for k, v in payload.items() if k not in write_only}
        if self._json is None and not sent:
            # Only write-only fields were sent, so the object need not load.
            # A body becomes its JSON so far, and a read of a field that the
            # body lacks loads it. That load needs the object's URL, so the
            # JSON keeps the key the object was saved under, as a load does,
            # if the body lacks it.
            json = self._readable_json(self.merge_response(resp, {}, {}))
            if json:
                key = self._pk_json_field
                if key not in json and "_pk_value" in vars(self):
                    json[key] = self._to_json(
                        self._pk_value, type(self)._pk.analyzed_type
                    )
                self._replace_json(json)
                self._json_is_partial = True
        else:
            self._replace_json(self.merge_response(resp, self.json, sent))
        self._pending_updates.clear()

    def _replace_json(self, json: dict[str, Any]) -> None:
        """Replace this object's JSON after a write or a load.

        Cached values stay valid for the fields whose JSON is unchanged, so
        only the others are dropped. In particular, a related object assigned
        before ``save()`` stays the field's value, and so does a related
        object with unsaved changes when a load does not change it.

        Args:
            json: The object's new JSON.
        """
        before = self._json if self._json is not None else {}
        json = self._readable_json(json)
        if self._url_override is not None:
            key: str = type(self)._pk.json_field
            saved = before.get(key, _MISSING)
            if key in self._pending_updates:
                saved = self._to_json(
                    self._saved_pk, type(self)._pk.analyzed_type
                )
            if saved is not _MISSING and not _json_equal(
                saved, json.get(key, _MISSING)
            ):
                self._url_override = None
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
        unless the body holds it. The body may change any field, including the
        primary key, so the object then names the object that the body
        describes.

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
            ResponseShapeError: If the response body is not JSON, or does not
                hold a JSON object.
            LimitExceeded: If the response JSON exceeds the depth limit.
        """
        json = {**current, **sent}
        if resp.content:
            json.update(self._object_from_response(self.api.json(resp)))

        return json

    def save(
        self,
        *,
        full: bool = False,
        files: Mapping[str, Any] | None = None,
        request_options: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist all pending lazy field updates.

        In ``"lazy"`` mode, field assignments are accumulated locally and sent
        in one ``PATCH`` request when ``save()`` is called. In ``"eager"``
        mode, assignments are patched immediately. Unless ``full`` or
        ``files`` is supplied, ``save()`` sends nothing when no updates remain.

        A value read from a field and then changed in place, such as a list
        appended to, is saved too, in either mode. Such a change is detected
        only when ``save()`` runs, or when an eager assignment sends a
        ``PATCH``, so ``json`` does not show it before then.

        After a successful ``PATCH``, ``merge_response()`` determines the
        object's JSON. By default, the object keeps its JSON, which already
        includes the saved updates, and the object that the response body
        holds, if any, is merged over it.

        With ``full=True``, ``save()`` sends a ``PUT`` that holds every
        writable declared field the object's JSON has, with its changes, as an
        API that replaces the whole object expects, even if nothing changed.
        It loads an unloaded or partial object first. Each value is encoded as
        an assignment would encode it, so a related object is sent as its
        primary key. Pending write-only fields are sent too.

        With ``files``, the write is a multipart request that uploads them,
        with the other fields as form fields, even if no field changed. A
        form field cannot hold ``None``, a mapping, an empty list, or a nested
        list, so such a value raises ``TypeError`` before anything is sent.

        Args:
            full: Whether to send the whole object with ``PUT`` rather than
                the changes with ``PATCH``. It loads an unloaded or partial
                object first.
            files: Optional files to upload, keyed by field name or by the
                API's field name, as ``requests`` takes them, such as
                ``{"image": open("a.png", "rb")}``.
            request_options: Optional keyword arguments for the request, such
                as ``headers={"If-Match": etag}``, as ``requests`` takes them.
                They cannot set the body or enable response streaming.

        Raises:
            ResponseShapeError: If the response body does not hold a JSON
                object. The server may have applied the updates, but they
                remain pending.
            TypeError: If ``request_options`` sets the body or streams the
                response, or a field cannot be sent as a form field.
            APIError: If the server returns an HTTP error status.
            RedirectError: If a redirect is unsafe or the final status is 3xx.
            LimitExceeded: If a request or its JSON exceeds the API's limits.
            FieldConversionError: If a field's value cannot be converted.
        """
        options = _request_options(request_options)
        self._queue_in_place_changes()
        if full:
            payload = self._full_payload()
            body = self._write_body(payload, files)
            resp = self.api.put(self.url, **body, **options)
            self._apply_patch_response(resp, payload)
            return
        if len(self._pending_updates) == 0 and not files:
            return

        payload = dict(self._pending_updates)
        body = self._write_body(payload, files)
        resp = self.api.patch(self.url, **body, **options)
        self._apply_patch_response(resp, payload)

    def _full_payload(self) -> dict[str, Any]:
        """Return the whole object's JSON for a ``PUT``, keyed by read key.

        Returns:
            Each declared field that the object's JSON has, encoded as an
            assignment encodes it, and each pending write-only field.

        Raises:
            FieldConversionError: If a field's value cannot be converted.
        """
        if self._json_is_partial:
            self._load_json()
        json = self.json
        payload: dict[str, Any] = {}
        for field in sorted(getattr(self, "_json_fields", ())):
            descriptor = getattr(type(self), field)
            if (
                descriptor.write_only
                or descriptor.read_only
                or descriptor.json_field not in json
            ):
                continue
            conversion = descriptor.analyzed_type
            with _converting(type(self), field):
                payload[descriptor.json_field] = self._to_json(
                    self._from_json(json[descriptor.json_field], conversion),
                    conversion,
                )
        write_only = self._write_only_keys()
        payload.update(
            (key, value)
            for key, value in self._pending_updates.items()
            if key in write_only
        )
        return payload

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

        unknown_key = self._url_override is not None and (
            (self._json is None and getattr(self, "_pk_value", None) is None)
            or (
                self._json is not None
                and self._pk_json_field not in self._json
            )
        )
        existing_pk = (
            payload.get(self._pk_json_field)
            if unknown_key
            else self._persisted_pk()
        )
        incoming_pk = payload.get(self._pk_json_field, existing_pk)
        if self._normalize_pk(incoming_pk) != self._normalize_pk(existing_pk):
            raise ValueError(
                "Incoming JSON primary key does not match this object"
            )

        if self._json is None:
            self._json = {}
            if existing_pk is not None:
                self._json[self._pk_json_field] = self._to_json(
                    existing_pk, type(self)._pk.analyzed_type
                )
            self._json_is_partial = True

        self._json.update(self._readable_json(payload))

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
        # Store value of primary key. An object read from a hyperlink may not
        # know its key, before it loads or from JSON that lacks it.
        unknown_key = self._url_override is not None and (
            self._json is None or self._pk_json_field not in self._json
        )
        self._pk_value = None if unknown_key else self._persisted_pk()
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
        value = self._from_json(self._to_json(value, conversion), conversion)
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

        An object read from a hyperlinked relation has the URL that the
        relation named, as an absolute URL.

        Raises:
            ValueError: If the primary key is ``None``, empty, or a dot
                segment. The URL would name another resource, such as the
                collection.
        """
        if self._url_override is not None:
            return self._url_override
        pk = self._to_json(self._persisted_pk(), type(self)._pk.analyzed_type)
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

        The mapping is the object's own state, not a copy, so treat it as
        read-only. Changing it, or a value inside it, bypasses change
        tracking, so ``save()`` may send nothing or the wrong fields. Copy it
        with ``copy.deepcopy()`` before changing it, and assign fields to
        change the object.

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
            ResponseShapeError: If the response does not hold a JSON object.
        """
        # Reloading drops cached values, so queue their in-place changes first.
        self._queue_in_place_changes()
        json = self._object_from_response(
            self.api.json(self.api.get(self.url))
        )

        # Some APIs leave the primary key out of a detail response, because the
        # URL holds it. Keep the key that the object was loaded by, so that the
        # object keeps its identity and URL.
        pk_json_field = getattr(type(self), "_pk_json_field", None)
        if pk_json_field is not None and pk_json_field not in json:
            if self._json is not None and pk_json_field in self._json:
                json[pk_json_field] = self._json[pk_json_field]
            elif "_pk_value" in vars(self):
                json[pk_json_field] = self._to_json(
                    self._pk_value, type(self)._pk.analyzed_type
                )

        # Unsaved lazy updates still take precedence over server state. The
        # object never keeps a write-only field's value.
        write_only = self._write_only_keys()
        json.update(
            (key, value)
            for key, value in self._pending_updates.items()
            if key not in write_only
        )
        # Keep the values of fields that the load left unchanged, such as a
        # related object with unsaved changes of its own.
        self._replace_json(json)
        self._json_is_partial = False

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
    def _encode_fields(
        cls, fields: Mapping[str, Any], *, query: bool = False
    ) -> dict[str, Any]:
        """Encode field values as JSON object members.

        Args:
            fields: Values keyed by Python attribute name for JSON-backed
                fields, or by JSON key for other values.
            query: Whether to encode a query, whose related values use keys.

        Returns:
            JSON values keyed by JSON key. JSON-backed fields are renamed by
            ``field_map`` and converted by ``_to_json()``. Other values are
            unchanged.
        """
        payload: dict[str, Any] = {}

        for field, value in fields.items():
            descriptor = getattr(cls, field, None)
            key = (
                descriptor.json_field
                if isinstance(descriptor, JSONProperty)
                else field
            )
            if key in payload:
                raise TypeError(f"Multiple fields use the JSON key {key}")
            if isinstance(descriptor, JSONProperty):
                if descriptor.read_only and not query:
                    raise AttributeError(
                        f"{cls.__name__}.{field} is read-only"
                    )
                with _converting(cls, field):
                    conversion = descriptor.analyzed_type
                    payload[key] = cls._to_json(
                        value,
                        _query_type(conversion) if query else conversion,
                    )
            else:
                payload[key] = value

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
            ValueError: If a parameter encodes to a value that requests would
                not send intact, so the query would ignore or change its
                constraint.
        """
        payload = cls._encode_fields(
            {
                name: value
                for name, value in params.items()
                if not cls._is_single_collection_item(name, value)
            },
            query=True,
        )
        for name, value in params.items():
            if cls._is_single_collection_item(name, value):
                # A single value, such as a query for the objects whose
                # collection holds it, converts as one item.
                descriptor = getattr(cls, name)
                collection = _unwrap_optional_type(descriptor.analyzed_type)
                assert isinstance(collection, CollectionType)
                with _converting(cls, name):
                    if descriptor.json_field in payload:
                        raise TypeError(
                            "Multiple fields use the JSON key "
                            f"{descriptor.json_field}"
                        )
                    payload[descriptor.json_field] = cls._to_json(
                        value, _query_type(collection.item_type)
                    )

        for name in params:
            descriptor = getattr(cls, name, None)
            json_field = (
                descriptor.json_field
                if isinstance(descriptor, JSONProperty)
                else name
            )
            value = _materialize_query_value(payload[json_field])
            if not isinstance(descriptor, JSONProperty):
                if isinstance(value, JSONObject):
                    value = cls._to_json(value, RelatedType(type(value)))
                elif isinstance(value, list):
                    value = [
                        cls._to_json(item, RelatedType(type(item)))
                        if isinstance(item, JSONObject)
                        else item
                        for item in value
                    ]
            problem = _unsendable_query_value(value)
            if problem is not None:
                raise ValueError(
                    f"Query parameter {name} for {cls.__name__} cannot be "
                    f"sent, because its value {problem}"
                )
            if isinstance(descriptor, JSONProperty):
                value = _query_value(value)
            payload[json_field] = value

        return payload

    @classmethod
    def create(
        cls: type[Self],
        *,
        api: API | None = None,
        fields: Mapping[str, Any] | None = None,
        files: Mapping[str, Any] | None = None,
        request_options: Mapping[str, Any] | None = None,
        **kwargs: Any,
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
            fields: Optional field values as a mapping, used as the keyword
                arguments are. A key in it may be anything, including ``api``,
                ``fields``, ``files``, or ``request_options``.
            files: Optional files to upload, as for ``save()``. The request is
                then multipart, with the fields as form fields.
            request_options: Optional keyword arguments for the request, such
                as ``headers``, as ``requests`` takes them. They cannot set the
                body.
            **kwargs: Field values submitted in Python form for JSON-backed
                fields, or already-JSON values for unknown extra keys.

        Returns:
            Newly created object instance.

        Raises:
            ResponseShapeError: If the response does not hold a JSON object.
            TypeError: If a field is given both as a keyword argument and in
                ``fields``, if ``request_options`` sets the body, or if a
                field's value cannot be sent as a form field with ``files``.
                Also raised for response streaming or conflicting wire names.
            AttributeError: If an ordinary value names a read-only field.
            APIError: If the server returns an HTTP error status.
            RedirectError: If a redirect is unsafe or the final status is 3xx.
            LimitExceeded: If a request or its JSON exceeds the API's limits.
            FieldConversionError: If a field's value cannot be converted.
        """
        options = _request_options(request_options)
        payload = cls._encode_fields(
            _query_params(fields, kwargs, "Field", "fields")
        )
        client = cls.api if api is None else api
        resp = _check_model_write(
            client.post(
                cls.class_url, **cls._write_body(payload, files), **options
            )
        )

        # The object keeps neither a write-only field nor an extra key.
        json_keys = cls._json_keys() - cls._write_only_keys()
        # The payload can hold the caller's own containers, such as a dict
        # field's value, so copy them before they become saved state.
        sent = {
            key: _copy_json(value)
            for key, value in payload.items()
            if key in json_keys
        }
        obj = cls(json={}, api=api)
        obj._replace_json(obj.merge_response(resp, {}, sent))
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
    ) -> Mapping[Any, Self]:
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
        return cls.collection_items(cls.api.json(resp))

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
            ResponseShapeError: If the payload is not a supported collection
                response.
        """
        if isinstance(payload, list):
            return payload

        raise ResponseShapeError(
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
            raise ResponseShapeError(
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
        if analyzed_type.hyperlinked:
            raise ValueError(
                f"{cls.__name__}.{field} is hyperlinked, and prefetching "
                "needs primary keys"
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
                        try:
                            pks.add(raw_value)
                        except TypeError as err:
                            raise ResponseShapeError(
                                f"{cls.__name__}.{field} has an invalid "
                                "related key"
                            ) from err
                    continue

                if not isinstance(raw_value, Iterable) or isinstance(
                    raw_value, (str, bytes, dict)
                ):
                    continue

                raw_items = tuple(raw_value)
                # Explicit prefetch only bulk-loads foreign-key style entries.
                # Embedded related JSON objects do not need to be prefetched.
                try:
                    pks.update(
                        item
                        for item in raw_items
                        if item is not None and not isinstance(item, dict)
                    )
                except TypeError as err:
                    raise ResponseShapeError(
                        f"{cls.__name__}.{field} has an invalid related key"
                    ) from err

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
            batch_items = [
                _OwnedJSON(item)
                for item in itertools.islice(iterator, cls.prefetch_batch_size)
            ]
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
        params: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> Results[Self]:
        """Query objects matching request parameters.

        The request is sent when ``filter()`` is called. Objects are created as
        the returned iterator advances.

        Query parameters are encoded the same way as a ``create()`` payload.
        Parameters named after JSON-backed fields are renamed by ``field_map``
        and converted to JSON values, so ``user=User(id=2)`` is sent as
        ``userId=2`` when ``field_map={"user": "userId"}``. Other parameters
        are sent unchanged, except that a model instance, also as an item of a
        collection, is sent as its primary key.

        Every parameter must have a value that requests sends intact. It must
        not be ``None``, an empty collection, or a mapping, and a collection
        must not hold ``None`` or another collection. requests omits ``None``
        and empty collections, sends only a mapping's keys, and flattens nested
        collections, so the query would ignore or change that constraint. Any
        iterable other than a string, bytes, or a mapping, such as a generator
        or a ``deque``, is a collection.

        The keyword arguments ``api``, ``prefetch``, ``defaults``, and
        ``params`` of the query methods are not query parameters, and the
        query methods reserve ``request_options``. Pass a parameter with one
        of these names in ``params``.

        Args:
            prefetch: Optional related field name or field names to bulk-load
                explicitly. Prefetched related objects may be shared by
                identity across parent objects within the same batch.
            api: Optional API to send the request with, instead of the API
                registered for the class. Returned objects keep it as their
                API, so their later requests use it too.
            params: Optional query parameters as a mapping, encoded as the
                keyword arguments are. A name in it may be anything, including
                ``api``, ``prefetch``, ``defaults``, or ``params``, which the
                query methods reserve for themselves.
            **kwargs: Query string parameters.

        Returns:
            Iterator over objects built from the response payload. Its
            ``count`` is the size of the whole collection, if the iterable
            that ``collection_items()`` returns has a ``count`` attribute, as
            ``follow_next_links()``'s does, and ``None`` otherwise.

        Raises:
            ValueError: If a ``prefetch`` field is not a JSON-backed
                related-object field, or if a parameter has a value that
                requests would not send intact.
            TypeError: If a parameter is given both as a keyword argument and
                in ``params``, or a keyword argument is reserved.
            PrefetchNotSupported: If the related model of a ``prefetch`` field
                does not implement ``bulk_get_by_pks()``.
        """
        _check_query_keywords(kwargs)
        query = _query_params(params, kwargs)
        prefetch_fields = _normalize_prefetch(prefetch)
        # Check the fields before the request, so a misspelled field or a model
        # without bulk lookup fails even when the query matches nothing.
        for field in prefetch_fields:
            cls._prefetch_info(field)

        client = cls.api if api is None else api
        resp = client.get(cls.class_url, params=cls._encode_query(query))
        token = _query_api.set((cls, client))
        try:
            items = cls.collection_items_from_response(resp)
        finally:
            _query_api.reset(token)

        count = _count_of(items)
        # Treat an empty prefetch list the same as no prefetch at all.
        if len(prefetch_fields) == 0:
            return Results(
                (cls(json=_OwnedJSON(item), api=api) for item in items), count
            )

        return Results(
            cls._prefetched_filter(items, prefetch_fields, api), count
        )

    @classmethod
    def all(
        cls: type[Self],
        *,
        prefetch: PrefetchFields = None,
        api: API | None = None,
    ) -> Results[Self]:
        """Return all objects for this resource type.

        Args:
            prefetch: Optional related field name or field names to bulk-load
                explicitly. Prefetched related objects may be shared by
                identity across parent objects within the same batch.
            api: Optional API to send the request with, instead of the API
                registered for the class. Returned objects keep it as their
                API, so their later requests use it too.

        Returns:
            Iterator over all objects, with ``count`` as for ``filter()``.

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
        params: Mapping[str, Any] | None = None,
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
            params: Optional query parameters as a mapping, as for
                ``filter()``.
            **kwargs: Query string parameters.

        Returns:
            Single matching object.

        Raises:
            DoesNotExist: If no objects matched.
            MultipleObjectsReturned: If more than one object matched.
            ValueError: If a ``prefetch`` field is not a JSON-backed
                related-object field, or if a parameter has a value that
                requests would not send intact.
            TypeError: If a parameter is given both as a keyword argument and
                in ``params``, or a keyword argument is reserved.
            PrefetchNotSupported: If the related model of a ``prefetch`` field
                does not implement ``bulk_get_by_pks()``.
        """
        _check_query_keywords(kwargs)
        query = _query_params(params, kwargs)
        results = iter(cls.filter(prefetch=prefetch, api=api, params=query))
        first = next(results, None)
        if first is None:
            raise DoesNotExist(f"No {_describe_lookup(cls, query)} exists")

        second = next(results, None)
        if second is not None:
            raise MultipleObjectsReturned(
                f"More than one {_describe_lookup(cls, query)} exists"
            )

        return first

    @classmethod
    def get_or_create(
        cls: type[Self],
        defaults: Mapping[str, Any] | None = None,
        *,
        api: API | None = None,
        params: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[Self, bool]:
        """Get one object by fields or create it if absent.

        The lookup query and the creation payload encode ``kwargs`` the same
        way as ``create()``. JSON-backed fields are renamed by ``field_map``
        and converted to JSON values, so ``user=User(id=2)`` is sent as
        ``userId=2`` when ``field_map={"user": "userId"}``. Other keys are sent
        unchanged, except that the lookup sends a model instance as its
        primary key, as ``filter()`` does. Every key in ``kwargs`` is sent in
        the lookup's query string, which servers and proxies may log, so pass
        a value that only the creation needs, such as a password, in
        ``defaults``.

        As in Django, a key that contains ``__`` and does not name a field,
        such as ``name__iexact`` or ``created__gte``, is a lookup rather than a
        field value. It is sent in the query but not in the creation payload.

        As in ``get()``, the server must apply every lookup parameter. If it
        ignores one, this method can return an object that does not match it,
        with ``created`` false. The lookup and the creation are separate
        requests, so two clients can both create the object. A uniqueness
        constraint on the server must prevent that.

        As in ``filter()``, a lookup parameter must have a value that requests
        sends intact, because the query would otherwise ignore or change it
        while the creation payload sends it as given. Such a value in
        ``defaults`` is sent only on creation and is allowed.

        A value that can be iterated only once, such as a generator, is read
        once, before the lookup, and its items are used for both requests.

        Args:
            defaults: Optional fields applied only when creating a new object.
            api: Optional API to send the lookup and any creation with, instead
                of the API registered for the class. The returned object keeps
                it as its API, so its later requests use it too.
            params: Optional fields and lookups as a mapping, used as the
                keyword arguments are. A name in it may be anything, including
                one that the query methods reserve, such as ``api``.
            **kwargs: Fields used for lookup and included in creation.

        Returns:
            Tuple ``(obj, created)`` where ``created`` indicates whether a new
            object was created.

        Raises:
            MultipleObjectsReturned: If lookup matches multiple objects.
            ValueError: If a lookup parameter has a value that requests would
                not send intact.
            TypeError: If a parameter is given both as a keyword argument and
                in ``params``, or a keyword argument is reserved.
        """
        _check_query_keywords(kwargs)
        # The lookup and the creation each iterate a collection value, so read
        # a single-pass iterable, such as a generator, only once.
        kwargs = {
            name: list(value)
            if isinstance(value, Iterable)
            and not isinstance(value, (Collection, str, bytes, Mapping))
            else value
            for name, value in _query_params(params, kwargs).items()
        }
        results = cls.filter(api=api, params=kwargs)
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

        return cls.create(api=api, fields=create_kwargs), True
