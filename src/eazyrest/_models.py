"""Typed JSON object mapping for REST resources."""

from __future__ import annotations

import collections
import contextlib
import copy
import dataclasses
import datetime
import decimal
import enum
import functools
import inspect
import itertools
import json
import math
import operator
import re
import sys
import types
import typing
import uuid
from collections.abc import (
    Callable,
    Collection,
    Container,
    Generator,
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
    MutableSequence,
    MutableSet,
    Sequence,
)
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from functools import cached_property
from types import MappingProxyType
from typing import (
    Annotated,
    Any,
    ClassVar,
    Literal,
    Self,
    TypeAlias,
    TypeVar,
    Union,
    cast,
    overload,
)
from urllib.parse import quote, unquote, urljoin

import requests

from ._conversion import (
    Conversion,
    _conversion_of,
    _final_supertype,
    _Found,
    _is_instance,
    _is_typed_dict,
    _schema_of_entry,
    _supertype,
    _table_of,
)
from ._errors import (
    DoesNotExist,
    EazyrestError,
    MultipleObjectsReturned,
    PrefetchNotSupported,
    ResponseShapeError,
    UsageError,
    _asked_url,
    _redact_url,
)
from ._protocols import Client, Response
from ._results import Results, _count_of
from .api import (
    API,
    _AnyAPI,
    _as_client,
    _canonical_url,
    _check_final_response,
    _merge_request_options,
    _origin,
    _permitted_origins,
    _registered_api,
    _takes_kwargs,
)
from .pagination import (
    Page,
    _check_page_size,
    _follow_pages,
    _PageSource,
    _request_page,
    next_links_page,
)
from .write_mode import WriteMode, validate_write_mode

if sys.version_info >= (3, 14):
    from annotationlib import Format

# typing_extensions recognizes its own TypedDicts as well as typing's, and
# reads the qualifiers of their keys on every supported Python. The package
# does not require it, so it is optional here.
_typing_extensions: types.ModuleType | None
try:
    import typing_extensions as _typing_extensions_module
except ImportError:  # pragma: no cover
    _typing_extensions = None
else:
    _typing_extensions = _typing_extensions_module


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


_DECLARATION_ERROR = "_eazyrest_declaration_error"
"""Attribute that marks a ``TypeError`` that refuses a model's declaration."""


def _passes_through(err: Exception) -> bool:
    """Tell whether a conversion error keeps its own type.

    A refusal of a model's declaration is no conversion of a value. An
    ``EazyrestError`` other than ``FieldConversionError``, such as the
    ``ResponseShapeError`` of loading a related object to learn its key,
    reports something other than a value that a field cannot convert.

    Args:
        err: Error that a conversion raised.

    Returns:
        Whether to raise ``err`` as it is.
    """
    if getattr(err, _DECLARATION_ERROR, False):
        return True
    return isinstance(err, EazyrestError) and not isinstance(
        err, FieldConversionError
    )


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
            raises propagates unchanged, and so does a refusal of the model's
            declaration, which is no conversion of a value, and any other
            ``EazyrestError``.
    """
    try:
        yield
    except FieldConversionError:
        raise
    except (TypeError, ValueError) as err:
        if _passes_through(err):
            raise
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
"""Prefetched related objects, by field name and then by canonical key.

The key is the text of the canonical key of a key, or the canonical URL of a
hyperlink.
"""

PrefetchFields: TypeAlias = str | Sequence[str] | None
"""Related field names requested for explicit prefetch."""

_api_registry: dict[type[JSONObject], API] = {}


@dataclass(frozen=True)
class _Query:
    """The APIs and request options of one query."""

    api: _AnyAPI | None
    """The API that the query passed as ``api=``, which its objects keep."""

    client: _AnyAPI
    """The client that sends the query's own requests, with its options."""

    options: Mapping[str, Any]
    """Request options that the query's requests send."""

    def related_client(self, model: type[JSONObject]) -> _AnyAPI:
        """Return the client for a bulk lookup of related objects.

        The lookup uses the query's ``api=``, or else the related model's own
        API. It gets the query's request options only when that API has the
        origin of the query's own client, so that credentials in them, such as
        an ``Authorization`` header, do not reach another model's host. A
        hyperlink lookup may still send them to an origin in that API's
        ``redirect_origins``, as reading the field sends the session's
        headers there.

        Args:
            model: Class of the related objects.

        Returns:
            The API for the lookup, request-scoped when the options apply.
        """
        base = model.api if self.api is None else self.api
        if self.options and _origin(base.base_url) == _origin(
            self.client.base_url
        ):
            return base._scoped(self.options)
        return base


class _ModelPageSource(_PageSource[Any]):
    """Reads pages of a model's collection as pages of objects."""

    def __init__(
        self,
        model: type[JSONObject],
        query: _Query,
        prefetch: Sequence[str],
    ) -> None:
        """Read the pages of ``model``'s collection for one query.

        Args:
            model: Class whose collection the pages hold.
            query: The query, whose client requests the pages.
            prefetch: Related fields to prefetch on each page.
        """
        self._client = query.client
        self._model = model
        self._query = query
        self._prefetch = prefetch

    @property
    def api(self) -> _AnyAPI:
        """The query's client, which requests the pages."""
        return self._client

    def read(self, url: str, allow_other_origins: bool) -> Page[Any]:
        """Request and read the page at ``url``.

        Args:
            url: Absolute URL of the page.
            allow_other_origins: Whether the page may have another origin.

        Returns:
            The page of objects.
        """
        return self.page(
            _request_page(self.api, url, allow_other_origins, from_server=True)
        )

    def page(self, resp: Response) -> Page[Any]:
        """Read a page of objects from its response.

        Args:
            resp: Response for the page.

        Returns:
            The page of objects, which can read the pages it links to when
            the model's ``collection_page()`` returned a page that can.

        Raises:
            TypeError: If ``collection_page()`` does not return a ``Page``.
        """
        model = self._model
        raw = model.collection_page(resp, api=self.api)
        if not isinstance(raw, Page):
            raise TypeError(
                f"{model.__name__}.collection_page() must return a Page, got "
                f"{type(raw).__name__}"
            )
        _check_page_size(self.api, raw.items)
        if self._prefetch:
            objects: list[JSONObject] = list(
                model._prefetched_filter(
                    raw.items, self._prefetch, self._query
                )
            )
        else:
            objects = [
                model(json=_OwnedJSON(item), api=self._query.api)
                for item in raw.items
            ]
        page: Page[Any] = Page(
            objects,
            url=raw.url,
            count=raw.count,
            next_url=raw.next_url,
        )
        page._defer_previous(lambda: raw.previous_url)
        if raw._source is not None:
            page._source = self
            page._allow_other_origins = raw._allow_other_origins
        page._asked_url = _asked_url(resp)
        return page


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
            # An object built with a request-scoped view keeps the view, which
            # has every member of Client and reads the rest from its API, so it
            # stands in for the API that this annotation promises.
            return cast("API", obj._api_override)

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


_READ_AS: Mapping[Any, type[Any]] = MappingProxyType(
    {
        list: list,
        tuple: tuple,
        set: set,
        frozenset: frozenset,
        collections.deque: collections.deque,
        Iterable: tuple,
        Collection: tuple,
        Sequence: tuple,
        MutableSequence: list,
        AbstractSet: frozenset,
        MutableSet: set,
    }
)
"""Each collection type whose items eazyrest converts, and the type that a
value of it reads as. An abstract type reads as an immutable one where it
can."""

_MAPPING_TYPES = (dict, Mapping, MutableMapping)
"""Mapping types whose values eazyrest converts, which read as a ``dict``."""


def _is_collection_type(ty: Any) -> bool:
    """Return whether a type represents a collection.

    Args:
        ty: Declared type to test.

    Returns:
        Whether ``ty`` is a collection type whose items eazyrest converts,
        such as ``list``, ``set``, ``Sequence``, or ``deque``.
    """
    return ty in _READ_AS


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
    object's absolute URL. The related model needs a primary-key field, as an
    object's key comes from its JSON once it loads.

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
class TypedDictType:
    """Type metadata for a ``TypedDict``, whose keys convert by annotation.

    Each declared key converts by its annotation, and an undeclared key is
    kept as JSON. A missing key is not an error. The keys are analyzed when
    they are first needed, so a ``TypedDict`` can refer to itself.
    """

    typed_dict: type[Any]

    @cached_property
    def keys(self) -> Mapping[str, AnalyzedType]:
        """Return the analyzed type of each declared key.

        Raises:
            TypeError: If a key's annotation cannot be resolved or converted,
                or holds a model.
        """
        return _typed_dict_keys(self.typed_dict)


@dataclass(frozen=True)
class MappingType:
    """Type metadata for a JSON object whose values all have one type.

    ``dict[str, T]`` and ``Mapping[str, T]`` read and write a JSON object
    whose values convert as ``T``.
    """

    value_type: AnalyzedType


@dataclass(frozen=True)
class FixedTupleType:
    """Type metadata for a fixed-length tuple, such as ``tuple[int, str]``.

    Its JSON form is an array of exactly that length, whose items convert
    by position.
    """

    item_types: tuple[AnalyzedType, ...]


@dataclass(frozen=True)
class NamedTupleType:
    """Type metadata for a ``NamedTuple``, whose JSON form is an array.

    The array's items convert by position, as the fields' annotations say.
    The fields are analyzed when they are first needed.
    """

    named_tuple: type[Any]

    @cached_property
    def fields(self) -> tuple[tuple[str, AnalyzedType], ...]:
        """Return each field's name and analyzed type, in order.

        Raises:
            TypeError: If an annotation cannot be resolved or converted, or
                holds a model.
        """
        hints = _hints_of(self.named_tuple)
        return tuple(
            (name, _structure_member(hints[name], self.named_tuple, name))
            for name in self.named_tuple._fields
        )


@dataclass(frozen=True)
class DataclassType:
    """Type metadata for a standard library dataclass.

    Its JSON form is an object whose keys are the fields that ``__init__``
    takes, each converting by its annotation. The fields are analyzed when
    they are first needed.
    """

    dataclass_type: type[Any]

    @cached_property
    def fields(self) -> tuple[tuple[str, AnalyzedType, bool], ...]:
        """Return each field's name, analyzed type, and whether it is required.

        Raises:
            TypeError: If an annotation cannot be resolved or converted, or
                holds a model.
        """
        cls = self.dataclass_type

        def hint(name: str) -> Any:
            try:
                return _dataclass_field_annotation(cls, name)
            except Exception as err:
                raise TypeError(
                    f"The annotations of {cls.__name__} cannot be resolved: "
                    f"{err}"
                ) from err

        return tuple(
            (
                field.name,
                _structure_member(hint(field.name), cls, field.name),
                field.default is dataclasses.MISSING
                and field.default_factory is dataclasses.MISSING,
            )
            for field in dataclasses.fields(self.dataclass_type)
            if field.init
        )


@dataclass(frozen=True)
class UnionType:
    """Type metadata for a union whose members are leaf types.

    It converts only if every member passes through unchanged, which a
    class's conversion table decides, since a JSON value does not say which
    member it is.
    """

    members: tuple[BaseType, ...]


@dataclass(frozen=True)
class NewTypeType:
    """Type metadata for a ``NewType`` of a type that eazyrest structures.

    The ``NewType``, or else its supertype, may have a conversion, which
    replaces the supertype's structure, such as a ``tuple``'s or a
    dataclass's. Without one, it converts as ``inner``. A ``NewType`` of a
    leaf type is a ``BaseType``, and one whose supertype holds a model
    analyzes as its supertype, so that its related objects read as any
    other field's do.
    """

    new_type: Any
    inner: AnalyzedType


AnalyzedType: TypeAlias = (
    BaseType
    | NewTypeType
    | RelatedType
    | OptionalType
    | CollectionType
    | MappingType
    | TypedDictType
    | FixedTupleType
    | NamedTupleType
    | DataclassType
    | UnionType
)


def _is_type_alias(ty: Any) -> bool:
    """Return whether a type is a named type alias.

    A named alias is a ``typing.TypeAliasType``, which the ``type`` statement
    creates, or its ``typing_extensions`` backport. An ordinary assignment, as
    in ``Points = list[Point]``, binds the type itself and is not one.

    The test reads the object's class and nothing of its value. Reading the
    value evaluates it, which raises the error of a name that is not defined
    yet, whatever that error is, and a test must not hide or change it. The
    caller that needs the value reads it, and gets that error.

    Args:
        ty: Type to test.

    Returns:
        Whether ``ty`` is a named type alias.
    """
    return type(ty).__name__ == "TypeAliasType"


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
        _UnresolvedAliasError: If a string reference names nothing in the
            alias's module, which a ``TypeError`` reports.
        Exception: If evaluating the alias's value raises, as for a name that
            is not defined yet, whatever that raises.
    """
    value = alias.__value__
    if not _has_forward_refs(value):
        return value

    try:
        return _resolve_in_module(value, alias.__module__, alias.__name__)
    except Exception as error:
        raise _UnresolvedAliasError(
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


class _KeylessFieldTypeError(TypeError):
    """A model without a primary-key field is the type of a field."""


class _UnresolvedAliasError(TypeError):
    """A type alias names a type by a string that does not resolve yet."""


class _PydanticMetadataError(TypeError):
    """``Annotated`` metadata that is for Pydantic, which eazyrest ignores."""


def _check_not_pydantic_metadata(metadata: Any) -> None:
    """Refuse ``Annotated`` metadata that is for Pydantic.

    Metadata is for Pydantic if it has Pydantic's schema hook, or its type
    comes from the ``pydantic`` package. That covers ``Field()``, the
    validators and serializers, and the encoders in ``Base64Bytes``. The
    constraints of ``annotated_types``, which other libraries use too, are not.
    eazyrest does not apply any of it, so it would change nothing. A conversion
    for the underlying type does not make it apply.

    Args:
        metadata: One item of ``Annotated`` metadata.

    Raises:
        TypeError: If ``metadata`` is for Pydantic.
    """
    module = type(metadata).__module__
    if hasattr(metadata, "__get_pydantic_core_schema__") or (
        module == "pydantic" or module.startswith("pydantic.")
    ):
        raise _PydanticMetadataError(
            f"The Annotated metadata {type(metadata).__name__} is for "
            "Pydantic, which eazyrest does not apply, so it would change "
            "nothing. Remove the Pydantic metadata, or register a Conversion "
            "for the type."
        )


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

    ``Annotated[T, ...]`` analyzes as ``T``, and its metadata does not change
    the conversion.

    A named type alias, such as one that the ``type`` statement creates,
    analyzes as the type it stands for, and so does a bare alias from
    ``typing``, such as ``typing.List``, which analyzes as ``list``. A
    ``TypedDict`` and a mapping with string keys, such as ``dict[str, T]``,
    analyze as structure whose keys or values eazyrest converts.

    Args:
        ty: Declared field type.

    Returns:
        Structured type metadata for ``ty``.

    Raises:
        TypeError: If ``ty`` holds a named type alias that refers to itself, or
            whose type parameters cannot be substituted. Also raised for a
            model without a primary-key field, which cannot be the type of a
            field, whether alone or inside an optional, a collection, or a
            ``NewType``. Also raised for a mapping whose keys are not strings,
            whose JSON form would read them as strings, for a model inside a
            mapping, for a union that holds a ``TypedDict``, and for a
            ``TypedDict`` that is parameterized. A closed ``TypedDict`` raises
            when its keys are first analyzed, since a conversion may replace
            it. A string in a named type alias that names nothing in the
            alias's module raises one that says the annotations cannot be
            resolved.
        NameError: If ``ty`` holds a named type alias whose value names a class
            that is not defined yet. Evaluating the value raises whatever the
            name raises, so an attribute of a module that does not have it yet
            raises an ``AttributeError``, and that is raised as it is.
    """
    ty = _expand_aliases(ty)
    ty_origin = typing.get_origin(ty)
    ty_args = typing.get_args(ty)
    if ty_origin is not None and not ty_args and not hasattr(ty, "__args__"):
        # A bare alias from typing, such as typing.List, which stands for its
        # origin, here list. A subscripted one has __args__, even tuple[()].
        ty = ty_origin
        ty_origin = None

    if ty_origin is Annotated:
        for metadata in ty_args[1:]:
            _check_not_pydantic_metadata(metadata)
        inner = analyze_type(ty_args[0])
        if any(isinstance(metadata, Hyperlinked) for metadata in ty_args[1:]):
            return _hyperlinked(inner)
        return inner

    if ty_origin in (Union, types.UnionType):
        non_none_args = tuple(arg for arg in ty_args if arg is not type(None))
        if len(non_none_args) == 1 and len(non_none_args) != len(ty_args):
            return OptionalType(analyze_type(non_none_args[0]))
        if any(_names_typed_dict(arg) for arg in non_none_args):
            raise TypeError(
                f"{ty!r} is a union that holds a TypedDict, which eazyrest "
                "cannot convert, because a JSON object does not say which "
                "member it is. Annotate the field as Any to keep its JSON."
            )
        union = _union_type(ty, non_none_args)
        if len(non_none_args) != len(ty_args):
            return OptionalType(union)
        return union

    supertype = _supertype(ty)
    if supertype is not None:
        inner = analyze_type(supertype)
        if isinstance(inner, BaseType):
            return BaseType(ty)
        if _class_key(inner) is not None or not _holds_model(inner):
            return NewTypeType(ty, inner)
        return inner

    if lenient_issubclass(ty, JSONObject):
        assert issubclass(ty, JSONObject)
        if _pk_property_of(ty) is None:
            if "_json_fields" not in vars(ty):
                # Only json_object() declares a class's fields, and it does so
                # on the class itself. A subclass inherits the attribute from a
                # decorated base, so look in the class's own namespace. A class
                # that it has not decorated has no key field either, and
                # decorating it is not enough if it still declares none, so the
                # message says both.
                raise _KeylessFieldTypeError(
                    f"{ty.__name__} must be decorated with @json_object, "
                    "which declares its fields, before it can be the type of "
                    "a field. Its decorator also needs a primary-key field, "
                    "which the class declares or json_object(pk=...) names. "
                    "Declare nested JSON without a key as a TypedDict or a "
                    "dataclass instead."
                )
            raise _KeylessFieldTypeError(
                f"{ty.__name__} has no primary-key field, so it cannot be "
                "the type of a field. Declare nested JSON as a TypedDict or "
                f"a dataclass, or declare the key field of {ty.__name__}."
            )
        return RelatedType(ty)

    if ty is tuple:
        # A bare tuple is a tuple of any length, of items read as they are.
        return CollectionType(tuple, BaseType(cast(type[Any], Any)))
    if ty_origin is tuple and len(ty_args) == 2 and ty_args[1] is Ellipsis:
        # tuple[T, ...] is a homogeneous tuple of any length.
        ty_args = ty_args[:1]
    elif ty_origin is tuple:
        item_types = tuple(analyze_type(arg) for arg in ty_args)
        for item_type in item_types:
            _check_no_models(item_type, ty)
        return FixedTupleType(item_types)

    if _is_collection_type(ty_origin) and len(ty_args) == 1:
        return CollectionType(
            collection_type=cast(type[Any], ty_origin),
            item_type=analyze_type(ty_args[0]),
        )

    if (
        ty_origin in _MAPPING_TYPES
        and len(ty_args) == 2
        and ty_args[0] in (str, Any)
    ):
        value_type = analyze_type(ty_args[1])
        _check_no_models(value_type, ty)
        return MappingType(value_type)

    if _is_typed_dict(ty_origin):
        raise TypeError(
            f"{ty!r} is a parameterized generic TypedDict, which eazyrest "
            "cannot convert. Annotate the TypedDict that it stands for."
        )
    if _is_typed_dict(ty):
        return TypedDictType(ty)

    _check_json_form(ty, ty_origin)
    if _is_named_tuple(ty):
        return NamedTupleType(ty)
    if _is_dataclass(ty):
        _check_no_init_only(ty)
        return DataclassType(ty)
    _check_is_type(ty, ty_origin)
    return BaseType(ty)


def _check_is_type(ty: Any, ty_origin: Any) -> None:
    """Refuse an annotation that is not a type, which would pass JSON through.

    A function, a module, an instance, a value, a special form such as
    ``LiteralString``, ``Never``, ``Self``, or ``Final``, and a ``TypeVar``
    name no type that eazyrest converts. Python can also evaluate a field's
    annotation to a method of the class, when a method shares the name of the
    annotation's type. A bare ``None`` passes, since it stands for ``NoneType``
    inside a parameterized type, and so does ``Literal[...]``.

    Args:
        ty: Declared type, which eazyrest would otherwise pass through.
        ty_origin: ``typing.get_origin(ty)``.

    Raises:
        TypeError: If ``ty`` is not a class, ``None``, or ``Literal[...]``.
    """
    if isinstance(ty, type) or ty is None or ty_origin is Literal:
        return
    raise TypeError(
        f"{ty!r} is not a type, so eazyrest cannot convert a field that it "
        "annotates, whose JSON would pass through unconverted. Annotate a "
        "class, such as int or datetime.datetime, or Any to keep any JSON. "
        "A method or an attribute of the class that has the type's name can "
        "hide it, so annotate such a type by a name that is not shadowed."
    )


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


def _is_dataclass(ty: Any) -> bool:
    """Return whether a type is a dataclass, rather than an instance of one.

    Args:
        ty: Type to test.

    Returns:
        Whether ``ty`` is a dataclass.
    """
    return isinstance(ty, type) and dataclasses.is_dataclass(ty)


def _check_no_init_only(ty: type[Any]) -> None:
    """Refuse a dataclass with an init-only field.

    An init-only field, of type ``dataclasses.InitVar``, is a parameter of
    ``__init__`` that the dataclass does not keep, so a read cannot give it the
    key's value and a write cannot find the value to write.

    Args:
        ty: Dataclass.

    Raises:
        TypeError: If ``ty`` has an init-only field, own or inherited.
    """
    for name, field in ty.__dataclass_fields__.items():
        if field._field_type is dataclasses._FIELD_INITVAR:  # type: ignore[attr-defined]
            raise TypeError(
                f"{ty.__name__} has the init-only field {name}, which a "
                "dataclass does not keep, so eazyrest cannot read or write "
                "it. Make it a field."
            )


def _union_type(ty: Any, members: tuple[Any, ...]) -> UnionType:
    """Analyze a union of the types that a JSON value could be.

    Args:
        ty: The union, for the message.
        members: Its members other than ``None``.

    Returns:
        The union's type metadata.

    Raises:
        TypeError: If a member is not a leaf type, such as a collection or a
            model, which a union cannot convert.
    """
    analyzed = tuple(analyze_type(member) for member in members)
    leaves = tuple(
        member for member in analyzed if isinstance(member, BaseType)
    )
    if len(leaves) != len(analyzed):
        raise TypeError(_union_message(ty))
    return UnionType(leaves)


def _union_message(ty: Any) -> str:
    """Return the message for a union that eazyrest cannot convert.

    Args:
        ty: The union.

    Returns:
        The message.
    """
    return (
        f"{ty!r} is a union whose members do not all pass through "
        "unchanged, which eazyrest cannot convert, because a JSON value does "
        "not say which member it is. Annotate the field as Any to keep "
        "its JSON."
    )


def _check_union(union: UnionType, table: Mapping[Any, Conversion]) -> None:
    """Check that every member of a union passes through unchanged.

    Args:
        union: The union's type metadata.
        table: Conversion table of the model whose field it is.

    Raises:
        TypeError: If a member has a conversion, or is a ``Literal`` with an
            enum member, which reads as the member rather than its value.
    """
    for member in union.members:
        ty = member.base_type
        if _conversion_of(ty, table) is not None or (
            typing.get_origin(ty) is Literal
            and any(isinstance(arg, enum.Enum) for arg in typing.get_args(ty))
        ):
            raise TypeError(
                _union_message(
                    functools.reduce(
                        operator.or_,
                        (member.base_type for member in union.members),
                    )
                )
            )


def _check_optional_union(
    analyzed: AnalyzedType, table: Mapping[Any, Conversion]
) -> None:
    """Check a union, or an optional one, whose value is ``None``.

    Args:
        analyzed: Analyzed type of the ``None`` value.
        table: Conversion table of the model whose field it is.

    Raises:
        TypeError: If ``analyzed`` is a union with a member that converts.
    """
    analyzed = _unwrap_optional_type(analyzed)
    if isinstance(analyzed, UnionType):
        _check_union(analyzed, table)


def _check_field_unions(
    analyzed: AnalyzedType, table: Mapping[Any, Conversion]
) -> None:
    """Check each union in a field's own type, as reading the field would.

    The walk goes through optionals, unions, collections, mappings,
    fixed-length tuples, and ``NewType``. It does not enter a structure or a
    model, whose members are checked when the field is first used.

    Args:
        analyzed: Analyzed type of a field.
        table: Conversion table of the model that declares the field.

    Raises:
        TypeError: If a union has a member that converts.
    """
    match analyzed:
        case UnionType():
            _check_union(analyzed, table)
        case OptionalType(inner):
            _check_field_unions(inner, table)
        case CollectionType(_, item):
            _check_field_unions(item, table)
        case MappingType(value):
            _check_field_unions(value, table)
        case FixedTupleType(items):
            for item in items:
                _check_field_unions(item, table)
        case NewTypeType(_, inner):
            _check_field_unions(inner, table)


def _pydantic_class(ty: Any) -> type[Any] | None:
    """Return the class of a type that Pydantic configures, if it is one.

    A class is Pydantic's if it carries ``__pydantic_config__``, as a
    ``TypedDict`` or a dataclass does after ``pydantic.with_config()``, or has
    Pydantic's schema hook ``__get_pydantic_core_schema__``, as a type written
    for Pydantic does, or derives from a ``BaseModel`` or ``RootModel`` of the
    ``pydantic`` package. The test goes by attribute and name, so it does not
    import Pydantic.

    Args:
        ty: Type to test, which may be a ``NewType`` or a parameterized
            generic.

    Returns:
        The class, or ``None`` if ``ty`` is not one that Pydantic configures.
    """
    cls = _final_supertype(ty)
    cls = typing.get_origin(cls) or cls
    if not isinstance(cls, type) or cls.__module__ == "builtins":
        return None
    if hasattr(cls, "__pydantic_config__") or hasattr(
        cls, "__get_pydantic_core_schema__"
    ):
        return cls
    for base in cls.__mro__:
        if base.__name__ in ("BaseModel", "RootModel") and (
            base.__module__ == "pydantic"
            or base.__module__.startswith("pydantic.")
        ):
            return cls
    return None


def _refuse_pydantic_class(ty: Any) -> None:
    """Refuse a class that Pydantic configures and no conversion owns.

    eazyrest does not apply Pydantic's validation, so a field of such a class
    would read its JSON without the validation that its declaration implies.

    Args:
        ty: Type of a field or of a member that eazyrest walks.

    Raises:
        TypeError: If ``ty`` is a class that Pydantic configures.
    """
    cls = _pydantic_class(ty)
    if cls is not None:
        raise TypeError(
            f"{cls.__name__} is configured for Pydantic, which eazyrest does "
            "not apply, so a field of that type would read its JSON without "
            "validation. Register a Conversion for it in the model's "
            "conversions, or remove the Pydantic configuration."
        )


_RAW_JSON_CLASSES: frozenset[Any] = frozenset(
    {Any, object, str, int, float, bool, dict, list, type(None)}
)
"""The classes whose JSON eazyrest reads and writes as it is: arbitrary JSON,
the JSON primitives, and a bare JSON object or array."""


def _refuse_unconverted_class(ty: Any) -> None:
    """Refuse a class whose JSON eazyrest has no way to read and write.

    A class that nothing converts, such as ``bytes``, ``Path``, a class of the
    application, ``Hashable``, ``type``, or ``Callable``, has no JSON form that
    eazyrest knows, so a value of it would pass through unconverted, and a
    declaration that says otherwise would not hold.

    Args:
        ty: Class that no entry of the model's conversion table converts, as a
            field or a member that eazyrest walks declares it.

    Raises:
        TypeError: If ``ty`` is not ``Any``, ``object``, a JSON primitive, a
            bare ``dict`` or ``list``, or a ``Literal``.
    """
    cls = ty
    if typing.get_origin(cls) is Literal:
        return
    try:
        if cls in _RAW_JSON_CLASSES:
            return
    except TypeError:
        # An unhashable annotation is no class that passes through.
        pass
    name = getattr(cls, "__name__", repr(cls))
    raise TypeError(
        f"{name} has no conversion, so eazyrest cannot say how its JSON reads "
        "or writes. Annotate Any to keep the JSON as it is, give a bare "
        "collection its items, as set[Any], or register a Conversion for "
        f"{name} in the model's conversions."
    )


def _check_admitted(
    analyzed: AnalyzedType,
    table: Mapping[Any, Conversion],
    seen: frozenset[type[Any]] = frozenset(),
    *,
    late: bool = False,
) -> None:
    """Check that eazyrest can convert a type and each member that it walks.

    The walk goes through optionals, unions, collections, mappings,
    fixed-length tuples, ``NewType``, and the members of a ``TypedDict``, a
    ``NamedTuple``, and a dataclass. A class that an entry of the table
    converts is not entered. It refuses a class that Pydantic configures and a
    class that nothing converts.

    A member whose annotations cannot be analyzed yet waits for the field's
    first use, unless ``late`` says that this is it.

    Args:
        analyzed: Analyzed type of a field.
        table: Conversion table of the model that owns the field.
        seen: Structures being walked, to stop at a cycle.
        late: Whether the field is in use, so that a member that cannot be
            analyzed raises.

    Raises:
        TypeError: If a member is refused. If ``late``, also whatever analyzing
            a member raises.
    """
    if _class_entry(analyzed, table) is not None:
        return
    members: Iterable[AnalyzedType] = ()
    match analyzed:
        case BaseType(base_type):
            if _conversion_of(base_type, table) is None:
                _refuse_pydantic_class(base_type)
                if (supertype := _supertype(base_type)) is not None:
                    _check_admitted(
                        analyze_type(supertype), table, seen, late=late
                    )
                else:
                    _refuse_unconverted_class(base_type)
        case UnionType(leaves):
            members = leaves
        case OptionalType(inner) | NewTypeType(_, inner):
            members = (inner,)
        case CollectionType(_, item) | MappingType(item):
            members = (item,)
        case FixedTupleType(items):
            members = items
        case TypedDictType(cls) | NamedTupleType(cls) | DataclassType(cls):
            _refuse_pydantic_class(cls)
            if cls in seen:
                return
            seen |= {cls}
            try:
                match analyzed:
                    case TypedDictType():
                        members = tuple(analyzed.keys.values())
                    case NamedTupleType():
                        members = tuple(item for _, item in analyzed.fields)
                    case DataclassType():
                        members = tuple(item for _, item, _ in analyzed.fields)
            except _PydanticMetadataError:
                raise
            except Exception:
                if late:
                    raise
                # Whatever analyzing a member raises, as for a name that is not
                # defined yet, waits for the field's first use.
                return
    for member in members:
        _check_admitted(member, table, seen, late=late)


def _hints_of(cls: type[Any]) -> dict[str, Any]:
    """Resolve the annotations of a class whose fields eazyrest converts.

    Args:
        cls: ``TypedDict``, ``NamedTuple``, or dataclass.

    Returns:
        Each annotation, resolved, with its ``Annotated`` metadata.

    Raises:
        TypeError: If an annotation cannot be resolved.
    """
    get_type_hints = (
        typing.get_type_hints
        if _typing_extensions is None
        else _typing_extensions.get_type_hints
    )
    try:
        return dict(get_type_hints(cls, include_extras=True))
    except Exception as err:
        raise TypeError(
            f"The annotations of {cls.__name__} cannot be resolved: {err}"
        ) from err


def _annotation_owner(cls: type[Any], name: str) -> type[Any]:
    """Return the class in a class's MRO that annotates a name.

    Args:
        cls: Class whose annotation is wanted.
        name: Name that a class in its MRO annotates.

    Returns:
        The first class in ``cls``'s MRO that annotates ``name`` itself.
    """
    return next(base for base in cls.__mro__ if name in _annotations(base))


def _resolve_in_module(
    value: Any,
    module: str,
    holder_name: str,
    localns: Mapping[str, Any] | None = None,
) -> Any:
    """Resolve a value as the annotation of a class in a module.

    A string in ``value``, at any depth, names a type as it does in an
    annotation: first in ``localns`` if given, then in the module's names. This
    is the one place that builds the class for ``get_type_hints()``, so every
    resolution reads names alike.

    Args:
        value: Annotation, or any value that holds strings that name types.
        module: Name of the module whose names the strings use.
        holder_name: Name of the class that holds the annotation, which an
            error from resolving may quote.
        localns: Names that take precedence over the module's, or ``None`` for
            none.

    Returns:
        ``value``, with its ``Annotated`` metadata and each string replaced by
        the type that it names.

    Raises:
        Exception: If a string names something undefined, whatever resolving it
            raises.
    """
    holder = type(
        holder_name,
        (),
        {"__module__": module, "__annotations__": {"value": value}},
    )
    return typing.get_type_hints(holder, localns=localns, include_extras=True)[
        "value"
    ]


def _resolve_annotation(
    owner: type[Any], name: str, hidden: Container[str] = ()
) -> Any:
    """Resolve one annotation of a class, with its ``Annotated`` metadata.

    Only this annotation is evaluated. Another one, such as a ``ClassVar`` or
    an excluded field, may name a type that exists only for type checkers, and
    it must not make this one unresolvable.

    A name resolves to the owner itself if it is the owner's own name, so that
    a class can name itself whatever its module binds the name to. Otherwise it
    resolves in the owner's module, and only then in the owner's namespace
    without ``hidden``, which lets a class defined inside a function name
    others from that function. This is the order of ``get_type_hints()`` on a
    class and of the ``TypedDict`` and ``NamedTuple`` path, so an attribute of
    the class that is not a field, such as a method named ``date``, does not
    shadow the module's ``date`` in a string annotation.

    Args:
        owner: Class that declares the annotation.
        name: Name that it annotates.
        hidden: Names of the owner's attributes that must not resolve, such as
            the descriptors or defaults of its fields, which could hide the
            types that annotations name.

    Returns:
        The annotation, resolved.

    Raises:
        Exception: If the annotation names something undefined, whatever
            resolving it raises.
    """
    module = sys.modules.get(owner.__module__)
    module_names = vars(module) if module is not None else {}
    localns = {
        key: value
        for key, value in vars(owner).items()
        if key not in hidden and key not in module_names
    }
    localns[owner.__name__] = owner
    return _resolve_in_module(
        _annotations(owner)[name], owner.__module__, owner.__name__, localns
    )


def _dataclass_field_annotation(cls: type[Any], name: str) -> Any:
    """Resolve the annotation of one field of a dataclass.

    Only this annotation is evaluated, so that a class variable whose
    annotation names a type that exists only for type checkers does not matter.
    The class's field attributes, such as defaults, do not hide the types that
    annotations name.

    Args:
        cls: Dataclass.
        name: Name of one of its fields.

    Returns:
        The field's annotation, resolved, with its ``Annotated`` metadata.

    Raises:
        Exception: If the annotation names something undefined, whatever
            resolving it raises.
    """
    names = {field.name for field in dataclasses.fields(cls)}
    return _resolve_annotation(_annotation_owner(cls, name), name, names)


def _structure_member(hint: Any, container: Any, name: str) -> AnalyzedType:
    """Analyze the annotation of a field of a ``NamedTuple`` or dataclass.

    Args:
        hint: The field's annotation.
        container: The class, for messages.
        name: The field's name, for messages.

    Returns:
        The field's type metadata.

    Raises:
        TypeError: If the annotation cannot be converted, or holds a model.
    """
    try:
        analyzed = analyze_type(hint)
    except _PydanticMetadataError as err:
        raise _PydanticMetadataError(
            f"{container.__name__}.{name}: {err}"
        ) from err
    _check_no_models(analyzed, container)
    return analyzed


def _names_typed_dict(ty: Any) -> bool:
    """Return whether a union member is a ``TypedDict``.

    Args:
        ty: Member of a union, which may be a named type alias or
            ``Annotated``.

    Returns:
        Whether ``ty`` is a ``TypedDict``, or a parameterized one.
    """
    ty = _expand_aliases(ty)
    if typing.get_origin(ty) is Annotated:
        ty = typing.get_args(ty)[0]
    cls = typing.get_origin(ty) or ty
    return _is_typed_dict(cls)


_NO_EXTRA_ITEMS: tuple[Any, ...] = (
    None,
    getattr(typing, "NoExtraItems", None),
    getattr(_typing_extensions, "NoExtraItems", None),
)
"""Values of ``__extra_items__`` for a ``TypedDict`` without extra items."""


def _key_qualifiers() -> frozenset[Any]:
    """Return the qualifiers that a ``TypedDict`` key's annotation may have.

    Returns:
        ``Required``, ``NotRequired``, and ``ReadOnly``, from ``typing`` and
        ``typing_extensions``, as far as each has them.
    """
    modules = [typing]
    if _typing_extensions is not None:
        modules.append(_typing_extensions)
    return frozenset(
        getattr(module, name)
        for module in modules
        for name in ("Required", "NotRequired", "ReadOnly")
        if hasattr(module, name)
    )


_KEY_QUALIFIERS = _key_qualifiers()


def _without_key_qualifiers(hint: Any) -> Any:
    """Remove a ``TypedDict`` key's qualifiers, inside ``Annotated`` too.

    PEP 655 allows ``Annotated[NotRequired[T], ...]`` as well as
    ``NotRequired[Annotated[T, ...]]``, and both mean the same key.

    Args:
        hint: The key's annotation.

    Returns:
        ``hint`` without ``Required``, ``NotRequired``, and ``ReadOnly``, with
        the metadata of any ``Annotated`` kept.
    """
    origin = typing.get_origin(hint)
    if origin in _KEY_QUALIFIERS:
        return _without_key_qualifiers(typing.get_args(hint)[0])
    if origin is Annotated:
        inner, *metadata = typing.get_args(hint)
        stripped = _without_key_qualifiers(inner)
        if stripped is not inner:
            return Annotated[(stripped, *metadata)]
    return hint


def _typed_dict_keys(ty: type[Any]) -> dict[str, AnalyzedType]:
    """Analyze the declared keys of a ``TypedDict``.

    ``Required``, ``NotRequired``, and ``ReadOnly`` say whether a key may be
    missing or changed, which conversion does not check, so they are removed.

    Args:
        ty: ``TypedDict`` class.

    Returns:
        The analyzed type of each declared key.

    Raises:
        TypeError: If ``ty`` is closed or has extra items (PEP 728), or if an
            annotation cannot be resolved or converted, or holds a model.
    """
    if getattr(ty, "__closed__", None) or not any(
        getattr(ty, "__extra_items__", None) is marker
        for marker in _NO_EXTRA_ITEMS
    ):
        raise TypeError(
            f"{ty.__name__} is closed or declares extra items, which eazyrest "
            "does not convert yet. Remove closed= and extra_items=, or give "
            "it a Conversion."
        )
    keys: dict[str, AnalyzedType] = {}
    for key, hint in _hints_of(ty).items():
        hint = _without_key_qualifiers(hint)
        try:
            analyzed = analyze_type(hint)
        except _PydanticMetadataError as err:
            raise _PydanticMetadataError(
                f"{ty.__name__}.{key}: {err}"
            ) from err
        _check_no_models(analyzed, ty)
        keys[key] = analyzed
    return keys


class _ModelInContainerError(TypeError):
    """A model inside a ``TypedDict``, a mapping, a tuple, or the like."""


def _holds_model(analyzed: AnalyzedType) -> bool:
    """Return whether an analyzed type reads related objects.

    Args:
        analyzed: Analyzed type.

    Returns:
        Whether ``analyzed`` is a related model, or an optional or a
        collection of one.
    """
    match analyzed:
        case RelatedType():
            return True
        case OptionalType(inner) | CollectionType(_, inner):
            return _holds_model(inner)
    return False


def _check_related_class(value: JSONObject, model_type: type[Any]) -> None:
    """Refuse a related object that is not of the relation's model.

    Writing the key or URL of an object of another model would name a resource
    of the wrong collection.

    Args:
        value: Object written as a relation.
        model_type: Model that the relation's field names.

    Raises:
        TypeError: If ``value``'s class is neither ``model_type`` nor a
            subclass of it. The field's name is added where the conversion
            runs.
    """
    if not isinstance(value, model_type):
        raise TypeError(
            f"A relation to {model_type.__name__} cannot take an object of "
            f"{type(value).__name__}, which is another model"
        )


def _has_key_of(cls: type[Any], model_type: type[Any]) -> bool:
    """Return whether a class is keyed by the field of a related model.

    A subclass can choose another key field, so its objects name resources by a
    key that the related model's relation does not hold.

    Args:
        cls: Class of an object that a relation holds.
        model_type: Model that the relation's field names.

    Returns:
        Whether the key field of ``cls`` is the key field of ``model_type``.
    """
    key = _pk_property_of(cls)
    related_key = _pk_property_of(model_type)
    return (key is None and related_key is None) or (
        key is not None
        and related_key is not None
        and key.field == related_key.field
    )


def _check_related_key(value: JSONObject, model_type: type[Any]) -> None:
    """Refuse a related object whose class is keyed by another field.

    Writing the key of such an object would name a resource of the related
    model by a key that it does not have.

    Args:
        value: Object written as a relation whose class is ``model_type`` or a
            subclass.
        model_type: Model that the relation's field names.

    Raises:
        TypeError: If the key of ``value``'s class is not the key of
            ``model_type``. The field's name is added where the conversion
            runs.
    """
    if not _has_key_of(type(value), model_type):
        raise TypeError(
            f"A relation to {model_type.__name__} cannot take an object of "
            f"{type(value).__name__}, which chose another primary key than "
            f"{model_type.__name__}'s"
        )


def _check_prefetched_class(
    obj: Any,
    model_type: type[Any],
    hook: str,
    owner: type[Any],
    field: str,
) -> None:
    """Refuse a prefetched object that is not of the relation's model.

    A parent would otherwise read an object of another model and resource as
    the related object.

    Args:
        obj: Object that the bulk hook returned.
        model_type: Model that the relation's field names.
        hook: Name of the bulk hook, for the message.
        owner: Class of the parent objects, for the message.
        field: Name of the relation's field, for the message.

    Raises:
        TypeError: If ``obj`` is not an instance of ``model_type``, which a
            subclass of it is.
    """
    if not isinstance(obj, model_type):
        raise TypeError(
            f"{model_type.__name__}.{hook}() returned an object of "
            f"{type(obj).__name__} for {owner.__name__}.{field}, which is "
            f"not a {model_type.__name__}"
        )


def _check_no_models(analyzed: AnalyzedType, container: Any) -> None:
    """Refuse a model inside a ``TypedDict`` or a mapping.

    Reading a related object inside such a value would need its own rules,
    for its API, for reloads, and for prefetch, which a later release may
    add.

    Args:
        analyzed: Analyzed type of a key or value of ``container``.
        container: The ``TypedDict`` or mapping type, for the message.

    Raises:
        TypeError: If ``analyzed`` holds a related model.
    """
    match analyzed:
        case RelatedType(model_type):
            name = getattr(container, "__name__", repr(container))
            raise _ModelInContainerError(
                f"{name} holds the model {model_type.__name__}, which "
                "eazyrest does not convert inside a TypedDict, a mapping, a "
                "fixed-length tuple, a NamedTuple, or a dataclass. Annotate "
                "the model's primary key type instead."
            )
        case OptionalType(inner):
            _check_no_models(inner, container)
        case CollectionType(_, item_type):
            _check_no_models(item_type, container)
        case MappingType(value_type):
            _check_no_models(value_type, container)
        case FixedTupleType(item_types):
            for item_type in item_types:
                _check_no_models(item_type, container)


def _check_json_form(ty: Any, ty_origin: Any) -> None:
    """Refuse a type whose JSON form eazyrest cannot read as the type.

    A mapping with keys other than strings has string keys in JSON, and a
    parameterized generic ``NamedTuple`` or dataclass would need its type
    arguments substituted, which a later release may do.

    Args:
        ty: Declared type, which eazyrest would otherwise pass through.
        ty_origin: ``typing.get_origin(ty)``.

    Raises:
        TypeError: If ``ty`` is such a type.
    """
    if ty_origin in _MAPPING_TYPES:
        raise TypeError(
            f"{ty!r} is a mapping whose keys are not strings, whose JSON form "
            "eazyrest would read as another type. Annotate its JSON form, "
            "such as dict[str, Any]."
        )
    if _is_dataclass(ty_origin) or _is_named_tuple(ty_origin):
        raise TypeError(
            f"{ty!r} is a parameterized generic class, which eazyrest cannot "
            "convert. Annotate the class itself."
        )
    if ty_origin is not None and ty_origin is not Literal:
        raise TypeError(
            f"{ty!r} is a parameterized type that eazyrest does not convert, "
            "whose items would pass through unconverted. Annotate a type that "
            "it converts, such as list[T] or dict[str, T]."
        )


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


def _related_key_type(analyzed_type: AnalyzedType) -> RelatedType | None:
    """Return the relation that a primary key's type names, if it does.

    Args:
        analyzed_type: Analyzed type metadata of a primary key field.

    Returns:
        The relation inside any optional or ``NewType`` wrappers, or ``None``
        if the type is not a relation.
    """
    while isinstance(analyzed_type, OptionalType | NewTypeType):
        analyzed_type = (
            analyzed_type.inner_type
            if isinstance(analyzed_type, OptionalType)
            else analyzed_type.inner
        )

    return analyzed_type if isinstance(analyzed_type, RelatedType) else None


def _held_key(obj: JSONObject) -> Any:
    """Return the primary key that an object holds, without a request.

    The object holds its key if its loaded JSON has the key field, or it has
    recorded a saved key, as when it was constructed with the key or has
    refreshed. The JSON takes precedence. A key that is a relation is the key
    that the object copied from the related object when the relation was
    assigned or loaded, so the related object that the field reads, which may
    be shared, plays no part.

    Args:
        obj: Model object.

    Returns:
        The key as ``pk`` returns it, so the related object's own key when the
        key is a relation. ``None`` if the model has no key field, or the
        object holds no key, or holds ``None``.

    Raises:
        ValueError: If the JSON holds a key that the field cannot convert. A
            conversion's own ``TypeError`` propagates too, and so does any
            other exception that a custom conversion raises.
        UsageError: If the key is a relation whose model's ``api`` was assigned
            after the class was defined.
        Exception: If the key's type cannot be resolved yet, whatever resolving
            it raises, as a ``NameError`` does for a class that is not defined.
    """
    prop = _pk_property_of(type(obj))
    if prop is None:
        return None
    json = obj._json
    if json is not None and prop.json_field in json:
        return _decode_key(obj, json[prop.json_field])
    return _decode_key(obj, obj._saved_key_json)


def _reduce_key(
    cls: type[JSONObject], key: Any
) -> tuple[type[JSONObject], AnalyzedType, Any] | None:
    """Reduce a saved key's JSON to the key of the model that holds the value.

    A key that is a relation names the related object's key, which is not a
    relation. An embedded object gives its key field's value, and a bare key
    already is the related key's JSON.

    Args:
        cls: Model whose key field holds ``key``.
        key: The JSON of the key field.

    Returns:
        The model whose conversions decode the key, the analyzed type of its
        key field, and the key's JSON for that field. ``None`` if the JSON does
        not give a key: it is ``null``, or it embeds an object without the key
        field.

    Raises:
        UsageError: If a related model had ``api`` assigned after the class was
            defined.
        Exception: If a key's type cannot be resolved yet, whatever resolving
            it raises, as a ``NameError`` does for a class that is not defined.
    """
    prop = _pk_property_of(cls)
    if prop is None or key is None:
        return None
    conversion = prop.analyzed_type
    related = _related_key_type(conversion)
    if related is None:
        return cls, conversion, key
    _check_api_attribute(related.model_type, UsageError)
    inner = _pk_property_of(related.model_type)
    # The check of the key refused a relation to a model without a key.
    assert inner is not None
    if isinstance(key, dict):
        if inner.json_field not in key:
            return None
        key = key[inner.json_field]
        if key is None:
            return None
    return related.model_type, inner.analyzed_type, key


def _resolve_key(cls: type[JSONObject]) -> None:
    """Resolve a model's whole key type, before any use of the key.

    Every use of a key resolves its type through this, before it converts
    anything. Resolving the type is everything that converting a key would
    resolve: the key's annotation, the value of each type alias in it, and the
    key type of the model that a key relation leads to. Converting a key then
    resolves no name. The result is cached on the key's property, and only once
    it is complete.

    Args:
        cls: Model class that has a primary key.

    Raises:
        TypeError: If the model's key declaration is refused, which carries a
            mark that tells it from a name that is not defined yet.
        Exception: Whatever resolving a name in the key's type raises, such as
            a ``NameError`` or an ``AttributeError``.
    """
    _ = cls._pk.analyzed_type


def _decode_key(obj: JSONObject, key: Any) -> Any:
    """Decode the JSON of an object's key field, as the field reads it.

    Args:
        obj: Model object, or any object of the model that owns the key field.
        key: The JSON of the key field.

    Returns:
        The key as ``pk`` returns it, so the related model's key when the key
        is a relation, decoded with that model's conversions. ``None`` if the
        JSON does not give a key.

    Raises:
        TypeError: If the key cannot be decoded.
        ValueError: If the key cannot be decoded.
        UsageError: If a related model had ``api`` assigned after the class was
            defined.
        Exception: If a key's type cannot be resolved yet, whatever resolving
            it raises, as a ``NameError`` does for a class that is not defined.
    """
    reduced = _reduce_key(type(obj), key)
    if reduced is None:
        return None
    owner, conversion, value = reduced
    # Decoding a key that is not a relation reads only the model's conversions,
    # so a related model's key is decoded without building an object.
    host = obj if owner is type(obj) else object.__new__(owner)
    return host._from_json(value, conversion)


def _native_key_kinds(
    owner: type[JSONObject], conversion: AnalyzedType
) -> frozenset[str] | None:
    """Return the JSON types that a key of a native codec may be given as.

    A key whose type is ``int`` or ``str``, or a ``Literal`` of such values,
    inside ``T | None``, ``NewType``, and ``Annotated``, has one JSON type. A
    value of another type would name another key than the server's spelling of
    it, which a later load would refuse. A conversion that the model registers
    for the key, or for a ``NewType`` or class inside it, decides its own wire
    type instead.

    Args:
        owner: Model that owns the key field.
        conversion: Analyzed type of the key field.

    Returns:
        The JSON types, or ``None`` if a registered conversion owns the key or
        the type is not one of these.
    """
    table = _table_of(owner)
    while _class_entry(conversion, table) is None and isinstance(
        conversion, OptionalType | NewTypeType
    ):
        conversion = (
            conversion.inner_type
            if isinstance(conversion, OptionalType)
            else conversion.inner
        )
    if _class_entry(conversion, table) is not None:
        return None
    if not isinstance(conversion, BaseType):
        return None
    base = conversion.base_type
    if _conversion_of(base, table) is not None:
        return None
    while hasattr(base, "__supertype__"):
        base = base.__supertype__
    if typing.get_origin(base) is Literal:
        return frozenset(
            kind
            for member in typing.get_args(base)
            if (kind := _json_kind(_literal_json(member, table))) is not None
        )
    if base in (int, str):
        return frozenset({base.__name__})
    return None


def _json_kind(value: Any) -> str | None:
    """Return the JSON type of a key's wire value, or ``None`` for another.

    Args:
        value: A value that encoding gave.

    Returns:
        ``"int"`` or ``"str"``. A ``bool`` is not an ``int``, because it
        encodes differently, and no other value is a key's wire value.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return "int"
    if isinstance(value, str):
        return "str"
    return None


def _build_key(obj: JSONObject, key: Any) -> tuple[Any, str] | None:
    """Build a key from the JSON that names it, as every key is built.

    The key is reduced to the related model's key if it is a relation. A key of
    a native codec must have the JSON type of its field. The key is decoded and
    encoded again by the field's codec, and the encoding must be a string or an
    integer that is not a ``bool``, whatever codec wrote it. Every identity of
    a key derives from the result: equality, the hash, the URL, the prefetch
    table, and the test of a change in place.

    Args:
        obj: Model object, or any object of the model that owns the key field.
        key: The JSON of the key field.

    Returns:
        The key's wire value, and its JSON text, or ``None`` if the JSON names
        no key.

    Raises:
        TypeError: If the key cannot be decoded or encoded, or is not a string
            or an integer as the field's codec writes it, or is of another JSON
            type than a native codec reads.
        ValueError: If the key cannot be decoded or encoded.
        UsageError: If a related model had ``api`` assigned after the class was
            defined.
        Exception: If a key's type cannot be resolved yet, whatever resolving
            it raises, as a ``NameError`` does for a class that is not defined.
    """
    reduced = _reduce_key(type(obj), key)
    if reduced is None:
        return None
    owner, conversion, value = reduced
    kinds = _native_key_kinds(owner, conversion)
    if kinds is not None and _json_kind(value) not in kinds:
        raise TypeError(
            f"The key must be a JSON {' or '.join(sorted(kinds))}, not a "
            f"{type(value).__name__}"
        )
    host = obj if owner is type(obj) else object.__new__(owner)
    decoded = host._from_json(value, conversion)
    encoded = owner._to_json(decoded, conversion)
    if encoded is None:
        return None
    if _json_kind(encoded) is None:
        raise TypeError(
            "The key must be written as a JSON string or integer, not a "
            f"{type(encoded).__name__}"
        )
    # A subclass of str or int is the plain value, so the key has one spelling.
    encoded = (
        str.__str__(encoded) if isinstance(encoded, str) else int(encoded)
    )
    return encoded, json.dumps(encoded)


def _key_problem(obj: JSONObject) -> str:
    """Say why an object's key is not known without a request.

    Args:
        obj: Model object whose key ``_held_key()`` does not give.

    Returns:
        ``"unloaded"`` if the object has no JSON yet or only a partial payload,
        so loading it, or giving its key, may help. Otherwise the JSON is
        complete: ``"missing"`` if it lacks the key field and the object has
        recorded no key, ``"none"`` if the saved key is ``None``, whatever a
        pending change holds, and ``"unreadable"`` if the object has a saved
        key that the field cannot convert.
    """
    state = vars(obj)
    json = state.get("_json")
    if json is None or state.get("_json_is_partial"):
        return "unloaded"
    prop = _pk_property_of(type(obj))
    saved = state.get("_saved_key_json")
    if prop is None or (prop.json_field not in json and saved is None):
        return "missing"
    return "none" if saved is None else "unreadable"


def _unhashable_key_message(obj: JSONObject) -> str:
    """Say why an object cannot be hashed, and what could help.

    Args:
        obj: Model object whose key is not known.

    Returns:
        The message of the ``TypeError`` that ``hash()`` raises. It advises
        loading, saving, or giving the key only if that can help.

    Raises:
        Exception: If the key's type cannot be resolved yet, whatever resolving
            it raises, other than a ``TypeError`` or a ``ValueError``, which
            the message reports. ``hash()`` raises it as it does for any object
            of the model until then.
    """
    name = type(obj).__name__
    detail = ""
    if obj._saved_key_json is not None:
        try:
            _build_key(obj, obj._saved_key_json)
        except (TypeError, ValueError) as err:
            detail = f" ({err})"
    match _key_problem(obj):
        case "missing":
            reason = "is missing from its JSON"
        case "none":
            reason = "is None"
        case "unreadable":
            reason = f"cannot be read from its JSON{detail}"
        case _:
            return (
                f"The {name} object's primary key is not known, so it cannot "
                "be hashed. Load or save the object, or give its key, first"
            )
    return f"The {name} object's primary key {reason}, so it cannot be hashed"


def _known_key(obj: JSONObject) -> Any:
    """Return a related object's primary key if it is known without a request.

    The key is known if the object holds it, or its JSON has it. A key that is
    a relation gives the related model's key, so nothing loads.

    Args:
        obj: Related object that is to be the key of a model.

    Returns:
        The object's key as a plain value, as ``pk`` returns it.

    Raises:
        ValueError: If the object has no key, or its key is not known without a
            request. The message says to load or save it first in the second
            case, unless the object has loaded complete JSON that lacks the key
            field, which loading again cannot change. If that JSON holds the
            key as ``None``, the message says that the key is None.
    """
    cls = type(obj)
    value = _held_key(obj)
    if value is not None:
        return value
    has_own_url = obj._url_override is not None or _url_is_overridden(cls)
    problem = _key_problem(obj)
    if has_own_url and problem == "unloaded":
        raise ValueError(
            f"The {cls.__name__} object's primary key is not known without "
            "a request, so load or save the object first, or give its key"
        )
    if has_own_url and problem == "missing":
        raise ValueError(
            f"The {cls.__name__} object's primary key is missing from its "
            "JSON, so it cannot be the key of another model"
        )
    raise ValueError(
        f"The {cls.__name__} object's primary key is None, so it cannot be "
        "the key of another model"
    )


def _url_is_overridden(cls: type[JSONObject]) -> bool:
    """Return whether a model class defines its own ``url``.

    Args:
        cls: Model class.

    Returns:
        Whether ``url`` is not ``JSONObject.url``, which needs the primary key.
    """
    return cls.url is not JSONObject.url


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
)
"""Types of field values that cannot be changed in place.

``bool`` is a subclass of ``int`` and ``datetime.datetime`` of
``datetime.date``. A ``frozenset`` or a ``tuple`` cannot change, but its
items can, as a dataclass can, so neither is listed.
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


# The options of the query methods. Each method reserves those it does not
# take, so that a later release can give any of them to any method without
# changing what a call that works now sends. 0.9.0's filter() took url.
_QUERY_OPTIONS = frozenset(
    {"api", "prefetch", "defaults", "request_options", "params", "url"}
)


def _query_params(
    params: Mapping[str, Any] | None,
    kwargs: Mapping[str, Any],
    kind: str = "Query parameter",
    argument: str = "params",
    reserved: AbstractSet[str] = _QUERY_OPTIONS,
) -> dict[str, Any]:
    """Return the parameters given as a mapping and as keywords.

    Args:
        params: Parameters passed as a mapping, whose names may be anything,
            including the names that the methods reserve.
        kwargs: Parameters passed as keyword arguments.
        kind: What a parameter is, for the error message.
        argument: Name of the mapping argument, for the error message.
        reserved: Names that a keyword argument may not have, although the
            method does not use them. The query methods reserve all of their
            options, those that a method takes being keyword arguments of its
            own.

    Returns:
        Every parameter, keyed by name.

    Raises:
        TypeError: If a name is given both ways, or a keyword argument has a
            reserved name.
    """
    clash = sorted(reserved & set(kwargs))
    if clash:
        raise TypeError(
            f"{clash[0]}= is reserved. Pass a {kind.lower()} named "
            f"{clash[0]} in {argument}={{{clash[0]!r}: ...}}"
        )
    if params is None:
        return dict(kwargs)
    both = sorted(set(params) & set(kwargs))
    if both:
        raise TypeError(
            f"{kind} {both[0]} is given both as a keyword argument and in "
            f"{argument}"
        )
    return {**params, **kwargs}


# Request arguments that a query may set. A query sends no body, builds its
# parameters itself, and reads every response.
_QUERY_OPTION_NAMES = frozenset(
    {
        "allow_redirects",
        "auth",
        "cert",
        "cookies",
        "headers",
        "hooks",
        "proxies",
        "timeout",
        "verify",
    }
)


def _check_options_mapping(options: Mapping[str, Any]) -> None:
    """Refuse request options that are not a mapping.

    Args:
        options: Value given as ``request_options``.

    Raises:
        TypeError: If ``options`` is not a mapping.
    """
    if not isinstance(options, Mapping):
        raise TypeError(
            f"request_options must be a mapping, got {type(options).__name__}"
        )


def _query_request_options(
    options: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return the request options of a query, checked.

    Args:
        options: Keyword arguments for the query's requests, such as
            ``headers``, as ``requests.Session.request()`` takes them.

    Returns:
        A copy of ``options`` without ``stream=False``, or an empty dict.

    Raises:
        TypeError: If ``options`` is not a mapping, sets a body or the query
            parameters, enables streaming, or names an argument that is not
            a request option.
    """
    if options is None:
        return {}
    _check_options_mapping(options)
    body = sorted(_WRITE_BODY_ARGUMENTS.intersection(options))
    if body:
        raise TypeError(
            f"request_options cannot set {', '.join(body)}, because a query "
            "sends no body"
        )
    if "params" in options:
        raise TypeError(
            "request_options cannot set params. Pass query parameters as "
            "keyword arguments or in params."
        )
    if options.get("stream"):
        raise TypeError(
            "Queries read their responses, so request_options cannot enable "
            "stream"
        )
    unknown = sorted(set(options) - _QUERY_OPTION_NAMES - {"stream"})
    if unknown:
        raise TypeError(
            f"request_options cannot set {', '.join(unknown)}, which are not "
            "request options"
        )
    return {name: value for name, value in options.items() if name != "stream"}


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
        TypeError: If ``options`` is not a mapping, sets the request body,
            which the write builds itself, enables streaming of the response,
            or names an argument that is not a request option.
    """
    if options is None:
        return {}
    _check_options_mapping(options)
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
    unknown = sorted(set(options) - _QUERY_OPTION_NAMES - {"params", "stream"})
    if unknown:
        raise TypeError(
            f"request_options cannot set {', '.join(unknown)}, which are not "
            "request options"
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


def _checked_hyperlink(api: _AnyAPI, url: str) -> str:
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
    allowed = _permitted_origins(api.base_url, api.redirect_origins)
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


_API_HOOKS = (
    "bulk_get_by_pks",
    "bulk_get_by_urls",
    "collection_page",
    "collection_items_from_response",
    "collection_items",
)
"""The hooks that take ``api``, and the keywords that later releases add."""

_KWARGS_HOOKS = ("merge_response", "object_json")
"""The other hooks, which take the keywords that later releases add."""


def _check_hook_signatures(cls: type[JSONObject]) -> None:
    """Check that the hooks a class overrides accept what eazyrest passes.

    eazyrest passes the hooks that take ``api`` the client as ``api=``, and
    later 1.x releases may pass more keyword arguments to every hook, so an
    override must have a ``**kwargs`` parameter. Checking when the class is
    defined, also for a mixin that is never decorated, reports an override
    without one at import, rather than when a query first calls it.

    Args:
        cls: Class being defined.

    Raises:
        TypeError: If the class overrides one of these hooks with a method
            that takes no ``**kwargs``.
    """
    for name in (*_API_HOOKS, *_KWARGS_HOOKS):
        if not cls._overrides_hook(name):
            continue
        owner = next(base for base in cls.__mro__ if name in vars(base))
        if not _takes_kwargs(vars(owner)[name]):
            takes = "api= and **kwargs" if name in _API_HOOKS else "**kwargs"
            raise TypeError(
                f"{owner.__name__}.{name}() must accept {takes}, which "
                "eazyrest passes to it, and pass them on when it calls "
                "another hook"
            )


def _check_removed_methods(cls: type[JSONObject]) -> None:
    """Refuse an override of a conversion method that 1.0 removed.

    ``from_json(self, value, conversion)`` and ``to_json(cls, value,
    conversion)`` were the alphas' override points, and ``from_json(self,
    value, ty)`` and ``to_json(self, value, ty)`` were 0.9.0's. eazyrest no
    longer calls any of them, so an override would be skipped without a word. A
    method of either name that takes neither a ``conversion`` nor a ``ty``
    parameter is the class's own.

    Args:
        cls: Class being defined.

    Raises:
        TypeError: If the class defines one of the methods with a
            ``conversion`` or ``ty`` parameter.
    """
    for name in ("from_json", "to_json"):
        # A mixin's method is found as the class's own would be.
        owner = next(
            (base for base in cls.__mro__ if name in vars(base)), None
        )
        method = None if owner is None else vars(owner)[name]
        if isinstance(method, (classmethod, staticmethod)):
            method = method.__func__
        if not callable(method):
            continue
        try:
            parameters = inspect.signature(method).parameters
        except (TypeError, ValueError):
            continue
        if "conversion" in parameters or "ty" in parameters:
            assert owner is not None
            raise TypeError(
                f"{owner.__name__}.{name}() is no longer called. Declare the "
                "conversions of the class's types in its conversions, as the "
                "guide's Custom field conversion section shows."
            )


def _pk_property_of(cls: type[JSONObject]) -> JSONProperty | None:
    """Return the property of a model's primary key.

    Args:
        cls: Model class.

    Returns:
        The property, or ``None`` for a model without a key field.
    """
    return getattr(cls, "_pk", None)


def _missing_field_error(
    obj: JSONObject, field: str, json_field: str
) -> AttributeError:
    """Return the error for a field that an object's loaded JSON lacks.

    Args:
        obj: Object that was read.
        field: Python attribute name.
        json_field: JSON key that the object's JSON lacks.

    Returns:
        The ``AttributeError`` to raise.
    """
    return AttributeError(
        f"'{type(obj).__name__}' object has no attribute '{field}': "
        f"JSON field '{json_field}' is missing"
    )


def _refuse_keyless_assignment(cls: type[JSONObject]) -> None:
    """Refuse a field value for an object of a model without a key.

    Args:
        cls: Model class.

    Raises:
        UsageError: If ``cls`` has no primary-key field. Such an object is a
            value read from JSON, which nothing can address to write.
    """
    if _pk_property_of(cls) is None:
        raise UsageError(
            f"{cls.__name__} has no primary-key field, so it is a read-only "
            "value. Build it with json=."
        )


def _refuse_keyless_request(cls: type[JSONObject], verb: str) -> None:
    """Refuse a request that needs an object of a model without a key.

    Args:
        cls: Model class.
        verb: What the request would do, such as ``"save"``.

    Raises:
        UsageError: If ``cls`` has no primary-key field, so its objects have
            nothing to address.
    """
    if _pk_property_of(cls) is None:
        raise UsageError(
            f"{cls.__name__} has no primary-key field, so it has nothing to "
            f"{verb}."
        )


def _holds_hyperlinked_set(analyzed: AnalyzedType) -> bool:
    """Return whether a type is a set of ``Hyperlinked()`` relations.

    The walk goes through optional wrappers and the items of collections, so a
    set inside a list is found, and so is a set of optional relations.

    Args:
        analyzed: Analyzed type of a field.

    Returns:
        Whether a set type, as ``set``, ``frozenset``, ``AbstractSet``, or
        ``MutableSet``, has hyperlinked related objects as its items.
    """
    analyzed = _unwrap_optional_type(analyzed)
    if not isinstance(analyzed, CollectionType):
        return False
    item = _unwrap_optional_type(analyzed.item_type)
    if _READ_AS[analyzed.collection_type] in (set, frozenset):
        return isinstance(item, RelatedType) and item.hyperlinked
    return _holds_hyperlinked_set(item)


def _check_not_hyperlinked_set(
    cls: type[JSONObject], field: str, analyzed: AnalyzedType
) -> None:
    """Refuse a field that is a set of ``Hyperlinked()`` relations.

    An object known only by its URL has no hash until it loads, and a set needs
    one for each member, so such a set could not be read.

    Args:
        cls: Model class that declares the field.
        field: Name of the field.
        analyzed: Analyzed type of the field.

    Raises:
        TypeError: If ``analyzed`` is a set of ``Hyperlinked()`` relations.
    """
    if _holds_hyperlinked_set(analyzed):
        error = TypeError(
            f"{cls.__name__}.{field} is a set of hyperlinked relations, and "
            "an object that is known only by its URL has no hash, so it "
            "cannot be in a set. Declare a list or a tuple instead."
        )
        # A conversion reports the error as a plain TypeError, as decorating
        # the class does, not as the conversion of a value.
        setattr(error, _DECLARATION_ERROR, True)
        raise error


def _check_key_not_hyperlinked(
    cls: type[JSONObject], field: str, analyzed: AnalyzedType
) -> None:
    """Refuse a primary key whose type is a hyperlinked relation.

    A key that is a hyperlinked relation reads as the related object's own key,
    which is no URL, so the model's own URL could not be built from it.

    Args:
        cls: Model class that declares the key.
        field: Name of the primary-key field.
        analyzed: Analyzed type of the field.

    Raises:
        TypeError: If ``analyzed`` is a ``Hyperlinked()`` relation.
    """
    related = _related_key_type(analyzed)
    if related is not None and related.hyperlinked:
        error = TypeError(
            f"{cls.__name__}.{field} is the primary key, and a primary key "
            "cannot be a hyperlinked relation. Declare it as a relation to "
            "the model without Hyperlinked()."
        )
        # A conversion reports the error as a plain TypeError, as decorating
        # the class does, not as the conversion of a value.
        setattr(error, _DECLARATION_ERROR, True)
        raise error


def _key_type_error(cls: type[JSONObject], field: str) -> TypeError:
    """Return the refusal of a primary key that is not a leaf or a relation.

    Args:
        cls: Model class that declares the key.
        field: Name of the primary-key field.

    Returns:
        A ``TypeError`` that a conversion reports as a plain one, as decorating
        the class does, not as the conversion of a value.
    """
    error = TypeError(
        f"{cls.__name__}.{field} is the primary key, and a primary key must "
        "be one value, such as an int, a str, a UUID, a datetime, a Decimal, "
        "an enum, or a class that a conversion of the model converts, or a "
        "relation without Hyperlinked() to a model whose key is one. It "
        "cannot be a float, a bool, a collection, a mapping, a tuple, a "
        "TypedDict, a NamedTuple, a dataclass, a union, or a relation to a "
        "model keyed by a relation. To key the model by another model, "
        "declare the key as a relation to that model, or as the type of that "
        "model's key."
    )
    setattr(error, _DECLARATION_ERROR, True)
    return error


def _refuses_key_type(error: TypeError) -> bool:
    """Return whether a ``TypeError`` from analyzing a key refuses its type.

    A name that is not defined yet is no refusal. It waits for the key's first
    use. A refusal that already says why, such as a model without a key, keeps
    its own message.

    Args:
        error: Error that ``analyze_type()`` raised for a key's type.

    Returns:
        Whether the key's type cannot convert, as opposed to one that cannot
        resolve yet or one that has its own error.
    """
    return not (
        isinstance(
            error,
            _KeylessFieldTypeError
            | _UnresolvedAliasError
            | _PydanticMetadataError,
        )
        or getattr(error, _DECLARATION_ERROR, False)
    )


def _is_plain_key_class(base: object, table: Mapping[Any, Conversion]) -> bool:
    """Return whether a type that converts as itself is a key type.

    Args:
        base: The type of a ``BaseType``, which may be a ``Literal`` or a
            ``NewType``.
        table: Conversion table of the model that declares the key.

    Returns:
        ``True`` for ``int``, ``str``, a subclass of one that is not a
        ``bool``, and a ``Literal`` of strings and integers. Any other class
        reads the server's JSON as it is unless a conversion converts it. That
        JSON may be an array or a mapping, or cannot be written as JSON, so the
        key would have more than one spelling or none. A ``float`` and a
        ``bool`` are no keys, because a number has more than one spelling and a
        ``bool`` is not an integer.
    """
    while hasattr(base, "__supertype__"):
        base = base.__supertype__
    if typing.get_origin(base) is Literal:
        return all(
            _json_kind(_literal_json(member, table)) is not None
            for member in typing.get_args(base)
        )
    return (
        isinstance(base, type)
        and issubclass(base, int | str)
        and not issubclass(base, bool)
    )


def _enum_values_are_keys(found: _Found) -> bool:
    """Return whether an enum that converts by value has keys for members.

    Args:
        found: The conversion of a key's type, as ``_conversion_of()`` found
            it.

    Returns:
        ``False`` for an enum that the default enum conversion owns, if the
        value of any member is not a string, an integer that is not a ``bool``,
        or ``None``. Every other conversion decides its own wire type, and a
        conversion that is not for an enum is not examined.
    """
    if found.key is not enum.Enum:
        return True
    values = (
        found.entry.encode(member, found.declared) for member in found.declared
    )
    # A member that writes null names no key, which has no URL.
    return all(
        value is None or _json_kind(value) is not None for value in values
    )


def _is_leaf_key(
    analyzed: AnalyzedType, table: Mapping[Any, Conversion]
) -> bool:
    """Return whether a primary key's type converts as one value.

    A leaf is a type that an entry of the conversion table converts, or a type
    that converts as itself, looking through ``T | None`` and ``NewType``. A
    class that holds many values, such as ``list``, and ``Any`` are not leaves,
    since their JSON can be a mapping or an array, whose spelling is not
    unique.

    Args:
        analyzed: Analyzed type of a primary key field.
        table: Conversion table of the model that declares the key.

    Returns:
        Whether the key's type is a leaf.
    """
    while _class_entry(analyzed, table) is None:
        match analyzed:
            case OptionalType(inner) | NewTypeType(_, inner):
                analyzed = inner
            case BaseType(base):
                found = _conversion_of(base, table)
                if found is not None:
                    return _enum_values_are_keys(found)
                _refuse_pydantic_class(base)
                return _is_plain_key_class(base, table)
            case _:
                return False
    return True


def _check_key_is_leaf_or_relation(
    cls: type[JSONObject],
    field: str,
    analyzed: AnalyzedType,
    *,
    strict: bool = False,
) -> None:
    """Refuse a primary key that is neither a leaf nor a relation to one.

    A key is a leaf, or a relation that is not ``Hyperlinked()`` to a model
    whose own key is a leaf. A chain of relations is refused, and so is a
    cycle, which is a chain that never ends.

    Args:
        cls: Model class that declares the key.
        field: Name of the primary-key field.
        analyzed: Analyzed type of the field.
        strict: Whether a name that is not defined in the related model's key
            raises, as it does when the key is first used. Otherwise the check
            waits for that use.

    Raises:
        TypeError: If the key is neither.
        Exception: If ``strict`` and the related model's key type cannot be
            resolved, whatever resolving it raises.
    """
    if _is_leaf_key(analyzed, _table_of(cls)):
        return
    related = _related_key_type(analyzed)
    if related is not None:
        try:
            inner = analyze_type(related.model_type._pk._resolve_type())
        except _ModelInContainerError:
            raise _key_type_error(cls, field) from None
        except Exception:
            if strict:
                raise
            return
        if _is_leaf_key(inner, _table_of(related.model_type)):
            return
    raise _key_type_error(cls, field)


def _analyze_field(
    prop: JSONProperty, resolved: Any, *, strict: bool
) -> AnalyzedType:
    """Analyze and check the resolved type of a field.

    Args:
        prop: The field.
        resolved: The field's annotation, resolved.
        strict: Whether a name that is not defined in a related model's key
            raises, as it does when the field is first used. Otherwise the key
            check waits for that use.

    Returns:
        The field's analyzed type.

    Raises:
        TypeError: If the annotation is ``None`` as a whole, or the type is
            refused, such as a model without a key, a primary key that is not
            one value or a relation to a model keyed by one, a
            ``Hyperlinked()`` primary key, or a set of ``Hyperlinked()``
            relations.
        Exception: If a name in a type alias or a related model's key type is
            not defined yet, whatever evaluating it raises.
    """
    if resolved is None or resolved is type(None):
        raise TypeError(
            "its annotation is None, so eazyrest cannot convert it. A field "
            "whose name is the name of its type, as in date: date = None, "
            "shadows the type in the class body, where the default has "
            "already replaced it. Rename the field with field_map=, or "
            "annotate the type by a name that is not shadowed, such as "
            "datetime.date."
        )
    try:
        analyzed = analyze_type(resolved)
    except TypeError as err:
        if prop.is_primary_key and _refuses_key_type(err):
            raise _key_type_error(prop.cls, prop.field) from err
        raise
    if prop.is_primary_key:
        _check_key_not_hyperlinked(prop.cls, prop.field, analyzed)
        _check_key_is_leaf_or_relation(
            prop.cls, prop.field, analyzed, strict=strict
        )
    _check_not_hyperlinked_set(prop.cls, prop.field, analyzed)
    return analyzed


def _field_type(prop: JSONProperty, *, strict: bool) -> AnalyzedType:
    """Resolve, analyze, and check a field's type: the one compilation path.

    Args:
        prop: The field.
        strict: Whether a name that is not defined in a related model's key
            raises, as it does when the field is first used. Otherwise the key
            check waits for that use.

    Returns:
        The field's analyzed type.

    Raises:
        TypeError: If the type is refused, as ``_analyze_field()`` describes.
        Exception: If the annotation cannot be resolved, as for a class that is
            not defined yet, whatever resolving it raises.
    """
    return _analyze_field(prop, prop._resolve_type(), strict=strict)


def _refuse_declared_type(
    cls: type[JSONObject], prop: JSONProperty, *, inherited: bool = False
) -> None:
    """Refuse one field whose own type eazyrest does not convert.

    The kinds of error are told apart by where they arise and their type, never
    by their message. Whatever resolving the annotation raises waits for the
    field's first use, and so does a type alias that names something not
    defined yet. Any other ``TypeError`` from analyzing the resolved annotation
    is a refusal. So is a union in the field's own type with a member that the
    model's conversion table converts, which reading the field would refuse.

    Args:
        cls: Decorated model class.
        prop: The field.
        inherited: Whether a base class declares the field. Only its unions are
            checked, against ``cls``'s table, since the base has checked the
            rest.

    Raises:
        TypeError: If the field's type is refused. Its message is
            ``Model.field: `` and the reason, which is its cause. A refusal
            already marked as a declaration error, and a keyless model, are
            raised as they are.
    """
    resolving = True
    try:
        resolved = prop._resolve_type()
        resolving = False
        if inherited:
            analyzed = analyze_type(resolved)
        else:
            analyzed = _analyze_field(prop, resolved, strict=False)
        _check_field_unions(analyzed, _table_of(cls))
        _check_admitted(analyzed, _table_of(cls))
    except Exception as err:
        if (
            resolving
            or not isinstance(err, TypeError)
            or isinstance(err, _UnresolvedAliasError)
        ):
            return
        if isinstance(err, _KeylessFieldTypeError) or getattr(
            err, _DECLARATION_ERROR, False
        ):
            raise
        refusal = TypeError(f"{cls.__name__}.{prop.field}: {err}")
        setattr(refusal, _DECLARATION_ERROR, True)
        raise refusal from err


def _refuse_declared_types(
    cls: type[JSONObject], declared: Iterable[JSONProperty]
) -> None:
    """Refuse at decoration a field whose own type eazyrest does not convert.

    Each declared field and the primary key are resolved and analyzed once. A
    field whose annotation cannot be resolved yet, or whose type alias names a
    class that is not defined yet, waits for the field's first use. So does a
    member of a ``TypedDict``, a dataclass, or a ``NamedTuple``, which is
    analyzed when it is first needed. Any other ``TypeError`` from analyzing
    the resolved type is a refusal, whose message names the field and whose
    cause is the reason. A field that a base declares is checked again for a
    union, when the class's conversions differ from the base's.

    Args:
        cls: Decorated model class.
        declared: The fields that the class declares.

    Raises:
        TypeError: If a field's type is refused, such as ``Counter[str]``, a
            union of a scalar and a list, a mapping whose keys are not strings,
            a model without a primary-key field, a set of ``Hyperlinked()``
            relations, or a primary key that is a ``Hyperlinked()`` relation,
            is not one value, or is neither a leaf nor a relation to a model
            keyed by one. The first field in declaration order raises, and then
            the primary key.
    """
    props = [*declared]
    pk = _pk_property_of(cls)
    if pk is not None and pk not in props:
        props.append(pk)
    for prop in props:
        _refuse_declared_type(cls, prop)
    for prop in _inherited_fields_with_new_table(cls, props):
        _refuse_declared_type(cls, prop, inherited=True)
        bound = prop._owned_by(cls)
        setattr(cls, prop.field, bound)
        if _pk_property_of(cls) is prop:
            cls._pk = bound


def _inherited_fields_with_new_table(
    cls: type[JSONObject], checked: Iterable[JSONProperty]
) -> list[JSONProperty]:
    """Return the inherited fields that a class's conversions may change.

    A union is refused when a member has a conversion, so a subclass whose
    table differs from its base's can make an inherited field's union one that
    eazyrest cannot convert.

    Args:
        cls: Decorated model class.
        checked: The fields that were checked as the class's own.

    Returns:
        The fields that a base declares, in name order, whose owner has a
        conversion table that differs from ``cls``'s.
    """
    table = _table_of(cls)
    own = {prop.field for prop in checked}
    props = []
    for name in sorted(cls._json_fields - own):
        prop = inspect.getattr_static(cls, name, None)
        if isinstance(prop, JSONProperty) and _table_of(prop._owner) != table:
            props.append(prop)
    return props


def _check_api_attribute(
    cls: type[JSONObject], error: type[Exception]
) -> None:
    """Refuse an API assigned to a model's ``api`` attribute.

    The attribute resolves each object's own API, from ``api=`` or the
    registry, so a value assigned to it on a class replaces that, and every
    ``api=`` is ignored. The message names ``register_api()`` on the class that
    owns the attribute when that class is a model, and otherwise on ``cls``,
    since a plain mixin has no ``register_api()``.

    Args:
        cls: Model class.
        error: Exception type to raise.

    Raises:
        Exception: Of type ``error``, if the first class in the MRO of ``cls``
            that has its own ``api`` has one other than eazyrest's.
    """
    for base in cls.__mro__:
        # Test for presence, since ``None`` is an assignment too. The first
        # class that has an ``api`` is the one that ``cls.api`` reads, so a
        # later one is never read and cannot shadow eazyrest's.
        if "api" not in (own := vars(base)):
            continue
        if isinstance(own["api"], APIDescriptor):
            return
        assigned = (
            f"{base.__name__}.api was assigned, which would make every "
            "api= ignored."
        )
        if issubclass(base, JSONObject):
            raise error(
                f"{assigned} Use {base.__name__}.register_api(api) instead."
            )
        raise error(
            f"{assigned} Remove it, and use {cls.__name__}."
            "register_api(api) instead."
        )


def encode_json(
    value: Any, annotation: Any, *, model: type[JSONObject] | None = None
) -> Any:
    """Return the JSON value that a field annotated ``annotation`` writes.

    Use it to encode a request body or a query parameter as fields encode
    their values, for example
    ``api.post(url, json=encode_json(body, Extension, model=Machine))`` with
    a ``TypedDict`` ``Extension`` that describes the body. The request
    methods send ``json=`` as it is given, so the body must already be JSON.

    Args:
        value: Python value to encode.
        annotation: Type that describes ``value``, as a field annotation
            would, such as a ``TypedDict`` or ``list[datetime.datetime]``.
        model: Model whose conversion table to encode by, so that the value
            is written as that model's fields write it. Without it, the
            default table.

    Returns:
        The JSON value, made of dictionaries with string keys, lists,
        strings, numbers, booleans, and ``None``.

    Raises:
        TypeError: If ``model`` is not a ``JSONObject`` subclass, if
            ``annotation`` cannot be converted, as for a field, or if
            ``value`` holds a value that does not encode to JSON, such as a
            model under a key annotated ``int``. The message names the
            declared key, field, or position where the value is, shows a
            mapping's own keys as ``[key]``, and does not quote the value.
        ValueError: If a value cannot be encoded, as for a field, or is a
            float that is not finite, which JSON cannot write.
    """
    if model is None:
        model = JSONObject
    elif not lenient_issubclass(model, JSONObject):
        raise TypeError(
            f"encode_json(model=) must be a JSONObject subclass, got {model!r}"
        )
    analyzed = analyze_type(annotation)
    _check_admitted(analyzed, _table_of(model), late=True)
    encoded = model._to_json(value, analyzed)
    _check_json(encoded, "")
    return encoded


def _check_json(value: Any, path: str) -> None:
    """Check that a value is made only of JSON types.

    Args:
        value: Value to check.
        path: Where ``value`` is in the encoded value, for the message, such
            as ``[key][0]``. A key that this check reaches was chosen by the
            input or an encoder, not declared, and can be a secret, so it is
            shown as ``[key]``.

    Raises:
        TypeError: If ``value`` holds a value of another type, or a mapping
            key that is not a string.
        ValueError: If ``value`` holds a float that is not finite, which
            JSON cannot write.
    """
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(
                f"The value{_at(path)} is a float that is not finite, which "
                "JSON cannot write"
            )
        return
    if value is None or isinstance(value, (str, int)):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_json(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(
                    f"The value{_at(path)} has a key that is not a string, "
                    "which JSON objects cannot have"
                )
            _check_json(item, f"{path}[key]")
        return
    raise TypeError(
        f"The value{_at(path)} has the type {type(value).__name__}, which "
        "does not encode to JSON. Annotate it with a type that eazyrest "
        "converts."
    )


def _literal_json(member: Any, table: Mapping[Any, Conversion]) -> Any:
    """Return the JSON value of a ``Literal``'s member.

    Args:
        member: The member, such as ``"open"`` or ``Status.OPEN``.
        table: Conversion table of the model whose field it is.

    Returns:
        An enum member encoded by its conversion, or any other member as it
        is.
    """
    if isinstance(member, enum.Enum):
        found = _conversion_of(type(member), table)
        if found is not None:
            return found.entry.encode(member, found.declared)
    return member


def _literal_member(
    ty: Any, value: Any, table: Mapping[Any, Conversion]
) -> Any:
    """Read a JSON value as the member of a ``Literal`` that it encodes.

    Args:
        ty: The ``Literal`` type.
        value: JSON value read.
        table: Conversion table of the model whose field it is.

    Returns:
        The enum member whose JSON value is ``value``, or else a copy of
        ``value``, which a ``Literal`` does not check.
    """
    for member in typing.get_args(ty):
        if isinstance(member, enum.Enum):
            encoded = _literal_json(member, table)
            # A JSON true is not the member whose value is 1.
            if type(encoded) is type(value) and encoded == value:
                return member
    return _copy_json(value)


def _checked_json(value: Any) -> Any:
    """Return a value after checking that it is made only of JSON types.

    Args:
        value: Encoded value.

    Returns:
        ``value``.

    Raises:
        TypeError: If ``value`` holds a value of another type.
    """
    _check_json(value, "")
    return value


def _at(path: str) -> str:
    """Return where a value is, for a message.

    Args:
        path: Keys and indexes, such as ``['window'][0]``, or empty for the
            whole value.

    Returns:
        `` at `` and ``path``, or nothing for an empty path.
    """
    return f" at {path}" if path else ""


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
        TypeError: If the class overrides a hook that takes ``api``, such as
            ``collection_items()``, with a method that takes no ``**kwargs``.
            Also raised if a field's type is a model without a primary-key
            field, alone or inside an optional, a collection, or a ``NewType``.
            Declare nested JSON without a key as a ``TypedDict`` or a
            dataclass. A class defined later raises when the field is first
            used. Also raised if the primary key is a ``Hyperlinked()``
            relation, which cannot form the model's URL. A key whose type is
            not yet defined raises when the key is first used. Also raised if a
            field is a set of ``Hyperlinked()`` relations, since an object
            known only by its URL has no hash. Declare a list or a tuple.
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
            TypeError: If a field's type is a model without a primary-key
                field, or the primary key is a ``Hyperlinked()`` relation.
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
        declared: list[JSONProperty] = []
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
            declared.append(prop)

        # pylint: disable=protected-access
        cls._json_fields = frozenset(fields)
        _refuse_declared_types(cls, declared)

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


def _base_schema(
    ty: Any, mode: str, table: Mapping[Any, Conversion]
) -> dict[str, Any]:
    """Return the JSON schema of a leaf type.

    A type with a conversion has its conversion's schema. A JSON scalar has
    its JSON type, and a ``Literal`` its values.

    Args:
        ty: Declared type of a ``BaseType``.
        mode: ``"read"`` or ``"write"``, which a conversion's schema may
            depend on, as a ``Decimal``'s does.
        table: Conversion table of the model whose field it is.

    Returns:
        The schema of the JSON value that the type reads and writes, or an
        empty schema, which allows any value, for a type whose JSON form
        eazyrest does not check, such as ``Any``.
    """
    if typing.get_origin(ty) is Literal:
        return {
            "enum": [
                _literal_json(member, table) for member in typing.get_args(ty)
            ]
        }
    found = _conversion_of(ty, table)
    if found is not None:
        return _schema_of_entry(found.entry, found.declared, mode)
    ty = _final_supertype(ty)
    if not isinstance(ty, type):
        return {}
    # A bool is an int too, so it comes first.
    formats: list[tuple[type, dict[str, Any]]] = [
        (bool, {"type": "boolean"}),
        (int, {"type": "integer"}),
        (float, {"type": "number"}),
        (str, {"type": "string"}),
        (type(None), {"type": "null"}),
    ]
    for base, schema in formats:
        if issubclass(ty, base):
            return dict(schema)
    return {}


def _hoist_defs(schema: dict[str, Any], defs: dict[str, Any]) -> Any:
    """Move a schema's definitions into shared ones, renaming on collision.

    Args:
        schema: A conversion's JSON schema, which may hold ``$defs``.
        defs: Definitions shared by the model's schema, updated in place.

    Returns:
        ``schema`` without ``$defs``, with its references rewritten to the
        names under which its definitions were added.
    """
    own = dict(schema.pop("$defs", {}))
    renamed = {name: name for name in own}

    def rewrite(value: Any) -> Any:
        """Return ``value`` with its definition references renamed.

        Args:
            value: Part of a schema.

        Returns:
            The part, with each ``$ref`` to a renamed definition rewritten.
        """
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, item in value.items():
                if key in _SCHEMA_DATA:
                    result[key] = item
                elif key in _SCHEMA_MAPS and isinstance(item, dict):
                    result[key] = {
                        name: rewrite(schema) for name, schema in item.items()
                    }
                elif key == "$ref":
                    result[key] = reference(item)
                else:
                    result[key] = rewrite(item)
            return result
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        return value

    def reference(item: Any) -> Any:
        """Return a reference into a definition, renamed.

        Args:
            item: A reference, such as ``#/$defs/Cat`` or
                ``#/$defs/Cat/properties/name``, whose first token after
                ``$defs`` names the definition, escaped as a JSON Pointer
                in a URI fragment.

        Returns:
            The reference with the definition's new name, or ``item`` if it
            does not name one of the schema's definitions.
        """
        if not isinstance(item, str) or not item.startswith("#/$defs/"):
            return item
        token, slash, rest = item[8:].partition("/")
        name = unquote(token).replace("~1", "/").replace("~0", "~")
        if name not in renamed:
            return item
        # A new name only adds a suffix, which needs no escaping.
        return f"#/$defs/{token}{renamed[name][len(name) :]}{slash}{rest}"

    def fits(name: str) -> bool:
        """Return whether a definition can be added under its new name.

        Args:
            name: Name of one of the schema's own definitions.

        Returns:
            Whether no other definition takes the new name, or a shared one
            with the same content after renaming does.
        """
        new = renamed[name]
        if new in defs:
            return bool(defs[new] == rewrite(own[name]))
        return all(renamed[other] != new for other in own if other != name)

    # Renaming a definition changes the definitions that refer to it, which
    # may then collide in turn, so rename until every definition fits. Each
    # rename takes a higher suffix, so this ends.
    suffixes = dict.fromkeys(own, 1)
    changed = True
    while changed:
        changed = False
        for name in own:
            while not fits(name):
                suffixes[name] += 1
                renamed[name] = f"{name}_{suffixes[name]}"
                changed = True

    for name, definition in own.items():
        defs[renamed[name]] = rewrite(definition)
    return rewrite(schema)


# Keywords whose values map names to schemas, and keywords whose values are
# JSON data rather than schemas.
_SCHEMA_MAPS = frozenset(
    {"properties", "patternProperties", "dependentSchemas", "$defs"}
)
_SCHEMA_DATA = frozenset({"const", "enum", "default", "examples"})


def _hoist_nested_defs(value: Any, defs: dict[str, Any]) -> Any:
    """Move the definitions of schemas embedded in a schema into shared ones.

    A conversion's schema is a standalone schema, whose local references,
    such as ``#/$defs/Part``, name its own ``$defs``. Embedded in a model's
    schema, its definitions move into the model's ``$defs``, renamed on
    collision, and only its own references are rewritten.

    Args:
        value: Part of a schema, after the model's own definitions have been
            hoisted.
        defs: Definitions shared by the model's schema, updated in place.

    Returns:
        ``value`` with no ``$defs`` below it, and its references rewritten.
    """
    if isinstance(value, list):
        return [_hoist_nested_defs(item, defs) for item in value]
    if not isinstance(value, dict):
        return value
    if "$defs" in value:
        value = _hoist_defs(dict(value), defs)
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in _SCHEMA_DATA:
            result[key] = item
        elif key in _SCHEMA_MAPS and isinstance(item, dict):
            result[key] = {
                name: _hoist_nested_defs(schema, defs)
                for name, schema in item.items()
            }
        else:
            result[key] = _hoist_nested_defs(item, defs)
    return result


def _array_schema(items: list[Any]) -> dict[str, Any]:
    """Return the schema of an array whose items are fixed by position.

    Args:
        items: Each position's schema.

    Returns:
        The schema, in the JSON Schema 2020-12 vocabulary.
    """
    return {
        "type": "array",
        "prefixItems": items,
        "items": False,
        "minItems": len(items),
        "maxItems": len(items),
    }


def _json_schema_of(
    analyzed: AnalyzedType,
    mode: str,
    defs: dict[str, Any],
    seen: frozenset[type],
    table: Mapping[Any, Conversion],
) -> Any:
    """Return the JSON schema of a field's analyzed type.

    Args:
        analyzed: Analyzed type of the field.
        mode: ``"read"`` or ``"write"``.
        defs: Definitions shared by the model's schema, updated in place.
        seen: Models whose key schema is being built, and structures whose
            schema is, to stop a cycle of related primary keys or of a
            structure inside itself.
        table: Conversion table of the model whose field it is.

    Returns:
        The schema of the JSON value that the field reads or writes.

    Raises:
        TypeError: If a conversion fails.
    """
    found = _class_entry(analyzed, table)
    if found is not None:
        return _schema_of_entry(found.entry, found.declared, mode)
    match analyzed:
        case NewTypeType(_, inner):
            return _json_schema_of(inner, mode, defs, seen, table)
        case OptionalType(inner):
            return {
                "anyOf": [
                    _json_schema_of(inner, mode, defs, seen, table),
                    {"type": "null"},
                ]
            }
        case CollectionType(collection_type, item_type):
            schema: dict[str, Any] = {
                "type": "array",
                "items": _json_schema_of(item_type, mode, defs, seen, table),
            }
            if _READ_AS[collection_type] in (set, frozenset):
                schema["uniqueItems"] = True
            return schema
        case RelatedType(model_type, hyperlinked):
            if hyperlinked:
                key: Any = {"type": "string", "format": "uri"}
            else:
                pk = model_type._pk
                if model_type in seen:
                    key = {}
                else:
                    with _converting(model_type, pk.field):
                        key = _json_schema_of(
                            pk.analyzed_type,
                            "write",
                            defs,
                            seen | {model_type},
                            _table_of(model_type),
                        )
            # A read also accepts the related object embedded as JSON.
            if mode == "read":
                return {"anyOf": [key, {"type": "object"}]}
            return key
        case TypedDictType(typed_dict):
            if typed_dict in seen:
                # A TypedDict inside itself allows any object there.
                return {"type": "object"}
            return {
                "type": "object",
                "properties": {
                    key: _json_schema_of(
                        key_type, mode, defs, seen | {typed_dict}, table
                    )
                    for key, key_type in analyzed.keys.items()
                },
                "additionalProperties": True,
            }
        case MappingType(value_type):
            return {
                "type": "object",
                "additionalProperties": _json_schema_of(
                    value_type, mode, defs, seen, table
                ),
            }
        case FixedTupleType(item_types):
            return _array_schema(
                [
                    _json_schema_of(item_type, mode, defs, seen, table)
                    for item_type in item_types
                ]
            )
        case NamedTupleType(named_tuple):
            if named_tuple in seen:
                # A NamedTuple inside itself allows any array there.
                return {"type": "array"}
            return _array_schema(
                [
                    _json_schema_of(
                        item_type, mode, defs, seen | {named_tuple}, table
                    )
                    for _, item_type in analyzed.fields
                ]
            )
        case DataclassType(dataclass_type):
            if dataclass_type in seen:
                # A dataclass inside itself allows any object there.
                return {"type": "object"}
            fields = analyzed.fields
            inside = seen | {dataclass_type}
            schema = {
                "type": "object",
                "properties": {
                    name: _json_schema_of(item_type, mode, defs, inside, table)
                    for name, item_type, _ in fields
                },
                "additionalProperties": False,
            }
            required = [name for name, _, needed in fields if needed]
            if required:
                schema["required"] = required
            return schema
        case UnionType(members):
            return {
                "anyOf": [
                    _base_schema(member.base_type, mode, table)
                    for member in members
                ]
            }
        case BaseType(base_type):
            return _base_schema(base_type, mode, table)
    raise TypeError(f"Unsupported analyzed type {type(analyzed).__name__}")


@contextlib.contextmanager
def _at_key(where: str) -> Generator[None, None, None]:
    """Name the key of a ``TypedDict`` or mapping in a conversion error.

    Args:
        where: The key, such as ``['start']``, or ``[key]`` for a key that the
            input chose, which the message does not quote.

    Yields:
        Nothing. A ``TypeError`` or ``ValueError`` raised inside the block is
        raised again with ``where`` before its message, but for a refusal of a
        model's declaration and an ``EazyrestError`` other than
        ``FieldConversionError``, which are raised as they are.

    Raises:
        TypeError: If the block raises a ``TypeError``.
        ValueError: If the block raises a ``ValueError`` that is not a
            ``TypeError``.
    """
    try:
        yield
    except TypeError as err:
        if _passes_through(err):
            raise
        raise TypeError(f"{where}: {err}") from err
    except ValueError as err:
        if _passes_through(err):
            raise
        raise ValueError(f"{where}: {err}") from err


def _each_item(
    convert: Callable[[Any], Any], items: Iterable[Any]
) -> list[Any]:
    """Convert each item of a collection, naming its position in an error.

    One ``try`` covers the loop, so a long collection pays for naming the
    position only when an item fails.

    Args:
        convert: Function that converts one item.
        items: The items.

    Returns:
        The converted items, in order.

    Raises:
        TypeError: If converting an item raises a ``TypeError``. A refusal of a
            model's declaration is raised as it is, without the position.
        ValueError: If converting an item raises a ``ValueError`` that is
            not a ``TypeError``.
    """
    converted: list[Any] = []
    index = 0
    try:
        # The except clauses read index.
        for index, item in enumerate(items):  # noqa: B007
            converted.append(convert(item))
    except TypeError as err:
        if _passes_through(err):
            raise
        raise TypeError(f"[{index}]: {err}") from err
    except ValueError as err:
        if _passes_through(err):
            raise
        raise ValueError(f"[{index}]: {err}") from err
    return converted


def _json_order(value: Any) -> tuple[int, Any]:
    """Return a key that orders JSON values, for writing a set's items.

    Every pair of JSON values compares. Numbers compare by value, and strings
    by code point, which orders ISO 8601 dates by date. Arrays and objects
    compare by their JSON text, with sorted keys.

    Args:
        value: JSON value.

    Returns:
        The kind of value, ranked, and a comparable form of it.
    """
    if value is None:
        return (0, 0)
    if isinstance(value, bool):
        return (1, value)
    if isinstance(value, (int, float)):
        return (2, value)
    if isinstance(value, str):
        return (3, value)
    return (
        4,
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ),
    )


def _fixed_items(value: Any, length: int, direction: str) -> list[Any]:
    """Return the items of a fixed-length array, checking its length.

    Args:
        value: JSON array read, or tuple written.
        length: The length that the annotation requires.
        direction: ``"read"`` or ``"write"``, for the message.

    Returns:
        The items.

    Raises:
        TypeError: If ``value`` is not an array, or a sequence to write.
        ValueError: If it has another length.
    """
    if not isinstance(value, (list, tuple)):
        what = "JSON arrays" if direction == "read" else "tuples or lists"
        raise TypeError(
            f"Fixed-length values must be {what}, got {type(value).__name__}"
        )
    if len(value) != length:
        raise ValueError(
            f"Fixed-length values must have {length} items, got {len(value)}"
        )
    return list(value)


def _class_key(analyzed: AnalyzedType) -> Any:
    """Return the class whose conversion would replace a type's structure.

    Args:
        analyzed: Analyzed type.

    Returns:
        The dataclass, ``NamedTuple``, or ``TypedDict``, or the ``NewType`` of
        one, which a conversion table entry may convert instead. ``None`` for
        any other type.
    """
    match analyzed:
        case DataclassType(key) | NamedTupleType(key) | TypedDictType(key):
            return key
        case NewTypeType(key, _):
            return key
    return None


def _class_entry(
    analyzed: AnalyzedType, table: Mapping[Any, Conversion]
) -> _Found | None:
    """Return the table entry that converts a class instead of its structure.

    Args:
        analyzed: Analyzed type.
        table: Conversion table of the model whose field it is.

    Returns:
        The conversion found for the type's class, if it has one.
    """
    key = _class_key(analyzed)
    if key is None:
        return None
    return _conversion_of(key, table)


def _encodes(value: Any, found: _Found) -> bool:
    """Return whether a conversion encodes a value.

    Args:
        value: Value to write.
        found: The conversion, as ``_conversion_of()`` found it.

    Returns:
        Whether ``value`` is an instance of the type that the entry is keyed
        by. A ``TypedDict`` has no instances, so an entry keyed by one, or
        by a ``NewType`` of one, encodes any mapping.
    """
    if _is_typed_dict(_final_supertype(found.key)):
        return isinstance(value, Mapping)
    return _is_instance(value, found.key)


def _check_mapping(
    value: Any, conversion: TypedDictType | MappingType
) -> None:
    """Check that a value can be a ``TypedDict`` or mapping field's value.

    Args:
        value: JSON value read, or Python value written.
        conversion: The field's ``TypedDict`` or mapping type.

    Raises:
        TypeError: If ``value`` is not a mapping, or has a key that is not a
            string.
    """
    if not isinstance(value, Mapping):
        raise TypeError(
            f"{_mapping_name(conversion)} values must be mappings, "
            f"got {type(value).__name__}"
        )
    if not all(isinstance(key, str) for key in value):
        raise TypeError(
            f"{_mapping_name(conversion)} keys must be strings, which JSON "
            "object keys are"
        )


def _mapping_name(conversion: TypedDictType | MappingType) -> str:
    """Return the name of a ``TypedDict`` or mapping type, for messages.

    Args:
        conversion: The type.

    Returns:
        The ``TypedDict``'s name, or ``Mapping``.
    """
    if isinstance(conversion, TypedDictType):
        return conversion.typed_dict.__name__
    return "Mapping"


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
        self._owner = cls
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

    def _same_as_stored(self, stored: Any, new: Any) -> bool:
        """Return whether a newly encoded value matches the stored JSON.

        The server may list a set's items in any order, while an encoded set
        lists them in one fixed order, so a set field compares without regard
        to order. Only the field's own set is compared so, whether the field is
        optional or a ``NewType`` of a set. A set nested in another collection
        or a mapping compares in order.

        Args:
            stored: JSON value that the object holds for this field.
            new: Encoded value that is being assigned.

        Returns:
            Whether the values are equal, as ``_json_equal()`` compares them,
            after sorting a stored set as an encoded one is sorted.
        """
        conversion = self.analyzed_type
        while True:
            if isinstance(conversion, OptionalType):
                conversion = conversion.inner_type
            elif isinstance(conversion, NewTypeType):
                # A NewType of a set is analyzed as one only if the set is
                # parameterized, and no conversion can be registered for such a
                # NewType, so the field's own structure is always its set.
                conversion = conversion.inner
            else:
                break
        if (
            isinstance(conversion, CollectionType)
            and _READ_AS[conversion.collection_type] in (set, frozenset)
            and isinstance(stored, list)
        ):
            stored = sorted(stored, key=_json_order)
        return _json_equal(stored, new)

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
        # A model without a key field has none.
        return getattr(type(obj), "_pk", None) is self

    def _resolve_type(self) -> Any:
        """Resolve the field type without caching it.

        Returns:
            Resolved Python type for the owning class attribute.

        Raises:
            Exception: If the annotation names something undefined, as a
                reference to a class defined later does until it is.
        """
        # json_object() put the fields in the class namespace as descriptors.
        # In a plain class a field without a default is absent from it, so a
        # string annotation such as "date" in ``date: date`` names the module's
        # date, not the field.
        hidden = {
            name
            for name, value in vars(self.cls).items()
            if isinstance(value, JSONProperty)
        }
        return _resolve_annotation(self.cls, self.field, hidden)

    @cached_property
    def analyzed_type(self) -> AnalyzedType:
        """Resolve and cache recursive type metadata.

        Raises:
            TypeError: If the type is refused, such as a primary key that is
                not one value or a relation to a model keyed by one, or is a
                ``Hyperlinked()`` relation, or a set of them. A type that names
                a class defined later is refused here, when it is first used.
            NameError: If a field's annotation names a class that is not
                defined. It is raised as it is, for a primary key too.
            Exception: If a primary key's type, or the key type of a model that
                it relates to, holds a name that cannot be resolved yet,
                whatever resolving it raises. It is cached only once it
                resolves.
        """
        analyzed = _field_type(self, strict=True)
        # Decoration could not check a member that names something not defined
        # yet, so check it, once, now that the field is in use.
        _check_admitted(analyzed, _table_of(self._owner), late=True)
        return analyzed

    def _owned_by(self, owner: type[JSONObject]) -> JSONProperty:
        """Return this field for a subclass whose conversions differ.

        What eazyrest accepts of a field's type depends on the conversions of
        the model that owns the field, so the descriptor that a base declares
        cannot decide it for a subclass with other conversions.

        Args:
            owner: The subclass.

        Returns:
            A copy of the descriptor that checks its type against ``owner``'s
            conversions, when it is first used.
        """
        bound = copy.copy(self)
        bound.__dict__.pop("analyzed_type", None)
        bound._owner = owner
        return bound

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

        raw = obj._working_value(self.json_field)
        if raw is _MISSING:
            # The field is not in loaded JSON. The key is read like any other
            # field, which loads the object when it can load, except that an
            # object that has recorded its key reads it from there without a
            # load, and a key that the object lacks even when it is loaded or
            # cannot load reads as None, because an object may have no key.
            is_key = self._is_key_of(obj)
            if is_key and obj._saved_key_json is not None:
                raw = obj._saved_key_json
            else:
                if is_key and obj._url_override is None:
                    # Loading would need a URL, which needs the key.
                    return None
                raw = obj._json_having(self.json_field)
                if raw is _MISSING:
                    if is_key:
                        return None
                    raise _missing_field_error(
                        obj, self.field, self.json_field
                    )

        if self.field in obj._field_cache:
            return obj._field_cache[self.field]

        with _converting(type(obj), self.field):
            value = obj._from_json(
                raw,
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
            UsageError: If the model has no primary-key field, so its objects
                are read-only values.
            AttributeError: If the backing JSON does not contain this field, or
                the field is the key of an object that already names a resource
                and the value names another key. Assigning the same key does
                nothing.
            FieldConversionError: If the value cannot be converted to the
                field's JSON value, or a primary key that an object takes is
                not a valid value of the field's type.
        """
        _refuse_keyless_assignment(type(obj))
        if self.write_only:
            with _converting(type(obj), self.field):
                encoded = obj._to_json(value, self.analyzed_type)
            obj._update_json_field(self.json_field, _copy_json(encoded))
            return

        if self._is_key_of(obj):
            self._set_key(obj, value)
            return

        if self.read_only:
            raise AttributeError(
                f"{type(obj).__name__}.{self.field} is read-only"
            )
        stored = obj._json_with_field(self.json_field, self.field)
        with _converting(type(obj), self.field):
            new_value = obj._to_json(value, self.analyzed_type)
            # Build the collection that the field keeps before anything is sent
            # or queued, so that a value it cannot hold, such as an object that
            # has no hash for a set, writes nothing.
            kept = self._assigned_related(value)
            # A set drops the objects that are equal by key, so encode what it
            # keeps. Then the write and the snapshot agree, and a later write
            # of another field does not send this one again.
            if isinstance(kept, AbstractSet):
                new_value = obj._to_json(kept, self.analyzed_type)

        # A pending update is not on the server, even though the working JSON
        # holds it. In eager mode, assigning the same value must send it.
        unsent = (
            obj.write_mode == "eager"
            and self.json_field in obj._pending_updates
        )
        if unsent or not self._same_as_stored(stored, new_value):
            # Store a copy, so that changing the assigned value in place later
            # cannot match the stored JSON and suppress the next update.
            obj._update_json_field(self.json_field, _copy_json(new_value))

        # Drop cached values only now, because the update may read this field.
        # An assignment also replaces any change made in place to the old
        # value.
        obj._forget_fields([self.field])

        # An eager write's response can name other related objects, which
        # then replace the assigned ones, as after save().
        final = obj._working_value(self.json_field)
        if not self._same_as_stored(final, new_value):
            return

        if kept is not _MISSING:
            # Keep the assigned related objects, so that reading the field
            # returns them. Assigning the objects the field already holds
            # must not discard their unsaved changes.
            obj._field_cache[self.field] = kept
            if not isinstance(kept, JSONObject):
                # The caller can change a collection in place, as one read
                # from JSON.
                obj._field_snapshots[self.field] = _copy_json(new_value)

    def _set_key(self, obj: JSONObject, value: Any) -> None:
        """Give an object its key, or check that an assignment keeps it.

        An object that names no resource yet, with no key and no URL of its
        own, takes the key and sends nothing. An object whose key is known, or
        that is read from a hyperlink and so names a resource, keeps its key.
        Assigning the same key does nothing.

        Args:
            obj: Object whose key is assigned.
            value: The key, or a related object when the key is a relation.

        Raises:
            AttributeError: If the object already names a resource and the
                value names another key, or any key for an object read from a
                hyperlink that has not learned its key.
            FieldConversionError: If the value cannot be converted to the key's
                JSON value, is of another JSON type than a key of type ``int``
                or ``str`` has, is not written as a string or an integer, or a
                related object cannot give its key.
        """
        # Convert the key as if it were read from JSON, so it has the same
        # type before and after the object loads. A related object passed as
        # the key gives its own key, which it must know without a request, and
        # the object holds that key, not the related object. The key is copied
        # here, so a later change of the related object does not change it. Its
        # class is checked as a relation write checks it, so a key that is not
        # a relation refuses an object, and one that is refuses an object of
        # another model.
        conversion = self.analyzed_type
        with _converting(type(obj), self.field):
            related = _related_key_type(conversion)
            if isinstance(value, JSONObject) and related is not None:
                _check_related_class(value, related.model_type)
                _check_related_key(value, related.model_type)
                value = _known_key(value)
            value = obj._key_as_read_from_json(value)
            encoded = obj._to_json(value, conversion)
            named = _build_key(obj, encoded)

        name = type(obj).__name__
        if obj._saved_key_json is not None:
            saved = obj._canonical_key()
            if (
                saved is not None
                and named is not None
                and saved[1] == named[1]
            ):
                return
            raise AttributeError(
                f"The key of a saved {name} object cannot change, so "
                f"{name}.{self.field} cannot be assigned another value. Name "
                f"another object as {name}({self.field}=...)"
            )
        if obj._url_override is not None:
            raise AttributeError(
                f"A {name} object that was read from a hyperlink learns its "
                f"key when it loads, so {name}.{self.field} cannot be "
                f"assigned. Name another object as {name}({self.field}=...)"
            )

        obj._record_saved_key(encoded)
        if obj._json is not None:
            # JSON that lacks the key, or holds None, now holds it.
            obj._json[self.json_field] = _copy_json(encoded)
        # The key reads from the saved key, and not as it was cached. A key
        # that is a relation keeps its prefetched object, which the key just
        # given names.
        obj._forget_fields([self.field], keep_prefetched=True)

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

        # Abstract collection types read as concrete ones, as in _from_json().
        return _READ_AS[collection_type](value)


class JSONObject:
    """Base class for typed REST resources backed by JSON payloads.

    Subclasses usually define annotated attributes and are decorated with
    ``@json_object`` so each field is backed by a ``JSONProperty`` descriptor.

    Reading a field of an object that has not been loaded sends a ``GET``
    request, so field access can raise ``requests`` exceptions. Instances cache
    state without locking and are not safe to share between threads.

    Objects compare by key, as Django models do. Two objects of exactly the
    same model are equal if they have the same canonical key: the key that the
    object last took from the server or was given, decoded and encoded again by
    the key's field type. A key that is a relation is the related object's key
    that the object copied when the relation was assigned or loaded. Any other
    object, such as one built without a key, equals only itself. Hashing an
    object whose key is not known raises ``TypeError``, so the hash cannot
    change when the key becomes known.

    Once the key is known, the hash never changes. A spelling of one key that
    encodes alike, such as ``Z`` for ``+00:00``, is the same key and is taken.
    A key of type ``int`` or ``str`` refuses a value of another JSON type where
    it is given, so ``User(id="1")`` raises ``FieldConversionError`` for an
    ``int`` key. Server state that names another key, from a load, a write's
    response, or ``update_from_json()``, is refused with
    ``ResponseShapeError``.

    Once the key is known, the program cannot change it. Assigning the key
    field, ``pk``, or a key relation a value that names another key raises
    ``AttributeError``, and a key changed in place makes ``save()``, an eager
    assignment, or a load raise ``UsageError``, before anything is sent.
    """

    class_url: ClassVar[str]
    """Relative URL for this class."""

    api: ClassVar[API] = cast(Any, APIDescriptor())
    """The API of this object or class, read only.

    An object's own API, from ``api=``, comes first, then the API that
    ``register_api()`` bound to the nearest class in the MRO. Assigning it
    would replace what resolves an object's own API. In a class body, that
    raises ``TypeError``. After the class is defined, the class's next
    construction, query, or ``create()`` raises ``UsageError`` before sending
    anything, and so does the next request of an existing object, such as
    ``save()``, ``delete()``, or a lazy load.
    """

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

    A class's entries are merged over its bases', and theirs over the default
    ones for ``datetime``, ``date``, ``time``, ``timedelta``, ``UUID``,
    ``Decimal``, and enums. A field type converts by the entry for exactly that
    type, else by the entry of the nearest class in its MRO, else, for a
    ``NewType``, as its supertype. An entry for a dataclass, a ``NamedTuple``,
    or a ``TypedDict`` replaces its structure.
    A key cannot be a model, or a type that eazyrest converts itself:
    ``object``, ``bool``, ``int``, ``float``, ``str``, ``None``'s type, or a
    collection type such as ``list`` or ``dict``. Declare the mapping in the
    class body: the class merges and checks its table when it is defined.
    """

    _saved_key_json: Any = None
    """The primary-key field's JSON in the object's last recorded server state.

    For an object that was given its key and has not loaded, this is the key's
    encoding, and ``pk`` decodes it. ``None`` if the object has recorded no
    key. Only ``_record_saved_key()`` sets it.
    """

    _api_override: _AnyAPI | None
    """Instance-scoped API override."""

    _write_mode: WriteMode
    """Whether field assignment is saved eagerly or lazily."""

    _pending_updates: dict[str, Any]
    """JSON field updates queued for the next ``save()``.

    This is the only place that a pending value is kept. ``_json`` does not
    hold it, and a field read and ``json`` lay it over ``_json``. It holds the
    value of a write-only field too, which no read shows.
    """

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
    """The JSON that the server sent, or ``None`` before the object loads.

    It holds no pending update and no write-only field. A field read and
    ``json`` see the pending updates laid over it.
    """

    _json_is_partial: bool
    """Whether ``_json`` holds only some of the object's fields."""

    _url_override: str | None = None
    """The URL of an object that a hyperlinked relation named, if any."""

    _write_options: Mapping[str, Any] = MappingProxyType({})
    """Request options of the enclosing ``write_options()`` block, if any."""

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
                entry, such as one keyed by a model, if two decorated
                ancestors of the class are not subclasses of one another, if
                the class overrides ``from_json()`` or ``to_json()`` with the
                alphas' signature, which is no longer called, or if it
                assigns ``api``, which ``register_api()`` sets.
        """
        super().__init_subclass__(**kwargs)
        _check_removed_methods(cls)
        _check_api_attribute(cls, TypeError)
        _check_hook_signatures(cls)

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
        api: Client | None = None,
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
                ``PATCH``. A model without a primary-key field takes none.

        Raises:
            UsageError: If field values are given for a model without a
                primary-key field. Its objects are read-only values, built with
                ``json=``.
        """
        # A class's api can be assigned after the class is defined.
        _check_api_attribute(type(self), UsageError)
        if kwargs:
            _refuse_keyless_assignment(type(self))
        self._api_override = None if api is None else _as_client(api)
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
        elif _pk_property_of(type(self)) is None:
            # An object without a key has no URL, so nothing to load.
            self._json = {}
        self._json_is_partial = False

        # Set the primary key first, because assigning any other field loads
        # the object from its URL.
        pk_names = {"pk"}
        pk_property = _pk_property_of(type(self))
        if pk_property is not None:
            pk_names.add(pk_property.field)
            if self._json is not None and pk_property.json_field in self._json:
                self._record_saved_key(self._json[pk_property.json_field])

        for name in sorted(kwargs, key=lambda name: name not in pk_names):
            setattr(self, name, kwargs[name])

    def __eq__(self, other: object) -> bool:
        """Return whether another object is this object's resource.

        Two objects are equal if they are the same object, or are of exactly
        the same model and have the same canonical key. A subclass is not equal
        to its base. The API, the JSON, and the field values are not compared.

        The canonical key is computed from the key that the object last
        recorded from the server, or was given. Its JSON is read as the related
        model's key if the key is a relation, decoded and encoded again by that
        key's field type, and written as text. So spellings of one value that
        encode alike, such as ``Z`` and ``+00:00`` for a ``datetime``, are one
        key, and so are a bare key and an embedded object of a relation. Keys
        that encode differently are two keys, as ``Decimal`` keys of different
        precision are. Equal objects have the same ``url``, unless one has its
        own URL, as an object read through a ``Hyperlinked()`` relation has, or
        its class overrides ``url``. A refresh leaves the saved key as it was.

        An object whose saved key is not known, or is ``None``, or cannot be
        converted, or whose model has no key field, equals only itself. So does
        one whose key's type, or the related model's key type, cannot be
        resolved yet, until it can be. Comparing sends no request, never reads
        ``url``, and does not depend on the identity of a decoded key.

        Args:
            other: Object to compare with.

        Returns:
            Whether the objects are equal, or ``NotImplemented`` if ``other``
            is not a model object.

        Raises:
            TypeError: If the model's key declaration is refused when the key
                is first used, as ``url`` refuses it.
            UsageError: If a key is a relation whose model's ``api`` was
                assigned after the class was defined.
            Exception: Any other exception that a custom conversion raises,
                other than ``TypeError`` or ``ValueError``.
        """
        if not isinstance(other, JSONObject):
            return NotImplemented
        if self is other:
            return True
        if type(self) is not type(other):
            return False
        key = self._canonical_key()
        if key is None:
            return False
        other_key = other._canonical_key()
        return other_key is not None and key[1] == other_key[1]

    def __hash__(self) -> int:
        """Return a hash that agrees with equality.

        The hash is that of the model and the canonical key, so once the key is
        known it never changes. A spelling of the same key does not change it,
        and server state that names another key is refused. Hashing raises
        ``TypeError`` until the key is known. An object of a model without a
        key field hashes by identity, because its key can never become known.
        Hashing sends no request.

        Returns:
            The hash.

        Raises:
            TypeError: If the model has a key field but the object's key is not
                known, or is ``None``. A hash that changed when the key became
                known would corrupt a set or dict that held the object. The
                message advises loading, saving, or giving the key only for an
                object that has no JSON yet or only part of it, and otherwise
                says that the key is missing from the JSON, is ``None``, or
                cannot be read. Also raised, as ``url`` raises it, if the
                model's key declaration is refused when the key is first used.
            UsageError: If the key is a relation whose model's ``api`` was
                assigned after the class was defined.
            NameError: If the key's annotation, or a type alias in its type,
                names a class that is not defined yet, as ``url`` raises it. An
                ``AttributeError`` for an attribute of a module that is not set
                yet is raised the same way. Until the class is defined, ``==``
                is ``False`` for the object.
            Exception: Any other exception that a custom conversion raises,
                other than ``TypeError`` or ``ValueError``.
        """
        if _pk_property_of(type(self)) is None:
            return object.__hash__(self)
        key = self._canonical_key()
        if key is None:
            raise TypeError(_unhashable_key_message(self))
        return hash((type(self), key[1]))

    def __repr__(self) -> str:
        """Return the class and primary key without sending a request.

        The key is shown as its JSON value, whether or not the object has
        loaded, as in ``<User id=1>``. A key that is a relation shows the
        related object's key, not the related object that the JSON may embed,
        so a log of the object holds none of its fields. If that key cannot be
        read yet, the value is shown as ``...``. An object known only by its
        URL shows the URL, without any user and password.

        Returns:
            The class name and primary key in angle brackets, or the class name
            alone, as in ``<User>``, if the class has no primary key.
        """
        name = type(self).__name__
        pk_property: JSONProperty | None = getattr(type(self), "_pk", None)
        if pk_property is None:
            return f"<{name}>"

        json = self._json
        if json is not None:
            value = json.get(pk_property.json_field)
        else:
            value = self._saved_key_json

        if value is None and self._url_override is not None:
            return f"<{name} url={_redact_url(self._url_override)!r}>"
        if isinstance(value, dict):
            try:
                reduced = _reduce_key(type(self), value)
            except Exception:  # noqa: BLE001
                # Nothing that repr() shows may come from the related object.
                return f"<{name} {pk_property.field}=...>"
            value = None if reduced is None else reduced[2]
        return f"<{name} {pk_property.field}={value!r}>"

    def _set_prefetched_related(
        self,
        prefetched_related: Mapping[str, Mapping[Any, JSONObject]],
    ) -> None:
        """Attach prefetched related-object mappings to this instance.

        Args:
            prefetched_related: Mapping from each prefetched field name to the
                related objects loaded for it, keyed by the text of their
                canonical key, or by canonical URL for a hyperlinked field.
        """
        # Copy the outer mapping so each parent can drop an entire prefetched
        # field cache independently while still sharing the related objects.
        self._prefetched_related = dict(prefetched_related)

    def _forget_fields(
        self,
        fields: Iterable[str] | None = None,
        *,
        keep_prefetched: bool = False,
    ) -> None:
        """Drop the cached state of fields, so the next read decodes anew.

        A field's cached value, the encoding of it that detects a change made
        in place, and its prefetched related objects go together, since a value
        that outlives the others would be read stale or saved wrongly.

        Args:
            fields: Python attribute names of the fields to drop, or ``None``
                for every field.
            keep_prefetched: Whether to keep the prefetched related objects. A
                key that is a relation looks up its related object there, by
                the key just given.
        """
        if fields is None:
            self._field_cache.clear()
            self._field_snapshots.clear()
            if not keep_prefetched:
                self._prefetched_related.clear()
            return

        for field in fields:
            self._field_cache.pop(field, None)
            self._field_snapshots.pop(field, None)
            if not keep_prefetched:
                self._prefetched_related.pop(field, None)

    def _check_embedded_key(
        self, arg: dict[str, Any], ty: type[JSONObject], field: str | None
    ) -> None:
        """Refuse an embedded related object whose key is not a key.

        The key goes through the constructor that builds every key. A key that
        cannot be built is an error of the relation's field, not an unknown
        identity, since the server's JSON for a relation names the related
        object. A key whose type is not defined yet is not refused now.

        Args:
            arg: JSON of the embedded related object.
            ty: Related model.
            field: Name of the relation's field, which the error names.

        Raises:
            FieldConversionError: If the embedded key is of another JSON type
                than the related model's key, or does not convert.
        """
        prop = _pk_property_of(ty)
        if field is None or prop is None or prop.json_field not in arg:
            return
        try:
            _resolve_key(ty)
        except Exception as err:
            if getattr(err, _DECLARATION_ERROR, False):
                raise
            return
        with _converting(type(self), field):
            _build_key(object.__new__(ty), arg[prop.json_field])

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
            field: Optional owning field name for prefetch lookup and for the
                error that a wrong key raises.
            hyperlinked: Whether a string value is the related object's URL.

        Returns:
            Instance of ``ty``.

        Raises:
            TypeError: If a hyperlinked value is neither a URL nor a mapping.
            ValueError: If a URL's origin is not one that the related model's
                API allows.
            UsageError: If a class in the MRO of ``ty`` had ``api`` assigned.
            FieldConversionError: If a key is of another JSON type than the
                related model's key. It names ``field`` when that is given.
        """
        # A hyperlink is checked against the related model's API before the
        # object is built, so refuse an assigned ``api`` first.
        _check_api_attribute(ty, UsageError)
        # Pass on only an instance override. Without one, the related object
        # uses the API registered for its own class.
        api = self._api_override
        # If the argument is a dict, we treat it as JSON.
        if isinstance(arg, dict):
            self._check_embedded_key(arg, ty, field)
            return ty(
                json=_OwnedJSON(arg), api=api, write_mode=self.write_mode
            )

        if hyperlinked:
            if not isinstance(arg, str):
                raise TypeError(
                    "Hyperlinked field values must be URLs or objects, got "
                    f"{type(arg).__name__}"
                )
            url = _checked_hyperlink(ty.api if api is None else api, arg)
            if field is not None and field in self._prefetched_related:
                prefetched = self._prefetched_related[field].get(
                    _canonical_url(url)
                )
                if prefetched is not None:
                    return cast(T, prefetched)
            related = ty(api=api, write_mode=self.write_mode)
            related._url_override = url
            return related

        related = ty(api=api, write_mode=self.write_mode)
        try:
            ty._pk._set_key(related, arg)
        except FieldConversionError as err:
            # The server's value is the relation field's, so the error names
            # that field and not the related model's key.
            if field is None or err.__cause__ is None:
                raise
            raise FieldConversionError(
                type(self), field, cast(Exception, err.__cause__)
            ) from err.__cause__
        if field is not None and field in self._prefetched_related:
            canonical = related._canonical_key()
            if canonical is not None:
                prefetched = self._prefetched_related[field].get(canonical[1])
                if prefetched is not None:
                    return cast(T, prefetched)

        return related

    def _read_at(
        self,
        where: str,
        value: Any,
        conversion: AnalyzedType,
        field: str | None,
    ) -> Any:
        """Read one item of a structure, naming it in a conversion error.

        Args:
            where: The item's position or declared key, such as ``[0]``.
            value: The item's JSON value.
            conversion: The item's analyzed type.
            field: Owning field name, as for ``_from_json()``.

        Returns:
            The item's value.
        """
        with _at_key(where):
            return self._from_json(value, conversion, field=field)

    @classmethod
    def _write_at(
        cls, where: str, value: Any, conversion: AnalyzedType
    ) -> Any:
        """Write one item of a structure, naming it in a conversion error.

        Args:
            where: The item's position or declared key, such as ``[0]``.
            value: The item's value.
            conversion: The item's analyzed type.

        Returns:
            The item's JSON value.
        """
        with _at_key(where):
            return cls._to_json(value, conversion)

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
            # A union that cannot convert is refused whatever its value.
            _check_optional_union(conversion, _table_of(type(self)))
            return value

        found = _class_entry(conversion, _table_of(type(self)))
        if found is not None:
            # A decode may keep the JSON it is given, which may belong to
            # another object, so give it a copy.
            return found.entry.decode(_copy_json(value), found.declared)

        match conversion:
            case OptionalType(inner_type):
                return self._from_json(value, inner_type, field=field)

            case NewTypeType(_, inner_type):
                return self._from_json(value, inner_type, field=field)

            case BaseType(base_type):
                found = _conversion_of(base_type, _table_of(type(self)))
                if found is not None:
                    return found.entry.decode(
                        _copy_json(value), found.declared
                    )
                if typing.get_origin(base_type) is Literal:
                    return _literal_member(
                        base_type, value, _table_of(type(self))
                    )

                # Changing the returned value in place must not change the
                # object's JSON without recording an update.
                return _copy_json(value)

            case RelatedType(model_type, hyperlinked):
                return self._create_related(
                    value,
                    model_type,
                    field=field,
                    hyperlinked=hyperlinked,
                )

            case TypedDictType():
                keys = conversion.keys
                _check_mapping(value, conversion)
                read: dict[str, Any] = {}
                for key, item in value.items():
                    if key not in keys:
                        read[key] = _copy_json(item)
                        continue
                    with _at_key(f"[{key!r}]"):
                        read[key] = self._from_json(
                            item, keys[key], field=field
                        )
                return read

            case MappingType(value_type):
                _check_mapping(value, conversion)
                read = {}
                for key, item in value.items():
                    # The input chose the key, so the message does not name it.
                    with _at_key("[key]"):
                        read[key] = self._from_json(
                            item, value_type, field=field
                        )
                return read

            case FixedTupleType(item_types):
                items = _fixed_items(value, len(item_types), "read")
                return tuple(
                    self._read_at(f"[{index}]", item, item_type, field)
                    for index, (item, item_type) in enumerate(
                        zip(items, item_types, strict=True)
                    )
                )

            case NamedTupleType(named_tuple):
                positions = conversion.fields
                items = _fixed_items(value, len(positions), "read")
                return named_tuple(
                    *(
                        self._read_at(f"[{name!r}]", item, item_type, field)
                        for item, (name, item_type) in zip(
                            items, positions, strict=True
                        )
                    )
                )

            case DataclassType(dataclass_type):
                fields = conversion.fields
                declared = {name for name, _, _ in fields}
                if not isinstance(value, Mapping):
                    raise TypeError(
                        f"{dataclass_type.__name__} values must be JSON "
                        f"objects, got {type(value).__name__}"
                    )
                if not set(value) <= declared:
                    raise ValueError(
                        f"{dataclass_type.__name__} has keys that it does not "
                        "declare, which a dataclass cannot keep"
                    )
                return dataclass_type(
                    **{
                        name: self._read_at(
                            f"[{name!r}]", value[name], item_type, field
                        )
                        for name, item_type, _ in fields
                        if name in value
                    }
                )

            case UnionType():
                _check_union(conversion, _table_of(type(self)))
                return _copy_json(value)

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

                converted_items = _each_item(
                    lambda item: self._from_json(item, item_type, field=field),
                    value,
                )

                return _READ_AS[collection_type](converted_items)

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
            # A union that cannot convert is refused whatever its value.
            _check_optional_union(conversion, _table_of(cls))
            return None

        found = _class_entry(conversion, _table_of(cls))
        if found is not None:
            if _encodes(value, found):
                return _checked_json(found.entry.encode(value, found.declared))
            # The entry replaces the structure, so another value is written
            # as it is, as for an entry of a leaf type.
            return _checked_json(value)

        match conversion:
            case OptionalType(inner_type):
                return cls._to_json(value, inner_type)
            case NewTypeType(_, inner_type):
                return cls._to_json(value, inner_type)
            case RelatedType(model_type, hyperlinked=True):
                if isinstance(value, JSONObject):
                    _check_related_class(value, model_type)
                    return value.absolute_url
                if isinstance(value, str):
                    return value
                raise TypeError(
                    "Hyperlinked field values must be objects or URLs, got "
                    f"{type(value).__name__}"
                )
            case RelatedType(model_type):
                if isinstance(value, JSONObject):
                    _check_related_class(value, model_type)
                    _check_related_key(value, model_type)
                    model_type = type(value)
                    if value.pk is None:
                        # Writing None would clear the relation.
                        raise ValueError(
                            f"The {model_type.__name__} object's primary key "
                            "is None, so it cannot be written as a relation"
                        )
                    value = value.pk

                return model_type._to_json(value, model_type._pk.analyzed_type)
            case BaseType(base_type):
                found = _conversion_of(base_type, _table_of(cls))
                # A subclass of the key reads as the key's type, such as a
                # plain datetime for a subclass of datetime, which must write
                # back.
                if found is not None and _encodes(value, found):
                    return _checked_json(
                        found.entry.encode(value, found.declared)
                    )

                if typing.get_origin(base_type) is Literal and isinstance(
                    value, enum.Enum
                ):
                    value = _literal_json(value, _table_of(cls))

                # A value that is not JSON must fail here, naming the field,
                # rather than in json.dumps() when it is sent.
                return _checked_json(value)
            case CollectionType(collection_type, item_type):
                if isinstance(value, RelatedCollection):
                    # Encoding the collection must not use up its single
                    # traversal.
                    value = value._related_objects()
                elif not _is_collection_value(value):
                    raise TypeError(
                        "Collection field values must be collections, "
                        f"got {type(value).__name__}"
                    )

                items = _each_item(
                    lambda item: cls._to_json(item, item_type), value
                )
                if _READ_AS[collection_type] in (set, frozenset):
                    # A set's iteration order depends on how it was built, and
                    # a string's hash on the process, so a set writes its items
                    # sorted by their JSON. Equal sets then write equal JSON,
                    # and replacing one with another is not a change.
                    items.sort(key=_json_order)
                return items
            case FixedTupleType(item_types):
                items = _fixed_items(value, len(item_types), "write")
                return [
                    cls._write_at(f"[{index}]", item, item_type)
                    for index, (item, item_type) in enumerate(
                        zip(items, item_types, strict=True)
                    )
                ]
            case NamedTupleType():
                positions = conversion.fields
                items = _fixed_items(value, len(positions), "write")
                return [
                    cls._write_at(f"[{name!r}]", item, item_type)
                    for item, (name, item_type) in zip(
                        items, positions, strict=True
                    )
                ]
            case DataclassType(dataclass_type):
                name = dataclass_type.__name__
                if not isinstance(value, dataclass_type):
                    raise TypeError(
                        f"{name} field values must be {name} instances, got "
                        f"{type(value).__name__}"
                    )
                declared = {field for field, _, _ in conversion.fields}
                if any(
                    field.init and field.name not in declared
                    for field in dataclasses.fields(value)
                ):
                    # Writing only the declared fields would drop the rest.
                    raise TypeError(
                        f"{type(value).__name__} has fields that {name} does "
                        f"not declare, which a {name} field cannot write"
                    )
                return {
                    name: cls._write_at(
                        f"[{name!r}]", getattr(value, name), item_type
                    )
                    for name, item_type, _ in conversion.fields
                }
            case UnionType():
                _check_union(conversion, _table_of(cls))
                return _checked_json(value)
            case TypedDictType():
                keys = conversion.keys
                _check_mapping(value, conversion)
                written: dict[str, Any] = {}
                for key, item in value.items():
                    if key in keys:
                        with _at_key(f"[{key!r}]"):
                            written[key] = cls._to_json(item, keys[key])
                    else:
                        # An undeclared key is written as it is, so it must
                        # already be JSON.
                        with _at_key("[key]"):
                            _check_json(item, "")
                        written[key] = item
                return written
            case MappingType(value_type):
                _check_mapping(value, conversion)
                written = {}
                for key, item in value.items():
                    with _at_key("[key]"):
                        written[key] = cls._to_json(item, value_type)
                return written
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
                cannot set the body or enable response streaming, and are
                merged over the options of an enclosing ``write_options()``
                block.

        Raises:
            UsageError: If the model has no primary-key field, or a class had
                ``api`` assigned.
            ValueError: If the object has no URL, because its primary key is
                ``None``, as for an object built without a key or read from
                JSON that lacks it.
            TypeError: If ``request_options`` is not a mapping, sets the body,
                or streams the response.
            APIError: If the server returns an HTTP error status.
            RedirectError: If the server answers the write with a redirect.
            LimitExceeded: If a request or its JSON exceeds the API's limits.
        """
        _refuse_keyless_request(type(self), "delete")
        options = _merge_request_options(
            self._write_options, _request_options(request_options)
        )
        self._request_api().delete(self.url, **options)

    def _request_api(self) -> API:
        """Return the API that this object's own requests go through.

        Returns:
            The API of this object, after checking that no class in its MRO had
            ``api`` assigned since the object was built.

        Raises:
            UsageError: If a class in the MRO had ``api`` assigned. The
                assignment would send the request through that API and ignore
                the object's own.
        """
        _check_api_attribute(type(self), UsageError)
        return self.api

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

    @contextlib.contextmanager
    def write_options(self, **options: Any) -> Generator[Self, None, None]:
        """Send request options with every write of this object in a block.

        Inside the ``with`` block, the ``PATCH`` of each eager assignment and
        every ``save()`` and ``delete()`` of this object send ``options``,
        keyword arguments for the request as ``requests`` takes them, such as
        ``headers={"If-Match": etag}``. Options that ``save()`` or
        ``delete()`` receive in ``request_options`` are merged over them. A
        nested block merges its options over the outer block's, and the outer
        block's options apply again when it ends. Loads and the writes of
        related objects do not send them.

        Args:
            **options: Keyword arguments for the requests of the writes. They
                cannot set the body or enable response streaming.

        Yields:
            This object.

        Raises:
            TypeError: If ``options`` set the body, enable streaming, or name
                an argument that is not a request option. Nothing has changed
                then.
        """
        checked = _request_options(options)
        previous = self._write_options
        self._write_options = _merge_request_options(previous, checked)
        try:
            yield self
        finally:
            self._write_options = previous

    def _update_json_field(self, json_field: str, value: Any) -> None:
        """Send or queue a JSON field update, as the write mode says.

        Args:
            json_field: Name of the backing JSON field.
            value: JSON-serializable field value.
        """
        if self.write_mode == "eager":
            self._queue_in_place_changes()
            payload = dict(self._pending_updates)
            payload[json_field] = value
            resp = self._request_api().patch(
                self.url, json=self._to_wire(payload), **self._write_options
            )
            self._apply_patch_response(resp, payload)
            return

        self._queue_update(json_field, value)

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

    def _record_saved_key(self, key: Any) -> None:
        """Record the key that the object's server state names.

        Call it wherever the object takes server state that has the key field:
        when it is built from JSON, when its key is first given, when it loads,
        when a write's response is recorded, and when ``update_from_json()``
        merges a payload. Nothing else sets the saved key, so a refresh leaves
        it.

        Args:
            key: The JSON of the key field. A container is copied.
        """
        self._saved_key_json = _copy_json(key)
        vars(self).pop("_canonical", None)

    def _canonical_key(self) -> tuple[Any, str] | None:
        """Return the canonical key of the saved key, computed on first use.

        The canonical key is a pure function of the saved key's JSON, and
        ``_record_saved_key()`` drops it with the JSON, so it cannot be stale.

        Returns:
            The key encoded as JSON, and its JSON text, or ``None`` if the
            saved key is unknown: ``null``, or JSON that the key field cannot
            decode or encode. An unknown key is kept, except while something in
            the key's type cannot be resolved yet, as a name in its annotation,
            a form, or a type alias is not defined. Then the key is unknown
            only until it can be, and the next call tries again.

        Raises:
            TypeError: If the model's key declaration is refused, as for a
                hyperlinked key whose model is defined after it.
            UsageError: If the key is a relation whose model's ``api`` was
                assigned after the class was defined.
            Exception: Any exception other than ``TypeError`` or ``ValueError``
                that a custom conversion raises.
        """
        state = vars(self)
        try:
            return cast("tuple[Any, str] | None", state["_canonical"])
        except KeyError:
            pass
        if self._saved_key_json is not None:
            try:
                _resolve_key(type(self))
            except Exception as err:
                if getattr(err, _DECLARATION_ERROR, False):
                    raise
                # Something in the key's type is not defined yet, so the key is
                # unknown for now. Do not keep that, or it would outlast the
                # definition. No hash can have been taken meanwhile, since
                # hashing raises while the key is unknown.
                return None
        try:
            canonical = _build_key(self, self._saved_key_json)
        except (TypeError, ValueError) as err:
            if getattr(err, _DECLARATION_ERROR, False):
                raise
            canonical = None
        state["_canonical"] = canonical
        return canonical

    def _queue_update(self, json_field: str, value: Any) -> None:
        """Queue a JSON field update for ``save()``.

        The update is kept only in the pending updates, where a field read and
        ``json`` find it laid over the server's JSON. The value of a write-only
        field is queued the same way, and no read shows it.

        Args:
            json_field: Name of the backing JSON field.
            value: JSON-serializable field value.
        """
        self._pending_updates[json_field] = value

    def _changes_in_place(self) -> list[tuple[str, JSONProperty, Any]]:
        """Find the cached values that were changed in place.

        Every changed value is encoded, so a value that cannot be encoded fails
        before anything is queued.

        An object of a model without a key is a read-only value that can never
        be written, so it has no changes.

        Returns:
            Each changed field, its descriptor, and the value's new encoding.

        Raises:
            FieldConversionError: If a changed value cannot be encoded.
            UsageError: If the key was changed in place to name another key.
                Nothing is queued, and the pending updates stay.
        """
        if _pk_property_of(type(self)) is None:
            return []

        changed: list[tuple[str, JSONProperty, Any]] = []
        for field, snapshot in self._field_snapshots.items():
            descriptor = getattr(type(self), field)
            with _converting(type(self), field):
                encoded = self._to_json(
                    self._field_cache[field], descriptor.analyzed_type
                )
            if descriptor._is_key_of(self):
                # A key is the same key if its canonical text is. Python
                # equality is not the test, since it equates 1 with 1.0.
                if self._key_text_of(encoded) == self._key_text_of(snapshot):
                    continue
                name = type(self).__name__
                raise UsageError(
                    f"The key of a saved {name} object cannot change, but "
                    f"{name}.{field} was changed in place. Name another "
                    f"object as {name}({field}=...)"
                )
            if _json_equal(encoded, snapshot):
                continue
            changed.append((field, descriptor, encoded))
        return changed

    def _key_text_of(self, key: Any) -> str | None:
        """Return the canonical text of the JSON of a key, if it is one.

        Args:
            key: The JSON of the key field.

        Returns:
            The key's canonical text, or ``None`` if the JSON is no key that
            the field accepts.
        """
        try:
            built = _build_key(self, key)
        except (TypeError, ValueError) as err:
            if getattr(err, _DECLARATION_ERROR, False):
                raise
            return None
        return None if built is None else built[1]

    def _queue_changes(
        self, changed: list[tuple[str, JSONProperty, Any]]
    ) -> None:
        """Queue the updates for values that were changed in place.

        Args:
            changed: The changes that ``_changes_in_place()`` found.
        """
        for field, descriptor, encoded in changed:
            self._queue_update(descriptor.json_field, _copy_json(encoded))
            self._field_snapshots[field] = _copy_json(encoded)

    def _queue_in_place_changes(self) -> None:
        """Queue updates for cached values that were changed in place.

        An object that has not loaded loads first, so the update is made to the
        object that the server has. The load queues the changes itself.

        Raises:
            FieldConversionError: If a changed value cannot be encoded.
            UsageError: If the key was changed in place.
        """
        changed = self._changes_in_place()
        if changed and self._json is None:
            self._load_json()
        else:
            self._queue_changes(changed)

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
        # The object never keeps a write-only field's value.
        write_only = self._write_only_keys()
        sent = {k: v for k, v in payload.items() if k not in write_only}
        if self._json is None and not sent:
            # Only write-only fields were sent, so the object need not load.
            # A body becomes its JSON so far, and a read of a field that the
            # body lacks loads it.
            json = self._readable_json(self.merge_response(resp, {}, {}))
            if json:
                self._replace_json(json, clear_pending=True)
                self._json_is_partial = True
            else:
                self._pending_updates.clear()
        else:
            self._replace_json(
                self.merge_response(resp, self.json, sent), clear_pending=True
            )

    def _refuse_other_key(self, key: Any, *, adopt: bool) -> None:
        """Refuse server state that names another key than the saved one.

        A known key never changes, so the hash of an object does not change
        once its key is known. A spelling of the same key is the same key.

        Args:
            key: The JSON that the server's state holds for the key field.
            adopt: Whether an object whose key is not known takes the key that
                the state names. If not, such an object refuses any key.

        Raises:
            ResponseShapeError: If the saved key is known and the state names
                no key or another key, or the saved key is not known and
                ``adopt`` is false. Nothing has changed.
            TypeError: If the model's key declaration is refused when the key
                is first used.
            Exception: If the key's type cannot be resolved yet, whatever
                resolving it raises, as ``url`` raises it.
        """
        _ = type(self)._pk.analyzed_type
        saved = self._canonical_key()
        if saved is None and adopt:
            return
        try:
            named = _build_key(self, key)
        except (TypeError, ValueError) as err:
            if getattr(err, _DECLARATION_ERROR, False):
                raise
            named = None
        if saved is not None and named is not None and named[1] == saved[1]:
            return
        raise ResponseShapeError(
            "Incoming JSON primary key does not match this "
            f"{type(self).__name__} object, whose key cannot change"
        )

    def _replace_json(
        self, json: dict[str, Any], *, clear_pending: bool = False
    ) -> None:
        """Replace the server's JSON that this object keeps.

        The object records the key of the JSON as its saved key, if that has
        the key field. No pending update holds the key, since the key of an
        object that names a resource cannot change, so the key of this JSON is
        the server's. A key that is known never changes, so JSON that names
        another key is refused before anything changes.

        Cached values stay valid for the fields whose working value is
        unchanged, the pending updates laid over the server's JSON, so only the
        others are dropped. In particular, a related object assigned before
        ``save()`` stays the field's value, and so does a related object with
        unsaved changes when a load does not change it.

        Args:
            json: The JSON that the server sent, or that a write's response
                made of the object's JSON.
            clear_pending: Whether the write that produced ``json`` took the
                pending updates, so they are dropped with the old JSON. The
                comparison of the working JSON before and after needs them gone
                between the two reads. Otherwise a pending value that a write's
                response normalized would still show over the response, and the
                field's cached value would stay stale.

        Raises:
            ResponseShapeError: If the server's JSON names another key than the
                saved key. The object is unchanged.
        """
        before = self._working_json()
        json = self._readable_json(json)
        if _pk_property_of(type(self)) is not None:
            key: str = type(self)._pk.json_field
            if key in json:
                self._refuse_other_key(json[key], adopt=True)
                self._record_saved_key(json[key])
        self._json = json
        if clear_pending:
            self._pending_updates.clear()
        after = self._working_json()

        for field in getattr(self, "_json_fields", ()):
            json_field = getattr(type(self), field).json_field
            if not _json_equal(
                before.get(json_field, _MISSING),
                after.get(json_field, _MISSING),
            ):
                self._forget_fields([field])

    def merge_response(
        self,
        resp: Response,
        current: Mapping[str, Any],
        sent: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Return this object's JSON after a successful write.

        ``save()``, eager field assignments, and ``create()`` call this hook
        once the server has accepted a write, and the result becomes the
        object's JSON. The default applies the sent fields to the current JSON,
        then merges over them the object that the response body holds, if there
        is a body. A body that omits fields, or no body at all, as with
        ``204 No Content``, therefore keeps their values. A field that the
        server computes, such as a modification time, keeps its old value
        unless the body holds it. The body may change any field, but not a
        primary key that is known. A body that names another key makes the
        object raise ``ResponseShapeError`` when it records the body.

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
            **kwargs: Keyword arguments that later releases may pass, which
                an override must accept.

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
                They cannot set the body or enable response streaming. They
                are merged over the options of an enclosing
                ``write_options()`` block.

        Raises:
            UsageError: If the model has no primary-key field, even when
                nothing is pending, a class had ``api`` assigned, or the key
                was changed in place. Nothing is sent, and the pending updates
                stay.
            ValueError: If the object has no URL, because its primary key is
                ``None``, as for an object built without a key or read from
                JSON that lacks it.
            ResponseShapeError: If the response body does not hold a JSON
                object, or names another primary key than the object has. The
                server may have applied the updates, but they remain pending.
            TypeError: If ``request_options`` is not a mapping, sets the body,
                or streams the response, or a field cannot be sent as a form
                field.
            APIError: If the server returns an HTTP error status.
            RedirectError: If the server answers the write with a redirect.
            LimitExceeded: If a request or its JSON exceeds the API's limits.
            FieldConversionError: If a field's value cannot be converted.
        """
        _refuse_keyless_request(type(self), "save")
        options = _merge_request_options(
            self._write_options, _request_options(request_options)
        )
        self._queue_in_place_changes()
        if full:
            payload = self._full_payload()
            body = self._write_body(payload, files)
            resp = self._request_api().put(self.url, **body, **options)
            self._apply_patch_response(resp, payload)
            return
        if len(self._pending_updates) == 0 and not files:
            return

        payload = dict(self._pending_updates)
        body = self._write_body(payload, files)
        resp = self._request_api().patch(self.url, **body, **options)
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
        JSON fields are discarded. A payload that has the key field names the
        object's saved key from then on.

        If this object has not been loaded, the payload becomes its JSON
        without a request. Reading a field that the payload lacks then loads
        the full object.

        Args:
            json: Full or partial JSON object for this instance. The object
                keeps a deep copy, so changing the mapping or its nested values
                later does not change the object.

        Raises:
            ResponseShapeError: If the payload has the key field and its key,
                or the object's saved key, is not known, or the two have
                different canonical keys, so that the payload does not name
                this object. A key is not known if it is ``None`` or cannot be
                converted. An object that has its own URL, as one read from a
                hyperlink has, takes the payload's key if its saved key is
                absent or ``None``. It is a ``ValueError``, and nothing is
                merged.
            TypeError: If the model's key declaration is refused when the key
                is first used.
            Exception: If the key's type cannot be resolved yet, whatever
                resolving it raises, as ``url`` raises it. Nothing is merged.
        """
        payload = copy.deepcopy(dict(json))
        if _pk_property_of(type(self)) is None:
            # An object without a key has no key to compare.
            self._merge_json(payload)
            return

        # Resolve the key's type first, so that a declaration that the key
        # refuses is refused here, as every other use of the key refuses it.
        _ = type(self)._pk.analyzed_type
        # A payload without the key names no other object. An object known only
        # by its URL takes the key that the payload names. Two keys that are
        # not known name no common resource, and an object whose key is not
        # known equals only itself.
        if self._pk_json_field in payload:
            self._refuse_other_key(
                payload[self._pk_json_field], adopt=self._key_is_unknown()
            )

        if self._json is None:
            self._json = {}
            if self._saved_key_json is not None:
                self._json[self._pk_json_field] = _copy_json(
                    self._saved_key_json
                )
            self._json_is_partial = True

        self._merge_json(payload)
        if self._pk_json_field in payload:
            self._record_saved_key(payload[self._pk_json_field])

    def _key_is_unknown(self) -> bool:
        """Return whether an object with its own URL does not know its key.

        An object has its own URL if it was read from a hyperlink or its class
        overrides ``url``. It knows its key if it has recorded one, and its URL
        does not say. A saved key of ``None`` is the same as none recorded.
        Whether the object has loaded is not the question, so a key that a
        ``refresh()`` kept stays known across another.

        Returns:
            Whether the object has its own URL and its saved key is absent or
            ``None``.
        """
        has_own_url = self._url_override is not None or _url_is_overridden(
            type(self)
        )
        return has_own_url and self._saved_key_json is None

    def _merge_json(self, payload: dict[str, Any]) -> None:
        """Merge authoritative JSON members into this object's JSON.

        Args:
            payload: JSON members, which replace those of the same keys, and
                discard their cached values and pending updates.
        """
        assert self._json is not None
        self._json.update(self._readable_json(payload))

        for field in self._json_fields:
            descriptor: JSONProperty = getattr(type(self), field)
            if descriptor.json_field not in payload:
                continue

            self._forget_fields([field])
            self._pending_updates.pop(descriptor.json_field, None)

    def refresh(self) -> None:
        """Mark this object stale and discard pending local updates.

        The saved key does not change, so the object keeps its equality, its
        hash, and its ``url``. The object holds the saved key until it loads.

        Raises:
            UsageError: If the model has no key field. Such an object has
                nothing to reload.
            ValueError: If the object has no URL to reload from, because its
                primary key is ``None``, empty, or a dot segment, as for an
                object built without a key or read from JSON that lacks it.
                Nothing is discarded then. Before 1.0.0, an object with an
                empty key was reset without an error.
        """
        _refuse_keyless_request(type(self), "refresh")
        # An object without a URL cannot reload, so refuse before discarding
        # its state.
        _ = self.url
        # The saved key stays, so the object still has its key without its
        # JSON. Discard unsaved local changes
        self._pending_updates.clear()
        self._forget_fields()
        # Clear JSON
        self._json = None
        self._json_is_partial = False

    @classmethod
    def register_api(cls, api: API) -> None:
        """Register a default API instance for this model class.

        Args:
            api: API that this class, and any subclass that registers none of
                its own, uses by default.

        Raises:
            TypeError: If ``api`` is not an ``API``, as for the ``api=``
                argument of a query or a model.
        """
        _api_registry[cls] = _registered_api(api)

    @property
    def pk(self) -> Any:
        """Return this object's primary key value.

        A model without a key field has the key ``None``, and so has an object
        built without a key or read from JSON that lacks it. A key that is a
        relation is the related object's key as this object copied it when the
        relation was assigned or loaded.
        """
        pk_property: JSONProperty | None = getattr(type(self), "_pk", None)
        if pk_property is None:
            return None
        value = pk_property.__get__(self, type(self))
        if isinstance(value, JSONObject):
            # The key is a relation. It is the related object's key as this
            # object copied it, not the related object's key now.
            return _held_key(self)
        return value

    @pk.setter
    def pk(self, value: Any) -> None:
        """Set this object's primary key value.

        Args:
            value: Primary-key value, or a related object when the key is a
                relation. The model holds the related object's key, so the
                object must have it without a request: it holds the key, or its
                JSON has it. An object of a model that is neither the
                relation's nor a subclass of it is refused, and so is any
                object when the key is not a relation, an object without a key,
                and one known only by its URL or whose own key is a relation
                that is not known. The assignment sends no request.

        Raises:
            AttributeError: If the model has no key field, or the object
                already names a resource and the value names another key.
                Assigning the key that the object has does nothing.
            FieldConversionError: If the first assignment to an object that has
                no key gives a related object that the key cannot take. For one
                whose key is not known, the message says to load or save it
                first.
        """
        pk_property: JSONProperty | None = getattr(type(self), "_pk", None)
        if pk_property is None:
            raise AttributeError(
                f"{type(self).__name__} has no primary-key field. Declare "
                "one, or name it with json_object(pk=...)."
            )
        # Delegate to the class-installed descriptor instead of shadowing the
        # class variable on this instance.
        pk_property.__set__(self, value)

    def _key_as_read_from_json(self, key: Any) -> Any:
        """Return a primary key in the form that the field reads.

        Args:
            key: The key, as ``pk`` returns it.

        Returns:
            The key converted as if it were read from JSON. A key that is a
            relation gives the related model's key, which is what ``pk``
            returns.
        """
        conversion = type(self)._pk.analyzed_type
        value = self._from_json(
            self._to_json(key, conversion),
            conversion,
            field=type(self)._pk.field,
        )
        while isinstance(value, JSONObject):
            value = _held_key(value)
        return value

    def _saved_key_value(self) -> Any:
        """Return the saved key's JSON value, which names the object's URL.

        Returns:
            The canonical key's JSON value, or ``None`` if the saved key is
            unknown.

        Raises:
            FieldConversionError: If the saved key's JSON cannot be converted.
        """
        canonical = self._canonical_key()
        if canonical is not None:
            return canonical[0]
        if self._saved_key_json is not None:
            # Say why the key cannot be read, rather than that it is None.
            with _converting(type(self), type(self)._pk.field):
                _build_key(self, self._saved_key_json)
        return None

    @property
    def url(self) -> str:
        """Return the resource URL for this object.

        The primary key is converted to its JSON value and percent-encoded, so
        it always forms a single path segment. The key is the one that the
        object last recorded from the server, or that it was given.

        Returns:
            Relative URL for this object. It ends with ``/`` unless the API's
            ``trailing_slash`` is ``False``.

        An object read from a hyperlinked relation has the URL that the
        relation named, as an absolute URL.

        Raises:
            ValueError: If the primary key is ``None``, empty, or a dot
                segment, or the class has no primary-key field. The URL would
                name another resource, such as the collection. The key is
                ``None`` for an object built without it or read from JSON that
                lacks it.
            Exception: If the key's type cannot be resolved yet, whatever
                resolving it raises: a ``NameError`` for a class that is not
                defined, an ``AttributeError`` for a module's attribute that is
                not set, and a ``FieldConversionError`` for a form whose
                annotations cannot be resolved.
        """
        if self._url_override is not None:
            return self._url_override
        if getattr(type(self), "_pk", None) is None:
            raise ValueError(
                f"{type(self).__name__} has no primary-key field, so it has "
                "no URL."
            )
        pk = self._saved_key_value()
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
        """Return this object's JSON, with its pending updates.

        The server's JSON, loaded if the object has not loaded, with the
        pending updates laid over it and no write-only field. After
        ``update_from_json()`` on an object that was not loaded, the server's
        JSON is the partial payload received so far.

        The mapping is new on each call, so changing it does not change the
        object. Its values are the object's own, so changing one in place
        bypasses change tracking, and ``save()`` may send nothing or the wrong
        fields. Copy it with ``copy.deepcopy()`` before changing a value, and
        assign fields to change the object.

        Returns:
            JSON payload representing this object.
        """
        if self._json is None:
            self._load_json()

        return self._working_json()

    def _working_json(self) -> dict[str, Any]:
        """Return the server's JSON with the pending updates laid over it.

        Returns:
            A new mapping, without write-only fields, whose values are the
            object's own. It is empty for an object that has not loaded.
        """
        if self._json is None:
            return {}

        json = dict(self._json)
        if self._pending_updates:
            hidden = self._write_only_keys()
            json.update(
                (key, value)
                for key, value in self._pending_updates.items()
                if key not in hidden
            )
        return json

    def _working_value(self, json_field: str) -> Any:
        """Return a field's pending value, or else its value in server JSON.

        Unlike ``json``, this does not hide a write-only field.

        Args:
            json_field: JSON key to look for.

        Returns:
            The pending value, if there is one, otherwise the server's value.
            ``_MISSING`` if neither has the field, or the object has not
            loaded.
        """
        if self._json is None:
            return _MISSING

        if json_field in self._pending_updates:
            return self._pending_updates[json_field]

        return self._json.get(json_field, _MISSING)

    def _load_json(self) -> None:
        """Load the server's JSON for this object, keeping pending updates.

        The pending updates stay where they are, and the working JSON still
        shows them over the loaded JSON.

        Raises:
            ResponseShapeError: If the response does not hold a JSON object, or
                names another primary key than the object has.
            UsageError: If the key was changed in place.
        """
        # Reloading drops cached values, so queue their in-place changes first.
        self._queue_changes(self._changes_in_place())
        json = self._object_from_response(
            self.api.json(self._request_api().get(self.url))
        )

        # Some APIs leave the primary key out of a detail response, because the
        # URL holds it. Keep the key that the object was loaded by, so that the
        # object keeps its identity and URL.
        pk_json_field = getattr(type(self), "_pk_json_field", None)
        if pk_json_field is not None and pk_json_field not in json:
            if self._json is not None and pk_json_field in self._json:
                json[pk_json_field] = self._json[pk_json_field]
            elif self._saved_key_json is not None:
                json[pk_json_field] = _copy_json(self._saved_key_json)

        # Keep the values of fields that the load left unchanged, such as a
        # related object with unsaved changes of its own.
        self._replace_json(json)
        self._json_is_partial = False

    def _json_having(self, json_field: str) -> Any:
        """Return a field's working value, loading when it may be missing.

        Args:
            json_field: JSON key to look for.

        Returns:
            The field's value in the working JSON, which is ``_MISSING`` if the
            full object lacks it, or if the object cannot load.
        """
        if self._json is None:
            self._load_json()

        value = self._working_value(json_field)
        if (
            value is _MISSING
            and self._json_is_partial
            and _pk_property_of(type(self)) is not None
        ):
            # The partial payload from update_from_json() does not say whether
            # the server has this field, so load the full object. An object
            # without a key never loads, because it has no URL.
            self._load_json()
            value = self._working_value(json_field)

        return value

    def _json_with_field(self, json_field: str, field: str) -> Any:
        """Return a field's working value after checking that it has one.

        Args:
            json_field: JSON key to look for.
            field: Python attribute name used in the error message.

        Returns:
            The field's value in the working JSON.

        Raises:
            AttributeError: If the loaded JSON does not contain ``json_field``.
        """
        value = self._json_having(json_field)
        if value is _MISSING:
            raise _missing_field_error(self, field, json_field)

        return value

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
    def _query_collection(
        cls, descriptor: JSONProperty
    ) -> CollectionType | None:
        """Return the collection that a field's query values are items of.

        Args:
            descriptor: The field.

        Returns:
            The field's ``CollectionType``, inside any optional or ``NewType``,
            or ``None`` for a field that does not convert as a collection.
        """
        analyzed = descriptor.analyzed_type
        while True:
            analyzed = _unwrap_optional_type(analyzed)
            # A NewType is analyzed as one only around a structure, and no
            # conversion can be registered for such a NewType, so the field's
            # own structure is always its collection.
            if not isinstance(analyzed, NewTypeType):
                break
            analyzed = analyzed.inner
        return analyzed if isinstance(analyzed, CollectionType) else None

    @classmethod
    def _is_whole_query_value(cls, name: str, value: Any) -> bool:
        """Return whether a field's conversion takes a value as a whole.

        A model for a related field, and an instance of a class that a
        conversion of the field's type, or of its collection's items, encodes,
        are one value even if they are iterable.

        Args:
            name: Name of the parameter.
            value: Query value.

        Returns:
            Whether ``name`` is a JSON-backed field and its conversion takes
            ``value`` as one value, not as a collection of values.
        """
        descriptor = getattr(cls, name, None)
        if not isinstance(descriptor, JSONProperty):
            return False
        collection = cls._query_collection(descriptor)
        conversion = _unwrap_optional_type(
            descriptor.analyzed_type
            if collection is None
            else collection.item_type
        )
        if isinstance(conversion, RelatedType):
            return isinstance(value, JSONObject)
        if collection is None and isinstance(
            conversion,
            (
                FixedTupleType,
                NamedTupleType,
                DataclassType,
                TypedDictType,
                MappingType,
            ),
        ):
            # Its items are not each of the field's type.
            return True
        found = _class_entry(conversion, _table_of(cls))
        if found is None and isinstance(conversion, BaseType):
            found = _conversion_of(conversion.base_type, _table_of(cls))
        return found is not None and _encodes(value, found)

    @classmethod
    def _query_items(cls, name: str, value: Any) -> tuple[list[Any], bool]:
        """Return the operands of a query parameter, each encoded.

        A value that the field's conversion takes as a whole is one operand.
        Otherwise an iterable that is not a string, bytes, or a mapping is read
        once and gives an operand for each of its items, which the item
        conversion of a collection field, or the field's conversion, encodes. A
        parameter that names no field is not converted, except that a model is
        its primary key.

        Args:
            name: Name of the parameter.
            value: Query value.

        Returns:
            The operands, and whether the value gave them as repeated ones.
        """
        descriptor = getattr(cls, name, None)
        convert: Callable[[Any], Any]
        guard: contextlib.AbstractContextManager[None]
        if isinstance(descriptor, JSONProperty):
            collection = cls._query_collection(descriptor)
            conversion = _query_type(
                descriptor.analyzed_type
                if collection is None
                else collection.item_type
            )

            def convert(item: Any) -> Any:
                return _query_value(cls._to_json(item, conversion))

            guard = _converting(cls, name)
        else:
            collection = None

            def convert(item: Any) -> Any:
                if isinstance(item, JSONObject):
                    return cls._to_json(item, RelatedType(type(item)))
                return item

            guard = contextlib.nullcontext()

        with guard:
            if cls._is_whole_query_value(name, value):
                encoded = convert(value)
                if isinstance(encoded, list):
                    return encoded, True
                return [encoded], False
            if (
                value is None
                or isinstance(value, (str, bytes, Mapping))
                or not isinstance(value, Iterable)
            ):
                return [convert(value)], False
            if isinstance(value, RelatedCollection):
                # Reading the collection must not use up its single traversal.
                value = value._related_objects()
            items = _each_item(convert, value)
            if collection is not None and _READ_AS[
                collection.collection_type
            ] in (set, frozenset):
                items.sort(key=_json_order)
            return items, True

    @classmethod
    def _query_operands(cls, name: str, value: Any) -> list[Any]:
        """Return the operands that a query sends for a parameter.

        Args:
            name: Name of the parameter.
            value: Query value.

        Returns:
            A nonempty list of scalars, which requests sends as one parameter
            repeated.

        Raises:
            ValueError: If the value is a mapping or ``None``, an empty
                collection, or holds ``None`` or another collection, which
                requests would omit, flatten, or send as its keys, so the query
                would ignore or change its constraint.
        """
        operands, repeated = cls._query_items(name, value)
        problem = None
        if not repeated and operands[0] is None:
            problem = "is None, and requests omits it"
        elif not repeated and isinstance(operands[0], Mapping):
            problem = "is a mapping, and requests sends only its keys"
        elif not operands:
            problem = "is an empty collection, and requests omits it"
        elif any(operand is None for operand in operands):
            problem = "contains None, and requests omits that item"
        elif any(
            isinstance(operand, Mapping) or _is_collection_value(operand)
            for operand in operands
        ):
            problem = "contains a collection, and requests flattens it"
        if problem is not None:
            raise ValueError(
                f"Query parameter {name} for {cls.__name__} cannot be sent, "
                f"because its value {problem}"
            )
        return operands

    @classmethod
    def _encode_query(cls, params: Mapping[str, Any]) -> dict[str, Any]:
        """Encode query parameters as ``_encode_fields()`` encodes fields.

        Each parameter becomes a list of operands, which requests sends as the
        parameter repeated. A parameter named after a field is encoded by the
        field's conversion, and a value for a collection field may be a single
        item, which matches the objects whose collection holds it. A boolean
        field value is sent as ``true`` or ``false``, as in JSON.

        Args:
            params: Query parameter values keyed as for ``_encode_fields()``.

        Returns:
            Operands by JSON key.

        Raises:
            TypeError: If two parameters have the same JSON key.
            ValueError: If a parameter has a value that requests would not send
                intact, so the query would ignore or change its constraint.
        """
        payload: dict[str, Any] = {}
        names: dict[str, str] = {}
        for name, value in params.items():
            descriptor = getattr(cls, name, None)
            key = (
                descriptor.json_field
                if isinstance(descriptor, JSONProperty)
                else name
            )
            if key in payload:
                clash = TypeError(f"Multiple fields use the JSON key {key}")
                for field in (names[key], name):
                    if isinstance(getattr(cls, field, None), JSONProperty):
                        with _converting(cls, field):
                            raise clash
                raise clash
            names[key] = name
            payload[key] = cls._query_operands(name, value)

        return payload

    @classmethod
    def create(
        cls: type[Self],
        *,
        api: Client | None = None,
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
            UsageError: If the model has no primary-key field, so there is
                nothing to create. Post with the API and build the object with
                ``json=`` instead.
            ResponseShapeError: If the response does not hold a JSON object.
            TypeError: If a field is given both as a keyword argument and in
                ``fields``, if ``request_options`` is not a mapping or sets the
                body, or if a field's value cannot be sent as a form field with
                ``files``. Also raised for response streaming or conflicting
                wire names.
            AttributeError: If an ordinary value names a read-only field.
            APIError: If the server returns an HTTP error status.
            RedirectError: If the server answers the write with a redirect.
            LimitExceeded: If a request or its JSON exceeds the API's limits.
            FieldConversionError: If a field's value cannot be converted.
        """
        _refuse_keyless_request(cls, "create")
        options = _request_options(request_options)
        payload = cls._encode_fields(
            _query_params(
                fields, kwargs, "Field", "fields", reserved=frozenset()
            )
        )
        cls._check_created_key(payload)
        # A class's api can be assigned after the class is defined, and the
        # request must not be sent with it.
        _check_api_attribute(cls, UsageError)
        api = None if api is None else _as_client(api)
        client = cls.api if api is None else api
        resp = client.post(
            cls.class_url, **cls._write_body(payload, files), **options
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
    def _check_created_key(cls, payload: Mapping[str, Any]) -> None:
        """Refuse a key that ``create()`` would send and the model refuses.

        The key goes through the constructor that builds every key, so a key
        that would raise where it is given raises here, before the request.

        Args:
            payload: The JSON that ``create()`` sends.

        Raises:
            FieldConversionError: If the key is not one that the key field
                takes.
        """
        prop = _pk_property_of(cls)
        if prop is None or payload.get(prop.json_field) is None:
            return
        with _converting(cls, prop.field):
            _build_key(cls(), payload[prop.json_field])

    @classmethod
    def bulk_get_by_pks(
        cls: type[Self],
        pks: set[Any],
        *,
        api: Client,
        **kwargs: Any,
    ) -> Mapping[Any, Self]:
        """Bulk-load objects by primary key for explicit prefetch support.

        Prefetch keeps the objects that this hook returns under the API of the
        query that prefetches, whatever API they were created with.

        Args:
            pks: Primary keys to load.
            api: The client to send the bulk request with: the API that the
                query passed as ``api=``, or else this class's registered API.
            **kwargs: Context that later 1.x releases may pass. An override
                must accept it, and pass it on when it calls another hook.

        Returns:
            The loaded objects, in a mapping whose keys are not read. Prefetch
            matches each object by the key that it holds, as ``==`` compares
            keys, so the mapping may key them as it likes. An object without a
            key is ignored. An object of another model, which is not an
            instance of this class or of a subclass, raises ``TypeError``.

        Raises:
            PrefetchNotSupported: Always, unless overridden by a subclass.
        """
        del pks, api, kwargs
        raise PrefetchNotSupported(
            f"{cls.__name__} does not support bulk prefetch"
        )

    @classmethod
    def bulk_get_by_urls(
        cls: type[Self],
        urls: set[str],
        *,
        api: Client,
        **kwargs: Any,
    ) -> Mapping[str, Self]:
        """Bulk-load objects by URL to prefetch a hyperlinked relation.

        Prefetching a field annotated ``Annotated[Model, Hyperlinked()]``
        calls this hook on ``Model`` with the URLs that the parent objects
        hold, after checking their origins as reading the field does. An
        object returned without a URL of its own takes the URL that named
        it. Prefetch keeps the objects under the API of the query that
        prefetches, whatever API they were created with.

        Args:
            urls: Absolute URLs of the objects to load.
            api: The client to send the bulk request with, as for
                ``bulk_get_by_pks()``.
            **kwargs: Context that later 1.x releases may pass, as for
                ``bulk_get_by_pks()``.

        Returns:
            The loaded objects, in a mapping keyed by URL. URLs are matched
            after resolving them against the API's base URL and normalizing
            case and default ports, and objects under URLs that were not
            requested are ignored. An object of another model, which is not an
            instance of this class or of a subclass, raises ``TypeError``.

        Raises:
            PrefetchNotSupported: Always, unless overridden by a subclass.
        """
        del urls, api, kwargs
        raise PrefetchNotSupported(
            f"{cls.__name__} does not support bulk prefetch by URL"
        )

    @classmethod
    def collection_page(
        cls, resp: Response, *, api: Client, **kwargs: Any
    ) -> Page[Any]:
        """Read one page of a collection response.

        ``get_page()`` calls this hook, and so does the default
        ``collection_items_from_response()``, so ``filter()`` and ``all()``
        read the page and the pages that it links to through it. The default
        reads Django REST Framework's page shape with ``next_links_page()``.
        Override it to return ``link_headers_page(api, resp)`` for an API that
        links pages in Link headers, or ``next_links_page()`` with other member
        names. A page built another way holds its items, but cannot read other
        pages, so ``filter()`` stops after its items.

        Args:
            resp: Response for the page.
            api: The query's client, as for ``collection_items()``, which the
                page uses to read the pages it links to.
            **kwargs: Context that later 1.x releases may pass, as for
                ``collection_items()``.

        Returns:
            The page of JSON objects.
        """
        del kwargs
        return next_links_page(api, resp)

    @classmethod
    def collection_items_from_response(
        cls, resp: Response, *, api: Client, **kwargs: Any
    ) -> Iterable[Any]:
        """Iterate the JSON objects of a collection response.

        ``filter()`` calls this hook with the response to its request, and
        ``all()`` through it. The default reads the response with
        ``collection_page()``, then the pages that it links to, so a class that
        overrides only ``collection_page()`` reads ``filter()`` and
        ``get_page()`` alike. A page without a way to read others ends the
        traversal after its items. ``max_pages`` and ``max_items`` count the
        whole traversal, and a link to a page already read, or to another
        origin, raises ``PaginationError``. If the class overrides
        ``collection_items()``, the default passes the decoded payload to it
        instead. Override this hook to read a collection in a way that
        ``collection_page()`` cannot express, such as items that depend on more
        than one response.

        Args:
            resp: Response to the collection request.
            api: The query's client, as for ``collection_items()``.
            **kwargs: Context that later 1.x releases may pass, as for
                ``collection_items()``. The default passes it on.

        Returns:
            Iterable of JSON objects used to construct model instances.

        Raises:
            ResponseShapeError: If the payload is neither a list nor a page.
        """
        if cls._overrides_hook("collection_items"):
            return cls.collection_items(api.json(resp), api=api, **kwargs)
        return _follow_pages(
            api,
            cls.collection_page(resp, api=api, **kwargs),
            lambda page_resp: cls.collection_page(
                page_resp, api=api, **kwargs
            ),
        )

    @classmethod
    def collection_items(
        cls, payload: Any, *, api: Client, **kwargs: Any
    ) -> Iterable[Any]:
        """Iterate JSON objects for a collection query.

        The default accepts only a list, and does not follow pages. A class
        that overrides this hook replaces the default reading of
        ``collection_items_from_response()``, which then passes it the decoded
        payload, so ``filter()`` and ``all()`` call it and ``get_page()`` does
        not. Override it to unwrap envelope responses, or to follow pagination
        links by issuing additional requests through ``api``, the query's
        client. Code that reads a collection itself should call ``filter()``,
        or ``follow_next_links()`` with ``url=``, rather than this hook.

        ``api`` is the API that the query passed as ``api=``, or the class's
        registered API. With ``request_options=`` it is a view of that API, not
        an ``API``. It sends the query's options with every request, through
        the API's own request methods, so use it to send requests, not to
        configure, close, or compare the API, or to check ``isinstance(api,
        API)``. ``cls.api`` is always the class's registered API, so a hook
        sends its requests through ``api``.

        This hook receives only the decoded payload. To read the response's
        headers or URL, override ``collection_items_from_response()``.

        Args:
            payload: Decoded JSON payload returned by a collection request.
            api: The query's client, for requests for later pages.
            **kwargs: Context that later 1.x releases may pass, to tell the
                hook more about the query. An override must accept it, and
                pass it on when it calls another hook or ``super()``.

        Returns:
            Iterable of JSON objects used to construct model instances.

        Raises:
            ResponseShapeError: If the payload is not a supported collection
                response.
        """
        del kwargs
        if isinstance(payload, list):
            return payload

        raise ResponseShapeError(
            f"{cls.__name__}.collection_items() expected a list response, "
            f"got {type(payload).__name__}"
        )

    @classmethod
    def json_schema(
        cls, mode: Literal["read", "write"] = "read"
    ) -> dict[str, Any]:
        """Return a JSON schema of the objects that this class reads or writes.

        In ``"read"`` mode, the properties are the JSON keys that the class
        reads, after ``field_map``, without write-only fields. In
        ``"write"`` mode, they are the keys that writes send, after
        ``write_map``, without read-only fields and with write-only ones.
        Properties follow the declaration order, base classes first.

        A built-in conversion gives its JSON form, such as a string with
        ``format`` ``date-time`` for ``datetime.datetime``. ``T | None`` allows
        ``null``, a collection is an array, and a relation is the related
        model's primary key, or its URL for ``Hyperlinked()``, which a read
        also accepts as an embedded object. The definitions of a conversion's
        schema move into the schema's ``$defs``, renamed with the references to
        them when two share a name. A ``TypedDict`` or ``dict[str, T]`` is an
        object. A type whose JSON form eazyrest does not check, such as
        ``Any``, allows any value.

        eazyrest has no required fields, so the schema lists none, and allows
        other properties, which a server may send. The schema is a JSON Schema
        2020-12 schema, as its ``$schema`` says, which differs from the
        dialect of OpenAPI 3.0, as in its ``prefixItems`` for a fixed-length
        tuple.

        Args:
            mode: ``"read"`` or ``"write"``.

        Returns:
            The JSON schema, as a dict.

        Raises:
            ValueError: If ``mode`` is neither ``"read"`` nor ``"write"``.
            NameError: If a field's annotation names a type that cannot be
                resolved, as when the field is read.
        """
        if mode not in ("read", "write"):
            raise ValueError(f"mode must be 'read' or 'write', got {mode!r}")

        # Declaration order, base classes first. A redeclared field keeps its
        # first position. The annotations give the order, because a class's
        # namespace holds a field with a default where the class declares it
        # but gains a field without one only when json_object() adds it.
        json_fields = getattr(cls, "_json_fields", ())
        fields: dict[str, None] = {}
        for base in reversed(cls.__mro__):
            for name in _annotations(base):
                if name in json_fields:
                    fields.setdefault(name)

        defs: dict[str, Any] = {}
        properties: dict[str, Any] = {}
        for field in fields:
            descriptor: JSONProperty = getattr(cls, field)
            if mode == "read":
                if descriptor.write_only:
                    continue
                key = descriptor.json_field
            else:
                if descriptor.read_only:
                    continue
                key = descriptor.write_field
            with _converting(cls, field):
                properties[key] = _json_schema_of(
                    descriptor.analyzed_type,
                    mode,
                    defs,
                    frozenset(),
                    _table_of(cls),
                )

        # A conversion's schema may hold definitions of its own, also inside
        # a definition, which move into the shared ones until none remain.
        properties = {
            key: _hoist_nested_defs(value, defs)
            for key, value in properties.items()
        }
        done: set[str] = set()
        while todo := [name for name in defs if name not in done]:
            for name in todo:
                defs[name] = _hoist_nested_defs(defs[name], defs)
                done.add(name)

        schema: dict[str, Any] = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": cls.__name__,
            "type": "object",
            "properties": properties,
            "additionalProperties": True,
        }
        if defs:
            schema["$defs"] = defs
        return schema

    @classmethod
    def object_json(cls, payload: Any, **kwargs: Any) -> Any:
        """Return the JSON object contained in a single-object response.

        Subclasses can override this hook when an API wraps single-object
        responses in an envelope or returns singleton lists.

        Args:
            payload: Decoded JSON payload returned by the object endpoint.
            **kwargs: Keyword arguments that later releases may pass, which
                an override must accept.

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
                ``bulk_get_by_pks()``, or ``bulk_get_by_urls()`` for a
                hyperlinked field.
            UsageError: If a class in the MRO of the related model had ``api``
                assigned, because the bulk request would be sent through it.
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
        hook = (
            "bulk_get_by_urls"
            if analyzed_type.hyperlinked
            else "bulk_get_by_pks"
        )
        if not model_type._overrides_hook(hook):
            raise PrefetchNotSupported(
                f"{cls.__name__}.{field} cannot be prefetched, because "
                f"{model_type.__name__} does not implement {hook}()"
            )
        # The bulk request goes through the related model's class API, which an
        # assignment to its ``api`` would replace.
        _check_api_attribute(model_type, UsageError)

        return descriptor, analyzed_type, collection_type

    @classmethod
    def _overrides_hook(cls, name: str) -> bool:
        """Return whether this class overrides one of ``JSONObject``'s hooks.

        Args:
            name: Name of the hook, such as ``"bulk_get_by_pks"``.

        Returns:
            Whether the nearest class in the MRO that defines ``name`` is not
            ``JSONObject``.
        """
        owner = next(base for base in cls.__mro__ if name in vars(base))
        return owner is not JSONObject

    @classmethod
    def _apply_prefetch(
        cls: type[Self],
        items: Iterable[Any],
        fields: Sequence[str],
        query: _Query,
    ) -> PrefetchMap:
        """Bulk-load related objects for a batch of parent JSON items.

        Args:
            items: Parent JSON objects to scan for related-object primary keys.
            fields: Model attribute names to prefetch.
            query: The query that prefetches.

        Returns:
            Mapping of each prefetched field name to related objects. A key
            that is not hyperlinked is keyed by the text of its canonical key,
            as ``==`` compares keys, and a hyperlink by its canonical URL.
            Fields with no primary keys to fetch are omitted.

        Raises:
            ValueError: If any field is not a JSON-backed related-object field.
            Exception: If the key type of a related model cannot be resolved
                yet, whatever resolving it raises, as ``url`` of the related
                model's objects raises it. The request is not sent.
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
            model_type = related_type.model_type
            if related_type.hyperlinked:
                prefetched_related[field] = cls._prefetch_hyperlinks(
                    model_type, pks, query, cls, field
                )
                continue
            # The objects that the bulk request returns match their parents by
            # key. A key that cannot be read yet matches none, so raise before
            # the request rather than keep an empty prefetch.
            _resolve_key(model_type)
            related_objects = model_type.bulk_get_by_pks(
                pks, api=query.related_client(model_type)
            )
            # A hook keys its result as it likes, by the key as the parents
            # spell it or as it decodes, so each object gives its own key.
            for obj in related_objects.values():
                _check_prefetched_class(
                    obj, model_type, "bulk_get_by_pks", cls, field
                )
                if not _has_key_of(type(obj), model_type):
                    raise TypeError(
                        f"{model_type.__name__}.bulk_get_by_pks() returned an "
                        f"object of {type(obj).__name__} for "
                        f"{cls.__name__}.{field}, which is not keyed as "
                        f"{model_type.__name__} is"
                    )
            prefetched_related[field] = {
                canonical[1]: obj
                for obj in related_objects.values()
                if (canonical := obj._canonical_key()) is not None
            }

        return prefetched_related

    @staticmethod
    def _prefetch_hyperlinks(
        model_type: type[JSONObject],
        values: set[Any],
        query: _Query,
        owner: type[JSONObject],
        field: str,
    ) -> dict[str, JSONObject]:
        """Bulk-load the objects that a hyperlinked field's URLs name.

        Each URL is checked as reading the field checks it, against the API
        that the parent objects give their related objects. A value that is
        not a URL, or not one that the API allows, is not loaded, so reading
        the field still raises.

        Args:
            model_type: Class of the related objects.
            values: Hyperlinks that the parent objects hold.
            query: The query that prefetches.
            owner: Class of the parent objects, for the message.
            field: Name of the relation's field, for the message.

        Returns:
            The loaded objects, keyed by canonical absolute URL. Each has the
            URL that named it, as an object that a hyperlink names does.

        Raises:
            TypeError: If the hook returns an object that is not a
                ``model_type``.
        """
        check_api = model_type.api if query.api is None else query.api
        urls: dict[str, str] = {}
        for value in values:
            if not isinstance(value, str):
                continue
            try:
                url = _checked_hyperlink(check_api, value)
            except ValueError:
                continue
            urls[_canonical_url(url)] = url
        if not urls:
            return {}

        loaded = model_type.bulk_get_by_urls(
            set(urls.values()), api=query.related_client(model_type)
        )
        prefetched: dict[str, JSONObject] = {}
        for url, obj in loaded.items():
            _check_prefetched_class(
                obj, model_type, "bulk_get_by_urls", owner, field
            )
            key = _canonical_url(urljoin(check_api.base_url, url))
            if key not in urls:
                continue
            if obj._url_override is None:
                obj._url_override = urls[key]
            prefetched[key] = obj
        return prefetched

    @classmethod
    def _prefetched_filter(
        cls: type[Self],
        items: Iterable[Any],
        fields: Sequence[str],
        query: _Query,
    ) -> Iterator[Self]:
        """Yield prefetched objects lazily in batches.

        Args:
            items: Parent JSON objects returned by a collection query.
            fields: Model attribute names to prefetch for each batch.
            query: The query that prefetches.

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

            prefetched_related = cls._apply_prefetch(
                batch_items, fields, query
            )
            parents = [
                cls(json=_OwnedJSON(item), api=query.api)
                for item in batch_items
            ]

            # Related objects inherit their parent's write mode and API. A
            # prefetched object is shared by every parent in the batch, so set
            # its mode once, from the mode the parents are created with, rather
            # than when a parent reads it. Reading a relation through one
            # parent must not change how it saves through another. A bulk hook
            # creates its objects with the client it received, which carries
            # the query's request options, so give them the query's API.
            for related_objects in prefetched_related.values():
                for related in related_objects.values():
                    related.set_write_mode(parents[0].write_mode)
                    related._api_override = query.api

            for obj in parents:
                obj._set_prefetched_related(prefetched_related)
                yield obj

    @classmethod
    def filter(
        cls: type[Self],
        *,
        prefetch: PrefetchFields = None,
        api: Client | None = None,
        request_options: Mapping[str, Any] | None = None,
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

        The keyword arguments ``api``, ``prefetch``, ``defaults``,
        ``request_options``, ``params``, and ``url`` of the query methods are
        not query parameters. ``get_page()`` takes ``url``, and the other
        methods reserve it, so ``filter(url=...)`` raises ``TypeError``. Pass a
        parameter with one of these names in ``params``.

        ``request_options`` are sent with every request of the query: the
        collection request, the requests for later pages that the collection
        hooks send with their ``api``, and the bulk lookups of ``prefetch``
        on the same origin. Returned objects do not keep them.

        Args:
            prefetch: Optional related field name or field names to bulk-load
                explicitly. Prefetched related objects may be shared by
                identity across parent objects within the same batch.
            api: Optional API to send the request with, instead of the API
                registered for the class. Returned objects keep it as their
                API, so their later requests use it too.
            request_options: Optional keyword arguments for the query's
                requests, such as ``headers`` or ``timeout``, as ``requests``
                takes them. They cannot set a body or ``params``, or enable
                response streaming.
            params: Optional query parameters as a mapping, encoded as the
                keyword arguments are. A name in it may be anything, including
                ``api``, ``prefetch``, ``defaults``, ``request_options``, or
                ``params``, which the query methods take for themselves.
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
                in ``params``, or ``request_options`` is not valid for a
                query.
            PrefetchNotSupported: If the related model of a ``prefetch`` field
                does not implement ``bulk_get_by_pks()``.
            RedirectError: If the response is a redirect that was not
                followed, such as a 304.
        """
        options = _query_request_options(request_options)
        query = _query_params(params, kwargs)
        prefetch_fields = _normalize_prefetch(prefetch)
        # Check the fields before the request, so a misspelled field or a model
        # without bulk lookup fails even when the query matches nothing.
        for field in prefetch_fields:
            cls._prefetch_info(field)

        # A class's api can be assigned after the class is defined, and the
        # request must not be sent with it.
        _check_api_attribute(cls, UsageError)
        api = None if api is None else _as_client(api)
        base = cls.api if api is None else api
        client = base._scoped(options) if options else base
        context = _Query(api, client, options)
        resp = _check_final_response(
            client.get(cls.class_url, params=cls._encode_query(query)),
            "the query has no collection to read",
        )
        items = cls.collection_items_from_response(resp, api=client)

        count = _count_of(items)
        # Treat an empty prefetch list the same as no prefetch at all.
        if len(prefetch_fields) == 0:
            return Results(
                (cls(json=_OwnedJSON(item), api=api) for item in items), count
            )

        return Results(
            cls._prefetched_filter(items, prefetch_fields, context), count
        )

    @classmethod
    def all(
        cls: type[Self],
        *,
        prefetch: PrefetchFields = None,
        api: Client | None = None,
        request_options: Mapping[str, Any] | None = None,
    ) -> Results[Self]:
        """Return all objects for this resource type.

        Args:
            prefetch: Optional related field name or field names to bulk-load
                explicitly. Prefetched related objects may be shared by
                identity across parent objects within the same batch.
            api: Optional API to send the request with, instead of the API
                registered for the class. Returned objects keep it as their
                API, so their later requests use it too.
            request_options: Optional keyword arguments for the query's
                requests, as for ``filter()``.

        Returns:
            Iterator over all objects, with ``count`` as for ``filter()``.

        Raises:
            ValueError: If a ``prefetch`` field is not a JSON-backed
                related-object field.
            TypeError: If ``request_options`` is not valid for a query.
            PrefetchNotSupported: If the related model of a ``prefetch`` field
                does not implement ``bulk_get_by_pks()``.
            RedirectError: If the response is a redirect that was not
                followed, such as a 304.
        """
        return cls.filter(
            prefetch=prefetch, api=api, request_options=request_options
        )

    @classmethod
    def get_page(
        cls: type[Self],
        *,
        url: str | None = None,
        prefetch: PrefetchFields = None,
        api: Client | None = None,
        request_options: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> Page[Self]:
        """Fetch one page of objects matching query parameters.

        Without ``url``, this sends the query as ``filter()`` does and returns
        the collection's first page. With ``url``, it requests that page, such
        as a page URL saved from ``next_url``, which must have the origin of
        the API's base URL. ``collection_page()`` reads each page.

        The page's ``next_page()`` and ``previous_page()`` request the pages
        around it with the same API, request options, and prefetch, and
        return pages of objects too.

        Args:
            url: Optional URL of the page to fetch, absolute or relative to the
                API's base URL. A page's URL holds its query, so ``url`` cannot
                be combined with query parameters.
            prefetch: Optional related field name or field names to bulk-load
                explicitly, for each page, as for ``filter()``.
            api: Optional API to send the requests with, as for ``filter()``.
            request_options: Optional keyword arguments for the requests, as
                for ``filter()``.
            params: Optional query parameters as a mapping, as for
                ``filter()``.
            **kwargs: Query string parameters, as for ``filter()``.

        Returns:
            The page of objects.

        Raises:
            ValueError: If a ``prefetch`` field is not a JSON-backed
                related-object field, or if a parameter has a value that
                requests would not send intact.
            TypeError: If ``url`` is given with query parameters, a parameter
                is given both as a keyword argument and in ``params``,
                ``request_options`` is not valid for a query, or
                ``collection_page()`` does not return a ``Page``.
            PrefetchNotSupported: If the related model of a ``prefetch`` field
                does not implement its bulk lookup.
            PaginationError: If ``url`` has another origin.
            RedirectError: If the response is a redirect that was not
                followed, such as a 304.
        """
        options = _query_request_options(request_options)
        query = _query_params(params, kwargs)
        if url is not None and query:
            raise TypeError(
                "get_page() takes url= or query parameters, not both, because "
                "a page's URL holds its query"
            )
        prefetch_fields = _normalize_prefetch(prefetch)
        for field in prefetch_fields:
            cls._prefetch_info(field)

        # A class's api can be assigned after the class is defined, and the
        # request must not be sent with it.
        _check_api_attribute(cls, UsageError)
        api = None if api is None else _as_client(api)
        base = cls.api if api is None else api
        client = base._scoped(options) if options else base
        source = _ModelPageSource(
            cls, _Query(api, client, options), prefetch_fields
        )
        if url is None:
            resp = _check_final_response(
                client.get(cls.class_url, params=cls._encode_query(query)),
                "the query has no collection to read",
            )
        else:
            resp = _request_page(client, url, allow_other_origins=False)
        return cast("Page[Self]", source.page(resp))

    @classmethod
    def get(
        cls: type[Self],
        *,
        prefetch: PrefetchFields = None,
        api: Client | None = None,
        request_options: Mapping[str, Any] | None = None,
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
            request_options: Optional keyword arguments for the query's
                requests, as for ``filter()``.
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
                in ``params``, or ``request_options`` is not valid for a
                query.
            PrefetchNotSupported: If the related model of a ``prefetch`` field
                does not implement ``bulk_get_by_pks()``.
            RedirectError: If the response is a redirect that was not
                followed, such as a 304.
        """
        query = _query_params(params, kwargs)
        results = iter(
            cls.filter(
                prefetch=prefetch,
                api=api,
                request_options=request_options,
                params=query,
            )
        )
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
        api: Client | None = None,
        request_options: Mapping[str, Any] | None = None,
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
        once, before the lookup, and its items are used for both requests. A
        value that the field's conversion takes as a whole is not read.

        Args:
            defaults: Optional fields applied only when creating a new object.
            api: Optional API to send the lookup and any creation with, instead
                of the API registered for the class. The returned object keeps
                it as its API, so its later requests use it too.
            request_options: Optional keyword arguments for the lookup's
                requests, as for ``filter()``, and for any creation, as for
                ``create()``.
            params: Optional fields and lookups as a mapping, used as the
                keyword arguments are. A name in it may be anything, including
                one that the query methods take for themselves, such as
                ``api``.
            **kwargs: Fields used for lookup and included in creation.

        Returns:
            Tuple ``(obj, created)`` where ``created`` indicates whether a new
            object was created.

        Raises:
            UsageError: If the model has no primary-key field, so there is
                nothing to create. No request is sent.
            MultipleObjectsReturned: If lookup matches multiple objects.
            ValueError: If a lookup parameter has a value that requests would
                not send intact.
            TypeError: If a parameter is given both as a keyword argument and
                in ``params``, or ``request_options`` is not valid for a
                query and a creation.
            RedirectError: If a response is a redirect that was not followed.
        """
        _refuse_keyless_request(cls, "create")
        # Check the options for both requests before sending either.
        _query_request_options(request_options)
        _request_options(request_options)
        # The lookup and the creation each iterate a collection value, so read
        # a single-pass iterable, such as a generator, only once.
        kwargs = {
            name: list(value)
            if isinstance(value, Iterable)
            and not isinstance(value, (Collection, str, bytes, Mapping))
            and not cls._is_whole_query_value(name, value)
            else value
            for name, value in _query_params(params, kwargs).items()
        }
        results = cls.filter(
            api=api, request_options=request_options, params=kwargs
        )
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

        return (
            cls.create(
                api=api, fields=create_kwargs, request_options=request_options
            ),
            True,
        )
