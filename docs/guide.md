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

Use `pk=` when the primary-key attribute is not named `id`. A decorated
subclass keeps the primary key it inherits, even if it redeclares that field,
unless it passes `pk=`. Use `field_map=` when the Python attribute name should
differ from the JSON key. Use `exclude=` for annotated attributes that should
not become JSON-backed fields. An attribute annotated with `ClassVar`, such as
`class_url: ClassVar[str]`, is a class attribute and never becomes a field.
`@json_object` raises `ValueError` if `pk=` names no field of the class, if a
`field_map=` key names no field declared on the class, or if `exclude=` names
an attribute that the class does not annotate, so a misspelled option does not
go unnoticed. It also raises `ValueError` if two fields, including inherited
ones, map to the same JSON key, because each field caches its own value and
assigning one would leave the other stale.

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

A minor release of eazyrest may add attributes to `JSONObject`, such as methods
and hooks. A model with a field of the same name then raises `ValueError` when
it is decorated, and `field_map=` keeps the field under another attribute name,
as above.

Use `write_map=` for a field that the API returns under one key and accepts
under another, such as a related object returned nested under `user` and
written as its primary key under `user_id`. Reads, queries, and the object's
JSON use the field's key, and writes send it under the mapped key. The
[Django REST Framework guide](drf.md#map-serializer-names-and-relation-shapes)
shows an example.

Use `write_only=` for a field that the API accepts in writes but never returns,
such as a password. It can be passed to `create()` or assigned, and the next
write sends it, but the object never keeps it in its JSON, assigning it does
not load the object, and reading it raises `AttributeError`:

```python
@json_object(write_only={"password"})
class Account(JSONObject):
    class_url = "/accounts/"

    id: int
    name: str
    password: str


account = Account(id=1)
account.password = "correct horse battery staple"
account.save()  # PATCH {"password": "..."}, without loading the account
```

A decorated subclass of a decorated model keeps the fields of its base class
and adds its own. Redeclaring a field preserves its `field_map` and `write_map`
keys unless an explicit mapping replaces them. It also inherits `write_only`
and `read_only` policies when those options are omitted. An explicit set
applies to the fields redeclared on that class, so an empty set clears their
inherited policy. A model inherits fields through a single chain of decorated
classes. A class whose decorated ancestors are not subclasses of one another,
such as `class Both(Named, Dated)` for two decorated models, raises `TypeError`
when it is defined. Put shared fields on one decorated base class instead.
Undecorated base classes, which declare no fields, can be mixed in freely, for
example to share hooks such as `collection_items()`.

Use `read_only=` for returned values that ordinary writes must omit, such as
computed values and file URLs. These fields remain readable and usable in
queries, but assigning them or passing their ordinary values to `create()`
raises `AttributeError`. A change made in place to a read-only value, such as
appending to a list, is not tracked or sent. A read-only primary key can still
identify a lazy object, as in `User(id=1)`. Explicit `files=` uploads are
separate from the returned value and may name a read-only file field.

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
without a request, converted as if it were read from JSON. A `uuid.UUID` key
given as a string therefore reads as a `UUID`, before and after the object
loads, and the constructor raises `ValueError` for a key that is not a UUID. An
object constructed from `json=` starts with that payload already loaded. It
keeps a deep copy of the payload, as `update_from_json()` does, so changing the
payload or its nested values afterward does not change the object.

Other keyword arguments to the constructor are assignments, made after the
primary key is set. `User(id=1, name="Ada")` therefore loads user 1 and queues
a change to its name, and in eager mode it also sends a `PATCH`. Use `create()`
to create a new object. An object constructed without a primary key has `pk`
`None` until it loads, and loading it raises `ValueError` because it has no
URL.

Because loading is lazy, reading a field can send a request and can raise the
same exceptions as one, such as `requests.HTTPError` or
`requests.ConnectionError`. Related objects load the same way, so reading
`todo.user.name` can send one request for the todo and another for its user.
`hasattr()` also loads the object, and it propagates request errors instead of
returning `False`.

`json` returns the object's own payload, not a copy, so treat it as read-only.
Changing it, or a value inside it, bypasses change tracking, and `save()` may
then send nothing or the wrong fields. Assign fields to change an object, and
copy the payload with `copy.deepcopy()` before changing it for another purpose.

An assignment converts the value for its field but does not otherwise check its
type, unless the field's type is one that Pydantic validates. A value that
cannot be written as JSON, such as an object of a type that eazyrest does not
convert or a float that is not finite, raises `FieldConversionError` at the
assignment, which leaves the field unchanged and queues nothing. A write that
the server refuses is different: its updates stay pending, so a later `save()`
sends them again.

`repr()` shows an object's class and primary key, as in `<User id=1>`, without
loading it.

Call `refresh()` to discard cached JSON, converted field values, prefetched
related objects, and pending lazy updates. The next field read reloads the
object from the API.

```python
user.refresh()
print(user.name)
```

An object built from JSON, such as an item that `filter()` returns or a related
object embedded in another object's JSON, counts as loaded. Reading a field
that its JSON lacks raises `AttributeError` rather than sending a request. An
API whose collection returns summaries, with fewer fields than its detail
endpoint, therefore gives objects that lack the other fields. Call `refresh()`
on such an object to load its detail from its `url` at the next read.

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

`url` ends with a slash, as Django REST Framework's routers expect. For an API
whose object URLs have no trailing slash, set the API's `trailing_slash`
attribute to `False`, and `url` becomes `/users/1`:

```python
api = API("https://api.example.com/")
api.trailing_slash = False
```

`url` converts the primary key to its JSON value before percent-encoding it, so
an enum key uses its value and a key always forms one path segment. For
example, the key `a/b` becomes `a%2Fb`.

The keys `None`, `""`, `"."`, and `".."` have no resource URL. Accessing `url`
raises `ValueError` because those keys could address the collection or another
resource.

## Field conversion

Field annotations drive JSON conversion when values are read from or written to
model instances. Scalar fields preserve normal JSON values, while a few common
Python types are converted automatically:

- `datetime.datetime` values decode from ISO 8601 strings, such as
  `2024-04-01T12:00:00Z`, or from Unix timestamps, and encode to ISO 8601
  strings. A string without a UTC offset decodes to a naive `datetime`, and a
  naive `datetime` encodes without an offset. ISO 8601 offsets are whole
  minutes, so a `datetime` whose offset has seconds, as the historical local
  mean time of some time zones gives, raises `FieldConversionError` when it is
  assigned or written. See `parse_datetime()`.
- `datetime.date` values decode from extended ISO 8601 dates in `YYYY-MM-DD`
  form, such as `2024-04-01`, and encode back to that form on every supported
  Python. Basic and week dates are rejected. A date with a time is rejected,
  and so is a `datetime.datetime` assigned to the field, although it is also a
  `datetime.date`.
- `uuid.UUID` values decode from UUID strings and encode to the standard
  hyphenated form.
- `datetime.timedelta` values decode from duration strings and encode to ISO
  8601 durations such as `P1DT2H`. Decoding accepts ISO 8601, Django's
  `DurationField` format, the format of `str(timedelta)`, unit forms such as
  `1h 30m`, `2 weeks`, or Go's `1h30m0s`, and numbers of seconds. Durations
  with years or months are rejected in every format, because those units have
  no fixed length. See `parse_duration()`.
- `datetime.time` values decode from ISO 8601 times such as `09:30`,
  `09:30:00`, or `09:30:00.5+02:00`, and encode to ISO 8601 times such as
  `09:30:00`. As for `datetime.datetime`, a time without an offset decodes to a
  naive `time`, and an offset that is not a whole number of minutes raises
  `FieldConversionError`.
- `decimal.Decimal` values decode from decimal strings such as `"1.10"` or from
  JSON numbers, and encode to strings in positional notation that keep their
  digits, such as `"1.10"`, as Django REST Framework writes a `DecimalField`. A
  number decodes from its shortest decimal form, so `0.1` reads as
  `Decimal("0.1")`, but a JSON number that has more digits than a float holds
  has already lost them, so prefer an API that writes decimals as strings.
  `NaN`, infinities, and strings with spaces or underscores are rejected, as is
  a decimal whose exponent is beyond 4300 in either direction, such as
  `"1E+20000000"`, whose positional form would be megabytes long, or
  `"0E-20000"`, which writes each zero after the point.
- `enum.Enum` values decode from their JSON values and encode back to their
  enum values.
- `JSONObject` subclasses decode from embedded objects or primary keys.

Every field reads JSON `null` as `None` and writes `None` as `null`, whatever
its type, so `T | None` does not change conversion. A server that sends `null`
for a field typed `list[int]` therefore reads as `None`, and assigning `None`
to that field writes `null`.

A named type alias, such as `type Dates = list[datetime.datetime]` or a
`typing_extensions.TypeAliasType`, converts as the type it stands for, also
with type arguments, as in `ListOf[datetime.datetime]`. A string inside an
alias, as in `list["datetime.datetime"]`, names a type in the module that
defines the alias. An alias that refers to itself, or names a type that cannot
be found, raises `FieldConversionError`.

A value that a field cannot convert, when it is read, assigned, or passed to
`create()` or a query, raises `FieldConversionError`. Its message names the
model and field, as in
`Booking.day: Date has a year, month, or day out of range`, but not the value,
the same on every Python version, and the conversion's own error is its
`__cause__`. It is both a `TypeError` and a `ValueError`, so code that catches
either one keeps working.

The messages of eazyrest's errors name models, fields, and query parameters,
but they do not quote values, which can be secrets that the server sent or that
the code assigned. The exceptions are URLs: an HTTP error (`APIError` and
`NotFound`), `RedirectError`, `PaginationError`, `ResponseShapeError`, and
`LimitExceeded` for a size or time limit name the URLs involved, with their
query strings, so a query parameter's value can appear. Send a secret in a
header or the body, not in the query. They leave out a user and password in a
URL, including the `Location` of a redirect, so `http://user:pass@host/x?a=b`
appears as `http://host/x?a=b` in the message, `args`, and `repr()`, but the
error's `request` and `response` keep the real URL. The `repr()` of a `Page`,
and of an object known only by its URL, leaves them out too. An exception that
requests itself raises, such as `requests.ConnectionError`, quotes the URL as
requests does. A Pydantic error says where and why a value is invalid, as in
`Asset.rating: Input should be greater than 0`, without the input. A mapping
key that the input chose appears as `[key]`, and a value that Pydantic would
quote, such as a discriminated union's unknown tag, appears as `[redacted]`.
The error that eazyrest replaced remains the `__cause__`, and it can quote the
value, so a logged traceback can still expose it. A message that your own
conversion code or validator writes appears as written.

A JSON value that an enum lacks, such as a status that the server added after
the client was written, raises `FieldConversionError`. Python calls an enum's
`_missing_()` classmethod for a value it lacks, so an enum can map such values
to a member of its own instead:

```python
import enum


class TicketStatus(str, enum.Enum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value):
        return cls.UNKNOWN
```

Reading the field does not change the object's JSON, so saving other fields
leaves the server's value alone. Assigning `TicketStatus.UNKNOWN` to the field
sends `"unknown"`.

Related-object writes encode primary keys using the related model's primary-key
annotation. Prefetch lookups and `update_from_json()` compare decoded keys, so
equivalent UUID spellings identify the same object.

Collection annotations are converted recursively, so item annotations are
honored inside `list`, `tuple`, `set`, `frozenset`, and `deque` fields, and
inside the abstract `Iterable`, `Collection`, `Sequence`, `MutableSequence`,
`Set`, and `MutableSet` from `collections.abc`. An abstract type reads as a
concrete one: `Iterable`, `Collection`, and `Sequence` as a tuple,
`MutableSequence` as a list, `Set` as a `frozenset`, and `MutableSet` as a
`set`. A set writes its items in one order, whatever order it iterates them in:
numbers by value, strings by code point, and arrays and objects by their JSON
text. Equal sets therefore write equal JSON, and replacing a set with an equal
one is not a change that `save()` sends, even when the server lists the items
in another order. That holds for a field annotated as a set or frozenset, an
optional one, or a `NewType` of either. A set nested in a list, tuple, or
mapping field is compared in the server's order. A set inside a
Pydantic-converted field is written by Pydantic, in iteration order. Any other
parameterized type, such as `Counter[str]`, raises `FieldConversionError` when
the field is first used, because its items would otherwise pass through
unconverted. Annotate a tuple of any length as `tuple[T, ...]`. As Python's
types say, `tuple[T]` is a tuple of exactly one item, and a bare `tuple` is a
tuple of any length whose items read as they are:

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

In this example, ISO 8601 strings in `starts_at` become `datetime.datetime`
objects, and JSON strings in `statuses` become `IssueStatus` values. Assigning
or creating objects with those typed values encodes the nested items back to
JSON.

An `Iterable` field of related objects, such as `members: Iterable[User]`, is
lazy. Reading it creates no related objects, and each is created when iteration
reaches it. The field can be iterated only once, so that each related object,
and any change you make to it while iterating, exists only once. A second
iteration raises `UsageError`, which is a `RuntimeError`. To iterate more than
once, call `list()` on the field, or annotate it as `list[User]` or
`tuple[User, ...]`, which create the related objects when the field is first
read and keep them.

A relation that the API represents by the related object's URL rather than its
primary key, as Django REST Framework's hyperlinked fields do, is annotated
with `Hyperlinked()`, as in `owner: Annotated[User, Hyperlinked()]`. Reading it
gives an object that loads from that URL, and writing it sends the object's
absolute URL, or a URL assigned as a string as given. Queries send its primary
key, as DRF relation filters expect. A URL on an origin that the related
model's API does not allow raises `FieldConversionError`. The
[Django REST Framework guide](drf.md#use-absolute-urls-for-hyperlinked-apis)
gives the details.

A related object that a field returns keeps your unsaved changes for as long as
the field's JSON is unchanged. Loading the missing fields of an object keeps
the related objects whose JSON the load left unchanged, and assigning a related
object, or a `list`, `tuple`, or `set` of them, keeps the objects you assigned.
When the server changes a relation, the field reads new related objects, and an
object that you still hold keeps its own changes, which its `save()` sends.

Collection payloads must be JSON arrays or another iterable collection shape.
Strings, bytes, and dictionaries are rejected for collection fields so
malformed payloads fail before they can be interpreted item by item. The same
holds for values assigned to collection fields or passed to `create()`, which
raise `TypeError` unless they are collections. In `filter()` and `get()`, a
single value for a collection field, such as `Team.filter(members=user)`, is
sent as one item and matches the objects whose collection holds it.

A `TypedDict` field reads a JSON object whose declared keys convert by their
annotations, as fields do, and writes each declared key in its JSON form. A key
that the `TypedDict` does not declare is kept as JSON, so that writing the
value back does not delete it on the server. A key may be missing, as a field
may, and reading it from the dictionary then raises `KeyError`, so `Required`,
`NotRequired`, and `ReadOnly` do not change conversion. A `TypedDict` may refer
to itself. `dict[str, T]`, `Mapping[str, T]`, and `MutableMapping[str, T]` read
a JSON object whose values all convert as `T`, as a `dict`:

```python
import datetime

from typing import TypedDict

from eazyrest import JSONObject, json_object


class Window(TypedDict):
    start: datetime.datetime
    length: datetime.timedelta
    note: str


@json_object
class Reservation(JSONObject):
    class_url = "/reservations/"

    id: int
    window: Window
    holidays: dict[str, datetime.date]
```

Here `reservation.window["start"]` is a `datetime.datetime`, and after
`reservation.window["length"] = datetime.timedelta(hours=2)`, `save()` sends
`"PT2H"`. eazyrest converts these types but does not validate them: as for a
field, a key declared `int` that holds a string reads the string. An error
names a declared key, as in `Reservation.window: ['start']: ...`, and a
collection item's position, as in `[1]`, but shows a key of a `dict[str, T]`,
which the server chose, as `[key]`. A value under an undeclared key is written
as it is, so assigning one that is not JSON, such as a `datetime`, raises
`FieldConversionError`. To have a `TypedDict` validated, annotate it for
Pydantic, as [Pydantic conversion](#pydantic-conversion) describes. A
`TypedDict` decorated with `pydantic.with_config()` is annotated for Pydantic,
which converts and validates it by its whole config, aliases included, as
[Pydantic conversion](#pydantic-conversion) describes. A plain `TypedDict`
whose key has a Pydantic alias raises `FieldConversionError`, because eazyrest
reads and writes keys by their own names, so decorate such a `TypedDict` for
Pydantic.

A fixed-length tuple, such as `tuple[int, datetime.date]`, reads a JSON array
of exactly that length, each position by its own annotation, and a `NamedTuple`
reads one by its fields, in order. Both write lists. A standard library
dataclass reads a JSON object by the fields that its `__init__` takes, each by
its annotation, and writes them all. A dataclass cannot keep keys that it does
not declare, and writing it back would delete them on the server, so a value
with such a key raises `FieldConversionError`, as does a value without a
required field. For the same reason, assigning an instance of a subclass that
adds fields raises `FieldConversionError`, because writing it would drop them.

A union reads and writes its JSON value as it is when every member does, as
`int | str`'s members do. A union with a member that converts, such as
`str | datetime.datetime`, or with a `Literal` that holds an enum member, which
reads as the member, raises `FieldConversionError` when the field is first
used, because a JSON value does not say which member it is. Annotate such a
union for Pydantic, with a discriminator if needed.

Some annotations raise `FieldConversionError` when the field is first used,
because eazyrest cannot read them as their types. A mapping whose keys are not
strings is one, because JSON object keys are strings. So is a model inside a
`TypedDict`, a mapping, a fixed-length tuple, a `NamedTuple`, or a dataclass,
because eazyrest does not convert related objects there yet, so annotate such a
key or field with the model's primary key type, such as `int`. So are a
`TypedDict` that is closed or has extra items (PEP 728), and a generic
`TypedDict`, `NamedTuple`, or dataclass with type arguments. Annotate such a
type for Pydantic, or annotate its JSON form, such as `dict[str, Any]`.

A field's value is cached when it is first read, so later reads return the same
value. If you change that value in place, `save()` sends the change, as it does
for an assignment:

```python
profile.settings["theme"] = "dark"
profile.save()
```

The object detects such a change by comparing the value's encoding with its
encoding when it was read. It does so only when `save()` runs, when an eager
assignment sends a `PATCH`, or before the object loads missing fields, so
`json` does not show the change before then. In eager mode, a change made in
place is therefore sent by the next `save()` or eager assignment, not
immediately. Assigning the field, `refresh()`, and an `update_from_json()`
payload that holds the field each discard a change made in place that has not
been sent.

## Pydantic conversion

Pydantic converts a field's value when the field's type mentions Pydantic. Such
a type is, or contains, one of the following:

- A class with Pydantic's schema hook, such as a Pydantic model,
  `pydantic.Base64Bytes`, or a type from `pydantic-extra-types`.
- A class with a Pydantic config, such as a Pydantic dataclass or a `TypedDict`
  decorated with `pydantic.with_config()`.
- `Annotated` metadata from Pydantic, such as `pydantic.Field()`,
  `pydantic.Strict()`, or `pydantic.AfterValidator()`.

Every other type converts as [Field conversion](#field-conversion) describes.
eazyrest converts a plain `TypedDict` itself, without validating it, and a
`Literal` passes through unchecked, except that a JSON value of one of its enum
members reads as the member, and the member writes as that JSON value. To have
Pydantic check such a type, annotate it with `Annotated[T, pydantic.Field()]`.
Constraints from `annotated_types` alone, such as `Annotated[int, Gt(0)]`, do
not mention Pydantic, because other libraries use them too.

Some of Pydantic's own types are made only of such constraints, so they do not
mention Pydantic either. `pydantic.PositiveInt`, for example, is
`Annotated[int, Gt(0)]`, and the same holds for `NegativeInt`,
`NonNegativeInt`, `PositiveFloat`, their relatives, and the result of
`conint()` or `confloat()` with only bounds. A field of such a type reads and
writes any value of the underlying type unchecked. Annotate it as
`Annotated[PositiveInt, pydantic.Field()]` to have Pydantic check it. Types
that carry Pydantic's own metadata, such as `StrictInt` and `FiniteFloat`,
mention Pydantic and are checked.

Pydantic conversion requires the `pydantic` extra, which installs Pydantic 2.12
or later:

```bash
pip install "eazyrest[pydantic]"
```

```python
import datetime
from typing import Annotated, Literal

from pydantic import Base64Bytes, BaseModel, ConfigDict, Field, with_config
from typing_extensions import TypedDict

from eazyrest import JSONObject, json_object
from eazyrest.pydantic import Duration


@with_config(ConfigDict(extra="allow"))
class Rendering(TypedDict):
    width: int
    height: int
    timeout: Duration


class Point(BaseModel):
    model_config = ConfigDict(extra="allow")

    x: float
    y: float


@json_object
class Logo(JSONObject):
    class_url = "/logos/"

    id: int
    image: Base64Bytes
    rendering: Annotated[Rendering, Field()]
    anchors: list[Point]
    shape: Annotated[Literal["square", "round"], Field()]
    published: Annotated[datetime.datetime, Field(description="First shown")]
```

Reading `Logo.image` decodes the base64 JSON string to `bytes`, and reading
`Logo.rendering` checks the mapping against `Rendering`, because its annotation
asks Pydantic to. A value that Pydantic rejects raises `FieldConversionError`.
Pydantic on Python before 3.12 requires `TypedDict` from `typing_extensions`.

eazyrest still handles related objects, `T | None`, collections,
`dict[str, T]`, and `TypedDict` keys around a type that Pydantic converts, so
`Logo.anchors` converts each item to a `Point`, and `dict[str, Point]` each
value. Pydantic converts the whole of any other type, such as `Point | str`,
which therefore cannot contain an eazyrest model.

Assigning a value, or passing it to `create()` or a query, writes the JSON
value that Pydantic serializes for a round trip, so a field typed as
`pydantic.Json[dict[str, int]]` writes JSON text, as it reads. A value of
another type, such as a `dict` assigned to a field typed as a Pydantic model,
is validated first. A field typed as `Base64Bytes` therefore writes both
`b"hello"` and `"aGVsbG8="` as `"aGVsbG8="`. A value that the field could not
read back raises `FieldConversionError` and sends nothing. This includes a
value that breaks a constraint, such as `0` for a field typed as
`Annotated[int, Field(gt=0)]`. As for every field, `None` reads and writes as
`null`.

A Pydantic model is written by its fields' aliases, as Pydantic reads it, so a
model whose config sets `alias_generator=to_camel` writes `darkMode` rather
than `dark_mode`. A value must also read back as the value written. Pydantic
writes a `SecretStr` or `SecretBytes` as `"**********"`, so writing one raises
`FieldConversionError`, and so does reading one, because a read encodes the
value to detect changes made in place. To send a secret, annotate its type with
a serializer that reveals it:

```python
from typing import Annotated

from pydantic import PlainSerializer, SecretStr

RevealedSecret = Annotated[
    SecretStr,
    PlainSerializer(
        lambda secret: secret.get_secret_value(),
        return_type=str,
        when_used="json",
    ),
]
```

A field typed as `RevealedSecret` writes the secret itself, and its `repr()`
still hides it.

`Annotated[T, ...]` converts as `T` does, and the metadata refines that
conversion. Where eazyrest converts `T` itself, as for `datetime.datetime`,
`datetime.date`, `datetime.time`, `datetime.timedelta`, `uuid.UUID`,
`decimal.Decimal`, and enums, eazyrest's conversion reads and writes the value
and Pydantic applies the metadata. `Logo.published` therefore reads as a plain
`datetime.datetime` field would, a constraint such as
`Field(ge=datetime.timedelta(0))` checks the decoded value, and a
`pydantic.PlainSerializer` replaces eazyrest's encoding. This holds for every
type that the annotation names, such as the items of
`Annotated[list[datetime.datetime], Field(max_length=3)]`, and for a `NewType`
of one of these types, which converts by its own entry in the model's table, if
it has one, as a plain field of it does. A model's conversions of other types,
such as `IPv4Address`, do not apply inside an annotation for Pydantic, which
converts those types by its own rules.

Inside a class that Pydantic converts, such as a Pydantic model, or a
`TypedDict` annotated for Pydantic, Pydantic's own rules apply, and they differ
from eazyrest's:

| Type | eazyrest | Pydantic |
| --- | --- | --- |
| `datetime.datetime` | Reads a number as seconds, and rejects `1.7e12` as out of range | Reads `1.7e12` as milliseconds |
| `datetime.datetime` | A UTC offset becomes `datetime.timezone` | Uses its own `TzInfo` class |
| `datetime.date` | Rejects numbers | Reads a number as a timestamp |
| `datetime.timedelta` | Reads Django's `DurationField` format, such as `1 02:03:04`, and rejects years and months | Rejects Django's format, and reads `P1Y` as 365 days |
| `datetime.time` | Rejects numbers | Reads a number as seconds since midnight |
| `decimal.Decimal` | Rejects spaces and underscores, and writes `Decimal("1E+2")` as `"100"` | Reads them, and writes `"1E+2"` |
| Enums | Reads only the members' values | Also reads `"1"` for a member whose value is `1` |

To convert a value inside such a class as an eazyrest field would, annotate it
with `Datetime`, `Date`, `Time`, `Duration`, `UUID`, or `Decimal` from
`eazyrest.pydantic`, as `Rendering.timeout` is above. These types convert by
the [conversion table](#custom-field-conversion) of the model whose field holds
them, so they follow its conversions, such as one that writes datetimes as
timestamps, and take their [JSON schemas](#json-schemas) from those conversions
too. Outside a model, they convert by the default table. Pydantic's own types,
such as `AwareDatetime`, keep Pydantic's rules.

A server may send keys that a `TypedDict` or a Pydantic model does not declare,
and saving a value that dropped them would delete them on the server. A `PATCH`
leaves the fields that it omits alone, but it replaces the whole value of each
field that it sends, so a key missing from that value is deleted. eazyrest
keeps such keys in a `TypedDict` that it converts, as
[Field conversion](#field-conversion) describes. Under Pydantic's default
config, both a `TypedDict` and a Pydantic model drop such keys. A `TypedDict`
that Pydantic converts must therefore set `extra="allow"`, or `extra="forbid"`
to reject such keys, or using the field raises `FieldConversionError`. Under
`extra="forbid"`, Pydantic writes a value without the keys that it does not
declare, so writing such a value raises `FieldConversionError` rather than
dropping them, at any depth. So does writing an instance of a dataclass's
subclass that has fields of its own, which Pydantic writes as the declared
class. A `TypedDict` or dataclass nested inside a field's type must follow the
same rules, at any depth. A Pydantic model follows its own config, so give it
`extra="allow"`, as `Point` has. A `TypedDict`, dataclass, or `NamedTuple`
without a config of its own inside a model takes the model's config. Pydantic
never writes undeclared keys for a dataclass, of Pydantic or the standard
library, even under `extra="allow"`, so a dataclass that a field type names
must set `extra="forbid"`, as with
`@pydantic.with_config(pydantic.ConfigDict(extra="forbid"))`, or be inside a
model with `extra="forbid"` without a config of its own, or using the field
raises `FieldConversionError`. A Pydantic dataclass always has a config of its
own, empty if declared without one, so it is accepted only with
`extra="forbid"` on itself, even inside a model with `extra="forbid"`. Nor does
Pydantic write a dataclass's init-only fields, of type `dataclasses.InitVar`,
or a field of a model or dataclass that it excludes from writes, with
`Field(exclude=True)` or `exclude_if`, so such a class raises
`FieldConversionError` too. A `TypedDict`'s extra items, which `extra_items=`
types, are checked as its keys are, and a dataclass's class variables, which
Pydantic neither reads nor writes, are not checked. A class whose fields'
annotations cannot be resolved cannot be checked, so it raises
`FieldConversionError`. A serializer that leaves a key out, such as a model's
`model_serializer`, is a model's own choice, which eazyrest does not check.

A value that Pydantic converts can be changed in place, as
[Field conversion](#field-conversion) describes. For example,
`logo.anchors[0].x = 2` followed by `logo.save()` sends the changed `anchors`.

## Custom field conversion

A model converts field values by a table of conversions, keyed by type. The
default table converts `datetime`, `date`, `time`, `timedelta`, `UUID`,
`Decimal`, and enums, as [Field conversion](#field-conversion) describes, and
any other type reads and writes its JSON value as it is. To convert another
type, or to convert one of these differently, declare `conversions` on a shared
base model, mapping the type to a `Conversion`:

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
of the declared type as JSON. `schema`, which is optional, is the JSON schema
of the JSON form, for [`json_schema()`](#json-schemas), or a function of the
declared type and the mode, `"read"` or `"write"`, that returns it. Without
one, the type's schema allows any value. `json_schema()` moves the schema's
`$defs` into the model's, so its references may name only its own definitions,
as `#/$defs/Name` or a pointer into one. A schema that refers to itself as `#`,
or has `$id`, an anchor, or `$defs` below its root, raises `TypeError`. Put the
recursive part in `$defs` and refer to it there. Both receive the declared
type, so one conversion can serve a family of types, as the default one for
enums does. The conversion applies wherever its type appears, so `Host.allowed`
converts each item. `null` reads as `None` and `None` writes as `null` without
a conversion, and `encode` is called only for instances of the type that the
entry is keyed by: any other value, such as a string assigned to
`Host.address`, is written as it is. `decode` and `encode` report a value that
they cannot convert by raising `TypeError` or `ValueError`, which reaches the
caller as a `FieldConversionError` that names the field, with the error as its
cause. Their messages should not quote the value, which may be a secret. Any
other exception is a bug in the conversion, and propagates unchanged. A value
written, or returned by `encode`, must be JSON: one that is not, such as an
`IPv4Address` assigned to a field of a model without its conversion, raises
`FieldConversionError` when it is assigned.

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
table, so an entry can reuse a default's `decode`, `encode`, or `schema`.

An entry can also convert a class whose structure eazyrest would otherwise
convert: a dataclass, a `NamedTuple`, a `TypedDict`, or a class that Pydantic
converts, such as a Pydantic model. The entry replaces the structure, so a
`Money` dataclass can read and write `"12 USD"`, and eazyrest does not analyze
the class's fields, so the entry can convert one that eazyrest refuses, such as
a dataclass that holds a model. A `NewType` of such a class converts by its own
entry, else by the class's. An entry for a `TypedDict` converts exactly that
`TypedDict`, because a `TypedDict` that extends it does not have it in its MRO,
and its `encode` is called for any mapping, because a `TypedDict` has no
instances. A field annotated with `Annotated[T, pydantic.Field()]` names
Pydantic itself, which is more specific than the table, so Pydantic converts
it, rather than an entry for `T`, except that the types that eazyrest converts
itself, and `NewType`s of them, still convert by the table, as
[Pydantic conversion](#pydantic-conversion) describes.

Pydantic conversion can replace a custom one: a field typed as
`Annotated[IPv4Address, pydantic.Field()]` reads and writes addresses as
strings.

Decoding accepts Unix timestamps for `datetime.datetime` fields, but encoding
writes ISO 8601 strings. For an API that requires timestamps in requests,
replace the `datetime` conversion:

```python
import datetime

from eazyrest import Conversion, JSONObject, parse_datetime


class TimestampObject(JSONObject):
    conversions = {
        datetime.datetime: Conversion(
            decode=lambda value, cls: parse_datetime(value),
            encode=lambda value, cls: value.timestamp(),
        ),
    }
```

## JSON schemas

`json_schema()` returns a JSON schema of the objects that a class reads, or
with `mode="write"`, of what its writes send, so a client can check its models
against a server's OpenAPI schema:

```python
schema = Todo.json_schema()
assert schema["properties"]["userId"] == {
    "anyOf": [{"type": "integer"}, {"type": "object"}]
}
```

The read schema's properties are the JSON keys after `field_map`, without
write-only fields. The write schema's are the keys after `write_map`, without
read-only fields and with write-only ones. A type with a conversion has the
conversion's schema, so a built-in one gives its JSON form, such as a string
with `format` `date-time` for `datetime.datetime` and an enum the values of its
members, and a `Literal` gives its members' JSON values. A read schema also
allows a number for `datetime.datetime` and `datetime.timedelta`, which read a
Unix timestamp and a number of seconds, and for `decimal.Decimal`. `T | None`
also allows `null`, and a collection is an array, whose items are unique for a
set. A relation is the related model's primary key, or a URL for a
`Hyperlinked()` relation, and a read also accepts the related object embedded
as JSON. A field that Pydantic converts gives Pydantic's validation schema in a
read schema, and its serialization schema in a write schema, so that a
serializer's return type describes what it writes, except that a
`pydantic.Json` field is the JSON text that it writes and a computed field,
which a write does not send, is left out. Its definitions, like those of a
conversion's schema, move into the schema's `$defs`, renamed with the
references to them when two share a name. A plain `TypedDict` is an object with
a property for each declared key that allows other properties, and one inside
itself is any object. A `NamedTuple` or dataclass inside itself is likewise any
array or object. `dict[str, T]` is an object whose properties all have `T`'s
schema. A type whose JSON form `eazyrest` does not check, such as `Any`, allows
any value.

`eazyrest` has no required fields, so the schema lists none. Every field reads
`null` as `None`, but the schema allows `null` only where the annotation does,
so that it describes the declared types, as a server's schema does. It allows
properties that the class does not declare, which a server may send.

The schema is in the JSON Schema 2020-12 dialect, which its `$schema` names.
OpenAPI 3.1 uses the same dialect, but OpenAPI 3.0, which Django REST
Framework's schema generator writes, uses an older one, which has no
`prefixItems`, the keyword of a fixed-length tuple's items, and no `$defs`.
Compare such a schema property by property, rather than validating with it.

### Finding fields that the server never sends

A declared field that the server does not send raises `AttributeError` only
when it is read, so a misspelled or removed field can go unnoticed. To find
such fields, read many objects and report the read keys that none of them
holds. A key missing from only some objects is a field that the server leaves
out of some, not drift, so it is not reported:

```python
def fields_never_sent(model, objects):
    """Return the read keys of model that no object's JSON holds."""
    keys = set(model.json_schema()["properties"])
    for obj in objects:
        keys -= obj.json.keys()
    return keys
```

For example, `fields_never_sent(Machine, Machine.filter())` in a deployment
test compares every machine that the list endpoint returns. A list endpoint
whose serializer leaves out fields that the detail endpoint sends reports those
fields too, so compare objects loaded from the detail endpoint for such an API.

## Collections

`filter()` and `all()` return iterators over model instances. The request is
sent when `filter()` or `all()` is called, and model instances are created as
the iterator advances. An iterator can be consumed only once, so convert it to
a list to iterate more than once:

```python
users = list(User.all())
admins = list(User.filter(role="admin"))
```

The iterator is a `Results`, whose `count` is the size of the whole collection
when the response reports it, as a page of Django REST Framework's
`PageNumberPagination` or `LimitOffsetPagination` does, and `None` otherwise.
It is known as soon as `filter()` returns, before any item or later page is
read:

```python
admins = User.filter(role="admin")
print(f"{admins.count} admins")
```

`filter()` takes `count` from the iterable that the collection hooks return.
`follow_next_links()`, which reads pages by default, returns one with the first
page's `count` member, and a hook of your own can return any iterable with an
integer `count` attribute.

`filter()` and `get()` encode a keyword argument named after a JSON-backed
field with the same JSON field name and conversion as `create()`. For example,
`Todo.filter(user=user)` sends the user's primary key under the field's JSON
key, a `datetime.datetime` value is sent as an ISO 8601 string, and a boolean
is sent as `true` or `false`, as JSON writes it. Other keyword arguments, such
as `ordering` or `created__gte`, are sent unchanged, as their `str()`, except
that a model instance, also as an item of a collection, is sent as its primary
key. A plain enum member is therefore sent as its `str()`, such as
`Status.DONE`, so pass its `value`. A collection repeats the parameter, so
`Todo.filter(tag=[a, b])` sends `tag=a&tag=b`, which a view that calls Django's
`QueryDict.getlist()` reads, as django-filter does for a filter on a
many-to-many field. django-filter's `__in` and `__range` lookups, and DRF's
`ordering`, read one comma-separated value instead, and see only the last of
repeated ones, so join the values yourself, as in
`Todo.filter(user__in=",".join(str(user.pk) for user in users))` or
`Todo.filter(ordering="due,-priority")`.

The keyword arguments `api`, `prefetch`, `defaults`, `request_options`,
`params`, and `url` of the query methods are their own options, not query
parameters. Each query method reserves the ones that it does not take, so
`filter(url=...)` and `filter(defaults=...)` raise `TypeError`. Pass a query
parameter of any name, including one of these, in the `params=` mapping, which
`filter()`, `get()`, and `get_or_create()` encode as they encode keyword
arguments. `create()` likewise takes field values in `fields=`, for a key such
as `api`, `fields`, or `request_options`, which `create()` reserves for itself:

```python
todos = Todo.filter(params={"api": "v2"}, completed=False)
todo = Todo.create(fields={"api": "v2"}, title="Write docs")
```

Every keyword argument to `filter()`, `get()`, or the lookup of
`get_or_create()` must have a value that requests sends intact. It must not be
`None`, an empty collection, or a mapping, and a collection must not hold
`None` or another collection. requests omits `None` and empty collections,
sends only a mapping's keys, and flattens nested collections, so the query
would ignore or change the constraint, and `get()` could return an object that
does not match. These methods raise `ValueError` instead of sending such a
query. Any iterable other than a string, bytes, or a mapping, such as a
generator, a `deque`, or a `range`, counts as a collection, and is read once.
Use the API's own null filter, such as `parent__isnull=True` in django-filter,
to match objects whose field is null.

`get_or_create()` returns the one object that matches its keyword arguments, or
creates an object from them if none matches. Its keyword arguments are sent in
the lookup's query string, which servers and proxies may log, so pass a value
that only the creation needs, such as a password, in `defaults`. The lookup
query and the creation payload use the same JSON field names and conversions as
`create()`. As in Django, a keyword argument that contains `__` and is not a
field, such as `title__iexact`, is sent only in the lookup query:

```python
todo, created = Todo.get_or_create(user=user, title="Write docs")
```

`get()` and `get_or_create()` trust the server to apply every lookup parameter.
A server that ignores a parameter, as django-filter does for one that names no
filter, returns objects whatever their value for that field. `get()` can then
return an object that does not match, and `get_or_create()` can return one with
`created=False` instead of creating an object. `eazyrest` does not check the
returned objects, because it cannot know how the server matches a parameter,
for example case-insensitively. Make sure that the API filters on each field
you look up by. The Django REST Framework guide shows
[a filter backend that rejects unknown parameters](drf.md#reject-unknown-filter-parameters).

`get()` queries the collection, as `filter()` does, and expects exactly one
object, so `get()` without parameters requires a collection of one object. To
load an object by its primary key, construct it, as in `User(id=1)`. `get()`
raises `DoesNotExist` when nothing matches and `MultipleObjectsReturned` when
more than one object does, and `get_or_create()` raises the latter too. Their
messages name the model and the lookup parameters, but not their values, as in
`No Todo matching title exists`. These exceptions and `PrefetchNotSupported`
derive from `EazyrestError`, so one `except` clause catches every error that
eazyrest itself defines.

`get_or_create()` is not atomic. It sends a `GET` and then, if nothing matched,
a `POST`, so two clients can both create the object. Rely on a uniqueness
constraint on the server, and handle the error it returns.

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
The method receives the primary keys found in the parent payloads, and as `api`
the client to request them with, which is the query's `api=` or else the
related model's own API. It must return related objects keyed by primary key:

```python
@json_object
class User(JSONObject):
    class_url = "/users/"

    id: int
    name: str

    @classmethod
    def bulk_get_by_pks(cls, pks, *, api, **kwargs):
        resp = api.get(cls.class_url, params={"id": sorted(pks)})
        return {
            user.pk: user
            for user in (cls(json=item, api=api) for item in api.json(resp))
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

A field annotated with `Hyperlinked()` holds URLs rather than primary keys, so
its related model implements `bulk_get_by_urls(cls, urls, *, api, **kwargs)`
instead, which receives the absolute URLs and returns the objects keyed by URL.
A URL is checked as reading the field checks it, so a URL on an origin that the
API does not allow is not loaded, and reading the field still raises
`FieldConversionError`. Two spellings of one URL, such as a relative and an
absolute one, name the same object.

Related objects inherit their parent's write mode, including when they are
prefetched. Enabling prefetch preserves when assignments are saved, even if the
related model's API has a different default write mode. A prefetched object is
shared by the parents in its batch, so it takes the write mode that they have
when the batch is loaded. Changing one parent's write mode afterward does not
change the mode of its prefetched related objects.

Prefetching is batched so collection iteration can stay lazy.
`prefetch_batch_size` controls how many parent JSON objects are scanned before
each related bulk lookup:

```python
Todo.prefetch_batch_size = 250
```

If you request prefetch for a related model that does not implement
`bulk_get_by_pks()`, `filter()`, `all()`, and `get()` raise
`PrefetchNotSupported` before sending a request. This is intentional: prefetch
should map to a real API bulk lookup instead of guessing. If a prefetch field
is not a related-object field, they raise `ValueError`, also before sending a
request.

## Response-shape hooks

Override these hooks if an API wraps responses in envelopes:

- `collection_items()` for collection endpoints
- `collection_items_from_response()` for collection endpoints whose items or
  later pages depend on the response itself, such as its headers or URL
- `object_json()` for single-object endpoints

The collection hooks receive the query's client as `api`, the API that the
query passed as `api=` or the class's registered API. Send any request for
later pages with it, so that a query with `api=` reads every page from that
API.

When the query has `request_options=`, `api` is a copy of the API that adds
them to every request and shares the API's session. Use it to send requests,
not to configure, close, or compare the API. Like a model write, the copy
raises `RedirectError` for a redirect that it did not follow, so a hook that
sends `allow_redirects=False` and reads the 3xx response itself works only in a
query without options. The copy is made with `copy.copy()`, so a subclass of
`API` that changes its own state while it sends requests, such as a refreshed
token, must keep that state on the session, which the copy shares.

Every hook may receive more keyword arguments in later 1.x releases, to tell it
more about the query or the write: those that take `api`, which are
`collection_items()`, `collection_items_from_response()`, `collection_page()`,
`bulk_get_by_pks()`, and `bulk_get_by_urls()`, and also `object_json()`,
`merge_response()`, and `API.raise_for_response()`. An override must therefore
accept `**kwargs`, and pass them on when it calls another hook or `super()`, as
the examples here do. A class that overrides a hook without `**kwargs`, also an
undecorated base class, raises `TypeError` when it is defined, rather than when
a query first calls the hook.

The hooks are typed by two protocols rather than by requests' classes, so that
a later 1.x release can send requests with another HTTP library without
changing them. `api` is a `Client`, which has the request methods, `base_url`,
`resolve_url()`, `json()`, and the limits, and a response is a `Response`,
which has the status, reason, headers, body, URL, request method, and redirect
history. At run time they are an `API` and a `requests.Response`. Annotate an
override's parameters with the protocols, because an override that annotates
them `API` or `requests.Response` fails type checking, and cast them to use
anything else, such as `api.session` or `resp.links`. The query methods,
`create()`, and the model constructor take a `Client` as `api=`, so a hook can
pass its `api` on.

For example, an API that returns singleton responses in a "result" field and
collection responses in a "results" field should define a new base class
derived from `JSONObject` with the following overrides:

```python
@classmethod
def collection_items(cls, payload, *, api, **kwargs):
    return payload["results"]


@classmethod
def object_json(cls, payload, **kwargs):
    return payload["result"]
```

### Following pages

When a collection endpoint splits its results into pages linked by a next-page
URL, as Django REST Framework's paginators do, `filter()` and `all()` read
every page by default. The default `collection_items_from_response()` returns
`follow_next_links(api, api.json(resp), url=resp.url)`, which yields the items
of each page and requests the next page only when iteration reaches it, so
`filter()` and `all()` stay lazy. A list payload is a single unpaginated page.
With `prefetch=`, `filter()` and `all()` read up to `prefetch_batch_size`
objects ahead of iteration to fill each batch, so they can request later pages
early.

`follow_next_links()` reads a page's items from `results` and the next page's
URL from `next`, as Django REST Framework does. For other names, override
`collection_items_from_response()` and pass `results_key=` and `next_key=`:

```python
from eazyrest import JSONObject, follow_next_links


class PagedObject(JSONObject):
    @classmethod
    def collection_items_from_response(cls, resp, *, api, **kwargs):
        return follow_next_links(
            api,
            api.json(resp),
            url=resp.url,
            results_key="items",
            next_key="next_page",
        )
```

A page whose next link is null, empty, or absent is the last page. A next link
to a page that was already read raises `PaginationError`, rather than reading
the same pages forever. A page that is not JSON, or whose results are not a
list, raises `ResponseShapeError`, as does a payload that is neither a list nor
a page.

A class that overrides `collection_items()` replaces this default: the default
`collection_items_from_response()` then passes the decoded payload to it, and
pages only if it does. The default `collection_items()` accepts only a list, so
code that reads a collection itself, such as a `bulk_get_by_pks()` hook, should
call `filter()` rather than `collection_items()`.

A next-page URL is resolved against the URL of the page that holds it, as a
browser resolves a link. An absolute URL, which is what Django REST Framework
sends, is used as is. A path such as `/v1/items/?page=2` is resolved against
the server's origin, and a query such as `?page=2` against the page's path. A
call of `follow_next_links()` without `url=` resolves links in the first page
against `base_url`, so pass the URL of the first page, as the default does.

`follow_next_links()` raises `PaginationError` for a next-page URL whose
scheme, host, or port differs from `base_url`, because the session would send
its headers and credentials there too. A server behind a misconfigured reverse
proxy can send such links, for example `http://` links from a server that does
not know that TLS ended at the proxy. Pass `allow_other_origins=True` for an
API that serves pages from another host.

The same origin check applies to every redirect from a next-page request,
before the redirect is followed. Existing session response hooks still run.
With `allow_other_origins=True`, a redirect is checked as for every other
request: it must lead to the origin of the page request or to one of the API's
`redirect_origins`. A query's client that does not follow redirects, because of
`request_options={"allow_redirects": False}`, skips the check, since nothing is
followed, and raises `RedirectError` for a redirect from a next-page request,
whichever origin it names.

Some APIs, such as GitHub's, return each page as a JSON list and name the next
page in a `Link` header with `rel="next"`. Return `follow_link_headers()` from
`collection_items_from_response()` for such an API. It resolves links and
checks origins as `follow_next_links()` does:

```python
from eazyrest import JSONObject, follow_link_headers


class GitHubObject(JSONObject):
    @classmethod
    def collection_items_from_response(cls, resp, *, api, **kwargs):
        return follow_link_headers(api, resp)
```

### Pages

`filter()` iterates every page of a collection. To show one page at a time
instead, as a paginated view does, call `get_page()`, which takes the same
arguments as `filter()`, and `url=`, and returns a `Page` of objects. Its
`count` is the size of the whole collection, if the page reports it, and
`next_url` and `previous_url` are the URLs of the pages around it.
`next_page()` and `previous_page()` request those pages with the same API,
request options, and prefetch, and return `None` at either end:

```python
page = Todo.get_page(completed=False)
for todo in page:
    print(todo.title)
print(page.count, page.next_url)
older = page.next_page()
```

`len(page)` is the number of items on the page, so a page without items is
false, even when its `next_url` names another page. Loop over pages with
`while page is not None:`, not `while page:`, which would stop at such a page.

`get_page(url=...)` fetches a page by a URL saved from `next_url` or
`previous_url`. The URL must have the origin of the API's base URL, and it
cannot be combined with query parameters, because a page's URL holds its query.

The `collection_page()` hook reads each page. By default it returns
`next_links_page(api, resp)`, which reads Django REST Framework's `results`,
`next`, `previous`, and `count` members. Pass other member names to
`next_links_page()`, or return `link_headers_page(api, resp)` for an API that
links its pages in `Link` headers with `next` and `prev` relations:

```python
from eazyrest import JSONObject, link_headers_page


class GitHubObject(JSONObject):
    @classmethod
    def collection_page(cls, resp, *, api, **kwargs):
        return link_headers_page(api, resp)
```

Links are checked as `follow_next_links()` checks them. A link to another
origin raises `PaginationError` unless the helper was given
`allow_other_origins=True`. A link back to a page already read in the same
direction raises `PaginationError`, and reading more than `max_pages` pages in
one direction raises `LimitExceeded`, while going forward and back again is
allowed. A page with more items than `max_items` raises `LimitExceeded`. A page
that a hook builds itself with `Page(...)` holds its items, but cannot read
other pages.

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

### Full updates

`save()` sends the changed fields in a `PATCH`. For an API that updates an
object only with a `PUT` of the whole object, call `save(full=True)`. It sends
every writable declared field that the object's JSON has, with its unsaved
changes and any pending write-only fields, even if nothing changed. Each value
is encoded as an assignment would encode it, so a related object is sent as its
primary key, and a field that `write_map` names under its write key. It loads
an unloaded or partial object first, preserving pending changes. Read-only
fields are omitted.

### Uploading files

`create()` and `save()` take `files=`, files to upload as requests takes them,
keyed by field name or by the API's field name. The write is then a multipart
request, with the other fields as form fields, and `save(files=...)` sends it
even if no field changed. Declare a file field as `str`, since an API such as
Django REST Framework returns the file's URL, and list it in `read_only=` so a
full save omits the URL. Explicit uploads still use `files=`:

```python
avatar = Avatar.create(name="me", files={"image": open("me.png", "rb")})
avatar.save(files={"image": ("new.png", data, "image/png")})
print(avatar.image)  # the URL that the response holds
```

A form field holds text, so booleans are sent as `true` and `false` and numbers
as strings, and a list of scalars is sent as repeated fields. A field whose
value is `None`, a mapping, an empty list, or a nested list cannot be sent this
way and raises `TypeError` before the request. Save such a field in a separate
write without files.

Requests reads a file object from its current position. Before retrying an
upload after a failure, reopen or rewind each file. A 307 or 308 redirect
resends the already prepared multipart body.

### Request options

`save()`, `create()`, and `delete()` take `request_options=`, keyword arguments
for the request as requests takes them, such as `headers` or `params`. The
names are those of the HTTP library that the client sends requests with,
requests for `API`, so a client for another library, which a later release may
add, can take other names. They cannot set the body, which the write builds, or
enable `stream=True`, because model writes consume their responses. Options
that are not a mapping raise `TypeError`, as they do for a query. An unfollowed
final 3xx raises `RedirectError`, including when `allow_redirects=False`, and
pending updates remain pending. A server that supports optimistic concurrency,
for example, refuses a write whose `If-Match` header no longer names the
object's current version:

```python
todo.title = "new"
todo.save(request_options={"headers": {"If-Match": etag}})
```

An assignment in eager mode sends its `PATCH` at once, so there is no call to
pass options to. Make the assignment inside a `write_options()` block instead.
Every write of the object inside the block sends the block's options: each
eager `PATCH`, and every `save()` and `delete()`, whose own `request_options`
are merged over them. Loads, and the writes of related objects, do not send
them. A nested block merges its options over the outer block's:

```python
todo.set_write_mode("eager")
with todo.write_options(headers={"If-Match": etag}):
    todo.title = "new"
```

The query methods `filter()`, `all()`, `get()`, and `get_or_create()` take
`request_options=` too, for every request that the query sends: the collection
request, the requests for later pages that a collection hook sends with its
`api`, and the bulk lookups of `prefetch=`. A bulk lookup gets the options only
when its API has the origin of the query's own, so that a header such as
`Authorization` does not reach another model's host. The options otherwise go
where the session's own headers go: a collection hook that reads pages with
`allow_other_origins=True` sends them to the pages' origin, and
`bulk_get_by_urls()` may send them to an origin in its API's
`redirect_origins`, as reading a hyperlinked field sends the session's headers
there. A query sends no body and builds its own parameters, so its options
cannot set `json`, `data`, `files`, or `params`, and it reads every response,
so they cannot enable `stream=True`. The objects that a query returns do not
keep its options:

```python
todos = Todo.filter(
    completed=False,
    request_options={"headers": {"Accept-Language": "fr"}, "timeout": 30},
)
```

`get_or_create()` sends its options with both the lookup and any creation. An
unfollowed final 3xx raises `RedirectError`, as for writes, and so does a
collection request answered with one, such as a `304 Not Modified`.

### Write responses

After a successful write, the object's JSON holds the fields that were sent,
with the object that the response body holds, if any, merged over them. A
response with no body, such as `204 No Content`, and a body that holds only
some fields, such as only the changed ones, therefore leave the other fields as
they were. A field that the server computes, such as a modification time, keeps
its old value unless the body holds it. Call `refresh()` to reload it.

`create()` works the same way, starting from the JSON-backed fields that it
sent. An extra keyword argument that is not a field, such as a write-only
password, is sent but not kept. If a field is in neither the payload nor the
body, as when the body holds only the new primary key, reading it loads the
object.

`merge_response()` computes the new JSON, and an API whose write responses hold
something else, such as a status message, can override it on a shared base
model:

```python
from eazyrest import JSONObject


class StatusObject(JSONObject):
    def merge_response(self, resp, current, sent, **kwargs):
        # The body is {"status": "ok"}, not the object.
        return {**current, **sent}
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
in an `API` subclass. An override should raise a subclass of `APIError`, and of
`NotFound` for a 404, so that `except APIError` catches its errors and a lazy
load of an object deleted on the server still raises `DoesNotExist`. The
[Django REST Framework guide](drf.md) shows one that keeps the default message
and `data`.

Requests follows redirects, but only a 307 or 308 redirect resends a request
unchanged. It resends a `POST` answered with a 301 or 302, and any other
request answered with a 302 or 303, as a `GET`, and it resends a `PATCH`,
`PUT`, or `DELETE` answered with a 301 without its body. The server would then
never receive the original request, so a redirect that would change the method
or drop the body raises `RedirectError` before it is followed, rather than
letting `create()`, `save()`, or `delete()` report success. A lazy update stays
pending. This happens, for example, when `base_url` uses `http://` and the
server redirects to HTTPS, or when a URL lacks the trailing slash that Django's
`APPEND_SLASH` adds, as with `trailing_slash = False` against a DRF router. Use
the URL that the server redirects to. A `GET`, or a `DELETE` without a body,
answered with a 301 is followed. A 303 is different: it says that the server
performed the request and points to its result, so its `RedirectError` says not
to retry the request, which would repeat it. Read the result instead, or reload
the object, whose update stays pending until then.

A redirect is followed only to the origin of the request it answers, that is,
its scheme, host, and port. Requests drops the `Authorization` header when a
redirect leaves the host, but it sends every other header, such as an API key
or a CSRF token, and a 307 or 308 resends the request body. A redirect to
another origin, including one from `http://` to `https://`, raises
`RedirectError` before anything is sent there. The check examines the URL
exactly as requests will send it, and it refuses a target that another client
could read as a different host, such as one with a backslash before an `@`. For
an API that redirects to another server on purpose, such as a download host,
list that origin in `redirect_origins`:

```python
api = API("https://api.example.com/")
api.redirect_origins = ["https://files.example.com"]
```

These checks guard the redirects that requests follows. A raw request sent with
`allow_redirects=False` follows none, so it returns the 3xx response as it is,
and the caller decides what to do with it. Requests does prepare
`Response.next` even then, and raises its own `ValueError` or `UnicodeError`
when it cannot. The request raises `RedirectError` instead, whether or not it
follows redirects. Any other exception that requests raises there, such as
`requests.TooManyRedirects` when `session.max_redirects` is 0, or
`requests.exceptions.UnrewindableBodyError` for a 307 or 308 that resends a
body that cannot rewind, comes through unchanged. A `Location` that requests
prepares without error is returned. Requests cannot prepare a `Location` with a
byte that is not UTF-8, or a host such as `http://[bad/`. It cannot prepare one
with a port that is out of range either, when it reads the port while it
prepares the next request. Whether it does depends on the request. It reads the
port, for example, when the request carries an `Authorization` header and the
redirect keeps the host, or when a no-proxy list applies, from `NO_PROXY` or
from a `no_proxy` entry in `proxies`, and the redirect names a host that is not
an IPv4 address. It consults neither `NO_PROXY` nor a `no_proxy` entry in
`proxies` when `session.trust_env` is false. When it does not read the port,
the response is returned. A model write, a query, and a next-page request
expect a final response, so they raise `RedirectError` for any unfollowed 3xx.

Pass `proxy=` to send requests through a proxy. The proxy takes precedence over
proxy environment variables such as `HTTPS_PROXY`.

A SOCKS proxy, such as the one that `ssh -D` opens, needs the `socks` extra,
`pip install "eazyrest[socks]"`. Without it, the first request raises
`requests.exceptions.InvalidSchema`. Use the `socks5h` scheme so that the proxy
resolves host names. With `socks5`, names are resolved locally, which fails for
a host that only the far side of the tunnel can resolve and sends the lookups
outside the tunnel:

```python
# After: ssh -D 1080 gateway.example.com
api = API(
    "https://internal.example.com/api/", proxy="socks5h://localhost:1080"
)
```

Pass `verify=` the path of a CA bundle to verify certificates against it, or
`verify=False` to disable TLS certificate verification. urllib3 then warns with
an `InsecureRequestWarning` for each request. eazyrest does not suppress the
warning, because a filter applies to the whole process and would hide it for
every other client too. To accept unverified connections everywhere, ignore it
yourself:

```python
import warnings

import urllib3

warnings.filterwarnings(
    "ignore", category=urllib3.exceptions.InsecureRequestWarning
)
```

Either `verify=` setting takes precedence over the `REQUESTS_CA_BUNDLE` and
`CURL_CA_BUNDLE` environment variables. With the default, `verify=True`,
requests uses the bundle that those variables name, if any.

By default, a request fails if connecting, or any single read from the socket,
waits more than 10 seconds. That bounds each wait, not the whole request, so a
server that sends a byte every few seconds could keep a request open
indefinitely. Set the `timeout` attribute to change the default, or pass
`timeout=` to a single request, as requests takes it: a number, a pair of
connect and read timeouts, or a `urllib3.Timeout`. While `total_timeout` is
set, each of these is cut to the time that remains, and a part that has no
limit waits for that time. Extra keyword arguments to `API()` configure a
`requests.adapters.HTTPAdapter` for the base URL's scheme, such as
`max_retries=` for retries:

```python
from urllib3.util import Retry

api = API("https://api.example.com/v1/", max_retries=Retry(total=3))
api.timeout = 30
```

urllib3 retries only idempotent methods by default, so a `POST` is not resent.
With the adapters that `API` creates, retry delays and intermediate response
bodies obey the same total-time and body-size budgets as the final response.
`reset_session()` keeps the session's configuration but not its adapters, so
pass the adapter arguments again when you call it.

### Request bodies

`API.post()`, `patch()`, and `put()` pass `json=` to requests, which writes it
with `json.dumps()`, so a body must already be JSON. A `datetime` or a model in
it raises `TypeError` before anything is sent. To write Python values as fields
write them, describe the body with a `TypedDict` and encode it with
`encode_json()`:

```python
import datetime

from typing import TypedDict

from eazyrest import encode_json


class Extension(TypedDict, total=False):
    duration: datetime.timedelta
    until: datetime.datetime


body = encode_json({"duration": datetime.timedelta(hours=2)}, Extension)
api.post("/reservations/1/extend/", json=body)
```

`encode_json(value, annotation)` returns the JSON value that a field annotated
`annotation` writes, so `body` is `{"duration": "PT2H"}`. Any field annotation
works, such as `datetime.datetime` for a query parameter or `list[Extension]`
for a list. A key annotated `pydantic.Base64Bytes` writes base64 through
Pydantic, so the annotation, not eazyrest, decides how bytes are written. It
converts by the default conversion table. Pass `model=` to convert by a model's
table instead, as that model's fields would, such as
`encode_json(body, Extension, model=Reservation)` for an API whose models
declare their own `conversions`. A value that does not encode to JSON, such as
a model under a key annotated `int`, raises `TypeError` naming its key.

### Limits

A server can send more than a client should accept, by mistake or on purpose.
Each `API` limits what one response or traversal can cost, and raises
`LimitExceeded` when a limit is reached. Its `limit` attribute names the `API`
attribute that sets it:

| Attribute | Default | Limits |
| --- | --- | --- |
| `max_response_bytes` | 16 MiB | The decompressed size of all the response bodies that one request reads, those of its redirects and challenges included, checked as they are read. |
| `total_timeout` | 300 seconds | The time of each request, including redirects, retries, retry delays, and reading response bodies. |
| `max_json_depth` | 128 | The nesting depth of arrays and objects in a response's JSON. |
| `max_pages` | 1,000 | The pages that one `follow_next_links()` or `follow_link_headers()` traversal reads, counting the first. |
| `max_items` | 100,000 | The items that one traversal yields. |

Set an attribute on an instance or in a subclass to change a limit, or set it
to `None` to remove it:

```python
api = API("https://api.example.com/v1/")
api.max_response_bytes = 64 * 1024 * 1024
api.max_pages = None
```

A limit is never met by truncating. A traversal that would read too many pages
raises rather than stopping early, so the items it yielded are never mistaken
for the whole collection. `total_timeout` is checked between the reads of a
body and between retry attempts. A server that sends a body's bytes no slower
than `timeout` allows can therefore make a request overrun it by at most one
`timeout`, but not one that sends the response's headers, a chunk's size, or a
compressed body's header byte by byte, which `http.client` and urllib3 read in
one call. So `total_timeout` bounds a slow body, not a hostile server, and
`timeout` bounds each wait. It cannot interrupt your own code either, such as a
validator or a hook. Each request of a traversal has its own `total_timeout`,
because a traversal is lazy and the time between pages is yours. Requests keeps
the body of each redirect that it follows, and of each 401 challenge that an
authentication handler answers, so `max_response_bytes` covers all the bodies
that one request reads, redirects and challenges included, and a request never
holds more than that in total. A challenge and a final body that together
exceed it raise `LimitExceeded`, though each is within it alone. The
`Content-Length` of a response that has no body, such as a 304, or the response
to a `HEAD` request, is not checked, though it may name the size of the entity
that it describes. A raw `API` request that passes `stream=True` reads its own
body, so `max_response_bytes` does not apply to a successful response, though
it does to the bodies of redirects, followed or not, and of a failed response,
which eazyrest reads, because requests reads a redirect's body itself to
prepare `Response.next`. The adapter reads each body before any response hook
runs, so an authentication handler such as `requests.auth.HTTPDigestAuth`
cannot read the body of a challenge past the limits either. Custom adapters and
application hooks control their own work. Use `api.json(response)` in hooks to
decode JSON with the client's depth limit and response-shape policy.

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
be overridden per instance by passing `api=` when creating an object, and per
query by passing `api=` to `filter()`, `all()`, `get()`, `create()`, or
`get_or_create()`. Objects that a query returns keep its API, so their later
requests, such as loading or saving, use it too. This suits an application that
sends requests with different credentials, for example one `API` per user:

```python
todos = Todo.filter(api=user_api, completed=False)
```

Prefetch passes a query's `api=` to `bulk_get_by_pks(api=...)`, or else the
related model's own API, and the collection hooks receive the query's client as
`api`, so a hook such as `follow_next_links(api, payload)` reads later pages
with it. While `filter()` calls a collection hook, `cls.api` is the query's API
too.

A related object loads through the API registered for its own class, so models
served by different APIs can refer to each other. If the parent object has an
`api=` override, its related objects use that override too.

## Errors

eazyrest raises a subclass of `EazyrestError` for each condition in the list
below, so `except EazyrestError` catches them all. Each also derives from the
exception type that it replaced, so code written for that type keeps working:

- `FieldConversionError`, a `TypeError` and a `ValueError`, for a value that a
  field cannot convert.
- `APIError`, a `requests.HTTPError`, for a response whose status reports a
  failure, and its subclass `NotFound` for a 404. `data` holds the parsed JSON
  body, and the message names the body's members other than `detail`, but not
  their text.
- `DoesNotExist` and `MultipleObjectsReturned`, for a lookup that expects one
  object. `NotFound` is also a `DoesNotExist`.
- `PrefetchNotSupported`, for a prefetch of a model without
  `bulk_get_by_pks()`.
- `RedirectError`, a `requests.HTTPError`, for an unsafe redirect, a redirect
  that is not followed when requests raises `ValueError` or `UnicodeError`
  while it prepares the next request, or an unfollowed final 3xx on a model
  write, a query, or a next-page request, or on a request that a hook sends
  through the `api` of a query with `request_options=`. Its `response` is the
  redirect. Another exception from that preparation comes through unchanged.
- `PaginationError`, a `ValueError`, for a next-page link or redirect to
  another origin, or to a page already read.
- `ResponseShapeError`, a `TypeError` and a `ValueError`, for a response that
  is not JSON, holds non-finite or unrepresentable numbers, or does not have
  the expected shape. Its message does not quote the body.
- `UsageError`, a `RuntimeError`, for a model without a registered API or a
  second iteration of a lazy related collection.
- `LimitExceeded`, for a response or traversal that exceeds one of the API's
  [limits](#limits).

A misuse of an argument, such as a query parameter that is not allowed, an
assignment to a read-only field, or an unsupported request option, raises a
`TypeError`, `ValueError`, or `AttributeError` instead, as the section on that
operation says.

An HTTP error status raises whatever `API.raise_for_response()` raises,
`APIError` or `NotFound` by default. Invalid JSON gives these errors
`data=None`. A depth-limit violation instead raises `LimitExceeded`, including
for an error body, and a transport failure raises the exception of the HTTP
library that sent the request. For `API`, that is the requests exception that
describes the failure, such as `requests.ConnectionError`. Invalid decorator
options raise `TypeError` or `ValueError` when the class is defined. Field
annotations resolve lazily, so an invalid conversion declaration can raise
`FieldConversionError` at first use.

## Threads

`eazyrest` is synchronous. Each request blocks until its response arrives, and
there is no `asyncio` interface.

A model instance caches its JSON, converted field values, and pending updates
without locking. Do not share an instance between threads if any of them reads
a field that may load, assigns a field, or calls `save()`, `refresh()`, or
`update_from_json()`. Create separate instances in each thread instead, for
example by calling `filter()` or `get()` in that thread.

`register_api()` updates a registry shared by the whole process. Register APIs
during startup, before other threads use the models.

Each `API` wraps one `requests.Session`, and requests does not promise that a
`Session` is safe to share between threads. Class-level queries such as
`filter()`, `get()`, and `create()` use the API registered for the class, so
threads that call them share its session, unless they pass `api=`. An instance
created with `api=`, or returned by a query that passed it, uses that API
instead.
