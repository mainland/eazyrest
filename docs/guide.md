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

The decorator also accepts options for common API naming differences:

```python
@json_object(
    pk="uuid",
    field_map={"created_at": "createdAt"},
    exclude={"display_label"},
)
class User(JSONObject):
    class_url = "/users/"

    uuid: str
    created_at: str
    display_label: str
```

Use `pk=` when the primary-key attribute is not named `id`. Use `field_map=`
when the Python attribute name should differ from the JSON key. Use `exclude=`
for annotated attributes that should not become JSON-backed fields.

A field must not replace a `JSONObject` attribute such as `url`, `json`, `pk`,
or `save`, and `@json_object` raises `ValueError` for such a field. Use
`field_map=` to give the JSON key another attribute name:

```python
@json_object(field_map={"resource_url": "url"})
class User(JSONObject):
    class_url = "/users/"

    id: int
    resource_url: str
```

A decorated subclass of a decorated model keeps the fields of its base class
and adds its own.

## Loading and caching

You can construct an object from a primary key or from an already-loaded JSON
payload:

```python
lazy_user = User(id=1)
loaded_user = User(json={"id": 1, "name": "Ada"})
```

An object constructed from a primary key is lazy. The first access to `json` or
a JSON-backed field other than the primary key performs a `GET` against the
object's `url`, caches the payload, and caches converted field values as they
are read. Reading the primary key returns the value given to the constructor
without a request. An object constructed from `json=` starts with that payload
already loaded.

Call `refresh()` to discard cached JSON, converted field values, prefetched
related objects, and pending lazy updates. The next field read reloads the
object from the API.

```python
user.refresh()
print(user.name)
```

## Resource URLs

`url` returns the model resource path built from `class_url` and the primary
key. `absolute_url` resolves that path against the registered API base URL
using the same URL resolution as request methods:

```python
user = User(id=1)

print(user.url)
# /users/1/

print(user.absolute_url)
# https://api.example.com/users/1/
```

If `class_url` is already absolute, `absolute_url` leaves it absolute.

`url` percent-encodes the primary key, so a key always forms one path segment.
For example, the key `a/b` becomes `a%2Fb`.

## Field conversion

Field annotations drive JSON conversion when values are read from or written to
model instances. Scalar fields preserve normal JSON values, while a few common
Python types are converted automatically:

- `datetime.datetime` values decode from Unix timestamps and encode back to
  timestamps.
- `datetime.timedelta` values decode from duration strings and encode to ISO
  8601 durations such as `P1DT2H`. Decoding accepts ISO 8601, Django's
  `DurationField` format, the format of `str(timedelta)`, unit forms such as
  `1h 30m`, `2 weeks`, or Go's `1h30m0s`, and numbers of seconds. Durations
  with years or months are rejected in every format, because those units have
  no fixed length. See `parse_duration()`.
- `enum.Enum` values decode from their JSON values and encode back to their
  enum values.
- `JSONObject` subclasses decode from embedded objects or primary keys.

Collection annotations are converted recursively, so item annotations are
honored inside `list`, `tuple`, `set`, `frozenset`, and `Iterable` fields.
Annotate a tuple of any length as `tuple[T, ...]`:

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
    statuses: tuple[IssueStatus, ...]
```

In this example, JSON timestamp values in `starts_at` become
`datetime.datetime` objects, and JSON strings in `statuses` become
`IssueStatus` values. Assigning or creating objects with those typed values
encodes the nested items back to JSON.

Collection payloads must be JSON arrays or another iterable collection shape.
Strings, bytes, and dictionaries are rejected for collection fields so
malformed payloads fail before they can be interpreted item by item.

Other generic annotations, such as `dict[str, T]` or `Sequence[T]`, are not
converted. Reading such a field returns its JSON value unchanged.

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

`filter()` and `all()` return iterators over model instances. The request is
sent when `filter()` or `all()` is called, and model instances are created as
the iterator advances. An iterator can be consumed only once, so convert it to
a list to iterate more than once:

```python
users = list(User.all())
admins = list(User.filter(role="admin"))
```

`get_or_create()` returns the one object that matches its keyword arguments, or
creates an object from them if none matches. The lookup query and the creation
payload use the same JSON field names and conversions as `create()`:

```python
todo, created = Todo.get_or_create(user=user, title="Write docs")
```

`filter()`, `all()`, and `get()` also accept `prefetch=...` for explicit
bulk-loading of related fields when the related model implements
`bulk_get_by_pks()`. Pass a single field name as a string, or multiple field
names as a sequence. Prefetched related objects may be shared by identity
across parent objects in the same batch, similar to Django's
`prefetch_related()`.

## Prefetching related objects

Related fields are lazy by default. If a JSON payload stores a related object
as a primary key, `eazyrest` creates a lazy related object and fetches it when
you first read one of its fields. That keeps initial collection queries small,
but it can produce one HTTP request per related object.

Use `prefetch=...` when you know you will access a related field for many
objects:

```python
todos = Todo.filter(userId=1, prefetch="user")

for todo in todos:
    print(todo.title, todo.user.name)
```

Explicit prefetch requires the related model to implement `bulk_get_by_pks()`.
The method receives the primary keys found in the parent payloads and must
return related objects keyed by primary key:

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

The exact implementation depends on the API. Some APIs accept repeated
parameters such as `?id=1&id=2`, and others use a comma-separated parameter
such as `?id__in=1,2` or a dedicated bulk endpoint. Put that API-specific shape
in `bulk_get_by_pks()`.

Prefetch works for single related-object fields and typed related collections.
Embedded related objects do not need prefetching, because `eazyrest` only
bulk-loads primary-key-style values. When the same related primary key appears
more than once in a prefetch batch, parent objects share the same related
instance.

Prefetching is batched so collection iteration can stay lazy.
`prefetch_batch_size` controls how many parent JSON objects are scanned before
each related bulk lookup:

```python
Todo.prefetch_batch_size = 250
```

If you request prefetch for a related model that does not implement
`bulk_get_by_pks()`, `eazyrest` raises `PrefetchNotSupported`. This is
intentional: prefetch should map to a real API bulk lookup instead of guessing.
If a prefetch field is not a related-object field, `filter()`, `all()`, and
`get()` raise `ValueError` before sending a request.

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

For out-of-band updates, such as a websocket message carrying a full or partial
object payload, call `update_from_json()` to merge server state into an
existing object without refetching it:

```python
todo.update_from_json({"id": 1, "completed": True})
```

Incoming fields are treated as authoritative server state, so queued lazy
updates for those same JSON fields are discarded. If the object has not been
loaded, the payload becomes its JSON without a request. Reading a field that
the payload lacks then loads the full object.

## API client

The `API` class wraps a `requests.Session`, resolves request URLs relative to
`base_url`, applies a default timeout, and raises for HTTP error responses. To
customize error handling for a particular API, override `raise_for_response()`
in an `API` subclass.

Pass `verify=False` to disable TLS certificate verification, which also
suppresses urllib3's `InsecureRequestWarning` for the whole process.

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

A related object loads through the API registered for its own class, so models
served by different APIs can refer to each other. If the parent object has an
`api=` override, its related objects use that override too.
