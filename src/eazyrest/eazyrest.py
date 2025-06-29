from __future__ import annotations

import datetime
import json
import typing
import urllib.parse
from collections.abc import Mapping, MutableSet, Set
from functools import cached_property
from typing import (
    Any,
    Callable,
    ClassVar,
    Dict,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
    Union,
    overload,
)

import dateutil.parser

from .api import API
from .dateparse import parse_duration

class DoesNotExist(Exception):
    pass

class MultipleObjectsReturned(Exception):
    pass

#
# Taken from:
#   https://github.com/pydantic/pydantic/blob/main/pydantic/_internal/_utils.py
#
def lenient_issubclass(cls: Any, class_or_tuple: Any) -> bool:
    return isinstance(cls, type) and issubclass(cls, class_or_tuple)

def get_annotations(obj: Any) -> Dict[str, type]:
    return obj.__dict__.get('__annotations__', {})

T = TypeVar('T', bound='JSONObject')

@overload
def json_object(cls: type[T], pk: str='id', field_map: Optional[Mapping[str, str]]=None, exclude: Set[str]=frozenset()) -> type[T]:
    ...

@overload
def json_object(cls: None=None, pk: str='id', field_map: Optional[Mapping[str, str]]=None, exclude: Set[str]=frozenset()) -> Callable[[type[T]], type[T]]:
    ...

def json_object(cls=None, pk: str='id', field_map: Optional[Mapping[str, str]]=None, exclude: Set[str]=frozenset()):
    """Define a JSON object."""
    if field_map is None:
        field_map = {}

    def wrap(cls: type[T]) -> type[T]:
        fields: MutableSet[str] = set()

        # We only process annotations for this class *without* any annotations
        # for superclasses, so we don't use typing.get_type_hints. We also want
        # to delay resolving references, whereas typing.get_type_hints *does*
        # resolve references.
        for field, ty in get_annotations(cls).items():
            if field not in exclude:
                json_field = field_map.get(field, field)

                if field == pk:
                    is_primary_key = True
                else:
                    is_primary_key = False

                prop = JSONProperty(cls, json_field, field, ty, is_primary_key=is_primary_key)

                setattr(cls, field, prop)

                fields.add(field)

        # pylint: disable=protected-access
        cls._json_fields = fields

        def _setattr(self, name, value):
            if name[0] != '_' and name not in cls._json_fields:
                raise AttributeError(f"'{cls.__name__:}' object has no attribute '{name:}'")

            super(cls, self).__setattr__(name, value)

        cls.__setattr__ = _setattr # type: ignore[method-assign]

        return cls

    # See if we're being called as @json_object or @json_object().
    if cls is None:
        # We're called with parens.
        return wrap

    # We're called as @json_object without parens.
    return wrap(cls)

class JSONProperty:
    """A JSON property"""
    cls: type[JSONObject]
    """Class to which this JSONProperty belongs"""

    field: str
    """Name of Python field corresponding to this property."""

    _ty: type
    """Field type"""

    json_field: str
    """Name of JSON field corresponding to this property."""

    is_primary_key: bool = False
    """Is this a primary key?"""

    def __init__(self,
                 cls: type[JSONObject],
                 json_field: str,
                 field:str,
                 ty: type,
                 is_primary_key: bool=False):
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
        """Type of this property (resolved)"""
        # Resolve type using typing.get_type_hints
        return typing.get_type_hints(self.cls)[self.field]

    def __get__(self, obj, objtype):
        if self.is_primary_key and obj._json is None:
            return obj._pk_value
        elif self.json_field not in obj.json:
            raise AttributeError
        else:
            assert self.ty is not None
            return obj.from_json(obj.json[self.json_field], self.ty)

    def __set__(self, obj, value):
        if self.is_primary_key and obj._json is None:
            obj._pk_value = value
        elif self.json_field not in obj.json:
            raise AttributeError
        else:
            assert self.ty is not None
            new_value = obj.to_json(value, self.ty)

            # Access obj.json instead of obj._json to force object to be loaded.
            if obj.json[self.json_field] != new_value:
                data = json.dumps({self.json_field: new_value})

                resp = obj.api.patch(obj.url,
                                     data=data,
                                     headers={'Content-Type': 'application/json'})
                obj._json = resp.json()

class JSONObject:
    class_url: ClassVar[str]
    """Relative URL for this class."""

    api: ClassVar[API]
    """The API associated with this object"""

    _pk: JSONProperty
    """The JSONProperty that is the primary key"""

    _pk_value: Optional[Any]
    """Value of the primary key. If None, look in JSON"""

    _pk_json_field: str
    """The field of the object's JSON representation that holds the primary key"""

    _json_fields: Set[str]
    """All JSON fields"""

    _json: Optional[Any]
    """Object's JSON representation"""

    # pylint: disable=redefined-outer-name
    def __init__(self, json: Optional[Any]=None, api: Optional[API]=None, **kwargs):
        if api is not None:
            # We override the class variable for this instance if an api is
            # provided.
            self.api = api # type: ignore[misc]

        self._json = json

        # Set all attributes passed in as keyword arguments
        for k, v in kwargs.items():
            setattr(self, k, v)

    def create_related(self, arg: Any, ty: type[T]):
        """Create a related object"""
        # If the argument is a dict, we treat it as JSON
        if isinstance(arg, dict):
            return ty(json=arg)
        else:
            assert(issubclass(ty, JSONObject))
            kwargs = {ty._pk_json_field: arg} # pylint: disable=protected-access
            return ty(**kwargs)

    def from_json(self, value: Any, ty: type):
        """Convert a JSON value to a property value"""
        ty_origin = typing.get_origin(ty)
        ty_args =  typing.get_args(ty)

        if value is None:
            return value
        elif ty is datetime.datetime:
            if self.api.datetime_string:
                return dateutil.parser.parse(value)
            else:
                return datetime.datetime.fromtimestamp(value, datetime.timezone.utc)
        elif ty is datetime.timedelta:
            return parse_duration(value)
        elif lenient_issubclass(ty, JSONObject):
            assert issubclass(ty, JSONObject)
            return self.create_related(value, ty)
        # List[T]
        elif ty_origin is list and len(ty_args) == 1 and lenient_issubclass(ty_args[0], JSONObject):
            return [self.create_related(arg, ty_args[0]) for arg in value]
        # Optional[T]
        elif ty_origin is Union and len(ty_args) == 2 and ty_args[1] is type(None):
            return self.from_json(value, ty_args[0])
        else:
            return value

    def to_json(self, value: Any, ty: type):
        """Convert a property value to JSON"""
        ty_origin = typing.get_origin(ty)
        ty_args =  typing.get_args(ty)

        if lenient_issubclass(ty, JSONObject):
            # If value is an int, assume it is a primary key already
            if isinstance(value, int):
                return value
            else:
                return value.pk
        elif ty is datetime.datetime:
            if self.api.datetime_string:
                return str(value)
            else:
                return value.timestamp()
        elif ty is datetime.timedelta:
            return str(value)
        # List[T]
        elif ty_origin is list and len(ty_args) == 1 and lenient_issubclass(ty_args[0], JSONObject):
            return [arg.pk for arg in value]
        # Optional[T]
        elif ty_origin is Union and len(ty_args) == 2 and ty_args[1] is type(None):
            return self.to_json(value, ty_args[0])
        else:
            return value

    def delete(self, *args, **kwargs):
        """Delete object"""
        self.api.delete(self.url, *args, **kwargs)

    def refresh(self):
        """Refresh object from API"""
        # Store value of primary key
        self._pk_value = self.pk
        # Clear JSON
        self._json = None

    @property
    def pk(self) -> JSONProperty:
        """Object's primary key"""
        # If the object's primary key is another JSON object, then that object's
        # primary key is this object's primary key.
        if isinstance(self._pk, JSONObject):
            return self._pk.pk
        else:
            return self._pk

    @pk.setter
    def pk(self, value):
        self._pk = value

    @property
    def url(self) -> str:
        """Relative URL for this object."""
        url = urllib.parse.urljoin(self.class_url, str(self.pk))

        if self.api.trailing_slash:
            return url + '/'
        else:
            return url

    @property
    def json(self) -> Any:
        """JSON representation of this object"""
        if self._json is None:
            data = self.api.get(self.url).json()
            # If results are returned as a list, get first result
            if isinstance(data, list):
                self._json = data[0]
            else:
                self._json = data

            # Have have JSON now, so delete _pk_value
            del self._pk_value

        return self._json

    @property
    def json_pretty(self) -> str:
        """Pretty-printed JSON representation of this object"""
        return json.dumps(self.json, indent=4, sort_keys=True)

    @classmethod
    def create(cls, **kwargs) -> JSONObject:
        """Create a single object."""
        resp = cls.api.post(cls.class_url, json=kwargs)
        return cls(json=resp.json())

    @classmethod
    def filter(cls, url: Optional[str]=None, **kwargs) -> Sequence[JSONObject]:
        """Filter objects."""
        if url is None:
            url = cls.class_url

        resp = cls.api.get(url, params=kwargs)
        return [cls(json=json) for json in resp.json()]

    @classmethod
    def all(cls) -> Sequence[JSONObject]:
        """Return all objects."""
        return cls.filter()

    @classmethod
    def get(cls, **kwargs) -> JSONObject:
        """Get a single object or None."""
        results = cls.filter(**kwargs)
        if len(results) == 0:
            raise DoesNotExist
        elif len(results) != 1:
            raise MultipleObjectsReturned
        else:
            return results[0]

    @classmethod
    def get_or_create(cls, **kwargs) -> Tuple[JSONObject, bool]:
        """Get or create an object."""
        results = cls.filter(**kwargs)
        if len(results) == 1:
            obj = results[0]

            # Update fields
            for key, val in kwargs.items():
                setattr(obj, key, val)

            return obj, False
        elif len(results) > 1:
            raise MultipleObjectsReturned

        obj = cls.create(**kwargs)
        return obj, True
