# Guide

## Defining models

Model classes inherit from `JSONObject` and are decorated with `@json_object`
so annotated fields are mapped to JSON-backed descriptors.

```python
from eazyrest import JSONObject, json_object


@json_object
class User(JSONObject):
    class_url = "/users/"

    id: int
    name: str
```

## Field conversion

Field annotations drive JSON conversion when values are read from or written to
model instances. Scalar fields preserve normal JSON values, while a few common
Python types are converted automatically:

- `datetime.datetime` values decode from Unix timestamps and encode back to
  timestamps.
- `datetime.timedelta` values decode from ISO 8601 durations and encode back to
  strings.
- `enum.Enum` values decode from their JSON values and encode back to their
  enum values.
- `JSONObject` subclasses decode from embedded objects or primary keys.

Collection annotations are converted recursively, so item annotations are
honored inside `list`, `tuple`, `set`, `frozenset`, and `Iterable` fields:

```python
import datetime
import enum

from eazyrest import JSONObject, json_object


class IssueStatus(str, enum.Enum):
    OPEN = "open"
    CLOSED = "closed"


@json_object
class Timeline(JSONObject):
    class_url = "/timelines/"

    id: int
    starts_at: list[datetime.datetime]
    statuses: tuple[IssueStatus]
```

In this example, JSON timestamp values in `starts_at` become
`datetime.datetime` objects, and JSON strings in `statuses` become
`IssueStatus` values. Assigning or creating objects with those typed values
encodes the nested items back to JSON.

Collection payloads must be JSON arrays or another iterable collection shape.
Strings, bytes, and dictionaries are rejected for collection fields so
malformed payloads fail before they can be interpreted item by item.

## Custom field conversion

A model converts field values by a table of conversions, keyed by type. The
default table converts `datetime`, `timedelta`, and enums, as the previous
section describes, and any other type reads and writes its JSON value as it is.
To convert another type, or to convert one of these differently, declare
`conversions` on a shared base model, mapping the type to a `Conversion`:

```python
import ipaddress

from eazyrest import Conversion, JSONObject, json_object


class APIObject(JSONObject):
    conversions = {
        ipaddress.IPv4Address: Conversion(
            decode=lambda value, cls: cls(value),
            encode=lambda value, cls: str(value),
        ),
    }


@json_object
class Host(APIObject):
    class_url = "/hosts/"

    id: int
    address: ipaddress.IPv4Address
    allowed: list[ipaddress.IPv4Address]
```

`decode` reads a JSON value as the declared type, and `encode` writes a value
of the declared type as JSON. Both receive the declared type, so one conversion
can serve a family of types, as the default one for enums does. The conversion
applies wherever its type appears, so `Host.allowed` converts each item. `null`
reads as `None` and `None` writes as `null` without a conversion, and `encode`
is called only for instances of the type that the entry is keyed by: any other
value, such as a string assigned to `Host.address`, is written as it is.

A class's `conversions` are merged over those of its bases, and theirs over the
default table, so a subclass can add conversions or replace its bases'. A field
type converts by the entry for exactly that type, else by the entry of the
nearest class in its MRO, so a subclass of `datetime.datetime` converts as a
`datetime`, and reads a plain `datetime`, which writes back, unless it has an
entry of its own. A `NewType` converts by its own entry, or else as its
supertype. A key cannot be a model, whose fields are relations, a type that
eazyrest converts itself: `object`, `bool`, `int`, `float`, `str`, `None`'s
type, or a collection type such as `list` or `dict`, or a type whose instances
`isinstance()` cannot recognize, such as `Any` or a `Protocol` that is not
runtime-checkable. An entry for `int` would also convert `bool` and `IntEnum`
fields, so to convert some integers differently, key the entry by a `NewType`,
such as `Cents = NewType("Cents", int)`. Declare `conversions` in the class
body, because a class merges and checks its table when it is defined, and
raises `TypeError` for a bad entry. `DEFAULT_CONVERSIONS` holds the default
table, so an entry can reuse a default's `decode` or `encode`.

## Collections

`filter()` and `all()` return iterables of model instances:

```python
users = list(User.all())
admins = list(User.filter(role="admin"))
```

`filter()` and `get()` also accept `prefetch=[...]` for explicit bulk-loading
of related fields when the related model implements `bulk_get_by_pks()`.
Prefetched related objects may be shared by identity across parent objects in
the same batch, similar to Django's `prefetch_related()`.

## Response-shape hooks

Override these hooks if an API wraps responses in envelopes:

- `collection_items()` for collection endpoints
- `object_json()` for single-object endpoints

For example, an API that returns singleton responses in a "result" field and
collection responses in a "results" field should define a new base class
derived from `JSONObject` with the following overrides:

```python
@classmethod
def collection_items(cls, payload):
    return payload["results"]

@classmethod
def object_json(cls, payload):
    return payload["result"]
```

## Write modes

APIs use lazy writes by default. Assignments update local state and queue a
pending patch until `save()` is called.

```python
todo.completed = True
todo.save()
```

To make eager writes the default for all objects using an API, configure the
client:

```python
api = API("https://example.com/", default_write_mode="eager")
Todo.register_api(api)
```

You can still override the mode for a specific object:

```python
todo = Todo(id=1, write_mode="eager")
todo.completed = True
```

Call `refresh()` to discard unsaved local changes and force the next read to
reload object state from the API.

## API client

The `API` class wraps a `requests.Session`, resolves request URLs relative to
`base_url`, applies a default timeout, and raises for HTTP error responses. To
customize error handling for a particular API, override `raise_for_response()`
in an `API` subclass.

Use the client as a context manager to ensure the session is closed:

```python
from eazyrest import API

with API("https://api.example.com/v1/") as api:
    response = api.get("/users/1")
```

Supported methods:

- `get()`
- `post()`
- `patch()`
- `put()`
- `delete()`

### API registration

An `API` instance can be bound to a model hierarchy with `register_api()`:

```python
BaseModel.register_api(api)
```

Registering the API on a shared base class is the cleanest pattern. The API can
be overridden per instance by passing `api=` when creating an object.
