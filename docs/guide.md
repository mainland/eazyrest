# Guide

## Defining models

Model classes inherit from `JSONObject` and are decorated with `@json_object` so annotated fields are mapped to JSON-backed descriptors.

```python
from eazyrest import JSONObject, json_object


@json_object
class User(JSONObject):
    class_url = "/users/"

    id: int
    name: str
```

## Field conversion

Field annotations drive JSON conversion when values are read from or written to model instances. Scalar fields preserve normal JSON values, while a few common Python types are converted automatically:

- `datetime.datetime` values decode from Unix timestamps and encode back to timestamps.
- `datetime.timedelta` values decode from ISO 8601 durations and encode back to strings.
- `enum.Enum` values decode from their JSON values and encode back to their enum values.
- `JSONObject` subclasses decode from embedded objects or primary keys.

Collection annotations are converted recursively, so item annotations are honored inside `list`, `tuple`, `set`, `frozenset`, and `Iterable` fields:

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

In this example, JSON timestamp values in `starts_at` become `datetime.datetime` objects, and JSON strings in `statuses` become `IssueStatus` values. Assigning or creating objects with those typed values encodes the nested items back to JSON.

Collection payloads must be JSON arrays or another iterable collection shape. Strings, bytes, and dictionaries are rejected for collection fields so malformed payloads fail before they can be interpreted item by item.

## Custom field conversion

Override `from_json()` and `to_json()` on a shared base model when an API needs conversion rules beyond the built-in `datetime`, `timedelta`, `Enum`, and related-object handling.

Both methods receive an `AnalyzedType` value. `AnalyzedType` is recursive metadata derived from the field annotation:

- `BaseType` describes a non-related Python type such as `str`, `int`, or `decimal.Decimal`.
- `RelatedType` describes a related `JSONObject` subclass.
- `OptionalType` wraps the analyzed type for `T | None`.
- `CollectionType` wraps the analyzed item type for `list[T]`, `tuple[T]`, `set[T]`, `frozenset[T]`, and `Iterable[T]`.

Most custom conversions only need to handle one metadata case and delegate everything else to `super()`. Pattern matching works well for this because `AnalyzedType` is a union of small metadata classes. Because collection conversion is recursive, a custom scalar conversion also applies inside typed collections.

```python
from decimal import Decimal
from typing import Any

from eazyrest import AnalyzedType, BaseType, JSONObject, json_object


class APIObject(JSONObject):
    def from_json(
        self,
        value: Any,
        conversion: AnalyzedType,
        *,
        field: str | None = None,
    ) -> Any:
        match conversion:
            case BaseType(Decimal) if value is not None:
                return Decimal(str(value))

        return super().from_json(value, conversion, field=field)

    @classmethod
    def to_json(cls, value: Any, conversion: AnalyzedType) -> Any:
        match conversion:
            case BaseType(Decimal) if value is not None:
                return str(value)

        return super().to_json(value, conversion)


@json_object
class Invoice(APIObject):
    class_url = "/invoices/"

    id: int
    total: Decimal
    line_totals: list[Decimal]
```

The `Invoice.total` field converts through the `Decimal` branch directly. The `Invoice.line_totals` field is a `CollectionType`, so the default collection handling recurses into each item and calls the same `Decimal` branch for every value. When overriding `from_json()`, keep forwarding `field=field` to `super()` so related-object prefetch lookups continue to work.

## Collections

`filter()` and `all()` return iterables of model instances:

```python
users = list(User.all())
admins = list(User.filter(role="admin"))
```

`filter()` and `get()` also accept `prefetch=[...]` for explicit bulk-loading of related fields when the related model implements `bulk_get_by_pks()`. Prefetched related objects may be shared by identity across parent objects in the same batch, similar to Django's `prefetch_related()`.

## Prefetching related objects

Related fields are lazy by default. If a JSON payload stores a related object as a primary key, `eazyrest` creates a lazy related object and fetches it when you first read one of its fields. That keeps initial collection queries small, but it can produce one HTTP request per related object.

Use `prefetch=[...]` when you know you will access a related field for many objects:

```python
todos = Todo.filter(userId=1, prefetch=["user"])

for todo in todos:
    print(todo.title, todo.user.name)
```

Explicit prefetch requires the related model to implement `bulk_get_by_pks()`. The method receives the primary keys found in the parent payloads and must return related objects keyed by primary key:

```python
@json_object
class User(JSONObject):
    class_url = "/users/"

    id: int
    name: str

    @classmethod
    def bulk_get_by_pks(cls, pks, *, api=None):
        api = cls.api if api is None else api
        resp = api.get(cls.class_url, params={"id": sorted(pks)})
        return {
            user.pk: user
            for user in (cls(json=item, api=api) for item in resp.json())
        }
```

The exact implementation depends on the API. Some APIs accept repeated parameters such as `?id=1&id=2`; others use a comma-separated parameter such as `?id__in=1,2` or a dedicated bulk endpoint. Put that API-specific shape in `bulk_get_by_pks()`.

Prefetch works for single related-object fields and typed related collections. Embedded related objects do not need prefetching; `eazyrest` only bulk-loads primary-key-style values. When the same related primary key appears more than once in a prefetch batch, parent objects share the same related instance.

Prefetching is batched so collection iteration can stay lazy. `prefetch_batch_size` controls how many parent JSON objects are scanned before each related bulk lookup:

```python
Todo.prefetch_batch_size = 250
```

If you request prefetch for a related model that does not implement `bulk_get_by_pks()`, `eazyrest` raises `PrefetchNotSupported`. This is intentional: prefetch should map to a real API bulk lookup instead of guessing.

## Response-shape hooks

Override these hooks if an API wraps responses in envelopes:

- `collection_items()` for collection endpoints
- `object_json()` for single-object endpoints

For example, an API that returns singleton responses in a "result" field and collection responses in a "results" field should define a new base class derived from `JSONObject` with the following overrides:

```python
@classmethod
def collection_items(cls, payload):
    return payload["results"]

@classmethod
def object_json(cls, payload):
    return payload["result"]
```

## Write modes

APIs use lazy writes by default. Assignments update local state and queue a pending patch until `save()` is called.

```python
todo.completed = True
todo.save()
```

To make eager writes the default for all objects using an API, configure the client:

```python
api = API("https://example.com/", default_write_mode="eager")
Todo.register_api(api)
```

You can still override the mode for a specific object:

```python
todo = Todo(id=1, write_mode="eager")
todo.completed = True
```

Call `refresh()` to discard unsaved local changes and force the next read to reload object state from the API.

For out-of-band updates, such as a websocket message carrying a full or partial object payload, call `update_from_json()` to merge server state into an existing object without refetching it:

```python
todo.update_from_json({"id": 1, "completed": True})
```

Incoming fields are treated as authoritative server state, so queued lazy updates for those same JSON fields are discarded.

## API client

The `API` class wraps a `requests.Session`, resolves request URLs relative to `base_url`, applies a default timeout, and raises for HTTP error responses. To customize error handling for a particular API, override `raise_for_response()` in an `API` subclass.

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

Registering the API on a shared base class is the cleanest pattern. The API can be overridden per instance by passing `api=` when creating an object.
