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
