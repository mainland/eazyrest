import datetime
import dateutil.parser
from functools import cached_property
import json
import typing
from typing import Any, Dict, Optional, Type, Union
import urllib.parse

from .api import API

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

def get_annotations(obj: Any) -> Dict[str, Any]:
    return obj.__dict__.get('__annotations__', {})

def json_object(cls: Optional[Type]=None, pk: str='id', field_map: Dict[str, str]={}):
    """Define a JSON object."""
    def wrap(cls):
        # We only process annotations for this class *without* any annotations
        # for superclasses, so we don't use typing.get_type_hints. We also want
        # to delay resolving references, whereas typing.get_type_hints *does*
        # resolve references.
        for field, ty in get_annotations(cls).items():
            json_field = field_map.get(field, field)

            kwargs = {'field': field, 'ty': ty}

            if field == pk:
                kwargs['is_primary_key'] = True

            prop = JSONProperty(cls, json_field, **kwargs)

            setattr(cls, field, prop)

        return cls

    # See if we're being called as @json_object or @json_object().
    if cls is None:
        # We're called with parens.
        return wrap

    # We're called as @json_object without parens.
    return wrap(cls)

class JSONProperty:
    """A JSON property"""
    cls: Type['JSONObject']
    """Class to which this JSONProperty belongs"""

    field: str
    """Name of Python field corresponding to this property."""

    _ty: Optional[Any]
    """Field type"""

    json_field: str
    """Name of JSON field corresponding to this property."""

    is_primary_key: bool = False
    """Is this a primary key?"""

    def __init__(self,
                 cls: Type['JSONObject'],
                 json_field: str,
                 field: Optional[str]=None,
                 ty: Optional[Any]=None,
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
    def ty(self) -> Optional[Any]:
        """Type of this property (resolved)"""
        # Resolve type using typing.get_type_hints
        return typing.get_type_hints(self.cls)[self.field]

    def create_related(self, obj: 'JSONObject', arg: Any, ty: Type):
        """Create a related object"""
        # If the argument is an int, we treat it as a primary key
        if isinstance(arg, int):
            kwargs = {ty._pk_json_field: arg}
            return ty(obj._api, **kwargs)
        else:
            return ty(obj._api, json=arg)

    def from_json(self, obj: 'JSONObject', value: Any, ty: Type):
        """Convert a JSON value to a property value"""
        ty_origin = typing.get_origin(ty)
        ty_args =  typing.get_args(ty)

        if value is None:
            return value
        elif ty == datetime.datetime:
            if obj._api.datetime_string:
                return dateutil.parser.parse(value)
            else:
                return datetime.datetime.fromtimestamp(value, datetime.timezone.utc)
        elif lenient_issubclass(ty, JSONObject):
            return self.create_related(obj, value, ty)
        # List[T]
        elif ty_origin == list and len(ty_args) == 1 and lenient_issubclass(ty_args[0], JSONObject):
            return [self.create_related(obj, arg, ty_args[0]) for arg in value]
        # Optional[T]
        elif ty_origin == Union and len(ty_args) == 2 and ty_args[1] == type(None):
            return self.from_json(obj, value, ty_args[0])
        else:
            return value

    def to_json(self, obj: 'JSONObject', value: Any, ty: Type):
        """Convert a property value to JSON"""
        ty_origin = typing.get_origin(ty)
        ty_args =  typing.get_args(ty)

        if lenient_issubclass(ty, JSONObject):
            # If value is an int, assume it is a primary key already
            if isinstance(value, int):
                return value
            else:
                return value.pk
        elif ty == datetime.datetime:
            if obj._api.datetime_string:
                return str(value)
            else:
                return value.timestamp()
        # List[T]
        elif ty_origin == list and len(ty_args) == 1 and lenient_issubclass(ty_args[0], JSONObject):
            return [arg.pk for arg in value]
        # Optional[T]
        elif ty_origin == Union and len(ty_args) == 2 and ty_args[1] == type(None):
            return self.to_json(obj, value, ty_args[0])
        else:
            return value

    def __get__(self, obj, objtype):
        if self.is_primary_key and obj._json is None:
            return obj._pk_value
        elif self.json_field not in obj.json:
            raise AttributeError
        else:
            return self.from_json(obj, obj.json[self.json_field], self.ty)

    def __set__(self, obj, value):
        if self.is_primary_key and obj._json is None:
            obj._pk_value = value
        elif self.json_field not in obj.json:
            raise AttributeError
        else:
            new_value = self.to_json(obj, value, self.ty)

            # Access obj.json instead of obj._json to force object to be loaded.
            if obj.json[self.json_field] != new_value:
                data = json.dumps({self.json_field: new_value})

                resp = obj._api.patch(obj.url,
                                      data=data,
                                      headers={'Content-Type': 'application/json'})
                obj._json = resp.json()

class JSONObject:
    class_url: str
    """Relative URL for this class."""

    _api: API
    """The API associated with this object"""

    _pk: JSONProperty
    """The JSONProperty that is the primary key"""

    _pk_value: Optional[Any]
    """Value of the primary key. If None, look in JSON"""

    _pk_json_field: str
    """The field of the object's JSON representation that holds the primary key"""

    _json: Any
    """Object's JSON representation"""

    def __init__(self, api, json: Any=None, **kwargs):
        self._api = api
        self._json = json

        # Set all attributes passed in as keyword arguments
        for k, v in kwargs.items():
            setattr(self, k, v)

    def delete(self, *args, **kwargs):
        """Delete object"""
        self._api.delete(self.url, *args, **kwargs)

    def refresh(self):
        """Refresh object from API"""
        # Store value of primary key
        self._pk_value = self.pk
        # Clear JSON
        self._json = None

    @property
    def pk(self):
        """Object's primary key"""
        return self._pk

    @pk.setter
    def pk(self, value):
        self._pk = value

    @property
    def url(self):
        """Relative URL for this object."""
        url = urllib.parse.urljoin(self.class_url, str(self._pk))
        if self._api.trailing_slash:
            return url + '/'
        else:
            return url

    @property
    def json(self):
        """JSON representation of this object"""
        if not self._json:
            data = self._api.get(self.url).json()
            # If results are returned as a list, get first result
            if isinstance(data, list):
                self._json = data[0]
            else:
                self._json = data

            # Have have JSON now, so delete _pk_value
            del self._pk_value

        return self._json

    @property
    def json_pretty(self):
        """Pretty-printed JSON representation of this object"""
        return json.dumps(self.json, indent=4, sort_keys=True)

    @classmethod
    def create(cls, api, **kwargs):
        """Create a single object."""
        resp = api.post(cls.class_url, json=kwargs)
        return cls(api, json=resp.json())

    @classmethod
    def filter(cls, api, **kwargs):
        """Filter objects."""
        resp = api.get(cls.class_url, params=kwargs)
        return [cls(api, json=json) for json in resp.json()]

    @classmethod
    def all(cls, api):
        """Return all objects."""
        return cls.filter(api)

    @classmethod
    def get(cls, api, **kwargs):
        """Get a single object or None."""
        results = cls.filter(api, **kwargs)
        if len(results) == 0:
            raise DoesNotExist
        elif len(results) != 1:
            raise MultipleObjectsReturned
        else:
            return results[0]

    @classmethod
    def get_or_create(cls, api, **kwargs):
        """Get or create an object."""
        results = cls.filter(api, **kwargs)
        if len(results) == 1:
            obj = results[0]

            # Update fields
            for key, val in kwargs.items():
                setattr(obj, key, val)

            return obj, False
        elif len(results) > 1:
            raise MultipleObjectsReturned

        obj = cls.create(api, **kwargs)
        return obj, True
