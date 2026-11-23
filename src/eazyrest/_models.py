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
import weakref
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
    NoReturn,
    Self,
    SupportsIndex,
    TypeAlias,
    TypeVar,
    Union,
    cast,
    overload,
)
from urllib.parse import quote

import requests

from ._aliases import (
    _expand_aliases,
    _resolve_in_module,
    _UnresolvedAliasError,
)
from ._conversion import (
    DEFAULT_CONVERSIONS,
    Conversion,
    _annotated_supertype,
    _conversion_of,
    _definition_name,
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
)
from ._protocols import Client, Response
from ._results import Results, _count_of
from .api import (
    API,
    _AnyAPI,
    _as_client,
    _check_final_response,
    _merge_request_options,
    _origin,
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


_USABLE_CLASSES: weakref.WeakSet[type] = weakref.WeakSet()
"""Classes that passed ``_require_decorated()``, so it checks each once."""


def _require_decorated(cls: type) -> None:
    """Refuse a model class that sets conversions and is not decorated.

    An undecorated subclass of a decorated model keeps the descriptors of its
    base, whose types were checked against the base's conversions. What a
    field's type may be depends on the conversions of the model that owns it,
    so a subclass that sets its own ``conversions`` must be decorated, which
    checks the inherited fields against them. ``__init_subclass__`` runs before
    the decorator, so the class is refused when it is first used, not when it
    is defined.

    Args:
        cls: Model class that an operation uses.

    Raises:
        TypeError: If ``cls`` is not decorated, has a decorated ancestor, and
            sets ``conversions`` itself or inherits them from a class that is
            not an ancestor of that decorated class, such as a mixin.
    """
    if cls in _USABLE_CLASSES:
        return
    decorated = next(
        (base for base in cls.__mro__ if "_json_fields" in vars(base)), None
    )
    if decorated is None or decorated is cls:
        # With no decorated ancestor no field was checked against another
        # table, and a decorated class checked its own.
        _USABLE_CLASSES.add(cls)
        return
    # Every class that contributes to the table but not to the decorated
    # ancestor's, whether it comes before the ancestor in the MRO or after it.
    undecorated = [
        base for base in cls.__mro__ if base not in decorated.__mro__
    ]
    for base in undecorated:
        if "conversions" in vars(base):
            error = TypeError(
                f"{cls.__name__} is not decorated, and "
                f"{base.__name__} sets conversions, which the fields that "
                f"{cls.__name__} inherits were not checked against. Decorate "
                f"{cls.__name__} with @json_object."
            )
            # A conversion reports the error as a plain TypeError, as
            # decorating the class would, not as the conversion of a value.
            setattr(error, _DECLARATION_ERROR, True)
            raise error
    _USABLE_CLASSES.add(cls)


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

The key is the canonical key itself, the ``int`` or the text that is sent.
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
        an ``Authorization`` header, do not reach another model's host.

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
        raw = _checked_page(model, model.collection_page(resp, api=self.api))
        _check_page_size(self.api, raw.items)
        if self._prefetch:
            objects: list[JSONObject] = list(
                model._prefetched_filter(
                    raw.items, self._prefetch, self._query
                )
            )
        else:
            objects = [
                model._from_item(item, index, self._query.api)
                for index, item in enumerate(raw.items)
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


def _checked_page(model: type[Any], page: Any) -> Page[Any]:
    """Return what a model's ``collection_page()`` gave, if it is a page.

    Args:
        model: The model whose hook was called.
        page: The hook's result.

    Returns:
        ``page``.

    Raises:
        TypeError: If ``page`` is not a ``Page``.
    """
    if not isinstance(page, Page):
        raise TypeError(
            f"{model.__name__}.collection_page() must return a Page, got "
            f"{type(page).__name__}"
        )
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


class _KeylessFieldTypeError(TypeError):
    """A model without a primary-key field is the type of a field."""


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
            "nothing. Remove the Pydantic metadata."
        )


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
    if cast(Any, ty) is None:
        # typing spells None as its type in a type argument, as in List[None],
        # and the builtin generics keep it as None.
        ty = type(None)
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
        return analyze_type(ty_args[0])

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

    supertype = _annotated_supertype(ty)
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


def _check_named_tuple_annotated(ty: type[Any]) -> None:
    """Refuse a tuple class whose fields have no annotations.

    A ``NamedTuple`` annotates each field, which says how the field converts.
    ``collections.namedtuple()`` makes a class without annotations, so nothing
    says how its fields read or write.

    Args:
        ty: Tuple class with named fields.

    Raises:
        TypeError: If a field has no annotation on the class or a base.
    """
    missing = [
        name
        for name in ty._fields
        if not any(name in _annotations(base) for base in ty.__mro__)
    ]
    if missing:
        raise TypeError(
            f"{ty.__name__} has the fields {', '.join(missing)} without "
            "annotations, so eazyrest cannot tell how they convert. Declare "
            f"{ty.__name__} with typing.NamedTuple, which annotates each "
            "field."
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
        literal = _literal_of(ty)
        if _conversion_of(ty, table) is not None or (
            literal is not None
            and any(
                isinstance(arg, enum.Enum) for arg in typing.get_args(literal)
            )
        ):
            raise TypeError(
                _union_message(
                    functools.reduce(
                        operator.or_,
                        (member.base_type for member in union.members),
                    )
                )
            )


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
    if _literal_of(cls) is not None:
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


def _check_json_scalars(name: str, values: Iterable[Any]) -> None:
    """Refuse a ``Literal`` or an enum with a value that JSON cannot carry.

    A value that is not a string, a number, a ``bool``, or ``None``, such as
    bytes or a tuple, has no reading from JSON and cannot be written to it, so
    a field of the type could neither be read nor be given a value.

    Args:
        name: What to call the type in the message.
        values: The JSON values that the type's members stand for.

    Raises:
        TypeError: If a value is not a JSON scalar.
    """
    for value in values:
        if value is not None and not isinstance(value, (str, int, float)):
            raise TypeError(
                f"{name} has a value of type {type(value).__name__}, which is "
                "not a JSON scalar, so its JSON could not be read or written. "
                "Use strings, integers, floats, bools, or None."
            )


def _literal_of(ty: Any) -> Any | None:
    """Return the ``Literal`` that a declared type is, or a ``NewType`` of.

    Args:
        ty: Declared type of a leaf.

    Returns:
        ``ty``, or the supertype of the ``NewType`` that ``ty`` is, if it is a
        ``Literal``. ``None`` for any other type.
    """
    ty = _final_supertype(ty)
    return ty if typing.get_origin(ty) is Literal else None


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
    first use, unless ``late`` says that this is it. So does the check of a
    union that holds a member that converts, which decoration makes only for a
    field's own type.

    Args:
        analyzed: Analyzed type of a field.
        table: Conversion table of the model that owns the field.
        seen: Structures being walked, to stop at a cycle.
        late: Whether the field is in use, so that a member that cannot be
            analyzed raises.

    Raises:
        TypeError: If a member is refused, a tuple class has fields without
            annotations, an enum has a member whose value is ``None``, or a
            ``Literal`` holds such a member. If ``late``, also a union with a
            member that converts, and whatever analyzing a member raises.
    """
    if _class_entry(analyzed, table) is not None:
        return
    members: Iterable[AnalyzedType] = ()
    match analyzed:
        case BaseType(base_type):
            if (literal := _literal_of(base_type)) is not None:
                _check_json_scalars(
                    "A Literal",
                    (
                        _literal_json(member, table)
                        for member in typing.get_args(literal)
                    ),
                )
                for member in typing.get_args(literal):
                    if not isinstance(member, enum.Enum):
                        continue
                    held = _conversion_of(type(member), table)
                    # As for an enum, a conversion of its own decides.
                    if (
                        held is not None
                        and held.key is enum.Enum
                        and held.entry.encode(member, held.declared) is None
                    ):
                        raise TypeError(
                            f"The member {type(member).__name__}."
                            f"{member.name} in a Literal has the value "
                            "None, which every field reads as no value, so "
                            "the member would never be read. Give it "
                            "another value, or write None in the Literal "
                            "instead."
                        )
            found = _conversion_of(base_type, table)
            if found is not None and found.key is enum.Enum:
                _check_json_scalars(
                    found.declared.__name__,
                    (
                        found.entry.encode(member, found.declared)
                        for member in found.declared
                    ),
                )
                for member in found.declared:
                    if found.entry.encode(member, found.declared) is None:
                        raise TypeError(
                            f"The member {member.name} of "
                            f"{found.declared.__name__} has the value None, "
                            "which every field reads as no value, so the "
                            "member would never be read. Give it another "
                            "value, or leave it out and declare the field "
                            "optional."
                        )
            if found is None:
                _refuse_pydantic_class(base_type)
                if (supertype := _supertype(base_type)) is not None:
                    _check_admitted(
                        analyze_type(supertype), table, seen, late=late
                    )
                else:
                    _refuse_unconverted_class(base_type)
        case UnionType(leaves):
            if late:
                # A structure's union is refused whatever the value, so a value
                # that holds none of it, such as an empty list, is refused too.
                _check_union(analyzed, table)
            members = leaves
        case OptionalType(inner) | NewTypeType(_, inner):
            members = (inner,)
        case CollectionType(_, item) | MappingType(item):
            members = (item,)
        case FixedTupleType(items):
            members = items
        case TypedDictType(cls) | NamedTupleType(cls) | DataclassType(cls):
            _refuse_pydantic_class(cls)
            if isinstance(analyzed, NamedTupleType):
                _check_named_tuple_annotated(cls)
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


def _has_extra_items_rule(ty: type[Any]) -> bool:
    """Return whether a ``TypedDict`` is closed or has extra items.

    PEP 728 inherits both from a superclass, but the class attributes
    ``__closed__`` and ``__extra_items__`` reflect only what the class itself
    passed. So a subclass of a closed ``TypedDict`` is closed, though its own
    attributes say it is not.

    Args:
        ty: ``TypedDict`` class.

    Returns:
        Whether ``ty`` or a ``TypedDict`` that it inherits from is closed or
        has extra items.
    """
    if getattr(ty, "__closed__", None) or not any(
        getattr(ty, "__extra_items__", None) is marker
        for marker in _NO_EXTRA_ITEMS
    ):
        return True
    for base in getattr(ty, "__orig_bases__", ()):
        base = typing.get_origin(base) or base
        if _is_typed_dict(base) and _has_extra_items_rule(base):
            return True
    return False


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
        TypeError: If ``ty`` is closed or has extra items (PEP 728), also by
            inheritance, or if an annotation cannot be resolved or converted,
            or holds a model.
    """
    if _has_extra_items_rule(ty):
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

    Writing the key of an object of another model would name a resource of the
    wrong collection.

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


def _check_own_key_object(value: JSONObject, model_type: type[Any]) -> None:
    """Refuse an object that is given for a model's own key.

    A model's key is not a relation, so the refusal calls it the key. An object
    of the model, or of a subclass that keeps its key, is the key it names.

    Args:
        value: Object given for the key of ``model_type``.
        model_type: The model whose key is compared.

    Raises:
        TypeError: If ``value``'s class is not ``model_type`` or a subclass of
            it that keeps its key. The field's name is added where the
            conversion runs.
    """
    if not isinstance(value, model_type):
        raise TypeError(
            f"The key of {model_type.__name__} cannot be an object of "
            f"{type(value).__name__}, which is another model. Pass its key, "
            f"or an object of {model_type.__name__}"
        )
    if not _has_key_of(type(value), model_type):
        raise TypeError(
            f"The key of {model_type.__name__} cannot be an object of "
            f"{type(value).__name__}, which chose another primary key than "
            f"{model_type.__name__}'s"
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


def _resolve_key(cls: type[JSONObject]) -> None:
    """Resolve a model's whole key type, before any use of the key.

    Every use of a key resolves its type through this, before it builds
    anything. Resolving the type is everything that building a key would
    resolve: the key's annotation and the value of each type alias in it.
    Building a key then resolves no name. The result is cached on the key's
    property, and only once it is complete.

    Args:
        cls: Model class that has a primary key.

    Raises:
        TypeError: If the model's key declaration is refused, which carries a
            mark that tells it from a name that is not defined yet.
        Exception: Whatever resolving a name in the key's type raises, such as
            a ``NameError`` or an ``AttributeError``.
    """
    _ = cls._pk.analyzed_type


def _key_kind_of(cls: type[JSONObject]) -> str:
    """Return the key type of a model that has a key field.

    Args:
        cls: Model class that has a primary key.

    Returns:
        ``"int"``, ``"str"``, or ``"UUID"``.

    Raises:
        TypeError: If the model's key declaration is refused.
        Exception: Whatever resolving a name in the key's type raises, as
            ``_resolve_key()`` raises it.
    """
    kind: str = cls._pk._identifier_kind
    return kind


_UUID_TEXT = re.compile(
    r"(?:[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}"
    r"|\{[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\}"
    r"|urn:uuid:[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}"
    r"|[0-9a-fA-F]{32})"
)
"""The spellings of a ``UUID`` key's text that a key reads, in ASCII hex.

These are the hyphenated, compact, braced, and URN forms. ``uuid.UUID`` reads
more, such as a ``0x`` prefix, a sign, an underscore, hyphens anywhere,
surrounding whitespace, and digits that are not ASCII, so a key of another
spelling is refused rather than read as the UUID that its digits name.
"""


def _build_identifier(kind: str, value: Any) -> int | str:
    """Build the wire scalar of a key from the value that names it.

    The value is the one that the caller or the server gave, before any field
    conversion, so no conversion can map two keys to one. An ``int`` key is an
    ``int`` that is not a ``bool``, so a float and a numeric string are
    refused. A ``str`` key is the text as given. A ``UUID`` key is a
    ``uuid.UUID`` or a string in the hyphenated, compact, braced, or URN form,
    in ASCII hex of either case, and is sent in its lowercase hyphenated form.
    A subclass of ``int``, ``str``, or ``UUID`` gives the plain value, so a key
    has one spelling whatever its class prints.

    Args:
        kind: The key type, as ``_key_kind()`` returns it.
        value: The key, from JSON or from the program.

    Returns:
        The key's wire scalar, an ``int`` or a ``str``.

    Raises:
        TypeError: If the value is not of the key type. The message does not
            quote it.
        ValueError: If a string is not in one of the four forms of a UUID, for
            a ``UUID`` key.
    """
    if kind == "int":
        if isinstance(value, int) and not isinstance(value, bool):
            return int.__index__(value)
    elif kind == "str":
        if isinstance(value, str):
            return str.__str__(value)
    elif isinstance(value, uuid.UUID):
        return str(uuid.UUID(int=value.int))
    elif isinstance(value, str):
        if _UUID_TEXT.fullmatch(value) is None:
            raise ValueError("UUID is not valid")
        return str(uuid.UUID(value))
    else:
        raise TypeError(
            "The key must be a UUID or a JSON str, not a "
            f"{type(value).__name__}"
        )
    raise TypeError(
        f"The key must be a JSON {kind}, not a {type(value).__name__}"
    )


def _build_key(cls: type[JSONObject], value: Any) -> int | str | None:
    """Build a model's key from the value that names it, as every key is built.

    Every identity of a key derives from the result: equality, the hash, the
    URL, a relation's write, the prefetch table, a query that compares the key,
    and a full ``PUT``.

    Args:
        cls: Model whose key the value names.
        value: The key, from JSON or from the program.

    Returns:
        The key's wire scalar, or ``None`` if the value is ``None``.

    Raises:
        TypeError: If the value is not of the model's key type.
        ValueError: If a string is not a UUID for a ``UUID`` key.
        Exception: If the key's type cannot be resolved yet, whatever resolving
            it raises, as a ``NameError`` does for a class that is not defined.
    """
    if value is None:
        return None
    return _build_identifier(_key_kind_of(cls), value)


def _public_key(cls: type[JSONObject], wire: Any) -> Any:
    """Return the value that ``pk`` gives for a key's wire scalar.

    Args:
        cls: Model whose key it is.
        wire: The key's wire scalar, or ``None``.

    Returns:
        A ``UUID`` for a ``UUID`` key, and the wire scalar for any other.
    """
    if wire is not None and _key_kind_of(cls) == "UUID":
        return uuid.UUID(wire)
    return wire


def _related_wire_key(value: JSONObject, model_type: type[Any]) -> Any:
    """Return the wire value of a related object's key, as a relation reads it.

    The relation's model builds the key, as every key is built, so a related
    object whose key that model would not read is refused here and not written.

    Args:
        value: Related object, of ``model_type`` or a subclass that keeps its
            key field.
        model_type: Model that the relation's field names.

    Returns:
        The key's wire value, a string or an integer.

    Raises:
        TypeError: If ``model_type`` cannot build the key from the object's.
        ValueError: If ``model_type`` cannot build the key from the object's,
            as a ``UUID`` key cannot from a ``str`` key that is not a UUID.
    """
    return _build_key(model_type, value._key)


def _json_identity(owner: type[Any], field: str, value: Any) -> str:
    """Return a text that tells a JSON value from every other.

    ``true`` and ``1`` differ, as do ``1`` and ``1.0``, which a set of the
    values would not tell apart.

    Args:
        owner: Class of the parent objects, for the message.
        field: Name of the relation's field, for the message.
        value: A value that the server sent for the relation.

    Returns:
        The value's JSON text.

    Raises:
        FieldConversionError: If the value cannot be a key at all, because it
            is not hashable, as a list is, or is not JSON. It names the field.
    """
    try:
        hash(value)
        return json.dumps(value)
    except (TypeError, ValueError) as err:
        with _converting(owner, field):
            raise ValueError("The key is not a hashable JSON value") from err


def _prefetch_keys(
    owner: type[Any],
    field: str,
    model_type: type[Any],
    values: Iterable[Any],
) -> set[Any]:
    """Build the keys that prefetch hands to ``bulk_get_by_pks()``.

    The related model builds each key, as it does when a parent reads the
    field, so the hook and the bulk request get no key that reading would
    refuse.

    Args:
        owner: Class of the parent objects.
        field: Name of the relation's field.
        model_type: Model that the field names.
        values: The distinct values that the parents hold, as the server sent
            them.

    Returns:
        The keys' wire values.

    Raises:
        FieldConversionError: If a value is not a key that ``model_type``
            reads. It names the relation's field.
    """
    keys: set[Any] = set()
    for value in values:
        with _converting(owner, field):
            keys.add(_build_key(model_type, value))
    return keys


def _bare_wire_key(value: Any, model_type: type[Any]) -> Any:
    """Return the wire value of a key that a relation is given as it is.

    The relation's model builds the key from the value itself, as it builds the
    key of a related object, so a value that model would not read is refused
    here and not written. A mapping is refused, because reading it would take
    it for an embedded object.

    Args:
        value: The key, as ``pk`` of the related model returns it.
        model_type: Model that the relation's field names.

    Returns:
        The key's wire value, a string or an integer.

    Raises:
        TypeError: If the value is a mapping, or ``model_type`` cannot build
            the key from it.
        ValueError: If ``model_type`` cannot build the key from it, as from a
            string that is not a UUID for a ``UUID`` key.
    """
    if isinstance(value, Mapping):
        raise TypeError(
            "A relation takes a related object or its key, not a mapping"
        )
    return _build_key(model_type, value)


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


class _BeforeCreation:
    """The key of the object that ``merge_response()`` reads in ``create()``.

    The response has not been read yet, so the object has no key.
    """

    def __reduce__(self) -> str:
        """Name the one instance, so a copy or a pickle is that instance.

        Returns:
            The module-level name of ``_BEFORE_CREATION``, which ``copy`` and
            ``pickle`` resolve to the same object.
        """
        return "_BEFORE_CREATION"


_BEFORE_CREATION = _BeforeCreation()


def _checked_merge(
    cls: type[Any], merged: Any, resp: Response
) -> dict[str, Any]:
    """Return what ``merge_response()`` gave, if it is a mapping.

    Args:
        cls: The model whose hook was called.
        merged: The hook's result.
        resp: The response to the write.

    Returns:
        The result, which is the object's JSON.

    Raises:
        ResponseShapeError: If the result is not a mapping, as when an override
            returns ``None``, a list, or a string. The error holds the
            response.
    """
    if not isinstance(merged, Mapping):
        raise ResponseShapeError(
            f"{cls.__name__}.merge_response() must return a JSON object, "
            f"got {type(merged).__name__}",
            response=resp,
        )
    return cast("dict[str, Any]", merged)


"""The one key of every object that reads a creation's response."""


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


def _rebuild_object(cls: type[T], wire_key: Any) -> T:
    """Return an object of a class that has its key and no other state.

    A pickle rebuilds an object with this and then restores its state, so the
    object has its key, and hashes, before any other object that holds it is
    rebuilt.

    Args:
        cls: The model.
        wire_key: The wire scalar of the object's key.

    Returns:
        A new object that has only its key.
    """
    obj = object.__new__(cls)
    obj.__dict__["_wire_key"] = wire_key
    return obj


# The lookups whose operands are values of the field, as Django's lookups of
# those names compare them.
_VALUE_LOOKUPS = frozenset(("exact", "in", "range", "gt", "gte", "lt", "lte"))

# The value suffixes that compare one value.
_ONE_VALUE_LOOKUPS = frozenset(("exact", "gt", "gte", "lt", "lte"))


# The kinds of key that a query parameter's name can start with, in the order
# in which one wins over another of the same length.
_KeyKind = typing.Literal["python", "pk", "json", "write"]
_KEY_KINDS = typing.get_args(_KeyKind)


@dataclass(frozen=True)
class _Name:
    """The field that a query parameter's name names, and by which key."""

    field: str
    """The Python name of the field."""

    by: _KeyKind
    """The key that the name starts with: the field's Python name, ``pk`` for
    the model's key, or the field's JSON key or write key."""

    suffix: str | None
    """What follows the key and ``__``, or ``None`` if the name is the key."""


@dataclass(frozen=True)
class _Parameter:
    """What a query parameter's name means, resolved once."""

    wire: str
    """The name that the parameter is sent under."""

    field: str | None
    """The field that its errors name, or ``None`` for a name of no field."""

    conversion: AnalyzedType | None
    """The conversion of each operand, or ``None`` to send it as given."""

    collection: CollectionType | None
    """A collection field's own type, for its own parameter."""

    structured: bool
    """Whether the field writes every value as a list or an object."""

    several: bool
    """Whether it is a lookup that takes several values."""

    own: bool
    """Whether it is a field's own parameter, which a creation sends."""

    key: RelatedType | None = None
    """The relation by which an operand that is no model is built, for the
    model's own key field, so that the key constructor builds it."""

    model: RelatedType | None = None
    """The relation by which an object of the model itself is built, for the
    model's own key field, which gives its own key."""

    one_value: bool = False
    """Whether it is a path through a relation that compares one value, so
    that a collection is refused as for a field's own one-value lookups."""

    key_suffix: str | None = None
    """For a parameter that names the model's own key, the lookup suffix that
    follows the name, empty for the key alone and for ``exact``. Two parameters
    with one suffix name one comparison."""

    named: _Name | None = None
    """The field that the name names, or ``None`` for a name of no field."""


def _distinct_sorted(items: Iterable[Any]) -> list[Any]:
    """Return JSON values in the order of ``_json_order()`` without repeats.

    Args:
        items: JSON values, as a set's items are written.

    Returns:
        The values sorted, with each run of equal values reduced to one.
    """
    distinct: list[Any] = []
    for item in sorted(items, key=_json_order):
        if not distinct or not _json_equal(distinct[-1], item):
            distinct.append(item)
    return distinct


def _lookup_operand_order(value: Any) -> tuple[int, Any]:
    """Return a key that orders the operands of a lookup given a set.

    Args:
        value: Encoded operand, which is not always JSON, since a name that
            names no field is not converted.

    Returns:
        The key of ``_json_order()`` for a JSON scalar, and for any other value
        the key of its ``str()``.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return _json_order(value)
    return _json_order(str(value))


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

    Raises:
        ValueError: If a name is given more than once, since the field would be
            bulk-loaded once for each copy.
    """
    if prefetch is None:
        return ()
    if isinstance(prefetch, str):
        return (prefetch,)

    names = tuple(prefetch)
    for position, name in enumerate(names):
        if name in names[:position]:
            raise ValueError(
                f"prefetch names {name!r} twice. Name each field once"
            )

    return names


T = TypeVar("T", bound="JSONObject")


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


def _field_named(cls: type[Any], name: str) -> JSONProperty | None:
    """Return the JSON-backed field that a class has under a name.

    The class attribute is read without running its descriptor, since any name
    may be given, and ``api`` is a descriptor that raises for a class with no
    registered API.

    Args:
        cls: Model class.
        name: Name to look up.

    Returns:
        The field, or ``None`` if the name is no field of the class.
    """
    try:
        found = inspect.getattr_static(cls, name)
    except AttributeError:
        return None
    return found if isinstance(found, JSONProperty) else None


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


def _class_url_of(cls: type[JSONObject]) -> str:
    """Return the URL of a model's collection.

    Args:
        cls: Model class.

    Returns:
        The class's ``class_url``.

    Raises:
        UsageError: If the class declares no ``class_url``, so it has no
            collection to request. Unlike an ``AttributeError``, it is not
            swallowed by ``hasattr()`` or by ``getattr()`` with a default.
    """
    try:
        return cls.class_url
    except AttributeError:
        raise UsageError(
            f"{cls.__name__} declares no class_url, so it has no URL to "
            'request. Declare it, as class_url = "/things/"'
        ) from None


_KEY_TYPES: Mapping[Any, str] = MappingProxyType(
    {int: "int", str: "str", uuid.UUID: "UUID"}
)
"""The types that a primary key may be, by the name of each."""


def _key_type_error(cls: type[JSONObject], field: str) -> TypeError:
    """Return the refusal of a primary key whose type is not a key type.

    Args:
        cls: Model class that declares the key.
        field: Name of the primary-key field.

    Returns:
        A ``TypeError`` that a conversion reports as a plain one, as decorating
        the class does, not as the conversion of a value.
    """
    error = TypeError(
        f"{cls.__name__}.{field} is the primary key, whose type must be the "
        "one that the service sends for it: a str, an int, or a UUID, alone "
        "or inside T | None, Annotated, or a NewType that no conversion of "
        "the model converts. A conversion may map two of the service's keys "
        "to one value, so a key cannot be converted. Declare the key as the "
        "service's type, and expose another view of it as a property, such "
        "as one that returns Code(self.id)."
    )
    setattr(error, _DECLARATION_ERROR, True)
    return error


def _relation_key_error(
    cls: type[JSONObject], field: str, model_type: type[JSONObject]
) -> TypeError:
    """Return the refusal of a primary key that is a relation.

    Args:
        cls: Model class that declares the key.
        field: Name of the primary-key field.
        model_type: Model that the relation names.

    Returns:
        A ``TypeError`` that a conversion reports as a plain one, as decorating
        the class does, not as the conversion of a value.
    """
    name = model_type.__name__
    related = _pk_property_of(model_type)
    kind = None
    if related is not None:
        try:
            kind = _key_kind(
                analyze_type(related._resolve_type()), _table_of(model_type)
            )
        except Exception:  # noqa: BLE001
            # The message names the type only if it can be resolved now, and
            # the related model's own key is refused where it is used.
            kind = None
    key_type = f"{kind}, " if kind is not None else ""
    error = TypeError(
        f"{cls.__name__}.{field} is the primary key and a relation to "
        f"{name}, and a primary key cannot be a relation. Declare it as "
        f"{key_type}the type of {name}'s key, and expose the related object "
        f"as a property that builds the {name} from the key."
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


def _key_kind(
    analyzed: AnalyzedType, table: Mapping[Any, Conversion]
) -> str | None:
    """Return the key type that a primary key's type declares, if any.

    A key type is ``int``, ``str``, or ``UUID``, which may be optional, and
    inside ``Annotated`` or a ``NewType``. A ``NewType`` that a conversion of
    the model converts is no key type, and neither is ``UUID`` if the model
    replaces eazyrest's conversion of it. Any other type is no key type: a
    conversion of it may map two of the service's keys to one value, as an
    enum's ``_missing_()`` or a timestamp past microseconds does.

    Args:
        analyzed: Analyzed type of a primary key field.
        table: Conversion table of the model that declares the key.

    Returns:
        ``"int"``, ``"str"``, or ``"UUID"``, or ``None`` for any other type.
    """
    while isinstance(analyzed, OptionalType):
        analyzed = analyzed.inner_type
    if not isinstance(analyzed, BaseType):
        return None
    ty: Any = analyzed.base_type
    while True:
        if typing.get_origin(ty) is Annotated:
            ty = typing.get_args(ty)[0]
            continue
        supertype = _supertype(ty)
        if supertype is None:
            break
        if ty in table:
            return None
        ty = supertype
    try:
        kind = _KEY_TYPES.get(ty)
    except TypeError:
        # An unhashable annotation is no key type.
        return None
    if kind == "UUID" and table.get(uuid.UUID) is not DEFAULT_CONVERSIONS.get(
        uuid.UUID
    ):
        return None
    return kind


def _check_key_type(
    cls: type[JSONObject], field: str, analyzed: AnalyzedType
) -> None:
    """Refuse a primary key whose type is not a key type.

    Args:
        cls: Model class whose key it is, which is the class that decorates or
            uses an inherited key, not the base that declares it.
        field: Name of the primary-key field.
        analyzed: Analyzed type of the field.

    Raises:
        TypeError: If the key is a relation, or its type is not one that
            ``_key_kind()`` admits, as a class that Pydantic configures is not.
    """
    related = _related_key_type(analyzed)
    if related is not None:
        raise _relation_key_error(cls, field, related.model_type)
    if _key_kind(analyzed, _table_of(cls)) is None:
        raise _key_type_error(cls, field)


def _refuse_bare_items(value: Any, conversion: AnalyzedType) -> None:
    """Refuse a bare key that was added in place to a collection of relations.

    A collection of relations that a field returns holds related objects, so an
    item that a caller adds to it in place must be one. An assignment takes a
    bare key and reads it back as an object, but a change in place would leave
    the key in the collection.

    Args:
        value: The value that the field returned and the caller may have
            changed in place.
        conversion: The field's analyzed type.

    Raises:
        ValueError: If the field holds a collection of relations and an item is
            not a related object. A sequence's error names the item's position.
            A ``None`` item is refused when the value is encoded.
    """
    # A NewType of a collection of models analyzes as the collection, so no
    # NewType wraps the structure here.
    analyzed = _unwrap_optional_type(conversion)
    if not (
        isinstance(analyzed, CollectionType)
        and isinstance(_unwrap_optional_type(analyzed.item_type), RelatedType)
        and isinstance(value, Collection)
    ):
        return

    def check(item: Any) -> Any:
        if item is not None and not isinstance(item, JSONObject):
            raise ValueError(_BARE_ITEM)
        return item

    if _READ_AS[analyzed.collection_type] in (set, frozenset):
        for item in value:
            check(item)
    else:
        _each_item(check, value)


def _stored_related_key(
    stored: Any, model: type[JSONObject], *, embedded: bool = False
) -> Any:
    """Return the built key of a related object that the JSON holds as a key.

    An embedded object is not a key, so it is returned as it is unless
    ``embedded`` says to read its key: replacing the object that a load or a
    response nested by a key is a change when the other fields of the object
    matter, as they do for any field whose JSON differs from the encoding of
    the new value.

    Args:
        stored: What the stored JSON holds for the relation.
        model: The related model.
        embedded: Whether an embedded object is replaced by the key that it
            holds, as an assignment compares it.

    Returns:
        The key as ``_build_key()`` builds it, so that two spellings of one key
        are equal, or ``stored`` itself if it is not a bare key that can be
        built, as ``null`` is, or an embedded object unless ``embedded``.
    """
    if isinstance(stored, dict):
        prop = _pk_property_of(model)
        if not embedded or prop is None or prop.json_field not in stored:
            return stored
        stored = stored[prop.json_field]
    try:
        built = _build_key(model, stored)
    except (TypeError, ValueError):
        return stored
    return stored if built is None else built


def _require_item_key(item: Any, model: type[JSONObject]) -> None:
    """Refuse an item of a collection of relations that names no key.

    A collection of relations is compared and written by its items' keys, so an
    item is a key, or a JSON object that names its own, and is never ``null``.

    Args:
        item: The item's JSON.
        model: The model that the collection's items relate to.

    Raises:
        ValueError: If the item is ``null``, or an object without the model's
            key or with a key of ``null``.
    """
    if item is None:
        raise ValueError(_NULL_ITEM)
    prop = _pk_property_of(model)
    if (
        isinstance(item, dict)
        and prop is not None
        and item.get(prop.json_field) is None
    ):
        raise ValueError(
            f"The item has no {model.__name__} key, so it is not a "
            f"{model.__name__}. Declare items that have no key as a "
            "TypedDict or a dataclass, which need none"
        )


_NULL_ITEM = (
    "A collection of relations holds relations, so no item is null. Leave "
    "the item out"
)

_BARE_ITEM = (
    "A collection of relations holds related objects, so an item that is "
    "added in place is an object, not a bare key. Add the related object"
)


def _declaration_error(message: str) -> TypeError:
    """Return the error for a field's declaration that is refused.

    Args:
        message: What is refused and what to declare instead.

    Returns:
        A ``TypeError`` that a conversion reports as a plain ``TypeError``, as
        decorating the class does, not as the conversion of a value.
    """
    error = TypeError(message)
    setattr(error, _DECLARATION_ERROR, True)
    return error


def _inside_wrappers(analyzed: AnalyzedType) -> AnalyzedType:
    """Return a type without its optional and ``NewType`` wrappers.

    Args:
        analyzed: Analyzed type metadata.

    Returns:
        The type inside any ``OptionalType`` and ``NewTypeType`` wrappers.
    """
    analyzed = _unwrap_optional_type(analyzed)
    while isinstance(analyzed, NewTypeType):
        analyzed = _unwrap_optional_type(analyzed.inner)
    return analyzed


def _holds_relations(analyzed: CollectionType) -> bool:
    """Return whether a collection holds relations at any depth.

    Args:
        analyzed: A collection's analyzed type.

    Returns:
        Whether its items, or the items of its items, are relations.
    """
    item = _inside_wrappers(analyzed.item_type)
    if isinstance(item, CollectionType):
        return _holds_relations(item)
    return isinstance(item, RelatedType)


def _check_relation_collection(subject: str, analyzed: AnalyzedType) -> None:
    """Refuse a collection of relations that cannot be compared by its keys.

    A change made in place to a collection of relations is found by comparing
    the items' keys, in order for a sequence and without order for a set, so
    the annotation must name one of the two. ``Collection`` and ``Iterable`` do
    not, and ``Iterable`` is left free for a related collection that a query
    loads. Every item has a key, so an item is not optional, and an item is not
    itself a collection of relations.

    Args:
        subject: What is declared, for the message, as ``Post.tags`` for a
            field.
        analyzed: Analyzed type of the field or the annotation.

    Raises:
        TypeError: If the field, inside any optional or ``NewType``, is a
            ``Collection`` or an ``Iterable`` of relations, or its item type is
            optional, or its item is a collection of relations.
    """
    analyzed = _inside_wrappers(analyzed)
    if not isinstance(analyzed, CollectionType):
        return
    item = _inside_wrappers(analyzed.item_type)
    if isinstance(item, CollectionType):
        if _holds_relations(item):
            raise _declaration_error(
                f"{subject} is a collection of collections of "
                "relations. A collection of relations holds relations and "
                "nothing else, so declare one collection, such as a list of "
                "the related model."
            )
        return
    if not isinstance(item, RelatedType):
        return
    model = item.model_type.__name__
    if isinstance(analyzed.item_type, OptionalType):
        raise _declaration_error(
            f"{subject} is a collection of optional {model}, "
            f"but every item has a key, so no item is None. Declare "
            f"{model} as the item type, and the collection optional if the "
            "server may send none."
        )
    kind = analyzed.collection_type
    if kind not in (Collection, Iterable):
        return
    name, article = (
        ("Collection", "a") if kind is Collection else ("Iterable", "an")
    )
    advice = (
        f"a sequence, such as list[{model}], or a set, such as set[{model}]"
        if kind is Collection
        else f"a sequence, such as Sequence[{model}], or a set, such as "
        f"set[{model}]"
    )
    raise _declaration_error(
        f"{subject} is {article} {name} of {model}, which does "
        "not say whether the order of its items counts, so eazyrest cannot "
        f"tell a change made in place from none. Declare {advice}."
    )


def _analyze_field(
    prop: JSONProperty,
    resolved: Any,
    *,
    owner: type[JSONObject],
) -> AnalyzedType:
    """Analyze and check the resolved type of a field.

    Args:
        prop: The field.
        resolved: The field's annotation, resolved.
        owner: The class whose conversions decide what a primary key may be,
            which is the class that is decorated or in use, not the base that
            declares an inherited key.

    Returns:
        The field's analyzed type.

    Raises:
        TypeError: If the annotation is ``None`` as a whole, or the type is
            refused, such as a model without a key, or a primary key that is
            not a ``str``, an ``int``, or a ``UUID``.
        Exception: If a name in a type alias is not defined yet, whatever
            evaluating it raises.
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
        _check_key_type(owner, prop.field, analyzed)
    _check_relation_collection(f"{prop.cls.__name__}.{prop.field}", analyzed)
    return analyzed


def _field_type(prop: JSONProperty) -> AnalyzedType:
    """Resolve, analyze, and check a field's type: the one compilation path.

    Args:
        prop: The field.

    Returns:
        The field's analyzed type.

    Raises:
        TypeError: If the type is refused, as ``_analyze_field()`` describes.
        Exception: If the annotation cannot be resolved, as for a class that is
            not defined yet, whatever resolving it raises.
    """
    return _analyze_field(prop, prop._resolve_type(), owner=prop._owner)


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
            analyzed = _analyze_field(prop, resolved, owner=cls)
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
            a model without a primary-key field, or a primary key whose type is
            not ``int``, ``str``, or ``UUID``, alone or optional. The first
            field in declaration order raises, and then the primary key.
    """
    own = [*declared]
    props = [*own]
    pk = _pk_property_of(cls)
    if pk is not None and pk not in props:
        props.append(pk)
    for prop in props:
        _refuse_declared_type(cls, prop)
    # An inherited key is checked as the class's own above, and is bound like
    # any other inherited field, so that its first use reads ``cls``'s table.
    for prop in _inherited_fields_with_new_table(cls, own):
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
    _check_relation_collection("The annotation", analyzed)
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
        ValueError: If ``value`` holds a float that is not finite, which JSON
            cannot write, or holds itself, or nests more deeply than Python can
            check.
    """
    try:
        _walk_json(value, path)
    except RecursionError:
        raise ValueError(
            f"The value{_at(path)} holds itself or nests too deeply to be JSON"
        ) from None


def _walk_json(value: Any, path: str) -> None:
    """Check each part of a value, which may recurse without bound.

    Args:
        value: Value to check.
        path: Where ``value`` is, as ``_check_json()`` takes it.

    Raises:
        TypeError: If ``value`` holds a value of another type, or a mapping key
            that is not a string.
        ValueError: If ``value`` holds a float that is not finite.
        RecursionError: If ``value`` holds itself, or nests more deeply than
            Python can check.
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
            _walk_json(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(
                    f"The value{_at(path)} has a key that is not a string, "
                    "which JSON objects cannot have"
                )
            _walk_json(item, f"{path}[key]")
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
            class keeps the primary key it inherits, and a redeclaration of
            that field must have the base's type. A class that inherits none
            uses a field named ``id`` declared on the class. The guide's "What
            a key may be" says which types a key may have.
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
            dataclass. Also raised if a field is a ``Collection`` or an
            ``Iterable`` of a model, alone or inside an optional or a
            ``NewType``, which does not say whether order counts, or a
            collection of models whose item type is optional or is itself a
            collection of models. A class defined later raises when the field
            is first used. A key whose type is not yet defined raises when the
            key is first used. Also raised if the class redeclares the key
            field that it inherits with another type, such as a ``NewType`` or
            an optional of the base's type. Also raised if the key's type is
            not one that a key may have, such as a relation, a ``float``, or a
            class that Pydantic configures. The message names the types that a
            key may be.
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
                field, a ``Collection`` or an ``Iterable`` of a model, or a
                collection of models with an optional item or an item that is a
                collection of models. Also if the key's type is not one that a
                key may have.
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
        above = _key_declared_above(cls, pk_field)
        if above is not None:
            for prop in declared:
                if prop.field == pk_field:
                    _check_redeclared_key(cls, above, prop)
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


def _key_declared_above(
    cls: type[JSONObject], field: str
) -> JSONProperty | None:
    """Return the nearest declaration of a key field in the bases of a class.

    A base that chose another key field still has the field, as an ordinary
    one, so only a declaration that was the key counts, wherever in the bases
    it is. A class that chooses the field as its key again, after a base chose
    another, then has the key's type that its bases gave the field.

    Args:
        cls: The class that is decorated.
        field: The name of the class's key field.

    Returns:
        The declaration of ``field`` that was a primary key in the nearest base
        that declares it so, or ``None``.
    """
    for base in cls.__mro__[1:]:
        declared = vars(base).get(field)
        if isinstance(declared, JSONProperty) and declared.is_primary_key:
            return declared
    return None


def _annotation_text(annotation: Any) -> str:
    """Return an annotation as it reads in source, for a message.

    Args:
        annotation: A declared type, or its text if it was written as a string.

    Returns:
        The text itself, a class's name, or the representation of a generic
        form such as ``Optional[int]`` without its module.
    """
    if isinstance(annotation, str):
        return annotation
    if typing.get_origin(annotation) is None and hasattr(
        annotation, "__name__"
    ):
        return str(annotation.__name__)
    return repr(annotation).replace("typing.", "")


def _check_redeclared_key(
    cls: type[JSONObject], inherited: JSONProperty, prop: JSONProperty
) -> None:
    """Refuse a subclass that redeclares its key field with another type.

    The subclass's objects are written to a relation to the base, and matched
    by prefetch, by the base's key, so a key of another type would name the
    base's resources by keys that the base does not have.

    Args:
        cls: The subclass that is decorated.
        inherited: The nearest declaration of that key field in the bases.
        prop: The subclass's own declaration of that field.

    Raises:
        TypeError: If the two annotations differ. A name that is not defined
            yet in either is compared as it is written.
    """
    try:
        base_type = inherited._resolve_type()
        own_type = prop._resolve_type()
    except Exception:  # noqa: BLE001
        # A name that is not defined yet cannot be resolved, so the annotations
        # are compared as they are written.
        base_type = _annotations(inherited.cls).get(inherited.field)
        own_type = _annotations(cls).get(prop.field)
    if own_type == base_type:
        return
    shown = _annotation_text(
        _annotations(inherited.cls).get(inherited.field, base_type)
    )
    error = TypeError(
        f"{cls.__name__}.{prop.field} redeclares the primary key that "
        f"{inherited.cls.__name__} declares as {shown}, with another type. A "
        f"subclass keeps its base's key, so declare {prop.field} as {shown}, "
        "or leave it out."
    )
    setattr(error, _DECLARATION_ERROR, True)
    raise error


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
    found = _conversion_of(ty, table)
    if found is not None:
        return _schema_of_entry(found.entry, found.declared, mode)
    ty = _final_supertype(ty)
    if typing.get_origin(ty) is Literal:
        return {
            "enum": [
                _literal_json(member, table) for member in typing.get_args(ty)
            ]
        }
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
            The part, with each ``$ref`` to a renamed definition rewritten, and
            each reference in a ``discriminator``'s ``mapping``. A bare schema
            name in a mapping is left as it is.
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
                elif key == "discriminator" and isinstance(item, dict):
                    result[key] = {
                        name: (
                            {tag: reference(ref) for tag, ref in part.items()}
                            if name == "mapping" and isinstance(part, dict)
                            else part
                        )
                        for name, part in item.items()
                    }
                else:
                    result[key] = rewrite(item)
            return result
        if isinstance(value, (list, tuple)):
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
        name = _definition_name(token)
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
    if isinstance(value, (list, tuple)):
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
        The schema, in the JSON Schema 2020-12 vocabulary, which does not allow
        ``prefixItems`` to be empty, so an array of no items has none.
    """
    schema: dict[str, Any] = {"type": "array"}
    if items:
        schema["prefixItems"] = items
    schema.update(
        {"items": False, "minItems": len(items), "maxItems": len(items)}
    )
    return schema


def _key_schema(cls: type[JSONObject]) -> dict[str, Any]:
    """Return the JSON schema of the key that a model's builder sends.

    Args:
        cls: Model that has a key field.

    Returns:
        ``integer`` for an ``int`` key, ``string`` for a ``str`` key, and
        ``string`` with the format ``uuid`` for a ``UUID`` key, in reads and
        writes alike. A key is never ``null``, even if its annotation is
        optional.
    """
    match _key_kind_of(cls):
        case "int":
            return {"type": "integer"}
        case "str":
            return {"type": "string"}
    return {"type": "string", "format": "uuid"}


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
        case RelatedType(model_type):
            pk = model_type._pk
            key = _key_schema(model_type)
            # A read also accepts the related object embedded as JSON, which
            # names its key, as every object of the model has one.
            if mode == "read":
                return {
                    "anyOf": [
                        key,
                        {
                            "type": "object",
                            "required": [pk.json_field],
                            "properties": {pk.json_field: key},
                        },
                    ]
                }
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


def _at_position(err: FieldConversionError, index: int) -> Exception:
    """Add an item's position to the cause of a field's conversion error.

    The error already names the field, so the position goes after it, and the
    field is named once.

    Args:
        err: Error that converting an item of a collection field raised.
        index: The item's position.

    Returns:
        ``err`` if it has no cause to carry the position. Otherwise an error
        for the same field whose cause has the position before its message.
    """
    cause = err.__cause__
    if not isinstance(cause, (TypeError, ValueError)):
        return err
    detail = str(err).removeprefix(f"{err.model.__name__}.{err.field}: ")
    kind = TypeError if isinstance(cause, TypeError) else ValueError
    indexed = kind(f"[{index}]: {detail}")
    indexed.__cause__ = cause
    error = FieldConversionError(err.model, err.field, indexed)
    error.__cause__ = indexed
    return error


def _each_item(
    convert: Callable[[Any], Any],
    items: Iterable[Any],
    *,
    positioned: bool = True,
) -> list[Any]:
    """Convert each item of a collection, naming its position in an error.

    One ``try`` covers the loop, so a long collection pays for naming the
    position only when an item fails.

    Args:
        convert: Function that converts one item.
        items: The items.
        positioned: Whether the items have positions. A set's iteration order
            varies from one run to the next, so an error for one of its items
            names none.

    Returns:
        The converted items, in order.

    Raises:
        TypeError: If converting an item raises a ``TypeError``. A refusal of a
            model's declaration is raised as it is, without the position, and
            so is a ``FieldConversionError``, which names its field.
        ValueError: If converting an item raises a ``ValueError`` that is
            not a ``TypeError``.
    """
    converted: list[Any] = []
    index = 0
    try:
        # The except clauses read index.
        for index, item in enumerate(items):  # noqa: B007
            converted.append(convert(item))
    except FieldConversionError as err:
        if not positioned:
            raise
        # It names its field, and a second conversion would name it again, so
        # the position goes into its message.
        positioned_error = _at_position(err, index)
        raise positioned_error from positioned_error.__cause__
    except TypeError as err:
        if not positioned or _passes_through(err):
            raise
        raise TypeError(f"[{index}]: {err}") from err
    except ValueError as err:
        if not positioned or _passes_through(err):
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

    def _same_as_stored(
        self,
        stored: Any,
        new: Any,
        *,
        by_key: bool = False,
        embedded: bool = False,
    ) -> bool:
        """Return whether a newly encoded value matches the stored JSON.

        The server may list a set's items in any order, while an encoded set
        lists them in one fixed order, so a set field compares without regard
        to order. Only the field's own set is compared so, whether the field is
        optional or a ``NewType`` of a set. A set nested in another collection
        or a mapping compares in order.

        Args:
            stored: JSON value that the object holds for this field.
            new: Encoded value that is being assigned.
            by_key: Whether a related object that the stored JSON holds as a
                bare key is compared by its built key, so that another spelling
                of the key is the same key. Otherwise the stored JSON is
                compared as it is.
            embedded: Whether a related object that the stored JSON embeds is
                compared by the key that it holds too, as an assignment does.

        Returns:
            Whether the values are equal, as ``_json_equal()`` compares them,
            after sorting a stored set as an encoded one is sorted.
        """
        comparable = self._comparable(stored, by_key=by_key, embedded=embedded)
        return _json_equal(comparable, new)

    def _comparable(
        self, stored: Any, *, by_key: bool, embedded: bool = False
    ) -> Any:
        """Return stored JSON in the form in which it is compared.

        A set field's items are sorted, as an encoded set's are. With
        ``by_key``, a related object that the JSON holds as a bare key is
        replaced by its built key, so that two spellings of one key compare
        equal. An embedded object is kept as it is, unless ``embedded``.

        Args:
            stored: What the stored JSON holds for this field.
            by_key: Whether to build the keys of related objects.
            embedded: Whether an embedded object is replaced by its key.

        Returns:
            The value to compare.
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
        if by_key and isinstance(conversion, RelatedType):
            stored = _stored_related_key(
                stored, conversion.model_type, embedded=embedded
            )
        elif isinstance(conversion, CollectionType) and isinstance(
            stored, list
        ):
            item = _unwrap_optional_type(conversion.item_type)
            if by_key and isinstance(item, RelatedType):
                stored = [
                    _stored_related_key(
                        value, item.model_type, embedded=embedded
                    )
                    for value in stored
                ]
            if _READ_AS[conversion.collection_type] in (set, frozenset):
                stored = _distinct_sorted(stored)
        return stored

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

    def _is_key_of_class(self, cls: type[JSONObject]) -> bool:
        """Return whether this property is the primary key of a class.

        Args:
            cls: The class that owns this property, or a subclass.

        Returns:
            Whether this property is ``cls``'s key.
        """
        return _pk_property_of(cls) is self

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

        This is the one place that reads a field's type, so every use of the
        field raises the same errors. Nothing is cached when it raises, so the
        field reads once what it names is defined.

        Raises:
            NameError: If a field's annotation, or a type alias in it, names a
                class that is not defined. It is raised as it is, for a primary
                key too.
            AttributeError: If a primary key's type, or the key type of a model
                that it relates to, names an attribute that a module does not
                have yet. It is raised as it is.
            TypeError: If a primary key's type is refused, such as one that is
                not a ``str``, an ``int``, or a ``UUID``, or holds a string in
                a type alias's value that cannot be resolved. It is raised as
                it is, as is the refusal of a model's declaration, which is
                marked as one.
            FieldConversionError: For every other failure to analyze or resolve
                the type, with the original error as its ``__cause__``, such as
                a ``TypedDict`` that is closed or has extra items, a member of
                a structure that names a class that exists only for type
                checkers, a dotted annotation whose module lacks the attribute
                yet, or a member that is refused when the field is first used.
        """
        try:
            analyzed = _field_type(self)
        except Exception as err:  # noqa: BLE001
            self._refuse_analysis(err)
        try:
            # Decoration could not check a member that names something not
            # defined yet, so check it, once, now that the field is in use.
            _check_admitted(analyzed, _table_of(self._owner), late=True)
        except Exception as err:  # noqa: BLE001
            self._refuse_analysis(err)
        return analyzed

    def _refuse_analysis(self, err: Exception) -> NoReturn:
        """Raise what reading this field's type raises for a failure.

        Args:
            err: What analyzing or resolving the type raised.

        Raises:
            Exception: ``err`` itself if the documented errors of a field at
                first use include it: a ``NameError``, the refusal of a model's
                declaration, and, for a primary key, what resolving its type
                raised.
            FieldConversionError: Otherwise, naming the model and the field,
                with ``err`` as its cause.
        """
        if (
            isinstance(err, FieldConversionError | NameError)
            or _passes_through(err)
            or (
                self.is_primary_key
                and (
                    isinstance(err, AttributeError)
                    or (
                        isinstance(err, TypeError)
                        and not _refuses_key_type(err)
                    )
                )
            )
        ):
            raise err
        raise FieldConversionError(self._owner, self.field, err) from err

    @cached_property
    def _identifier_kind(self) -> str:
        """Return the key type of a primary key, once its type resolves.

        Returns:
            ``"int"``, ``"str"``, or ``"UUID"``, which selects how the key is
            built.

        Raises:
            NameError: As ``analyzed_type`` raises it.
            AttributeError: As ``analyzed_type`` raises it.
            TypeError: As ``analyzed_type`` raises it.
            FieldConversionError: As ``analyzed_type`` raises it. Nothing is
                cached when any of them is raised.
        """
        kind = _key_kind(self.analyzed_type, _table_of(self._owner))
        # Analyzing the key's type refused every type that is no key type.
        assert kind is not None
        return kind

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
        bound.__dict__.pop("_identifier_kind", None)
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

        if self._is_key_of(obj):
            # The object has its key from construction, which the JSON's key
            # names, so reading it needs no load.
            return obj._key_value()

        raw = obj._working_value(self.json_field)
        if raw is _MISSING:
            raw = obj._json_having(self.json_field)
            if raw is _MISSING:
                raise _missing_field_error(obj, self.field, self.json_field)

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
            value, (*_UNCHANGEABLE_VALUES, JSONObject)
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
        # Convert the value before anything is read from the server, so that a
        # value that is refused sends no request, not even the load's.
        with _converting(type(obj), self.field):
            new_value = obj._to_json(value, self.analyzed_type)
            # Build the collection that the field keeps before anything is sent
            # or queued, so that a value it cannot hold, such as an object that
            # has no hash for a set, writes nothing.
            kept = self._assigned_related(value)
        stored = obj._json_with_field(self.json_field, self.field)

        # A pending update is not on the server, even though the working JSON
        # holds it. In eager mode, assigning the same value must send it.
        unsent = (
            obj.write_mode == "eager"
            and self.json_field in obj._pending_updates
        )
        # An embedded related object is the same as its key, so assigning the
        # object that the field holds sends nothing.
        unchanged = not unsent and self._same_as_stored(
            stored, new_value, by_key=True, embedded=True
        )
        if not unchanged:
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
        if not unchanged and not self._same_as_stored(
            final, new_value, by_key=True
        ):
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
        """Check that an assignment to an object's key keeps its key.

        An object has its key from construction, and it cannot change, so
        assigning the same key does nothing, and any other key is refused.

        Args:
            obj: Object whose key is assigned.
            value: The key, as a value of the key's type or its JSON.

        Raises:
            AttributeError: If the value names another key than the object's.
            FieldConversionError: If the value is not of the key's type, such
                as a model object, a mapping, or ``"7"`` for an ``int`` key.
        """
        with _converting(type(obj), self.field):
            named = _build_key(type(obj), value)
        if named == obj._key:
            return
        name = type(obj).__name__
        raise AttributeError(
            f"The key of a {name} object cannot change, so "
            f"{name}.{self.field} cannot be assigned another value. Name "
            f"another object as {name}({self.field}=...)"
        )

    def _assigned_related(self, value: Any) -> Any:
        """Return the field value that keeps assigned related objects.

        Args:
            value: Value assigned to this field.

        Returns:
            ``value``, if it is a related object for a related field. For a
            collection of related objects, a new collection of the field's type
            that holds the same objects. ``_MISSING`` for any other value,
            which the field reads from its JSON instead.
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
            # A one-shot iterable was used up by encoding it.
            or not isinstance(value, Collection)
            or isinstance(value, (str, bytes, Mapping))
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

    Every object of a model with a key field has its key from construction,
    given to the constructor or read from its JSON, and a constructor without
    one raises. Objects compare by key, as Django models do. Two objects of
    exactly the same model are equal if they have the same key, built as the
    scalar that is sent for it, and they hash alike. An object of a model
    without a key field equals only itself.

    The key never changes, so the hash never does either. Another spelling of a
    ``UUID`` key, such as its uppercase text, is the same key and is taken. A
    key of type ``int`` or ``str`` refuses a value of another JSON type where
    it is given, so ``User(id="1")`` raises ``FieldConversionError`` for an
    ``int`` key. Server state that names another key, from a load, a write's
    response, or ``update_from_json()``, is refused with
    ``ResponseShapeError``. Assigning the key field or ``pk`` a value that
    names another key raises ``AttributeError``, before anything is sent. A key
    is a ``str``, an ``int``, or a ``UUID``, so it cannot be changed in place.
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

    prefetch_batch_size: ClassVar[int | None] = 100
    """Maximum number of parent objects to prefetch in one batch.

    A query with ``prefetch=`` refuses a value that is not an ``int``, a
    ``bool`` among them, with ``TypeError``, and one below ``1`` with
    ``ValueError``, before it sends a request. ``None`` reads the whole
    collection as one batch.
    """

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

    _wire_key: Any
    """The wire scalar of the object's primary key, which never changes.

    The constructor builds it, from the key that it is given or from the key
    in its JSON, and nothing changes it afterward. It is ``None`` only for an
    object of a model without a key field, and ``_BEFORE_CREATION`` for the
    object that ``create()`` gives ``merge_response()``. ``_key`` reads it.
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
            **kwargs: Field values. Without ``json``, they include the primary
                key, by its field's name or as ``pk``. Every other field value
                is an assignment, as after construction: it loads the object if
                ``json`` is omitted, and in ``"eager"`` mode it sends a
                ``PATCH``. A model without a primary-key field takes none.

        Raises:
            TypeError: If the model has a key field, ``json`` is omitted, and
                no key is given, or ``None`` is. The message names the key
                field and points to ``create()``, which makes a new resource.
            FieldConversionError: If the key that is given, or the key in
                ``json``, is not of the key's type, or ``json`` lacks the key
                or holds ``null`` for it.
            UsageError: If field values are given for a model without a
                primary-key field. Its objects are read-only values, built with
                ``json=``.
        """
        # A class's api can be assigned after the class is defined.
        _require_decorated(type(self))
        _check_api_attribute(type(self), UsageError)
        self._wire_key = None
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

        # The key comes first, because assigning any other field loads the
        # object from its URL, which the key names.
        pk_property = _pk_property_of(type(self))
        if pk_property is not None:
            self._wire_key = self._initial_key(pk_property, kwargs)
        for name, value in kwargs.items():
            setattr(self, name, value)

    def _initial_key(
        self, prop: JSONProperty, kwargs: Mapping[str, Any]
    ) -> int | str:
        """Build the key that a new object has from construction.

        Args:
            prop: The model's key field.
            kwargs: The field values that the constructor was given.

        Returns:
            The key's wire scalar, from the JSON if the object has JSON, and
            otherwise from the first key in ``kwargs``, by its field's name or
            as ``pk``. Another key in ``kwargs`` is assigned afterward, as any
            field is.

        Raises:
            TypeError: If the object has no JSON and ``kwargs`` gives no key.
            FieldConversionError: If the key is not of the key's type, or the
                JSON lacks the key or holds ``null`` for it.
            Exception: If the key's type cannot be resolved yet, whatever
                resolving it raises: a ``NameError`` for a class that is not
                defined, an ``AttributeError`` for a module's attribute that is
                not set, and a ``TypeError`` for a string in a type alias that
                names nothing. A key's type that is refused once it resolves
                raises its ``TypeError``.
        """
        cls = type(self)
        if self._json is not None:
            with _converting(cls, prop.field):
                if self._json.get(prop.json_field) is None:
                    held = "null" if prop.json_field in self._json else "no"
                    raise ValueError(
                        f"The JSON holds {held} key, so it names no "
                        f"{cls.__name__}"
                    )
                given = self._json[prop.json_field]
        else:
            given = next(
                (
                    value
                    for name, value in kwargs.items()
                    if name in ("pk", prop.field)
                ),
                None,
            )
            if given is None:
                raise TypeError(
                    f"A {cls.__name__} object names a resource by its key, "
                    f"so it needs {cls.__name__}.{prop.field}, as in "
                    f"{cls.__name__}({prop.field}=...), or JSON that holds "
                    f"it. Call create() to make a new {cls.__name__}."
                )
        # Resolving the type raises what the name raises, which is no
        # conversion of the key.
        _resolve_key(cls)
        with _converting(cls, prop.field):
            key = _build_key(cls, given)
        # The key was not None, and the builder gives a scalar for any other.
        assert key is not None
        return key

    def __copy__(self) -> Self:
        """Return a copy that shares only the API with this object.

        A copy is made as ``copy.deepcopy()`` makes one, so the related
        objects, the values read before the copy, the pending updates, and the
        JSON are the copy's own. An assignment or a change made in place to one
        object is neither read from nor sent by the other.

        Returns:
            A new object of the same class.
        """
        return copy.deepcopy(self)

    def __deepcopy__(self, memo: dict[int, Any]) -> Self:
        """Return a deep copy that shares only the API with this object.

        The API that the object was given as ``api=`` is shared, as the API
        that the class registers is, because a client holds a session, which a
        copy of the object has no reason to own. A related object that holds
        the same API shares it too.

        Args:
            memo: The copies that ``copy.deepcopy()`` has made so far.

        Returns:
            A new object of the same class.
        """
        copied = object.__new__(type(self))
        memo[id(self)] = copied
        state = vars(copied)
        for name, value in self.__getstate__().items():
            state[name] = (
                value
                if name == "_api_override"
                else copy.deepcopy(value, memo)
            )
        return copied

    def __getstate__(self) -> dict[str, Any]:
        """Return the state that a copy or a pickle keeps.

        The options of a ``write_options()`` block belong to the block, so a
        copy or a pickle made inside one does not hold them.

        Returns:
            The object's attributes, without the write options.
        """
        return {
            name: value
            for name, value in vars(self).items()
            if name not in ("_write_options", "_write_blocks")
        }

    def __reduce_ex__(self, protocol: SupportsIndex) -> tuple[Any, ...]:
        """Return what a pickle holds, with the key before the state.

        An object is rebuilt with its key first, so that it hashes as soon as
        it exists. A set of objects that hold each other hashes one of them
        before its state is restored.

        Args:
            protocol: The pickle protocol.

        Returns:
            The reconstructor with the class and the key, and the state.
        """
        return (
            _rebuild_object,
            (type(self), self._wire_key),
            self.__getstate__(),
        )

    def __setstate__(self, state: dict[str, Any]) -> None:
        """Restore the state of a pickled or copied object.

        Args:
            state: The attributes that the object had when it was pickled.

        Raises:
            UsageError: If the model has a key field and the state holds no
                key, as the state of a pickle written before the object kept
                its key does not.
        """
        if _pk_property_of(type(self)) is not None and (
            "_wire_key" not in state
        ):
            raise UsageError(
                f"The state of this {type(self).__name__} object holds no "
                "key, so it cannot be restored. Pickle the object again, or "
                "read it from the server."
            )
        self.__dict__.update(state)
        self.__dict__.setdefault("_wire_key", None)

    def __eq__(self, other: object) -> bool:
        """Return whether another object is this object's resource.

        Two objects are equal if they are the same object, or are of exactly
        the same model and have the same canonical key. A subclass is not equal
        to its base. The API, the JSON, and the field values are not compared.

        The canonical key is the key that the object was built with, as the
        scalar that is sent for it: the text of a ``str`` key, the integer of
        an ``int`` key, and the lowercase hyphenated text of a ``UUID`` key. So
        two spellings of one ``UUID`` are one key, and two ``str`` keys that
        differ in case are two. Equal objects have the same ``url``, unless
        their class overrides ``url``.

        An object of a model without a key field equals only itself. Comparing
        sends no request and never reads ``url``.

        Args:
            other: Object to compare with.

        Returns:
            Whether the objects are equal, or ``NotImplemented`` if ``other``
            is not a model object.

        Raises:
            UsageError: For the object that ``merge_response()`` reads during
                ``create()``, which has no key yet.
        """
        if not isinstance(other, JSONObject):
            return NotImplemented
        if self is other:
            return True
        if (
            type(self) is not type(other)
            or _pk_property_of(type(self)) is None
        ):
            return False
        return bool(self._key == other._key)

    def __hash__(self) -> int:
        """Return a hash that agrees with equality.

        The hash is that of the model and the canonical key, which never
        changes. A spelling of the same key does not change it, and server
        state that names another key is refused. An object of a model without a
        key field hashes by identity. Hashing sends no request.

        Returns:
            The hash.

        Raises:
            UsageError: For the object that ``merge_response()`` reads during
                ``create()``, which has no key yet.
        """
        if _pk_property_of(type(self)) is None:
            return object.__hash__(self)
        return hash((type(self), self._key))

    def __repr__(self) -> str:
        """Return the class and primary key without sending a request.

        The key is shown as its JSON value, whether or not the object has
        loaded, as in ``<User id=1>``: the ``int`` or the text that is sent for
        it, so a ``UUID`` key shows its lowercase hyphenated text. The object
        that ``merge_response()`` reads during ``create()`` has no key yet and
        shows ``...``.

        Returns:
            The class name and primary key in angle brackets, or the class name
            alone, as in ``<User>``, if the class has no primary key.
        """
        name = type(self).__name__
        pk_property: JSONProperty | None = getattr(type(self), "_pk", None)
        if pk_property is None:
            return f"<{name}>"
        key = self._wire_key
        if key is _BEFORE_CREATION:
            return f"<{name} {pk_property.field}=...>"
        return f"<{name} {pk_property.field}={key!r}>"

    @property
    def _key(self) -> Any:
        """Return the wire scalar of the object's primary key.

        Returns:
            The key, or ``None`` for an object of a model without a key field.

        Raises:
            UsageError: For the object that ``merge_response()`` reads during
                ``create()``, which has no key yet.
        """
        key = self._wire_key
        cls = type(self)
        if key is _BEFORE_CREATION:
            raise UsageError(
                f"The {cls.__name__} object that merge_response() reads "
                "during create() has no key yet, so its pk, url, hash, and "
                "comparison with another object of its model cannot be used. "
                "Read what the hook needs from the response."
            )
        return key

    def _set_prefetched_related(
        self,
        prefetched_related: Mapping[str, Mapping[Any, JSONObject]],
    ) -> None:
        """Attach prefetched related-object mappings to this instance.

        Args:
            prefetched_related: Mapping from each prefetched field name to the
                related objects loaded for it, keyed by their canonical key,
                the ``int`` or the text that is sent for it.
        """
        # Copy the outer mapping so each parent can drop an entire prefetched
        # field cache independently while still sharing the related objects.
        self._prefetched_related = dict(prefetched_related)

    def _forget_fields(self, fields: Iterable[str] | None = None) -> None:
        """Drop the cached state of fields, so the next read decodes anew.

        A field's cached value, the encoding of it that detects a change made
        in place, and its prefetched related objects go together, since a value
        that outlives the others would be read stale or saved wrongly.

        Args:
            fields: Python attribute names of the fields to drop, or ``None``
                for every field.
        """
        if fields is None:
            self._field_cache.clear()
            self._field_snapshots.clear()
            self._prefetched_related.clear()
            return

        for field in fields:
            self._field_cache.pop(field, None)
            self._field_snapshots.pop(field, None)
            self._prefetched_related.pop(field, None)

    def _check_embedded_key(
        self, arg: dict[str, Any], ty: type[JSONObject], field: str
    ) -> None:
        """Refuse an embedded related object whose key is not a key.

        The related model builds the key, as every key is built. A key that
        cannot be built is an error of the relation's field, not an unknown
        identity, since the server's JSON for a relation names the related
        object. A key whose type is not defined yet is not refused now.

        Args:
            arg: JSON of the embedded related object.
            ty: Related model.
            field: Name of the relation's field, which the error names.

        Raises:
            FieldConversionError: If the embedded object lacks the key, holds
                ``null`` for it, or holds a key that is not of the related
                model's key type.
        """
        prop = _pk_property_of(ty)
        if prop is None:
            return
        try:
            _resolve_key(ty)
        except Exception as err:
            if getattr(err, _DECLARATION_ERROR, False):
                raise
            return
        with _converting(type(self), field):
            if arg.get(prop.json_field) is None:
                raise ValueError(
                    f"The embedded {ty.__name__} object has no key, so it "
                    f"names no {ty.__name__}"
                )
            _build_key(ty, arg[prop.json_field])

    def _create_related(
        self,
        arg: Any,
        ty: type[T],
        *,
        field: str | None = None,
    ) -> T:
        """Create a related object from embedded JSON or a key.

        Args:
            arg: Either a JSON mapping for eager object creation, or a primary
                key value for lazy lookup.
            ty: Related ``JSONObject`` subclass type.
            field: Optional owning field name for prefetch lookup and for the
                error that a wrong key raises.

        Returns:
            Instance of ``ty``.

        Raises:
            UsageError: If a class in the MRO of ``ty`` had ``api`` assigned.
            FieldConversionError: If a key is of another JSON type than the
                related model's key. It names ``field`` when that is given.
        """
        # Refuse an assigned ``api`` before the object is built.
        _check_api_attribute(ty, UsageError)
        # Pass on only an instance override. Without one, the related object
        # uses the API registered for its own class.
        api = self._api_override
        # If the argument is a dict, we treat it as JSON.
        if isinstance(arg, dict):
            # An embedded object is read as the value of a field.
            assert field is not None
            self._check_embedded_key(arg, ty, field)
            return ty(
                json=_OwnedJSON(arg), api=api, write_mode=self.write_mode
            )

        try:
            with _converting(ty, ty._pk.field):
                key = _build_key(ty, arg)
        except FieldConversionError as err:
            # The server's value is the relation field's, so the error names
            # that field and not the related model's key.
            if field is None or err.__cause__ is None:
                raise
            raise FieldConversionError(
                type(self), field, cast(Exception, err.__cause__)
            ) from err.__cause__
        if field is not None and field in self._prefetched_related:
            prefetched = self._prefetched_related[field].get(key)
            if prefetched is not None:
                return cast(T, prefetched)

        return ty(pk=key, api=api, write_mode=self.write_mode)

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
        cls,
        where: str,
        value: Any,
        conversion: AnalyzedType,
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
                if (literal := _literal_of(base_type)) is not None:
                    return _literal_member(
                        literal, value, _table_of(type(self))
                    )

                # Changing the returned value in place must not change the
                # object's JSON without recording an update.
                return _copy_json(value)

            case RelatedType(model_type):
                return self._create_related(value, model_type, field=field)

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

                related = _unwrap_optional_type(item_type)

                def read_item(item: Any) -> Any:
                    if isinstance(related, RelatedType):
                        _require_item_key(item, related.model_type)
                    return self._from_json(item, item_type, field=field)

                converted_items = _each_item(read_item, value)

                return _READ_AS[collection_type](converted_items)

            case _:
                raise TypeError(
                    f"Unsupported analyzed type {type(conversion).__name__}"
                )

    @classmethod
    def _to_json(
        cls,
        value: Any,
        conversion: AnalyzedType,
    ) -> Any:
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
            case RelatedType(model_type):
                if isinstance(value, JSONObject):
                    _check_related_class(value, model_type)
                    _check_related_key(value, model_type)
                    return _related_wire_key(value, model_type)

                return _bare_wire_key(value, model_type)
            case BaseType(base_type):
                found = _conversion_of(base_type, _table_of(cls))
                # A subclass of the key reads as the key's type, such as a
                # plain datetime for a subclass of datetime, which must write
                # back.
                if found is not None and _encodes(value, found):
                    return _checked_json(
                        found.entry.encode(value, found.declared)
                    )

                if _literal_of(base_type) is not None and isinstance(
                    value, enum.Enum
                ):
                    value = _literal_json(value, _table_of(cls))

                # A value that is not JSON must fail here, naming the field,
                # rather than in json.dumps() when it is sent.
                return _checked_json(value)
            case CollectionType(collection_type, item_type):
                if not _is_collection_value(value) or (
                    isinstance(value, JSONObject)
                    and isinstance(item_type, RelatedType)
                ):
                    # A model that defines __iter__ is iterable, but its
                    # members are not the items of a collection of models.
                    raise TypeError(
                        "Collection field values must be collections, "
                        f"got {type(value).__name__}"
                    )

                relates = isinstance(
                    _unwrap_optional_type(item_type), RelatedType
                )

                def write_item(item: Any) -> Any:
                    if relates and item is None:
                        raise ValueError(_NULL_ITEM)
                    return cls._to_json(item, item_type)

                is_set = _READ_AS[collection_type] in (set, frozenset)
                items = _each_item(write_item, value, positioned=not is_set)
                if is_set:
                    # A set's iteration order depends on how it was built, and
                    # a string's hash on the process, so a set writes its items
                    # sorted by their JSON. Equal sets then write equal JSON,
                    # and replacing one with another is not a change. Two
                    # members can write one JSON value, as objects of two
                    # classes that have one key do, so each value is once.
                    items.sort(key=_json_order)
                    items = [
                        item
                        for index, item in enumerate(items)
                        if index == 0
                        or not _json_equal(items[index - 1], item)
                    ]
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
                        f"[{name!r}]",
                        getattr(value, name),
                        item_type,
                    )
                    for name, item_type, _ in conversion.fields
                }
            case UnionType():
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
                empty or a dot segment.
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
            UsageError: When a block ends while a block of the same object that
                began later is still open, as two generators that each hold a
                block open can end in the order they began. The ended block's
                options are gone from the object by then, and the other block's
                own keep applying until it ends, and then no options remain.
        """
        checked = _request_options(options)
        merged = _merge_request_options(self._write_options, checked)
        # An object that has no options of its own reads the class's, so the
        # last block must leave it without any, as it found it.
        blocks: list[Mapping[str, Any]] = vars(self).setdefault(
            "_write_blocks", []
        )
        blocks.append(merged)
        self._write_options = merged
        try:
            yield self
        finally:
            out_of_order = blocks[-1] is not merged
            del blocks[[block is merged for block in blocks].index(True)]
            if blocks:
                self._write_options = blocks[-1]
            else:
                del self._write_options
                del vars(self)["_write_blocks"]
            if out_of_order:
                raise UsageError(
                    f"A write_options() block of this {type(self).__name__} "
                    "ended while a block that began later is still open. End "
                    "blocks in the reverse of the order that they began, as "
                    "nested with statements do"
                )

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
            TypeError: If a payload value cannot be a form field, a file is
                named by the primary key under any of its names, or a file's
                form field is one that the payload already writes.
        """
        wire = cls._to_wire(payload)
        if not files:
            return {"json": wire}
        form = {name: _form_value(name, value) for name, value in wire.items()}
        key_property = _pk_property_of(cls)
        key_names = (
            frozenset()
            if key_property is None
            else frozenset(
                {
                    "pk",
                    key_property.field,
                    key_property.json_field,
                    key_property.write_field,
                }
            )
        )
        uploads = {}
        for name, file in files.items():
            if name in key_names:
                raise TypeError(
                    f"The upload {name!r} names the primary key of "
                    f"{cls.__name__}, which a request cannot upload. Give "
                    "the key as a field value, or leave it out."
                )
            descriptor = _field_named(cls, name)
            key = name if descriptor is None else descriptor.write_field
            if key in uploads:
                raise TypeError(f"Multiple files write the form key {key}")
            if key in form:
                raise TypeError(
                    f"The upload {name!r} writes the form key {key}, which "
                    "a field of the request already writes."
                )
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

        Every caller has refused an object of a model without a key, which is a
        read-only value that can never be written, before it asks.

        Returns:
            Each changed field, its descriptor, and the value's new encoding.

        Raises:
            FieldConversionError: If a changed value cannot be encoded.
        """
        changed: list[tuple[str, JSONProperty, Any]] = []
        for field, snapshot in self._field_snapshots.items():
            descriptor = getattr(type(self), field)
            current = self._field_cache[field]
            with _converting(type(self), field):
                _refuse_bare_items(current, descriptor.analyzed_type)
                encoded = self._to_json(current, descriptor.analyzed_type)
            if _json_equal(encoded, snapshot):
                continue
            changed.append((field, descriptor, encoded))
        return changed

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

        A cached value comes from a read or an assignment, and both load the
        object first, so the updates are made to the object that the server
        has.

        Raises:
            FieldConversionError: If a changed value cannot be encoded.
        """
        self._queue_changes(self._changes_in_place())

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
            json = self._readable_json(self._merged_response(resp, {}, {}))
            if json:
                self._replace_json(json, clear_pending=True)
                self._json_is_partial = True
            else:
                self._pending_updates.clear()
        else:
            self._replace_json(
                self._merged_response(resp, self.json, sent),
                clear_pending=True,
            )

    def _merged_response(
        self,
        resp: requests.Response,
        current: Mapping[str, Any],
        sent: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Return what ``merge_response()`` gives for a write's response.

        Args:
            resp: Successful response to the write.
            current: The object's JSON before the write.
            sent: JSON fields that the write sent.

        Returns:
            The object's JSON after the write.

        Raises:
            ResponseShapeError: If the response is an empty list or a list of
                several objects, or ``merge_response()`` returns something that
                is not a mapping. Its ``response`` is the response, and nothing
                has changed. Any other error of ``merge_response()`` reaches
                the caller unchanged.
        """
        return _checked_merge(
            type(self), self.merge_response(resp, current, sent), resp
        )

    def _refuse_other_key(self, key: Any) -> None:
        """Refuse server state that names another key than the object's.

        An object's key never changes, so its hash does not either. A spelling
        of the same key is the same key.

        Args:
            key: The JSON that the server's state holds for the key field.

        Raises:
            ResponseShapeError: If the state names no key, as ``null`` does, or
                a key that is not of the key's type, or another key. Nothing
                has changed.
        """
        try:
            named = _build_key(type(self), key)
        except (TypeError, ValueError):
            named = None
        if named == self._key:
            return
        raise ResponseShapeError(
            "Incoming JSON primary key does not match this "
            f"{type(self).__name__} object, whose key cannot change"
        )

    def _replace_json(
        self, json: dict[str, Any], *, clear_pending: bool = False
    ) -> None:
        """Replace the server's JSON that this object keeps.

        The key of an object never changes, so JSON that names another key is
        refused before anything changes. No pending update holds the key, so
        the key of this JSON is the server's.

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
                object's. The object is unchanged.
        """
        before = self._working_json()
        json = self._readable_json(json)
        if _pk_property_of(type(self)) is not None:
            key: str = type(self)._pk.json_field
            if key in json:
                self._refuse_other_key(json[key])
        after = self._laid_over(json, {} if clear_pending else None)
        # Decide what to drop before anything changes, so that no error can
        # leave the caches behind the JSON.
        stale = []
        for field in getattr(self, "_json_fields", ()):
            prop = getattr(type(self), field)
            old = before.get(prop.json_field, _MISSING)
            new = after.get(prop.json_field, _MISSING)
            if _json_equal(old, new):
                continue
            try:
                # Another spelling of one key is no change.
                same = _json_equal(
                    prop._comparable(old, by_key=True),
                    prop._comparable(new, by_key=True),
                )
            except (NameError, AttributeError, TypeError):
                # A type that cannot be resolved yet cannot be compared by key.
                # Its field has no cached value, since reading one needs the
                # type, so there is nothing to drop.
                continue
            if not same:
                stale.append(field)

        self._json = json
        if clear_pending:
            self._pending_updates.clear()
        self._forget_fields(stale)

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
        unless the body holds it. The body may change any field, but not the
        primary key. A body that names another key makes the object raise
        ``ResponseShapeError`` when it records the body.

        For ``create()``, there is no object yet, because the response has not
        named its key, so ``self`` is an object of the class with no key and no
        JSON, which only this call sees. Its ``api`` and its class's hooks may
        be used. Its ``pk``, its key field, ``url``, and ``hash()`` must not
        be, and nor must ``==`` with another object of its model: they raise
        ``UsageError``. It equals itself. ``create()`` then builds the object
        that it returns from the result, which must hold the key, or
        ``create()`` raises ``ResponseShapeError``.

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
            json.update(self._object_from_response(self.api.json(resp), resp))

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
                nothing is pending, or a class had ``api`` assigned. Nothing is
                sent, and the pending updates stay.
            ValueError: If the object has no URL, because its primary key is
                empty or a dot segment.
            ResponseShapeError: If the response body does not hold a JSON
                object, as when it is an empty list or a list of several
                objects, or names another primary key than the object has. The
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
            if descriptor._is_key_of(self):
                # The key is sent as it was built, as every write sends it.
                payload[descriptor.json_field] = self._key
                continue
            conversion = descriptor.analyzed_type
            with _converting(type(self), field):
                payload[descriptor.json_field] = self._to_json(
                    self._from_json(
                        json[descriptor.json_field], conversion, field=field
                    ),
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
        JSON fields are discarded. A payload may lack the key field, but one
        that has it must name the object's own key. A relation field whose JSON
        the payload leaves unchanged keeps its related objects, with their
        unsaved changes, as a load does.

        If this object has not been loaded, the payload becomes its JSON
        without a request. Reading a field that the payload lacks then loads
        the full object.

        Args:
            json: Full or partial JSON object for this instance. The object
                keeps a deep copy, so changing the mapping or its nested values
                later does not change the object.

        Raises:
            ResponseShapeError: If the payload has the key field and names
                another key than the object's, or ``null``, or a key that is
                not of the key's type, so that the payload does not name this
                object. It is a ``ValueError``, and nothing is merged.
        """
        payload = copy.deepcopy(dict(json))
        if _pk_property_of(type(self)) is None:
            # An object without a key has no key to compare.
            self._merge_json(payload)
            return

        # A payload without the key names no other object.
        if self._pk_json_field in payload:
            self._refuse_other_key(payload[self._pk_json_field])

        if self._json is None:
            self._json = {self._pk_json_field: self._key}
            self._json_is_partial = True

        self._merge_json(payload)

    def _merge_json(self, payload: dict[str, Any]) -> None:
        """Merge authoritative JSON members into this object's JSON.

        The members replace those of the same keys, and discard the cached
        values and pending updates of their fields. A field of related objects
        whose JSON the payload leaves unchanged is not dropped, as a load
        leaves it, so that a related object keeps its unsaved changes. A change
        made in place to the field's own collection, or a pending update of the
        field, still goes.

        Args:
            payload: JSON members to merge.
        """
        assert self._json is not None
        payload = self._readable_json(payload)
        named = [
            field
            for field in self._json_fields
            if getattr(type(self), field).json_field in payload
        ]
        # Decide what to drop before anything changes, so that no error can
        # leave the caches behind the JSON.
        stale = [
            field
            for field in named
            if not self._keeps_related_objects(field, payload)
        ]
        self._json.update(payload)
        self._forget_fields(stale)
        for field in named:
            self._pending_updates.pop(
                getattr(type(self), field).json_field, None
            )

    def _keeps_related_objects(
        self, field: str, payload: Mapping[str, Any]
    ) -> bool:
        """Return whether a merge leaves a field's cached objects as they are.

        Args:
            field: Python attribute name of a field that the payload names.
            payload: The JSON that the merge applies.

        Returns:
            Whether the field holds related objects that are cached, its JSON
            is unchanged as a load would compare it, it has no pending update,
            and its collection was not changed in place.
        """
        prop: JSONProperty = getattr(type(self), field)
        if (
            field not in self._field_cache
            or prop.json_field in self._pending_updates
        ):
            return False
        assert self._json is not None
        try:
            if not _holds_model(prop.analyzed_type):
                return False
            old = self._json.get(prop.json_field, _MISSING)
            if not _json_equal(
                prop._comparable(old, by_key=True),
                prop._comparable(payload[prop.json_field], by_key=True),
            ):
                return False
            snapshot = self._field_snapshots.get(field, _MISSING)
            if snapshot is _MISSING:
                return True
            with _converting(type(self), field):
                current = self._field_cache[field]
                _refuse_bare_items(current, prop.analyzed_type)
                encoded = self._to_json(current, prop.analyzed_type)
            return _json_equal(encoded, snapshot)
        except (NameError, AttributeError, TypeError):
            # A type that cannot be resolved yet, or a value that cannot be
            # encoded, cannot be compared by key, so the field is dropped.
            return False

    def refresh(self) -> None:
        """Mark this object stale and discard pending local updates.

        The key does not change, so the object keeps its equality, its hash,
        and its ``url``.

        Raises:
            UsageError: If the model has no key field. Such an object has
                nothing to reload.
            ValueError: If the object has no URL to reload from, because its
                primary key is empty or a dot segment. Nothing is discarded
                then. Before 1.0.0, an object with an empty key was reset
                without an error.
        """
        _refuse_keyless_request(type(self), "refresh")
        # An object without a URL cannot reload, so refuse before discarding
        # its state.
        _ = self.url
        # The key stays, so the object still has its key without its JSON.
        # Discard unsaved local changes
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

        A model without a key field has the key ``None``. A ``UUID`` key is a
        ``UUID``, and any other key is the ``int`` or ``str`` that is sent.
        """
        pk_property: JSONProperty | None = getattr(type(self), "_pk", None)
        if pk_property is None:
            return None
        return pk_property.__get__(self, type(self))

    @pk.setter
    def pk(self, value: Any) -> None:
        """Set this object's primary key value.

        An object has its key from construction, and it never changes, so the
        assignment only checks the value.

        Args:
            value: Primary-key value. The assignment sends no request.

        Raises:
            AttributeError: If the model has no key field, or the value names
                another key than the object's. Assigning the key that the
                object has does nothing, in any spelling of it.
            FieldConversionError: If the value is not of the key's type, as a
                model object, a mapping, or ``"7"`` for an ``int`` key is not.
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

    def _key_value(self) -> Any:
        """Return the key that ``pk`` gives.

        Returns:
            The key, as ``_public_key()`` gives it, or ``None`` for an object
            of a model without a key field.
        """
        return _public_key(type(self), self._key)

    @property
    def url(self) -> str:
        """Return the resource URL for this object.

        The primary key is written as it is sent and percent-encoded, so it
        always forms a single path segment.

        Returns:
            Relative URL for this object. It ends with ``/`` unless the API's
            ``trailing_slash`` is ``False``.

        Raises:
            ValueError: If the primary key is empty or a dot segment, or the
                class has no primary-key field. The URL would name another
                resource, such as the collection.
        """
        if getattr(type(self), "_pk", None) is None:
            raise ValueError(
                f"{type(self).__name__} has no primary-key field, so it has "
                "no URL."
            )
        pk = self._key
        pk_text = str(pk)
        if pk is None or pk_text in ("", ".", ".."):
            raise ValueError(
                f"{type(self).__name__} object has primary key {pk!r}, "
                "so it has no URL"
            )

        collection = _class_url_of(type(self)).rstrip("/")
        url = f"{collection}/{quote(pk_text, safe='')}"

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

        return self._laid_over(self._json, None)

    def _laid_over(
        self, json: Mapping[str, Any], pending: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        """Return JSON with pending updates laid over it.

        Args:
            json: The server's JSON.
            pending: The pending updates to lay over it, or ``None`` for this
                object's own.

        Returns:
            A new mapping, without write-only fields, whose values are the
            object's own.
        """
        if pending is None:
            pending = self._pending_updates
        result = dict(json)
        if pending:
            hidden = self._write_only_keys()
            result.update(
                (key, value)
                for key, value in pending.items()
                if key not in hidden
            )
        return result

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
            ResponseShapeError: If the response does not hold a JSON object, as
                when it is an empty list or a list of several objects, or names
                another primary key than the object has.
        """
        # Reloading drops cached values, so queue their in-place changes first.
        self._queue_changes(self._changes_in_place())
        resp = self._request_api().get(self.url)
        json = self._object_from_response(self.api.json(resp), resp)

        # Some APIs leave the primary key out of a detail response, because the
        # URL holds it. Keep the key that the object was loaded by, so that the
        # object keeps its identity and URL. Its JSON keeps the spelling that
        # the server gave, as a created object's JSON does, which the built key
        # may not have, as a UUID in capitals.
        pk_json_field = getattr(type(self), "_pk_json_field", None)
        if pk_json_field is not None and pk_json_field not in json:
            if self._json is not None and pk_json_field in self._json:
                json[pk_json_field] = self._json[pk_json_field]
            else:
                json[pk_json_field] = self._key

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
            # the server has this field, so load the full object.
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
        cls,
        fields: Mapping[str, Any],
        *,
        query: bool = False,
    ) -> dict[str, Any]:
        """Encode field values as JSON object members.

        Args:
            fields: Values keyed by Python attribute name for JSON-backed
                fields, or by JSON key for other values.
            query: Whether to encode a query, whose related values use keys.

        Returns:
            JSON values keyed by JSON key. JSON-backed fields are renamed by
            ``field_map`` and converted by ``_to_json()``. The key, by its
            field's name, by ``pk``, or by its JSON or write key, is built as
            every key is. Other values are unchanged.

        Raises:
            AttributeError: If a value names a read-only field, the key among
                them, by any of its names.
            TypeError: If two values name one field, or a name is the key's
                JSON or write key and also another field's.
            FieldConversionError: If a value cannot be converted, as a key that
                is not of the key's type cannot, or the key is ``None`` and the
                fields are not a query's.
        """
        payload: dict[str, Any] = {}
        names: dict[str, str] = {}
        key_property = _pk_property_of(cls)

        for given, value in fields.items():
            field = cls._created_field(given, key_property)
            descriptor = _field_named(cls, field)
            key = field if descriptor is None else descriptor.json_field
            if key in payload:
                raise TypeError(
                    f"Multiple fields use the JSON key {key}: {names[key]} "
                    f"and {given}"
                )
            names[key] = given
            if descriptor is not None:
                if descriptor.read_only and not query:
                    raise AttributeError(
                        f"{cls.__name__}.{field} is read-only"
                    )
                with _converting(cls, field):
                    if descriptor is key_property:
                        if value is None and not query:
                            raise ValueError(
                                "A new object's key cannot be None. Leave "
                                "the key out to let the server assign it"
                            )
                        # The key is sent as it is built, under its write key,
                        # and kept under its JSON key for the response merge.
                        payload[key] = _build_key(cls, value)
                    else:
                        conversion = descriptor.analyzed_type
                        payload[key] = cls._to_json(value, conversion)
            else:
                payload[key] = value

        return payload

    @classmethod
    def _created_field(
        cls, name: str, key_property: JSONProperty | None
    ) -> str:
        """Return the field that a name given to ``create()`` sets.

        A field's Python name sets that field, and comes first. ``pk`` and the
        key's JSON and write keys set the key, whose read-only policy and
        builder then apply, as they do to the key field's own name. Any other
        name is an extra, sent as it is.

        Args:
            name: The name, as a keyword argument or a key of ``fields``.
            key_property: The model's key field, if it has one.

        Returns:
            The Python name of the field, or ``name`` for an extra.

        Raises:
            TypeError: If the name is the key's JSON or write key and also the
                JSON or write key of another field, so that it names no one
                field.
        """
        if key_property is None or _field_named(cls, name) is not None:
            return name
        if name == "pk":
            return key_property.field
        if name not in (key_property.json_field, key_property.write_field):
            return name
        others = sorted(
            field
            for field in getattr(cls, "_json_fields", ())
            if field != key_property.field
            and name
            in (
                (descriptor := getattr(cls, field)).json_field,
                descriptor.write_field,
            )
        )
        if others:
            raise TypeError(
                f"{cls.__name__}.create() was given {name}, which names the "
                f"key {key_property.field} and also "
                f"{', '.join(others)}. Pass each by its Python name."
            )
        return key_property.field

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
    def _structure_of(cls, name: str) -> AnalyzedType | None:
        """Return the structure that a field writes every value as.

        Such a field's own parameter takes no value, since requests would send
        the list as separate values and an object as its keys. The caller must
        encode the value as the API expects.

        Args:
            name: Name of a JSON-backed field.

        Returns:
            The field's analyzed type, inside any optional or ``NewType``, if
            it is a fixed-length tuple, a named tuple, a dataclass, a
            ``TypedDict``, or a mapping, and not a collection of items, and no
            conversion of the model converts it instead. ``None`` for any other
            field.
        """
        descriptor = getattr(cls, name)
        if cls._query_collection(descriptor) is not None:
            return None
        table = _table_of(cls)
        analyzed = descriptor.analyzed_type
        while True:
            analyzed = _unwrap_optional_type(analyzed)
            if _class_entry(analyzed, table) is not None:
                # The conversion decides what the field writes.
                return None
            if not isinstance(analyzed, NewTypeType):
                break
            analyzed = analyzed.inner
        if isinstance(
            analyzed,
            (
                FixedTupleType,
                NamedTupleType,
                DataclassType,
                TypedDictType,
                MappingType,
            ),
        ):
            return analyzed
        return None

    @classmethod
    def _writes_structure(cls, name: str) -> bool:
        """Return whether a field writes every value as a list or an object.

        Args:
            name: Name of a JSON-backed field.

        Returns:
            Whether ``_structure_of()`` finds a structure for the field.
        """
        return cls._structure_of(name) is not None

    @classmethod
    def _takes_whole(
        cls, conversion: AnalyzedType, value: Any, *, structured: bool
    ) -> bool:
        """Return whether a conversion takes a value as one operand.

        Args:
            conversion: The conversion of each operand.
            value: Query value.
            structured: Whether the field writes every value as a list or an
                object, so that any value is one.

        Returns:
            Whether ``value`` is one operand even if it is iterable: a model
            object, or a value that a registered conversion encodes.
        """
        if isinstance(value, JSONObject):
            # A model object is one value for any parameter, and never its
            # members, whatever it iterates as.
            return True
        conversion = _unwrap_optional_type(conversion)
        if isinstance(conversion, RelatedType):
            return False
        if structured:
            return True
        found = _class_entry(conversion, _table_of(cls))
        if found is None and isinstance(conversion, BaseType):
            found = _conversion_of(conversion.base_type, _table_of(cls))
        return found is not None and _encodes(value, found)

    @classmethod
    def _converts_whole(
        cls,
        conversion: AnalyzedType | None,
        value: Any,
        structured: bool,
    ) -> bool:
        """Return whether a conversion that takes a value whole converts it.

        A field that writes every value as a list or an object takes any value
        whole, but only a value that it can encode is one operand of it.

        Args:
            conversion: The conversion of each operand.
            value: Query value that the conversion takes whole.
            structured: Whether the field writes every value as a list or an
                object.

        Returns:
            Whether the conversion encodes ``value``.
        """
        if not structured or conversion is None:
            return True
        try:
            cls._to_json(value, conversion)
        except (TypeError, ValueError):
            return False
        return True

    @classmethod
    def _takes_whole_value(cls, name: str, value: Any) -> bool:
        """Return whether a parameter's conversion takes a value whole.

        Args:
            name: Name of the parameter.
            value: Query value.

        Returns:
            Whether the query would send ``value`` as one operand, or refuse it
            for a lookup of several values, rather than read its items.
        """
        parameter = cls._parameter(name)
        if parameter.conversion is None:
            # A name that is no field sends a model as its key, so a model that
            # can be iterated is still one operand.
            return isinstance(value, JSONObject)
        return cls._takes_whole(
            parameter.conversion,
            value,
            structured=parameter.structured and not parameter.several,
        )

    @classmethod
    def _resolve_name(cls, name: str) -> _Name | None:
        """Resolve a query parameter's name to the field that it names.

        A name starts with a key of a field, either the whole name or followed
        by ``__`` and a suffix. The keys of a field are its Python name, its
        JSON key, and its write key, and ``pk`` is a key of the model's key
        field. Of the keys that the name starts with, the longest wins, so that
        a key that holds ``__`` is found whole. Of equal length, the Python
        name wins over ``pk``, ``pk`` over a JSON key, and a JSON key over a
        write key. Every check of a parameter's name reads this result.

        Args:
            name: Name of the parameter.

        Returns:
            The field, the kind of key, and the suffix after it, or ``None`` if
            the name starts with no key of a field. A field named ``type_``
            gives ``type___in`` as ``type_`` and ``in``.
        """
        keys: list[tuple[str, _Name]] = []
        for field in getattr(cls, "_json_fields", ()):
            descriptor = getattr(cls, field)
            keys.append((field, _Name(field, "python", None)))
            keys.append((descriptor.json_field, _Name(field, "json", None)))
            keys.append((descriptor.write_field, _Name(field, "write", None)))
        key_field = _pk_property_of(cls)
        if key_field is not None:
            keys.append(("pk", _Name(key_field.field, "pk", None)))
        found: tuple[tuple[int, int], _Name] | None = None
        for key, candidate in keys:
            if name == key:
                suffix = None
            elif name.startswith(f"{key}__"):
                suffix = name[len(key) + 2 :]
            else:
                continue
            rank = (len(key), -_KEY_KINDS.index(candidate.by))
            if found is None or rank > found[0]:
                found = (rank, dataclasses.replace(candidate, suffix=suffix))
        return None if found is None else found[1]

    @classmethod
    def _field_parameter(
        cls,
        field: str,
        wire: str,
        *,
        several: bool,
        own: bool,
        suffix: str = "",
    ) -> _Parameter:
        """Resolve a parameter that compares the values of a field.

        Args:
            field: Python name of the field.
            wire: The name that the parameter is sent under.
            several: Whether it is a lookup that takes several values.
            own: Whether it is the field's own parameter.
            suffix: The lookup suffix after the field's name, empty for the
                field's own parameter.

        Returns:
            The parameter. The model's key field builds each operand that is no
            model with the key constructor, in its own parameter and in a
            lookup on it.

        Raises:
            FieldConversionError: If the field's type cannot be resolved.
        """
        descriptor = getattr(cls, field)
        with _converting(cls, field):
            structured = cls._writes_structure(field)
            collection = cls._query_collection(descriptor)
            analyzed = (
                descriptor.analyzed_type
                if collection is None
                else collection.item_type
            )
            key = None
            model = None
            key_suffix = None
            if descriptor is _pk_property_of(cls):
                key_suffix = "" if suffix == "exact" else suffix
                model = RelatedType(cls)
                key = model
        return _Parameter(
            wire,
            field,
            analyzed,
            collection if own else None,
            structured,
            several,
            own,
            key=key,
            model=model,
            key_suffix=key_suffix,
        )

    @classmethod
    def _parameter(cls, name: str) -> _Parameter:
        """Resolve a query parameter's name, once, to what its operands are.

        A field's own name compares the field's values. A lookup on a field,
        ``<field>__<suffix>``, compares the field's values if the suffix is a
        value suffix. Any other name, such as a path through a relation, or a
        suffix that does not compare values, compares something else, and its
        operands are sent as given, a model as its key. A field's JSON key and
        write key are ordinary names, sent as given, but a collection for one
        that compares one value is refused as it is for the field's Python
        name, and the model's key is compared by them as by ``pk``.

        Args:
            name: Name of the parameter.

        Returns:
            The parameter.

        Raises:
            FieldConversionError: If the type of the field that the parameter
                compares cannot be resolved.
        """
        several = name.endswith(("__in", "__range"))
        named = cls._resolve_name(name)
        if named is None:
            return _Parameter(name, None, None, None, False, several, False)
        descriptor = getattr(cls, named.field)
        suffix = named.suffix
        compares = suffix is None or suffix in _VALUE_LOOKUPS
        if named.by == "python":
            wire = (
                descriptor.json_field
                if suffix is None
                else f"{descriptor.json_field}__{suffix}"
            )
            if compares:
                return dataclasses.replace(
                    cls._compared_parameter(named, wire, several), named=named
                )
        elif named.by == "pk":
            if compares:
                return dataclasses.replace(
                    cls._compared_parameter(named, name, several), named=named
                )
            wire = name
        else:
            # A JSON key and a write key are sent as given, and the model's key
            # is still compared by them.
            wire = name
        key_suffix = None
        if compares and descriptor is _pk_property_of(cls):
            key_suffix = "" if suffix in (None, "exact") else suffix
        if suffix is None:
            one = named.by in ("json", "write") and not cls._holds_items(
                descriptor
            )
        else:
            one = (
                named.by in ("json", "write") and suffix in _ONE_VALUE_LOOKUPS
            ) or cls._compares_one_value_through(named.field, suffix)
        return _Parameter(
            wire,
            None,
            None,
            None,
            False,
            several,
            False,
            one_value=one,
            key_suffix=key_suffix,
            named=named,
        )

    @classmethod
    def _compared_parameter(
        cls, named: _Name, wire: str, several: bool
    ) -> _Parameter:
        """Return the parameter that compares a field's values by a name.

        Args:
            named: The field and the suffix, which is a value suffix or none.
            wire: The name that the parameter is sent under.
            several: Whether the name is a lookup that takes several values.

        Returns:
            The parameter that the field's Python name, or ``pk`` for its key,
            gives with that suffix, whatever key named the field.

        Raises:
            FieldConversionError: If the field's type cannot be resolved.
        """
        own = named.suffix is None and named.by != "pk"
        return cls._field_parameter(
            named.field,
            wire,
            several=several and named.suffix is not None,
            own=own,
            suffix=named.suffix or "",
        )

    @classmethod
    def _holds_items(cls, descriptor: JSONProperty) -> bool:
        """Return whether a field is a collection of items.

        Args:
            descriptor: The field.

        Returns:
            Whether the field's type is a collection of items, whose own
            parameter takes a collection. ``False`` for a field whose type
            cannot be resolved yet, which a name sent as given does not need.
        """
        try:
            return cls._query_collection(descriptor) is not None
        except (FieldConversionError, NameError):
            return False

    @classmethod
    def _compares_one_value_through(cls, field: str, rest: str) -> bool:
        """Return whether a path through a relation compares one value.

        Args:
            field: Python name of the field that the path starts with.
            rest: The rest of the path, after the field's name and ``__``.

        Returns:
            Whether the field is a relation, or a collection of them, and the
            path ends with a suffix that compares one value, or is the related
            model's key, so that a collection given for it names no one value.
        """
        segments = rest.split("__")
        if segments[-1] in _ONE_VALUE_LOOKUPS:
            one = True
        elif len(segments) == 1:
            one = segments[0] == "pk"
        else:
            return False
        try:
            analyzed = _unwrap_optional_type(getattr(cls, field).analyzed_type)
        except (FieldConversionError, NameError):
            # A lookup that compares no value of the field needs no type of it,
            # so a field that cannot be resolved yet does not stop it.
            return False
        if isinstance(analyzed, CollectionType):
            analyzed = _unwrap_optional_type(analyzed.item_type)
        if not isinstance(analyzed, RelatedType):
            return False
        if not one:
            prop = _pk_property_of(analyzed.model_type)
            one = prop is not None and segments[0] in (
                prop.field,
                prop.json_field,
            )
        return one

    @classmethod
    def _query_operands(
        cls, name: str, value: Any, style: str = "comma"
    ) -> list[Any]:
        """Return the operands that a query sends for a parameter.

        A value that the parameter's conversion takes as a whole is one
        operand, even if the conversion writes it as a list. Otherwise an
        iterable that is not a string, bytes, or a mapping is read once and
        gives an operand for each of its items. A parameter that compares the
        values of a field encodes its operands as the field's own parameter
        does. One that compares a key builds each operand with the key
        constructor. Any other parameter is not converted, except that a model
        is its primary key.

        Args:
            name: Name of the parameter.
            value: Query value.
            style: How a lookup that takes several values sends them,
                ``"comma"`` or ``"repeat"``.

        Returns:
            A nonempty list of scalars, which requests sends as one parameter
            repeated. A lookup that takes several values gives one string of
            its operands joined with commas, under ``"comma"``.

        Raises:
            ValueError: If the value is a collection and the parameter compares
                one value of a field, as ``exact``, the bare name, ``gt``,
                ``gte``, ``lt``, and ``lte`` do, unless the field's conversion
                takes it whole or it is a collection field's own parameter.
                Also if the value is a mapping or ``None``, an empty
                collection, or holds ``None`` or another collection, which
                requests would omit, flatten, or send as its keys, or is one
                value that its field's conversion writes as a list, which
                requests would send as separate values, so the query would
                ignore or change its constraint. Also if a lookup that takes
                several values is given anything but a collection, a
                ``__range`` is given other than two operands, or an operand's
                text is empty, or holds a comma under ``"comma"``.
            FieldConversionError: If the parameter's conversion refuses an
                operand.
        """
        parameter = cls._parameter(name)
        guard = (
            contextlib.nullcontext()
            if parameter.field is None
            else _converting(cls, parameter.field)
        )
        whole = cls._takes_whole_value(name, value)
        several = parameter.several
        if (
            not several
            and parameter.field is not None
            and parameter.collection is None
            and _is_collection_value(value)
            and not (
                whole
                and cls._converts_whole(
                    parameter.conversion, value, parameter.structured
                )
            )
        ):
            # A server applies one value of a repeated parameter and ignores
            # the others, so a query for several would change its meaning.
            raise ValueError(
                f"Query parameter {name} for {cls.__name__} cannot be sent, "
                "because it compares one value and its value is a "
                "collection, which requests would send as the parameter "
                "repeated, and a server applies only one of those. Pass one "
                "value, or use a lookup that takes several, such as "
                f"{parameter.field}__in"
            )
        if parameter.one_value and _is_collection_value(value) and not whole:
            head, _, tail = name.rpartition("__")
            several_name = head if tail in _ONE_VALUE_LOOKUPS else name
            raise ValueError(
                f"Query parameter {name} for {cls.__name__} cannot be sent, "
                "because it compares one value and its value is a "
                "collection, which requests would send as the parameter "
                "repeated, and a server applies only one of those. Pass one "
                "value, or use a lookup that takes several, such as "
                f"{several_name}__in"
            )
        if several and (
            isinstance(value, JSONObject)
            or not _is_collection_value(value)
            or whole
        ):
            raise ValueError(
                f"Query parameter {name} for {cls.__name__} cannot be sent, "
                "because it takes several values and its value is not a "
                "collection of them. Pass a list"
            )

        def convert(item: Any) -> Any:
            conversion = parameter.conversion
            if conversion is not None:
                if parameter.key is not None and isinstance(item, JSONObject):
                    _check_own_key_object(item, cls)
                if (
                    parameter.model is not None
                    and isinstance(item, JSONObject)
                    and (parameter.key is not None or isinstance(item, cls))
                ):
                    # An object gives the key that its class builds, or is
                    # refused as another model.
                    conversion = parameter.model
                elif (
                    parameter.key is not None
                    and item is not None
                    and not isinstance(item, (JSONObject, Mapping))
                    and not _is_collection_value(item)
                ):
                    # A value that no key could be is left to the checks of
                    # what a query can send.
                    conversion = parameter.key
                return _query_value(cls._to_json(item, conversion))
            if not isinstance(item, JSONObject):
                return item
            return item._key

        with guard:
            if (
                whole
                or value is None
                or isinstance(value, (str, bytes, Mapping))
                or not isinstance(value, Iterable)
            ):
                operands, repeated = [convert(value)], False
            else:
                operands, repeated = _each_item(convert, value), True
                if parameter.collection is not None and _READ_AS[
                    parameter.collection.collection_type
                ] in (set, frozenset):
                    operands = _distinct_sorted(operands)
                elif isinstance(value, AbstractSet) and not several:
                    # A set has no order, so the same query would otherwise
                    # give another URL in another process. Any set is one, a
                    # dict's key view and a class of the program's own among
                    # them.
                    operands.sort(key=_lookup_operand_order)
        problem = None
        if not repeated and operands[0] is None:
            problem = "is None, and requests omits it"
        elif not repeated and isinstance(operands[0], Mapping):
            problem = "is a mapping, and requests sends only its keys"
        elif not repeated and isinstance(operands[0], list):
            problem = (
                "is one value that the field's conversion writes as a list, "
                "which requests would send as separate values, and a server "
                "cannot tell those from a filter on several values"
            )
        elif not operands:
            problem = "is an empty collection, and requests omits it"
        elif any(operand is None for operand in operands):
            problem = "contains None, and requests omits that item"
        elif any(
            isinstance(operand, Mapping) or _is_collection_value(operand)
            for operand in operands
        ):
            problem = "contains a collection, and requests flattens it"
        elif several and name.endswith("__range") and len(operands) != 2:
            problem = (
                f"holds {len(operands)} items, and a range takes two, "
                "its start and its end"
            )
        if problem is not None:
            raise ValueError(
                f"Query parameter {name} for {cls.__name__} cannot be sent, "
                f"because its value {problem}"
            )
        if several:
            if isinstance(value, AbstractSet):
                operands.sort(key=_lookup_operand_order)
            texts = [str(operand) for operand in operands]
            if "" in texts:
                raise ValueError(
                    f"Query parameter {name} for {cls.__name__} cannot be "
                    "sent, because an item's text is empty, which "
                    "django-filter would read as no filter"
                )
            if style == "comma":
                if any("," in text for text in texts):
                    raise ValueError(
                        f"Query parameter {name} for {cls.__name__} cannot "
                        "be sent, because an item's text holds a comma, "
                        "which the server would read as two items. Set "
                        'the API\'s list_lookup_style to "repeat" if it '
                        "reads repeated parameters"
                    )
                return [",".join(texts)]
        return operands

    @classmethod
    def _encode_query(
        cls, params: Mapping[str, Any], client: Client
    ) -> dict[str, Any]:
        """Encode query parameters as ``_encode_fields()`` encodes fields.

        Each parameter becomes a list of operands, which requests sends as the
        parameter repeated. A parameter named after a field is encoded by the
        field's conversion, and a value for a collection field may be a single
        item, which matches the objects whose collection holds it. A boolean
        field value is sent as ``true`` or ``false``, as in JSON.

        A parameter whose name ends in ``__in`` or ``__range`` takes a
        collection of values, which the client's ``list_lookup_style`` sends as
        one comma-separated value or as the parameter repeated.

        Args:
            params: Query parameter values keyed as for ``_encode_fields()``.
            client: The client that sends the query, whose
                ``list_lookup_style`` decides how a lookup of several values is
                sent.

        Returns:
            Operands by JSON key.

        Raises:
            ValueError: If two parameters are sent under one name, or both
                compare the model's key. Also if a parameter has a value that
                requests would not send intact, so the query would ignore or
                change its constraint, or the client's ``list_lookup_style`` is
                not ``"comma"`` or ``"repeat"``.
        """
        style = client.list_lookup_style
        if style not in ("comma", "repeat"):
            raise ValueError(
                f"list_lookup_style must be 'comma' or 'repeat', not {style!r}"
            )
        payload: dict[str, Any] = {}
        senders: dict[str, str] = {}
        keys: dict[str, str] = {}
        for name, value in params.items():
            parameter = cls._parameter(name)
            if parameter.wire in senders:
                raise ValueError(
                    f"Query parameters {senders[parameter.wire]} and {name} "
                    f"for {cls.__name__} both send {parameter.wire}"
                )
            senders[parameter.wire] = name
            key_suffix = parameter.key_suffix
            if key_suffix is not None:
                if key_suffix in keys:
                    raise ValueError(
                        f"Query parameters {keys[key_suffix]} and "
                        f"{name} for {cls.__name__} both compare its key. "
                        "Name the key once"
                    )
                keys[key_suffix] = name
            payload[parameter.wire] = cls._query_operands(name, value, style)

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
            Exception: Whatever resolving a name in the key's type raises, such
                as a ``NameError`` for a class that is not defined, before any
                request.
            ResponseShapeError: If the response does not hold a JSON object, as
                when it is an empty list or a list of several objects, or the
                object's JSON after ``merge_response()`` lacks the key, holds
                ``null`` for it, holds a key that is not of the key's type, or
                names another key than the one that the request sent. The
                resource may have been created, and the error's ``response``
                holds the response.
            TypeError: If the model's key type is refused, which is checked
                before any request. Also raised if a field is given both as a
                keyword argument and in ``fields``, if ``request_options`` is
                not a mapping or sets the body, or if a field's value cannot be
                sent as a form field with ``files``. Also raised for response
                streaming or conflicting wire names, and for a value under a
                name that is no field that is not made of JSON types, as a
                tuple, or a mapping with a key that is not a string.
            AttributeError: If an ordinary value names a read-only field.
            APIError: If the server returns an HTTP error status.
            RedirectError: If the server answers the write with a redirect.
            LimitExceeded: If a request or its JSON exceeds the API's limits.
            FieldConversionError: If a field's value cannot be converted, or
                the key is ``None``. Leave the key out to let the server assign
                it.
        """
        _require_decorated(cls)
        _refuse_keyless_request(cls, "create")
        # The response is read for a key of this type, so a type that cannot be
        # resolved or is refused must raise before the request.
        _resolve_key(cls)
        options = _request_options(request_options)
        api = None if api is None else _as_client(api)
        payload = cls._encode_fields(
            _query_params(
                fields, kwargs, "Field", "fields", reserved=frozenset()
            ),
        )
        if files is None:
            # An extra has no annotation, so eazyrest cannot say how a tuple,
            # or a mapping with a key that is not a string, reads back as JSON.
            _checked_json(cls._to_wire(payload))
        # A class's api can be assigned after the class is defined, and the
        # request must not be sent with it.
        _check_api_attribute(cls, UsageError)
        client = cls.api if api is None else api
        resp = client.post(
            _class_url_of(cls), **cls._write_body(payload, files), **options
        )

        # The object keeps neither a write-only field nor an extra key.
        json_keys = cls._json_keys() - cls._write_only_keys()
        # The payload can hold the caller's own containers, such as a dict
        # field's value, and merge_response() may change what it is given, so
        # it gets copies. The object copies the result again.
        sent = {
            key: _copy_json(value)
            for key, value in payload.items()
            if key in json_keys
        }
        # The server may have made the resource, which only the response names,
        # so every refusal of the response holds it.
        try:
            merged = _checked_merge(
                cls,
                cls._before_creation(api).merge_response(resp, {}, sent),
                resp,
            )
            cls._refuse_other_created_key(payload, merged, resp)
            obj = cls(json=merged, api=api)
        except ResponseShapeError as err:
            if err.response is None:
                err.response = resp
            raise
        except FieldConversionError as err:
            if err.model is not cls or err.field != cls._pk.field:
                raise
            raise ResponseShapeError(
                f"The response to the creation of a {cls.__name__} names no "
                f"key that the {cls.__name__} has, although it may have been "
                f"created: {err}",
                response=resp,
            ) from err
        # A new object has no earlier state, so a field that neither the
        # payload nor the response holds is unknown rather than absent.
        obj._json_is_partial = not json_keys.issubset(obj._json)
        return obj

    @classmethod
    def _refuse_other_created_key(
        cls,
        payload: Mapping[str, Any],
        merged: Mapping[str, Any],
        resp: Response,
    ) -> None:
        """Refuse a creation's response that names another key than was sent.

        A request that sent a key creates the resource that the key names, so a
        response that names another key contradicts it, as the response to a
        ``save()`` that names another key does. A response that names no key
        takes the one that was sent, and one whose key cannot be built is left
        to the construction of the object, which refuses it.

        Args:
            payload: The fields that the request sent, by read key.
            merged: The JSON that ``merge_response()`` returned.
            resp: The response to the creation.

        Raises:
            ResponseShapeError: If the request sent a key and the JSON names
                another. The error holds the response.
        """
        field: str = cls._pk.json_field
        if field not in payload or field not in merged:
            return
        sent = _build_key(cls, payload[field])
        try:
            named = _build_key(cls, merged[field])
        except (TypeError, ValueError):
            return
        if named != sent:
            # The keys can be secrets, and the error holds the response, so the
            # message quotes neither.
            raise ResponseShapeError(
                f"The response to the creation of a {cls.__name__} names "
                "another key than the request sent, so it contradicts the "
                "request, although the resource may have been created",
                response=resp,
            )

    @classmethod
    def _before_creation(cls, api: _AnyAPI | None) -> Self:
        """Return the object whose ``merge_response()`` reads a creation.

        The hook is a method of an object, and ``create()`` has none until the
        response names its key, so the hook reads the response for one that has
        no JSON and no key yet. Only the hook sees it, and ``create()`` builds
        the object that it returns from the hook's result.

        Args:
            api: The API that ``create()`` was given, if any.

        Returns:
            An object of ``cls`` with empty JSON, which is never returned.
        """
        obj = object.__new__(cls)
        obj._api_override = api
        obj._write_mode = "lazy"
        obj._pending_updates = {}
        obj._field_cache = {}
        obj._field_snapshots = {}
        obj._prefetched_related = {}
        obj._json = {}
        obj._json_is_partial = False
        obj._wire_key = _BEFORE_CREATION
        return obj

    @classmethod
    def _from_item(cls, item: Any, index: int, api: Client | None) -> Self:
        """Build an object from an item of a collection response.

        Args:
            item: The item's JSON.
            index: The item's position in the collection, or in its page.
            api: The query's API, which the object keeps, if it was given.

        Returns:
            The object.

        Raises:
            ResponseShapeError: If the item is not a JSON object, lacks the
                key, holds ``null`` for it, or holds a key that is not of the
                key's type. The message names the item's position.
        """
        if not isinstance(item, Mapping):
            raise ResponseShapeError(
                f"Item [{index}] of the {cls.__name__} collection must be a "
                f"JSON object, got {type(item).__name__}"
            )
        try:
            return cls(json=_OwnedJSON(item), api=api)
        except FieldConversionError as err:
            prop = _pk_property_of(cls)
            if prop is None or err.model is not cls or err.field != prop.field:
                raise
            raise ResponseShapeError(
                f"Item [{index}] of the {cls.__name__} collection names no "
                f"key: {err}"
            ) from err

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
            keys, so the mapping may key them as it likes, and a ``UUID`` key
            matches in any spelling. An object of another model, which is not
            an instance of this class or of a subclass, raises ``TypeError``.

        Raises:
            PrefetchNotSupported: Always, unless overridden by a subclass.
        """
        del pks, api, kwargs
        raise PrefetchNotSupported(
            f"{cls.__name__} does not support bulk prefetch"
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
            TypeError: If ``collection_page()`` does not return a ``Page``.
        """
        if cls._overrides_hook("collection_items"):
            return cls.collection_items(api.json(resp), api=api, **kwargs)
        return _follow_pages(
            api,
            _checked_page(cls, cls.collection_page(resp, api=api, **kwargs)),
            lambda page_resp: _checked_page(
                cls, cls.collection_page(page_resp, api=api, **kwargs)
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
        model's primary key, which a read also accepts as an embedded object.
        The definitions of a conversion's schema move into the schema's
        ``$defs``, renamed with the references to them when two share a name. A
        ``TypedDict`` or ``dict[str, T]`` is an object. A type whose JSON form
        eazyrest does not check, such as ``Any``, allows any value.

        A read schema requires the primary key's JSON name, because every
        object that is read holds its key, and a write schema requires nothing,
        because the server may assign the key. Both allow other properties,
        which a server may send. A key's ``integer`` is JSON Schema's, which
        takes ``7.0``, but the key's builder refuses a float, so a key that the
        schema accepts as ``7.0`` still raises ``FieldConversionError``. The
        schema is a JSON Schema 2020-12 schema, as its ``$schema`` says, which
        differs from the dialect of OpenAPI 3.0, as in its ``prefixItems`` for
        a fixed-length tuple.

        Args:
            mode: ``"read"`` or ``"write"``.

        Returns:
            The JSON schema, as a dict.

        Raises:
            ValueError: If ``mode`` is neither ``"read"`` nor ``"write"``.
            NameError: If a field's annotation names a type that cannot be
                resolved, as when the field is read.
            FieldConversionError: If a field's type cannot be converted, as
                when the field is read, such as a member of a ``TypedDict``
                that refers to itself through a type alias.
            TypeError: If the class is not decorated, has a decorated ancestor,
                and sets ``conversions`` itself or inherits them from a class
                that is not an ancestor of that decorated class, such as a
                mixin.
        """
        _require_decorated(cls)
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
                properties[key] = (
                    _key_schema(cls)
                    if descriptor._is_key_of_class(cls)
                    else _json_schema_of(
                        descriptor.analyzed_type,
                        mode,
                        defs,
                        frozenset(),
                        _table_of(cls),
                    )
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
        key_property = _pk_property_of(cls)
        if (
            mode == "read"
            and key_property is not None
            and key_property.json_field in properties
        ):
            schema["required"] = [key_property.json_field]
        if defs:
            schema["$defs"] = defs
        return schema

    @classmethod
    def object_json(cls, payload: Any, **kwargs: Any) -> Any:
        """Return the JSON object contained in a single-object response.

        Subclasses can override this hook when an API wraps single-object
        responses in an envelope. The default reads a list of one object as
        that object. A write or a load whose response is an empty list or a
        list of several objects, which this hook does not unwrap, raises
        ``ResponseShapeError``, with the response, rather than the errors
        below.

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

    def _object_from_response(
        self, payload: Any, resp: Response
    ) -> dict[str, Any]:
        """Return the JSON object that a single-object response holds.

        Args:
            payload: Decoded JSON payload of the response.
            resp: The response that the payload was decoded from.

        Returns:
            A copy of the mapping that ``object_json()`` extracts.

        Raises:
            ResponseShapeError: If ``object_json()`` raises ``DoesNotExist`` or
                ``MultipleObjectsReturned`` for a list, or does not return a
                mapping. The error holds the response. Any other error of
                ``object_json()`` reaches the caller unchanged.
        """
        cls = type(self)
        try:
            json = cls.object_json(payload)
        except (DoesNotExist, MultipleObjectsReturned) as err:
            # A list is not the object that a write, a load, or a creation
            # returns. These errors describe a query, which none of them is.
            if self._wire_key is _BEFORE_CREATION:
                where = (
                    f"creation of a {cls.__name__} is not one object, "
                    "although the resource may have been created"
                )
            else:
                where = f"write or load of a {cls.__name__} is not one object"
            raise ResponseShapeError(
                f"The response to the {where}: {err}", response=resp
            ) from err
        if not isinstance(json, Mapping):
            raise ResponseShapeError(
                f"{cls.__name__} response must hold a JSON object, "
                f"got {type(json).__name__}",
                response=resp,
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
            UsageError: If a class in the MRO of the related model had ``api``
                assigned, because the bulk request would be sent through it.
        """
        descriptor = _field_named(cls, field)
        if descriptor is None:
            # The field name is the invalid value, so this is a ValueError even
            # though the check is on the descriptor's type.
            raise ValueError(
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
        if not model_type._overrides_hook("bulk_get_by_pks"):
            raise PrefetchNotSupported(
                f"{cls.__name__}.{field} cannot be prefetched, because "
                f"{model_type.__name__} does not implement bulk_get_by_pks()"
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
            Mapping of each prefetched field name to related objects, keyed by
            their canonical keys, as ``==`` compares keys. Fields with no
            primary keys to fetch are omitted.

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

            # Each distinct value once, by its JSON type and value, so that
            # true and 1 stay apart.
            raw_keys: dict[str, Any] = {}

            for item in items:
                if descriptor.json_field not in item:
                    continue

                raw_value = item[descriptor.json_field]

                if raw_value is None:
                    continue

                if collection_type is None:
                    if not isinstance(raw_value, dict):
                        raw_keys.setdefault(
                            _json_identity(cls, field, raw_value), raw_value
                        )
                    continue

                if not isinstance(raw_value, Iterable) or isinstance(
                    raw_value, (str, bytes, dict)
                ):
                    continue

                # Explicit prefetch only bulk-loads foreign-key style entries.
                # Embedded related JSON objects do not need to be prefetched.
                for entry in tuple(raw_value):
                    if entry is not None and not isinstance(entry, dict):
                        raw_keys.setdefault(
                            _json_identity(cls, field, entry), entry
                        )

            if len(raw_keys) == 0:
                continue

            # The related model loads through its own registered API, unless
            # the query passed api=.
            model_type = related_type.model_type
            # The objects that the bulk request returns match their parents by
            # key. A key that cannot be read yet matches none, so raise before
            # the request rather than keep an empty prefetch.
            _resolve_key(model_type)
            related_objects = model_type.bulk_get_by_pks(
                _prefetch_keys(cls, field, model_type, raw_keys.values()),
                api=query.related_client(model_type),
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
                obj._key: obj for obj in related_objects.values()
            }

        return prefetched_related

    @classmethod
    def _check_prefetch_batch_size(cls) -> None:
        """Refuse a ``prefetch_batch_size`` that no batch can have.

        Raises:
            TypeError: If the size is neither an ``int`` nor ``None``. A
                ``bool`` is not a size.
            ValueError: If the size is below 1, which reads no object.
        """
        size = cls.prefetch_batch_size
        if size is None:
            return
        if isinstance(size, bool) or not isinstance(size, int):
            raise TypeError(
                f"The prefetch_batch_size of {cls.__name__} must be an int or "
                f"None, got {type(size).__name__}"
            )
        if size < 1:
            raise ValueError(
                f"The prefetch_batch_size of {cls.__name__} must be at least "
                "1, or None to read the collection as one batch"
            )

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
        start = 0

        while True:
            raw_items = list(
                itertools.islice(iterator, cls.prefetch_batch_size)
            )
            if len(raw_items) == 0:
                return

            # Build the parents first, so that an item that cannot be one is
            # refused before any hook sends a request for the batch.
            parents = [
                cls._from_item(item, index, query.api)
                for index, item in enumerate(raw_items, start)
            ]
            prefetched_related = cls._apply_prefetch(
                [_OwnedJSON(item) for item in raw_items], fields, query
            )
            start += len(raw_items)

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

        A parameter that compares one value of a field, which is the field's
        name or a lookup with the suffix ``exact``, ``gt``, ``gte``, ``lt``, or
        ``lte``, takes no collection, because a server applies only one of a
        repeated parameter. Use ``id__in`` for several values of ``id``. A
        collection field's name and a value that the field's conversion takes
        whole are exempt.

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
            ``count`` is the size of the whole collection, if the iterable that
            ``collection_items_from_response()`` returns has a ``count``
            attribute, as the default's does for a page that reports one, and
            ``None`` otherwise.

        Raises:
            ValueError: If a ``prefetch`` field is not a JSON-backed
                related-object field or is named twice, if a parameter has a
                value that requests would not send intact, or if
                ``prefetch_batch_size`` is below 1 and ``prefetch`` is given.
            FieldConversionError: If a parameter's value is of the wrong type
                for its field, as a text for an ``int`` key, or a related key
                that ``prefetch`` reads from an item is.
            ResponseShapeError: If an item of the response is not a JSON
                object, or holds no key, or a key that is not of the key's
                type. The message names the item's position, and the error is
                raised when iteration reaches the item.
            TypeError: If the model's key type is refused once it resolves. The
                error is raised before the request only if a parameter names
                the key, and otherwise as the first item is built, so an empty
                collection raises nothing. Also raised if a parameter is given
                both as a keyword argument and in ``params``,
                ``request_options`` is not valid for a query, or
                ``prefetch_batch_size`` is neither an ``int`` nor ``None`` and
                ``prefetch`` is given.
            PrefetchNotSupported: If the related model of a ``prefetch`` field
                does not implement ``bulk_get_by_pks()``.
            RedirectError: If the response is a redirect that was not
                followed, such as a 304.
            APIError: If the server returns an HTTP error status, as
                ``NotFound`` for a 404.
            LimitExceeded: If a request or the traversal exceeds a limit of the
                API.
            PaginationError: If a next-page link leads to another origin, or to
                a page that was already read.
        """
        _require_decorated(cls)
        options = _query_request_options(request_options)
        query = _query_params(params, kwargs)
        prefetch_fields = _normalize_prefetch(prefetch)
        # Check the fields before the request, so a misspelled field or a model
        # without bulk lookup fails even when the query matches nothing.
        for field in prefetch_fields:
            cls._prefetch_info(field)
        if prefetch_fields:
            cls._check_prefetch_batch_size()

        # A class's api can be assigned after the class is defined, and the
        # request must not be sent with it.
        _check_api_attribute(cls, UsageError)
        api = None if api is None else _as_client(api)
        base = cls.api if api is None else api
        client = base._scoped(options) if options else base
        context = _Query(api, client, options)
        resp = _check_final_response(
            client.get(
                _class_url_of(cls), params=cls._encode_query(query, client)
            ),
            "the query has no collection to read",
        )
        items = cls.collection_items_from_response(resp, api=client)

        count = _count_of(items)
        # Treat an empty prefetch list the same as no prefetch at all.
        if len(prefetch_fields) == 0:
            return Results(
                (
                    cls._from_item(item, index, api)
                    for index, item in enumerate(items)
                ),
                count,
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
                related-object field or is named twice, or ``prefetch`` is
                given and ``prefetch_batch_size`` is below 1.
            FieldConversionError: If a related key that ``prefetch`` reads from
                an item is of the wrong type for its field.
            ResponseShapeError: If an item of the response is not a JSON
                object, or holds no key, or a key that is not of the key's
                type. The message names the item's position, and the error is
                raised when iteration reaches the item.
            TypeError: If ``request_options`` is not valid for a query, or the
                model's key type is refused, as for ``filter()``.
            PrefetchNotSupported: If the related model of a ``prefetch`` field
                does not implement ``bulk_get_by_pks()``.
            RedirectError: If the response is a redirect that was not
                followed, such as a 304.
            APIError: If the server returns an HTTP error status, as
                ``NotFound`` for a 404.
            LimitExceeded: If a request or the traversal exceeds a limit of the
                API.
            PaginationError: If a next-page link leads to another origin, or to
                a page that was already read.
        """
        _require_decorated(cls)
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
                related-object field or is named twice, or if a parameter has a
                value that requests would not send intact.
            FieldConversionError: If a parameter's value is of the wrong type
                for its field, as a text for an ``int`` key, or a related key
                that ``prefetch`` reads from an item is.
            ResponseShapeError: If an item of the page is not a JSON object, or
                holds no key, or a key that is not of the key's type. The
                message names the item's position.
            TypeError: If ``url`` is given with query parameters, a parameter
                is given both as a keyword argument and in ``params``,
                ``request_options`` is not valid for a query, or
                ``collection_page()`` does not return a ``Page``. Also if the
                model's key type is refused, as for ``filter()``.
            PrefetchNotSupported: If the related model of a ``prefetch`` field
                does not implement its bulk lookup.
            PaginationError: If ``url`` has another origin.
            RedirectError: If the response is a redirect that was not
                followed, such as a 304.
            APIError: If the server returns an HTTP error status, as
                ``NotFound`` for a 404.
            LimitExceeded: If a request exceeds a limit of the API.
        """
        _require_decorated(cls)
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
        if prefetch_fields:
            cls._check_prefetch_batch_size()

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
                client.get(
                    _class_url_of(cls),
                    params=cls._encode_query(query, client),
                ),
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
                related-object field or is named twice, or if a parameter has a
                value that requests would not send intact.
            FieldConversionError: If a parameter's value is of the wrong type
                for its field, as a text for an ``int`` key, or a related key
                that ``prefetch`` reads from an item is.
            ResponseShapeError: If an item of the response is not a JSON
                object, or holds no key, or a key that is not of the key's
                type. The message names the item's position.
            TypeError: If a parameter is given both as a keyword argument and
                in ``params``, or ``request_options`` is not valid for a query.
                Also if the model's key type is refused, as for ``filter()``.
            PrefetchNotSupported: If the related model of a ``prefetch`` field
                does not implement ``bulk_get_by_pks()``.
            RedirectError: If the response is a redirect that was not
                followed, such as a 304.
            APIError: If the server returns an HTTP error status, as
                ``NotFound`` for a 404.
            LimitExceeded: If a request or the traversal exceeds a limit of the
                API.
            PaginationError: If a next-page link leads to another origin, or to
                a page that was already read.
        """
        _require_decorated(cls)
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
        ``defaults``. The lookup sends a key that is given under its JSON or
        write name as it is built, as the creation sends it, where ``filter()``
        sends that name as given.

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

        Before it sends the lookup, this method refuses an argument that
        ``create()`` could not send, after the checks that ``filter()`` makes
        of the lookup. A model under a name that is no field, for example, is
        sent as its key in a lookup but cannot be a body. The error therefore
        does not depend on whether the lookup finds an object.

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
                not send intact, or is the key's JSON name, which differs from
                the key's field name, and holds ``__``.
            FieldConversionError: If a field's value cannot be converted, as
                when a field that holds a collection is given a single item, or
                if the creation's key is ``None``. Also if a lookup value is of
                the wrong type for its field.
            ResponseShapeError: If an item of the lookup's response is not a
                JSON object, or holds no key, or a key that is not of the key's
                type. The creation raises what ``create()`` raises, as
                ``APIError`` for an HTTP error status and
                ``ResponseShapeError`` for a response that names no key.
            TypeError: If a parameter is given both as a keyword argument and
                in ``params``, ``request_options`` is not valid for a query and
                a creation, or a creation payload value is not made of JSON
                types, as a model under a name that is no field.
            Exception: Whatever resolving a name in the key's type raises, such
                as a ``NameError`` for a class that is not defined, before the
                lookup.
            RedirectError: If a response is a redirect that was not followed.
            APIError: If the server returns an HTTP error status, as
                ``NotFound`` for a 404.
            LimitExceeded: If a request or the traversal exceeds a limit of the
                API.
            PaginationError: If a next-page link of the lookup leads to another
                origin, or to a page that was already read.
        """
        _require_decorated(cls)
        _refuse_keyless_request(cls, "create")
        # The creation reads its response for a key of this type, so raise
        # before the lookup if the type cannot be resolved or is refused.
        _resolve_key(cls)
        # Check the options for both requests before sending either.
        _query_request_options(request_options)
        _request_options(request_options)

        # The lookup and the creation each iterate a collection value, so read
        # a single-pass iterable, such as a generator, only once. A value that
        # the field's conversion takes whole is no collection of values, and
        # the query reads it itself.
        def read_once(values: Mapping[str, Any]) -> dict[str, Any]:
            """Return ``values`` with each single-pass iterable read."""
            return {
                name: list(value)
                if isinstance(value, Iterable)
                and not isinstance(value, (Collection, str, bytes, Mapping))
                and not cls._takes_whole_value(name, value)
                else value
                for name, value in values.items()
            }

        kwargs = read_once(_query_params(params, kwargs))
        key_property = _pk_property_of(cls)
        names = {name: cls._resolve_name(name) for name in kwargs}
        for name, named in names.items():
            if (
                named is not None
                and named.by in ("json", "write")
                and named.suffix is None
                and "__" in name
            ):
                # The lookup would read the name as a field and a suffix, and
                # the creation would not send it as the field.
                if key_property is not None and (
                    named.field == key_property.field
                ):
                    advice = f"the key as {key_property.field} or pk"
                    subject = "the key's"
                else:
                    advice = f"{named.field} as {named.field}"
                    subject = f"{named.field}'s"
                raise ValueError(
                    f"Query parameter {name} for {cls.__name__} is "
                    f"{subject} {'JSON' if named.by == 'json' else 'write'} "
                    "name, which holds '__', so a lookup would read it as a "
                    f"field and a suffix. Look {advice}"
                )

        def sets_field(name: str) -> bool:
            """Return whether a name is a field of the creation.

            It is if it is a whole key of a field, or it is no field's and
            holds no ``__``, which a lookup would read as a field and a suffix.
            """
            named = names[name]
            return "__" not in name if named is None else named.suffix is None

        create_kwargs = {
            name: value for name, value in kwargs.items() if sets_field(name)
        }
        if defaults is not None:
            create_kwargs.update(read_once(defaults))

        # Refuse an argument that the lookup or the creation could not send
        # before sending anything. The lookup's own checks come first, then the
        # payload's, so the outcome does not depend on what the server holds.
        # filter() sends its request when it is called, so its checks are made
        # here, ahead of the payload's.
        _check_api_attribute(cls, UsageError)
        client = cls.api if api is None else _as_client(api)
        cls._encode_query(kwargs, client)
        payload = cls._encode_fields(create_kwargs)
        _checked_json(cls._to_wire(payload))
        # The creation sends the key as it is built, so the lookup does too,
        # under the JSON or write name that the caller gave. filter() sends
        # such a name as given, because it is an ordinary query parameter.
        lookup = {}
        for name, value in kwargs.items():
            named = names[name]
            if (
                key_property is not None
                and named is not None
                and named.field == key_property.field
                and named.by in ("json", "write")
                and named.suffix is None
            ):
                with _converting(cls, key_property.field):
                    value = _build_key(cls, value)
            lookup[name] = value
        results = cls.filter(
            api=api, request_options=request_options, params=lookup
        )
        first = next(results, None)
        if first is not None:
            second = next(results, None)
            if second is not None:
                raise MultipleObjectsReturned(
                    f"More than one {_describe_lookup(cls, kwargs)} exists"
                )

            return first, False

        return (
            cls.create(
                api=api, fields=create_kwargs, request_options=request_options
            ),
            True,
        )
