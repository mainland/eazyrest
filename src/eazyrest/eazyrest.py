"""Typed JSON object mapping for REST resources."""

from __future__ import annotations

import datetime
import typing
from collections.abc import (
    Callable,
    Iterable,
    Mapping,
    MutableSet,
    Set,
)
from functools import cached_property
from typing import (
    Any,
    ClassVar,
    TypeVar,
    Union,
    cast,
    overload,
)

from typing_extensions import Self

from .api import API
from .dateparse import parse_duration


class DoesNotExist(Exception):
    """Raised when no object matches a query that expects one result."""


class MultipleObjectsReturned(Exception):
    """Raised when a query expected one object but got multiple results."""


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


def get_annotations(obj: Any) -> dict[str, type]:
    """Get direct annotations from an object without resolving inheritance.

    Args:
        obj: Object whose ``__annotations__`` dictionary should be read.

    Returns:
        The object's direct annotations dictionary, or an empty mapping when no
        annotations are defined.
    """
    return obj.__dict__.get("__annotations__", {})


T = TypeVar("T", bound="JSONObject")


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
        for field, ty in get_annotations(cls).items():
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
            assert self.ty is not None
            return obj.from_json(obj.json[self.json_field], self.ty)

    def __set__(self, obj: JSONObject, value: Any) -> None:
        """Set a field value and persist it with ``PATCH`` when necessary.

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
            assert self.ty is not None
            new_value = obj.to_json(value, self.ty)

            # Access obj.json instead of obj._json to force a load first.
            if obj.json[self.json_field] != new_value:
                resp = obj.api.patch(
                    obj.url, json={self.json_field: new_value}
                )
                obj._json = resp.json()


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

    _pk_value: Any
    """Value of the primary key.

    If None, look in JSON
    """

    _api_override: API | None
    """Instance-scoped API override."""

    _json: Any
    """Object's JSON representation."""

    # pylint: disable=redefined-outer-name
    def __init__(
        self,
        json: Any = None,
        api: API | None = None,
        **kwargs: Any,
    ) -> None:
        """Create a JSON-backed object.

        Args:
            json: Optional JSON payload for eager initialization.
            api: Optional API instance overriding the class-level API.
            **kwargs: Field values, typically including the primary key.
        """
        self._api_override = api
        self._json = json

        # Set all attributes passed in as keyword arguments
        for k, v in kwargs.items():
            setattr(self, k, v)

    def create_related(self, arg: Any, ty: type[T]) -> T:
        """Create a related object from embedded JSON or a foreign key.

        Args:
            arg: Either a JSON mapping for eager object creation or a primary
                key value for lazy lookup.
            ty: Related ``JSONObject`` subclass type.

        Returns:
            Instance of ``ty``.
        """
        # If the argument is a dict, we treat it as JSON
        if isinstance(arg, dict):
            return ty(json=arg)
        else:
            assert issubclass(ty, JSONObject)
            kwargs = {ty._pk_json_field: arg}  # pylint: disable=protected-access
            return ty(**kwargs)

    def from_json(self, value: Any, ty: type[Any]) -> Any:
        """Convert a raw JSON value to a typed Python value.

        Args:
            value: Value read from JSON payload.
            ty: Target Python type.

        Returns:
            Converted Python value.
        """
        ty_origin = typing.get_origin(ty)
        ty_args = typing.get_args(ty)

        if value is None:
            return value
        elif ty is datetime.datetime:
            return datetime.datetime.fromtimestamp(
                value, datetime.timezone.utc
            )
        elif ty is datetime.timedelta:
            return parse_duration(value)
        elif lenient_issubclass(ty, JSONObject):
            assert issubclass(ty, JSONObject)
            return self.create_related(value, ty)
        # List[T]
        elif (
            ty_origin is list
            and len(ty_args) == 1
            and lenient_issubclass(ty_args[0], JSONObject)
        ):
            return [self.create_related(arg, ty_args[0]) for arg in value]
        # Optional[T]
        elif (
            ty_origin is Union
            and len(ty_args) == 2
            and ty_args[1] is type(None)
        ):
            return self.from_json(value, ty_args[0])
        else:
            return value

    def to_json(self, value: Any, ty: type[Any]) -> Any:
        """Convert a typed Python value to a JSON-serializable value.

        Args:
            value: Python value to serialize.
            ty: Declared field type.

        Returns:
            JSON-serializable representation of ``value``.
        """
        ty_origin = typing.get_origin(ty)
        ty_args = typing.get_args(ty)

        if lenient_issubclass(ty, JSONObject):
            # If value is an int, assume it is a primary key already
            if isinstance(value, int):
                return value
            else:
                return value.pk
        elif ty is datetime.datetime:
            return value.timestamp()
        elif ty is datetime.timedelta:
            return str(value)
        # List[T]
        elif (
            ty_origin is list
            and len(ty_args) == 1
            and lenient_issubclass(ty_args[0], JSONObject)
        ):
            return [arg.pk for arg in value]
        # Optional[T]
        elif (
            ty_origin is Union
            and len(ty_args) == 2
            and ty_args[1] is type(None)
        ):
            return self.to_json(value, ty_args[0])
        else:
            return value

    def delete(self, *args: Any, **kwargs: Any) -> None:
        """Delete this object from the remote API.

        Args:
            *args: Positional arguments forwarded to ``API.delete``.
            **kwargs: Keyword arguments forwarded to ``API.delete``.
        """
        self.api.delete(self.url, *args, **kwargs)

    def refresh(self) -> None:
        """Mark this object stale so it reloads from the API on next access."""
        # Store value of primary key
        self._pk_value = self.pk
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

            # Have have JSON now, so delete _pk_value
            del self._pk_value

        return self._json

    @classmethod
    def create(cls: type[Self], **kwargs: Any) -> Self:
        """Create and return a new remote object.

        Args:
            **kwargs: Fields submitted as JSON in the ``POST`` request.

        Returns:
            Newly created object instance.
        """
        resp = cls.api.post(cls.class_url, json=kwargs)
        return cls(json=resp.json())

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
    def filter(
        cls: type[Self], url: str | None = None, **kwargs: Any
    ) -> Iterable[Self]:
        """Query objects matching request parameters.

        Args:
            url: Optional endpoint override; defaults to ``class_url``.
            **kwargs: Query string parameters.

        Returns:
            Iterable of objects built from the response payload.
        """
        if url is None:
            url = cls.class_url

        resp = cls.api.get(url, params=kwargs)
        return (cls(json=item) for item in cls.collection_items(resp.json()))

    @classmethod
    def all(cls: type[Self]) -> Iterable[Self]:
        """Return all objects for this resource type.

        Returns:
            Iterable of all objects.
        """
        return cls.filter()

    @classmethod
    def get(cls: type[Self], **kwargs: Any) -> Self:
        """Fetch exactly one object matching query parameters.

        Args:
            **kwargs: Query string parameters.

        Returns:
            Single matching object.

        Raises:
            DoesNotExist: If no objects matched.
            MultipleObjectsReturned: If more than one object matched.
        """
        results = iter(cls.filter(**kwargs))
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
