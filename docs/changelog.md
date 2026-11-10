# Changelog

## 1.0.0 (unreleased)

eazyrest 1.0 is the first stable release. From 1.0 on, the names that the
`eazyrest` package exports, the `eazyrest.pydantic` types, and the behavior
that the guide documents follow semantic versioning. A breaking change waits
for 2.0. A minor release may add attributes to `JSONObject`, such as methods
and hooks, so a model with a field of a new attribute's name raises
`ValueError` when it is decorated, until `field_map=` gives the field another
attribute name.

The tags `v1.0.0a1` through `v1.0.0a5` name commits from an earlier history
that was later rewritten. They are not ancestors of 1.0.0.

### Migrating from 1.0.0a5

The items below change what code written for the alpha `v1.0.0a5` does, or
whether it type-checks. The 0.9.0 items, the new features below, and the guide
describe them further.

- The hooks `collection_items()`, `collection_items_from_response()`,
  `collection_page()`, `bulk_get_by_pks()`, and `bulk_get_by_urls()` must
  accept `api=` and `**kwargs`, and `object_json()`, `merge_response()`, and
  `API.raise_for_response()` must accept `**kwargs`. A class that overrides one
  of them without, also an undecorated base class, raises `TypeError`, naming
  the hook, when it is defined. `api` is required in the bulk hooks too, so
  code that calls them itself must pass it. The bulk hooks return a `Mapping`
  of the model's objects.
- Annotate a hook's response as `eazyrest.Response` and its client as
  `eazyrest.Client`. An override annotated `requests.Response` or `API` fails
  type checking.
- `filter()` reads a Django REST Framework page, a `results` list with a `next`
  link, by default, where `v1.0.0a5` raised `ResponseShapeError`. An override
  of `collection_items()` that only read such pages can go.
- `JSONObject.from_json()` and `to_json()` are gone, and so are the exports
  `AnalyzedType`, `BaseType`, `CollectionType`, `OptionalType`, `PydanticType`,
  and `RelatedType`. Declare conversions instead, as
  `conversions = {T: Conversion(decode=..., encode=..., schema=...)}` on a
  model's base class, and encode a request body with
  `encode_json(value, annotation, model=...)`. A class that still overrides
  either method with its `conversion` parameter raises `TypeError` when it is
  defined, since the override would never be called. No public function decodes
  a value as a field does, so code that called `from_json()` to read an
  action's response must construct a model or read the JSON itself.
- A plain `TypedDict`, `dict[str, T]`, fixed-length tuple, `NamedTuple`, or
  dataclass field converts its keys, items, or fields, where `v1.0.0a5` kept
  their JSON, so a `datetime.datetime` under a `TypedDict` key reads as a
  `datetime`. Annotate the JSON form, such as `dict[str, Any]`, to keep the
  JSON, or `Any` to keep any JSON, since `dict[str, Any]` now requires a JSON
  object. Types whose JSON would read as another type, such as a mapping whose
  keys are not strings, a model inside a mapping, or a union with a member that
  is not a JSON scalar, such as `list[str] | str` or
  `dict[str, Any] | list[Any]`, raise `FieldConversionError`, also for a `null`
  value. Annotate such a union `Any`, or for Pydantic. A dataclass field reads
  an instance, where `v1.0.0a5` read a `dict`, and refuses a key that the
  dataclass lacks and a `dict` assigned to it. A `NamedTuple` with defaults
  refuses a shorter array. A parameterized generic `TypedDict`, and a
  parameterized type whose items eazyrest does not convert, such as
  `Counter[str]`, raise.
- A plain `TypedDict`, dataclass, or `NamedTuple` whose annotations name a type
  that is imported only under `TYPE_CHECKING` raises `FieldConversionError`
  when the field is first used, where `v1.0.0a5` passed its JSON through.
  Import the type at run time.
- `tuple[T]` is a tuple of exactly one item, so a longer array raises
  `FieldConversionError`, where `v1.0.0a5` read an array of any length as a
  tuple. Annotate a tuple of any length as `tuple[T, ...]`. A bare `tuple`
  field reads a tuple, where `v1.0.0a5` read a list.
- `MutableSequence`, `Collection`, `Set`, `MutableSet`, `deque`, and
  `MutableMapping` fields convert their items, where `v1.0.0a5` returned the
  JSON values, and the abstract types read as concrete ones: `Collection` as a
  tuple, `Set` as a `frozenset`, and `MutableSet` as a `set`, where `v1.0.0a5`
  read a list.
- A field typed as a subclass of a type that converts, such as a subclass of
  `datetime`, converts as that type, and a `NewType` as its supertype, where
  `v1.0.0a5` read their JSON values unchanged.
- A `Literal` field reads the JSON value of one of its enum members as the
  member, where `v1.0.0a5` read the JSON value.
- `decimal.Decimal` and `datetime.time` fields read as `Decimal` and `time`,
  where `v1.0.0a5` left the strings. So do `Annotated[Decimal, Field()]` and
  `Annotated[time, Field()]`, which now convert by eazyrest's table, as other
  Pydantic fields do: a decimal string with spaces, such as `" 1.0 "`, is
  refused, and `Decimal("1E+2")` writes `"100"`. `deque[T]` reads a `deque`.
- In a field that Pydantic converts, a dataclass, of Pydantic or the standard
  library, must set `extra="forbid"`, and a `TypedDict` `extra="allow"` or
  `"forbid"`, at any depth, including behind a named alias, a `NewType`, a
  `NamedTuple`'s field, a generic class's type argument, or a `TypedDict`'s
  `extra_items=`, or using the field raises `FieldConversionError`. So does a
  dataclass with an init-only field, which Pydantic reads but never writes, and
  a class whose annotations cannot be resolved, whose fields therefore cannot
  be checked.
- A write that Pydantic would make without some keys or fields of a `TypedDict`
  or dataclass raises `FieldConversionError`, and so does a value that cannot
  be written as JSON, such as a float that is not finite, when it is assigned,
  where `v1.0.0a5` accepted the assignment. Inside a type that eazyrest does
  not convert, such as an `Any` field or a value of `dict[str, Any]`, a tuple,
  which reads back as a list, and a mapping whose keys are not strings are such
  values. Assign a list instead of a tuple.
- In a field that Pydantic converts, a field that Pydantic excludes from
  writes, with `Field(exclude=True)` or `exclude_if`, and a dataclass inside a
  Pydantic model raise `FieldConversionError`, because a write would drop their
  keys. A dataclass that sets `extra="forbid"` itself is accepted, and so is a
  dataclass without a config of its own inside a model with `extra="forbid"`. A
  dataclass with any other config of its own is refused there too.
- A Pydantic field's write schema is its serialization schema, not its
  validation schema.
- A set or frozenset field writes its items in a fixed order, sorted as JSON
  values, rather than in the set's order, and assigning a set equal to the
  server's sends no update, whatever order the server lists the items in. That
  holds for an optional set and a `NewType` of a set. A set inside a field that
  Pydantic converts is still written in the set's order.
- `conversions` is a `JSONObject` attribute, so a model with a field of that
  name raises `ValueError` when it is decorated, and a class variable, method,
  or property of that name raises `TypeError` when the class is defined. Give
  the field another attribute name, with `field_map=` to keep its JSON key.
- `api=` and `register_api()` take an `API`. Another object, such as a test
  double, raises `TypeError`.
- `json_schema()` describes fields that it gave as `{}`, such as `Decimal`,
  `time`, `int | str`, `dict[str, Any]`, and tuples, and names its dialect,
  JSON Schema 2020-12, in `$schema`, so a schema that a client compared with a
  saved copy changes.
- The default `collection_items_from_response()` returns a `Results` iterator,
  which reads once, for a list payload. Code that calls the hook itself and
  reads the result twice must make a list of it.
- Each query method refuses the options of the query methods that it does not
  take, `api`, `prefetch`, `defaults`, `request_options`, `params`, and `url`,
  as keyword arguments. Pass a query parameter with one of these names in
  `params=`.
- `max_response_bytes` limits the bodies of one request together, where
  `v1.0.0a5` limited each body alone, so a request that follows redirects could
  hold the limit once for each of them in `resp.history`. A request whose
  redirect bodies, challenge bodies, and final body total more than the limit
  raises `LimitExceeded`. A 304 response, or one to a `HEAD` request, which has
  no body, does not raise for the `Content-Length` that it declares.
- A `urllib3.Timeout` passed as `timeout=` is accepted while `total_timeout` is
  set, with its connect and read values cut to the time that remains. It raised
  `TypeError`.
- The messages of `APIError`, `NotFound`, `RedirectError`, `PaginationError`,
  `ResponseShapeError`, and `LimitExceeded` leave out a user and password in
  the URL that they quote, so code that compared a message with a base URL such
  as `http://user:pass@host/` must compare it without them. The `repr()` of a
  `Page`, and of an object known only by its URL, leaves them out too.
- A 401 challenge whose body is larger than `max_response_bytes`, or that
  arrives past `total_timeout`, raises `LimitExceeded` when the session uses an
  authentication handler that reads the body, such as
  `requests.auth.HTTPDigestAuth`. Such a challenge was read without a limit.
  Its body also counts against the limit that the final body shares, so a
  challenge and a final body that are each within `max_response_bytes` raise
  `LimitExceeded` when they total more.
- A raw request sent with `allow_redirects=False` returns a 3xx response, where
  it raised `RedirectError` for a `Location` on another origin and for a `POST`
  answered with a 301, 302, or 303, though nothing was followed. A model write,
  a query, and a next-page request raise `RedirectError` for an unfollowed 3xx.
  A next-page request to the API's origin that is answered with a redirect to
  another origin, when the query's client does not follow redirects, raised
  `PaginationError`, which advised `allow_other_origins=True`, though that
  option would follow nothing. It raises `RedirectError` now. A next-page URL
  that itself names another origin still raises `PaginationError`. A `Location`
  that cannot be parsed raises `RedirectError`, where `v1.0.0a5` let requests'
  `UnicodeDecodeError` or `ValueError` escape, so code that caught them must
  catch `RedirectError`. So does any other `Location` for which requests raises
  `ValueError` or `UnicodeError` while it prepares the next request, such as a
  port that is out of range when the request carries an `Authorization` header
  and the redirect keeps the host. A `Location` that requests prepares without
  error is returned. The body of an unfollowed redirect still counts against
  `max_response_bytes` and `total_timeout`, also when the request passes
  `stream=True`.
- A response body that stalls until `total_timeout` raises `LimitExceeded`,
  where it raised `requests.ConnectionError` when `timeout` was `None` or above
  `total_timeout`. A read that times out below the deadline, because of a
  `timeout` that you set, still raises requests' own exception.
- `save()`, `delete()`, and `create()` raise `TypeError` for `request_options`
  that is not a mapping, as the query methods do, where `v1.0.0a5` raised
  `AttributeError`.
- A relation, assigned, given to `create()` or a query, or in a collection of
  relations, refuses an object of a class that is neither the field's model nor
  a subclass of it with `FieldConversionError`, where `v1.0.0a5` sent that
  object's key or URL. So does a primary key that is a relation, given to the
  constructor or the `pk` setter, and a model object given for a primary key
  that is not a relation. A related object without a key is refused there too,
  where `v1.0.0a5` accepted it and `url` raised `ValueError`. The model holds
  the related object's key, so the object must have its key without a request,
  as an object built from a key or loaded from JSON that has it does. A related
  object that `Hyperlinked()` named and that has not loaded, or whose own key
  is a relation that is not known, raises `FieldConversionError`, where
  `v1.0.0a5` accepted it and loaded it when the key was first read, even for
  `repr()` and `update_from_json()`. Load or save it first. If it has loaded
  and its JSON lacks the key field, the message says that the key is missing
  from its JSON, and if the JSON holds the key as `null`, that the key is
  `None`. The constructor, the setter, `repr()`, and `update_from_json()` send
  no request, and the field reads as a new related object that holds the key,
  also after `refresh()`, where `v1.0.0a5` returned the object that was given,
  and the plain key after `refresh()`.
- A primary key that is a relation reads as a related object after `refresh()`,
  however it was set: as a related object, as a plain value such as
  `Profile(user=2)`, or from JSON. `v1.0.0a5` read the plain key, an `int` for
  `user: User`, after `refresh()` in every case. Code that works with that
  `int`, such as `User(id=profile.user)` after a refresh, raises
  `FieldConversionError`. Use `profile.user` or `profile.user.pk`.
- An object read from JSON that lacks its key field, such as an item that a
  `HyperlinkedModelSerializer` without `id` returned, has the key `None`, where
  `v1.0.0a5` raised `AttributeError` from `pk`, `url`, `save()`, `delete()`,
  `refresh()`, and a relation write. `url`, `save()`, `delete()`, and
  `refresh()` raise `ValueError`, and a relation write raises
  `FieldConversionError`. That includes an object read through a
  `Hyperlinked()` relation, whose `pk` is `None` once it loads and still lacks
  the key, where `v1.0.0a5` raised `AttributeError`. Reading `pk` loads an
  object that holds partial JSON without the key, as reading any field does.
- A model whose primary key is a `Hyperlinked()` relation raises `TypeError`
  when it is decorated, naming the field, where `v1.0.0a5` defined it and
  raised a bare `TypeError` from `url`, `save()`, and `delete()`. Annotate the
  key as a relation without `Hyperlinked()`. A key whose type is not defined
  yet when the class is decorated raises the same `TypeError`, not
  `FieldConversionError`, when the key is first used, also when the model is an
  item of another model's collection field.
- `refresh()` raises `ValueError` for an object built without a key, such as
  `Todo()`, and for one whose key is empty, `.`, or `..`, where `v1.0.0a5`
  reset it without an error. It raises before it discards anything.
- Bind an API with `Base.register_api(api)`. Assigning it, as in
  `Base.api = api`, would make `api=` ignored, so a class body that assigns
  `api` raises `TypeError`, and constructing an object of a class whose base
  had it assigned, or querying or creating one, raises `UsageError` before
  sending anything.
- A field whose type is a model without a primary-key field raises `TypeError`
  when the class is defined or, for a class defined later, when the field is
  first used, where `v1.0.0a5` defined the class and read the holder's other
  fields. Declare nested JSON without a key as a `TypedDict` or a Pydantic
  model.
- A relation given an object without a key, as in `Todo.create(user=User())`,
  raises `FieldConversionError`, where `v1.0.0a5` wrote it as `null`. Save the
  related object first, or assign `None` to clear an optional relation.
- In eager mode, assigning a field a value equal to one of its updates that has
  not been sent sends the `PATCH`, where `v1.0.0a5` sent nothing and left the
  update pending. An update is unsent when it was made in lazy mode, or when a
  change made in place was found as the object loaded missing fields.

### Migrating from 0.9.0

Much of the library changed between 0.9.0 and 1.0. Each item below is a change
that can break code written for 0.9.0, followed by what to do about it.

#### Installation

- eazyrest no longer depends on `certifi`, `dateparser`, `pytimeparse2`, or
  `tzlocal`, and it depends on `requests` without its `socks` extra. Install
  `eazyrest[socks]` to send requests through a SOCKS proxy. Without it, the
  first request through one raises `requests.exceptions.InvalidSchema`.
- Every dependency now has a minimum version: requests 2.33.0, urllib3 2.8.0,
  isodate 0.7.2, python-dateutil 2.9.0.post0, and, before Python 3.11,
  typing_extensions 4.14.1. The new `pydantic` extra needs Pydantic 2.12, or
  2.14.1 on Python 3.15, and below 3.
- Python 3.10 through 3.15 are supported.

#### Writes

- Assigning a field no longer sends a `PATCH` at once. By default, an object is
  in lazy write mode, and assignments wait for `save()`, which sends them in
  one `PATCH`. Call `save()` after changing an object, or construct the API
  with `API(url, default_write_mode="eager")`, or call
  `obj.set_write_mode("eager")`, to send each assignment as it is made.
- A write response is merged into the object through the new `merge_response()`
  hook, rather than replacing its JSON. A response without a body, such as
  `204 No Content`, keeps the values that were sent. `create()` keeps only the
  JSON-backed fields it sent, so a write-only key such as a password is not
  kept.
- A redirect that would change a write's method or drop its body, such as a 301
  in answer to a `PATCH` to a URL without its trailing slash, raises
  `RedirectError` before it is followed, and a lazy update stays pending. Use
  the URL that the server expects, for example by setting `API.trailing_slash`.
- `create()` sends each field under its JSON key, as `field_map` names it, as a
  write does, where 0.9.0 sent the Python name.
- `create()` and `get_or_create()` on a model without a primary-key field raise
  `UsageError` before sending anything. 0.9.0 created such an object, and its
  `get_or_create()` returned one that matched if there was one. Look an object
  up with `get()` or `filter()`, which still work on such a model, and to add
  one, post to the endpoint with the API and build the object from the
  response's JSON with `json=`.
- `delete()` takes request options in `request_options=`, as `save()` and
  `create()` do, rather than as positional and keyword arguments for
  `API.delete()`. Replace `obj.delete(headers=h)` with
  `obj.delete(request_options={"headers": h})`.

#### Queries

- `filter()` and `all()` return iterators rather than lists, and the request is
  sent when they are called. Call `list()` on the result to iterate it more
  than once. The iterator is a `Results`, whose `count` is the collection's
  size when the response reports it.
- A collection request answered with a redirect that is not followed, such as a
  `304 Not Modified`, raises `RedirectError` rather than reading its body as
  the collection.
- `filter(url=...)` is removed, and raises `TypeError`. Override `class_url`,
  or call the API directly.
- Query parameters named after JSON-backed fields are encoded as `create()`
  encodes them: renamed by `field_map`, converted to JSON, and with booleans
  sent as `true` and `false`.
- A query parameter whose value requests would drop or change raises
  `ValueError` before the request: `None`, an empty collection or iterator, a
  mapping, or a collection holding `None` or another collection. In 0.9.0, such
  a constraint was silently omitted, so `get()` could return an object that
  does not match it.
- The keyword arguments `api`, `prefetch`, `defaults`, `request_options`,
  `params`, and `url` of the query methods are their own options. Pass a query
  parameter with one of these names in `params=`. `create()` likewise reserves
  `api`, `fields`, `files`, and `request_options`, and takes field values with
  those names in `fields=`.
- `get_or_create()` returns an existing object as the server sent it. 0.9.0
  assigned every keyword argument to the object it found, which sent a `PATCH`.
  To change the object, assign its fields and call `save()`.
- In `get_or_create()`, a key that contains `__` and is not a field, such as
  `name__iexact`, is sent only in the lookup, not in the creation payload.
  Every keyword argument is sent in the lookup's query string, so pass a value
  that only the creation needs, such as a password, in `defaults`.
- `DoesNotExist` and `MultipleObjectsReturned` derive from `EazyrestError`
  rather than directly from `Exception`, and their messages name the model and
  the lookup parameters, but not their values.
- A failed request raises `APIError`, or `NotFound` for a 404, instead of a
  plain `requests.HTTPError`. Both are `requests.HTTPError`, so existing
  handlers still catch them, but the message differs: it names the method, URL,
  status, and the members of a JSON body other than `detail`, and the parsed
  body is in `data`. `NotFound` is also a `DoesNotExist`, so a 404 on a lazy
  load is caught by `except DoesNotExist`. An override of
  `API.raise_for_response()` should raise subclasses of `APIError` and
  `NotFound` to keep both.

#### Fields and conversion

- Fields convert their values by annotation. `datetime`, `date`, `time`,
  `timedelta`, `UUID`, `Decimal`, enum, optional, collection, and
  related-object fields read and write their JSON forms, and so do their items,
  as in `list[datetime.datetime]`. A set writes its items in a fixed order, so
  equal sets write equal JSON.
- A `datetime` field writes ISO 8601 rather than a timestamp, and reads ISO
  8601 or a Unix timestamp. `parse_datetime()` no longer uses `dateparser` or
  the local time zone, so it reads only what a field reads, and its
  `use_dateparser` and `tzinfo` arguments are removed. To write timestamps,
  replace the `datetime` conversion in a model's `conversions`, as the guide
  shows. A `datetime` whose UTC offset is not a whole number of minutes raises
  `FieldConversionError` rather than writing an offset that ISO 8601 lacks.
- A `timedelta` field writes an ISO 8601 duration. `parse_duration()` rejects
  years and months, whose length varies, no longer reads the `:SS` and
  `D:HH:MM:SS` forms, such as `":30"` and `"1:02:03:04"`, although it still
  reads `"1:30"` and `"1 02:03:04"`, and reads microseconds. A value out of
  range raises `ValueError` rather than `OverflowError`.
- A value that a field cannot convert raises `FieldConversionError`, which is
  both a `TypeError` and a `ValueError`. Its message names the model and field
  but not the value.
- Assigning a non-collection to a collection field, or a `datetime` to a `date`
  field, raises.
- A field typed as a subclass of a type that converts, such as a subclass of
  `datetime`, converts as that type, and a `NewType` as its supertype, where
  0.9.0 read their JSON values unchanged.
- A `TypedDict` field converts its declared keys by their annotations, and a
  `dict[str, T]` or `Mapping[str, T]` field its values, where 0.9.0 returned
  the JSON value unchanged. Undeclared keys are kept. A `TypedDict` decorated
  with `pydantic.with_config()` is converted and validated by Pydantic, by its
  whole config.
- Fixed-length tuples and `NamedTuple`s convert as JSON arrays by position, and
  standard library dataclasses as JSON objects by field, where 0.9.0 returned
  the JSON value unchanged. `tuple[T]` is a tuple of one item, as Python's
  types say, so annotate a tuple of any length as `tuple[T, ...]`. A union
  converts only if each of its members passes through unchanged, as
  `int | str`'s do.
- A mapping whose keys are not strings, a union with a member that converts,
  such as `str | datetime`, a model inside a `TypedDict`, a mapping, a
  fixed-length tuple, a `NamedTuple`, or a dataclass, and any parameterized
  type whose items eazyrest does not convert, such as `Counter[str]`, raise
  `FieldConversionError`, because eazyrest cannot read them as their types.
  Annotate them for Pydantic, or annotate their JSON form, such as
  `dict[str, Any]`. Pydantic does not convert eazyrest models either, so
  annotate a model inside one of these with its primary key type, such as
  `int`. `MutableSequence`, `Collection`, `Set`, `MutableSet`, `deque`, and
  `MutableMapping` fields convert their items, where 0.9.0 returned them
  unchanged.
- Assigning a value that is not JSON to a field, such as an `IPv4Address`
  without a conversion, or a float that is not finite, raises
  `FieldConversionError` naming the field, rather than failing when the request
  is sent.
- A `Literal` field reads the JSON value of one of its enum members as the
  member, and writes the member as that value, where 0.9.0 read the JSON value
  unchanged.
- `from_json()` and `to_json()` are no longer override points. Declare a
  model's `conversions` instead, mapping each type to a `Conversion` that
  decodes and encodes it, as the guide's Custom field conversion section shows.
  `create_related()` is private. A class that still overrides either method
  with 0.9.0's `ty` parameter, as `from_json(self, value, ty)`, raises
  `TypeError` when it is defined, since the override would never be called.

#### Declarations

- A field whose type is a model without a primary-key field now raises
  `TypeError` when the class is defined or, for a class defined later, when the
  field is first used. 0.9.0 read such a field but could not write it. Declare
  nested JSON without a key as a `TypedDict` or a Pydantic model. For a model
  class that is not decorated with `@json_object`, the error says to decorate
  it and that it also needs a key field, or to declare nested JSON as a
  `TypedDict` or a Pydantic model instead.
- An attribute annotated `ClassVar`, such as `limit: ClassVar[int] = 10`, is a
  class attribute, not a field. 0.9.0 made it a field, read from the object's
  JSON.
- `json_object()` takes its options as keyword arguments only. It raises
  `ValueError` for a field that would replace a `JSONObject` attribute, for a
  `pk=`, `field_map=`, or `exclude=` name that is not a field, and for two
  fields with the same JSON key. A class with decorated ancestors that are not
  subclasses of one another raises `TypeError`.
- The `JSONObject` constructor takes `json` and `api` as keyword arguments
  only. Replace `Item(payload)` with `Item(json=payload)`.
- A decorated subclass keeps the primary key that it inherits.
- Bind an API with `Base.register_api(api)`. Assigning it, as in
  `Base.api = api`, which 0.9.0's README showed, would make `api=` ignored, so
  a class body that assigns `api` raises `TypeError`, and constructing an
  object of a class whose base had it assigned, or querying or creating one,
  raises `UsageError` before sending anything.
- Redeclaring a field also preserves its read and write keys and, when those
  options are omitted, its write-only and read-only policies. Explicit mappings
  or policy sets override those defaults.

#### URLs and HTTP

- A request's URI is appended to the path of the base URL, where 0.9.0 joined
  it with `urljoin()`, so that `API("https://h.example.com/api/v1/")` requests
  `/users/` as `https://h.example.com/api/v1/users/`, where 0.9.0 requested
  `https://h.example.com/users/`. A `class_url` that repeats the base URL's
  path must drop it.
- A detail response that is a list of more than one item raises
  `MultipleObjectsReturned`, where 0.9.0 read the first item.
- `reset_session()` keeps the session's headers, cookies, and query parameters,
  which 0.9.0 dropped.
- A relation, assigned, given to `create()` or a query, or in a collection of
  relations, refuses an object of a class that is neither the field's model nor
  a subclass of it with `FieldConversionError`. 0.9.0 sent that object's key on
  assignment, raised `TypeError` from `create()`, and sent the object's
  `repr()` as the parameter of a query. A primary key that is a relation, given
  to the constructor or the `pk` setter, refuses such an object too, and so
  does a primary key that is not a relation. A related object without a key is
  refused there as well, where 0.9.0 accepted it and `url` raised
  `AttributeError`. The field reads as a new related object that holds the key,
  also after `refresh()`, where 0.9.0 returned the object that was given, and
  the plain key after `refresh()`.
- A primary key that is a relation reads as a related object when it is given
  as a plain value, as `Profile(user=2)` does, where 0.9.0 read the plain
  value, an `int` for `user: User`. It reads as a related object after
  `refresh()` too, however it was set, where 0.9.0 read the plain key. Code
  that works with that `int`, such as `User(id=profile.user)`, raises
  `FieldConversionError`. Use `profile.user` or `profile.user.pk`.
- `url` percent-encodes the primary key as one path segment, and an object
  whose key is missing, empty, `.`, or `..` has no URL. An object read from
  JSON that lacks its key field has the key `None`, so `url`, `save()`,
  `delete()`, and `refresh()` raise `ValueError` for it, and a relation write
  raises `FieldConversionError`. `refresh()` raises `ValueError` for an object
  whose key is `None`, empty, `.`, or `..`, where 0.9.0 reset an object with an
  empty or dot key without an error and raised `AttributeError` for one built
  without a key.
- The second positional argument of `API()` is `proxy`, not `pool_connections`.
  Pass connection-pool settings, and other `requests.adapters.HTTPAdapter`
  arguments such as `max_retries=`, as keyword arguments.
- `verify=` takes a CA bundle path as well as a boolean, and a path or `False`
  takes precedence over `REQUESTS_CA_BUNDLE`. `verify=False` no longer
  suppresses urllib3's `InsecureRequestWarning` for the whole process. Filter
  the warning yourself if you accept unverified connections.
- A redirect is followed only to the origin of the request it answers, or to an
  origin in `API.redirect_origins`, so that session headers such as API keys
  are not sent elsewhere. A `Location` that cannot be parsed, such as
  `http://[bad/` or one with a byte that is not UTF-8, raises `RedirectError`,
  where 0.9.0 let requests' `ValueError` or `UnicodeDecodeError` escape. So
  does a `Location` with a port that is out of range, when the redirect is
  followed or requests must read the port of one that is not, where 0.9.0
  raised `ValueError` or `requests.exceptions.InvalidURL`. `RedirectError` is
  not a `ValueError`, so code that caught them must catch it.
- Assigning `API.cookies` replaces the session's cookies rather than merging
  into them. Call `api.cookies.update(jar)` to add cookies.
- Each API limits the size, time, and JSON depth of a response, and the pages
  and items of a traversal, and raises `LimitExceeded` at a limit. See the
  guide's Limits section to change or remove them.

#### Modules

- The module `eazyrest.eazyrest` is now the private `eazyrest._models`. Import
  every name from `eazyrest`, as its `__all__` lists them.

### New features

- A model without a primary-key field can be read on its own, for example from
  each item of an endpoint with `Entry(json=item)`. Its objects are read-only
  values with the key `None` and no URL. Giving field values to the
  constructor, assigning a field, and calling `save()`, `refresh()`,
  `delete()`, `create()`, or `get_or_create()` raise `UsageError`, and `url`
  raises `ValueError`. Such an object never loads, so reading a field that its
  JSON lacks raises `AttributeError`. To add one, post with the API and build
  the object from the response with `json=`. Using such a model as the type of
  a field, alone or inside an optional, a collection, or a `NewType`, raises
  `TypeError`, when the class is defined or, for a class defined later, when
  the field is first used. Declare nested JSON without a key as a `TypedDict`
  or a Pydantic model.
- Lazy loading of objects and related objects, and explicit bulk prefetch
  through `prefetch=` and `bulk_get_by_pks()`.
- `filter()` and `all()` read every page of a collection in Django REST
  Framework's page shapes by default, through the default
  `collection_items_from_response()`. A class that overrides
  `collection_items()` keeps its own paging.
- Pagination helpers `follow_next_links()`, for Django REST Framework's page
  shapes, and `follow_link_headers()`, for RFC 8288 `Link` headers, with the
  `collection_items_from_response()` hook. The collection hooks receive the
  query's client as `api`, so a hook reads later pages with the API that the
  query used. Every hook also takes `**kwargs`, for context that a later 1.x
  release may pass, and an override must accept it, which is checked when the
  class is defined. Page navigation with `get_page()`, which returns a `Page`
  whose `next_page()` and `previous_page()` request the pages around it, read
  by the `collection_page()` hook with `next_links_page()` or
  `link_headers_page()`.
- Pydantic conversion of fields whose types use Pydantic, through the
  `pydantic` extra, and the `eazyrest.pydantic` types `Datetime`, `Date`,
  `Time`, `Duration`, `UUID`, and `Decimal`. A `TypedDict` or dataclass that
  would drop the keys it does not declare is rejected. A Pydantic model follows
  its own config, so with the default config it drops undeclared keys,
  `extra="allow"` keeps them, and `extra="forbid"` refuses them with
  `FieldConversionError`. A field must read back what it writes, so a secret
  such as a `SecretStr`, which Pydantic writes as a mask, is refused unless a
  serializer reveals it. Models are written by their fields' aliases.
- Named type aliases, including `typing_extensions.TypeAliasType` and the
  `type` statement.
- `json_schema()`, the JSON schema of what a model reads or writes.
- Conversion tables: a model's `conversions` maps a type to a `Conversion` that
  reads and writes its JSON form, and gives its JSON schema, which the
  `eazyrest.pydantic` types follow inside the model's Pydantic fields, merged
  over its bases' and the default table, so a shared base model can convert a
  type of its own, such as `IPv4Address`, or replace a default, such as writing
  datetimes as timestamps. An entry can also replace the conversion of a
  dataclass, a `NamedTuple`, a `TypedDict`, or a Pydantic model.
  `DEFAULT_CONVERSIONS` holds the default table. A class checks its table when
  it is defined, and refuses a key that is a model or a type that eazyrest
  converts itself, such as `int`, whose entry would also convert `bool`.
- `encode_json(value, annotation, *, model=None)`, which encodes a request body
  or a query parameter as a field annotated `annotation` writes it, by the
  default conversion table or by `model`'s, for `API.post(json=...)` and the
  other request methods, which send `json=` as it is given.
- Eager and lazy write modes, `save()`, `refresh()`, `update_from_json()`,
  detection of in-place changes to field values, and `merge_response()`.
- For Django REST Framework APIs: `write_map=` for a relation read nested and
  written by key, `write_only=` for a field such as a password, `read_only=`
  for returned values that writes omit, `Hyperlinked()` for relations
  represented by URLs, which `bulk_get_by_urls()` can prefetch, file uploads
  with `files=` on `create()` and `save()`, full updates with
  `save(full=True)`, request options such as headers with `request_options=` on
  `save()`, `create()`, `delete()`, and the query methods, which send them with
  every request of the query, and `write_options()` for the writes of an
  object, eager ones included, the collection's size as `count`, and `APIError`
  with the parsed error body. Hyperlinked writes send URLs and relation queries
  send primary keys.
- `API.put()`, use of `API` as a context manager, `raise_for_response()`,
  `resolve_url()`, `trailing_slash`, `redirect_origins`, `reset_session()` that
  keeps the session's configuration, `register_api()`, and `api=` on queries
  and `create()`.
- `absolute_url`, and a `repr()` that shows the class and primary key without
  loading the object.
- The exceptions `EazyrestError`, `APIError`, `NotFound`,
  `FieldConversionError`, `PrefetchNotSupported`, `RedirectError`,
  `PaginationError`, `ResponseShapeError`, `UsageError`, and `LimitExceeded`.
  Each one that replaces another exception type also derives from that type.
- `eazyrest.__version__`.
- The protocols `Response`, `Request`, and `Client`, which type the responses
  that `raise_for_response()`, `merge_response()`, the collection hooks, and
  the pagination helpers receive, and the client that the hooks receive as
  `api`, by what eazyrest guarantees of any HTTP library, so that a later
  release can add another library without changing these signatures. `API`
  still returns a `requests.Response`, and the hooks still receive an `API`.
  Annotate a hook override's parameters as `eazyrest.Response` and
  `eazyrest.Client`, because an override annotated `requests.Response` or `API`
  fails type checking, and cast them to use anything else, such as `resp.links`
  or `api.session`. `bulk_get_by_pks()` and `bulk_get_by_urls()` return a
  `Mapping` of objects of the model, so an override may return a `dict` of its
  own class. The query methods, `create()`, and the model constructor take a
  `Client` as `api=`. The names that `request_options=` accepts and the
  exceptions for a failed connection are those of the client's HTTP library,
  requests for `API`.
- `API.json(response)` decodes JSON in application hooks with the same depth,
  numeric, and response-shape policy as model reads.
- Documentation, with a user guide and a guide to Django REST Framework APIs.

### Security

- A redirect is checked against the URL exactly as requests will send it, so a
  `Location` such as `https://api.example.com\@evil.example/` cannot hide
  another host from the origin check.
- Pydantic error messages leave out input values, mapping keys that the input
  chose, and discriminated-union tags.
- An error that quotes a URL, `APIError`, `NotFound`, `RedirectError`,
  `PaginationError`, `ResponseShapeError`, and `LimitExceeded`, leaves out the
  user and password of the URL in its message, `args`, and `repr()`, as
  `http://host/` for `http://user:pass@host/`. This holds for the `Location` of
  a redirect that a `RedirectError` quotes, also when it has no scheme, as
  `//host/` for `//user:pass@host/`, and when it begins with a control
  character or has a tab, carriage return, or line feed inside, which requests
  ignores. The URL's query is still quoted. The error's `request` and
  `response` keep the real URL.
- A `RedirectError` escapes the control characters of the `Location` that it
  quotes, as an HTTP error does for the reason phrase, so a server cannot write
  escape sequences or forged lines into a log.
- Response limits also cover intermediate retry bodies and retry delays with
  the adapters that `API` creates. Model writes reject response streaming and
  unfollowed final 3xx statuses. A full save loads partial state first, and
  multipart writes reject empty lists rather than silently omitting them.
- Invalid JSON numbers raise `ResponseShapeError`, malformed error bodies give
  `APIError.data=None`, and JSON depth violations raise `LimitExceeded` for
  success and error responses alike. Bounded body reads preserve Requests'
  transport exceptions.
- The adapters that `API` creates read a response's body within the response
  limits before any response hook runs, so an authentication handler cannot
  read the body of a challenge past `max_response_bytes` or `total_timeout`.
- A hyperlinked relation is followed only to an origin that the related model's
  API allows, and `APIError` messages do not quote the error body, which can
  quote submitted values.
- Releases are built and published from a tag by a workflow that uses trusted
  publishing, with the workflow's actions pinned by commit.
