# Changelog

## 1.0.0 (unreleased)

eazyrest 1.0 is the first stable release. From 1.0 on, the names that the
`eazyrest` package exports and the behavior that the guide documents follow
semantic versioning. A breaking change waits for 2.0. A minor release may add
attributes to `JSONObject`, such as methods and hooks, so a model with a field
of a new attribute's name raises `ValueError` when it is decorated, until
`field_map=` gives the field another attribute name.

The tags `v1.0.0a1` through `v1.0.0a5` name commits from an earlier history
that was later rewritten. They are not ancestors of 1.0.0.

### Migrating from 1.0.0a5

The items below change what code written for the alpha `v1.0.0a5` does, or
whether it type-checks. The 0.9.0 items, the new features below, and the guide
describe them further.

- `Hyperlinked()` and the hook `bulk_get_by_urls()` no longer exist, so
  importing `Hyperlinked` raises `ImportError`. Declare the relation by key, as
  `owner: User`, which the server's serializer sends as the related object's
  primary key, or as an embedded object. A relation by key is read, written,
  queried, and prefetched with `bulk_get_by_pks()`. An object that is known
  only by its URL no longer exists.
- A field that is a collection of related objects must say whether the order of
  its items counts. `Collection[User]` and `Iterable[User]`, also optional or
  in a `NewType`, raise `TypeError` when the class is decorated, naming the
  field, where `v1.0.0a5` read the first as a tuple and the second as a lazy
  collection that could be iterated once. Declare a sequence, such as
  `list[User]` or `Sequence[User]`, or a set, such as `set[User]`. A change
  made in place is found by comparing the items' keys, in order for a sequence
  and without order for a set, so replacing an item with an object of the same
  key is not a change. A set is sent as its distinct keys, sorted.
- The items of a collection of related objects are not optional and are not
  collections of related objects. `list[User | None]`, `set[User | None]`, and
  `tuple[User | None, ...]` raise `TypeError` when the class is decorated,
  naming the field, as do `list[list[User]]`, `list[set[User]]`, and any other
  collection of such collections. `v1.0.0a5` admitted both, and read a `null`
  item as `None`, though the server's serializer never sends one. Declare the
  items as `User`, and the collection optional if the server may send none.
- A parameter that compares one value of a field, the field's name or a lookup
  with the suffix `exact`, `gt`, `gte`, `lt`, or `lte`, raises `ValueError`
  naming the parameter before any request when it is given a collection, as in
  `filter(id=[7, 8])`, `filter(owner=[7, 8])`, or `filter(due__gt=[a, b])`.
  `v1.0.0a5` sent such a collection repeated, which django-filter reads as its
  last value, so the query matched fewer objects than it named. Use a lookup
  that takes several values, as in `filter(id__in=[7, 8])`. The name of a field
  that is a collection still sends each item, and so does a value that the
  field's conversion takes whole. A path through a relation that compares one
  value does the same, as in `filter(owner__id=[7, 8])` or
  `filter(owner__id__gt=[1, 2])`, where `v1.0.0a5` sent the parameter repeated.
- The same holds when a field is named by its JSON key or its write key, as in
  `filter(postId=[7, 8])`, `filter(Title__gt=["a", "b"])`, or
  `filter(userId=[3, 4])`, and when a path through a relation starts with the
  relation's JSON key, as in `filter(userId__id=[3, 4])`, where `v1.0.0a5` sent
  the parameter repeated. So is it when the key holds `__`, as `doc__id` for a
  key `id` or `the__title` for a field `title`, which the query now reads as
  that key and not as another field's name and a suffix. A field that is a
  collection still takes a collection by its JSON key.
- A field's JSON key or write key sends no conversion, so it raises
  `ValueError` naming the parameter and advising the field's name, as in
  `filter(Span__in=Span(1, 2))` or `filter(Color=Color.RED)`, for a value that
  is no string, number, or `None`, such as a boolean, an enum member, a
  `datetime`, or an instance of a class that `conversions` registers, and for
  each such item of a lookup that takes several values. `v1.0.0a5` sent the
  value's `str()`, as `True` for a boolean, or its items. Pass it by the
  field's name, which converts it. A model object given for a relation is still
  sent as its key, but one given for a field whose type is not defined yet is
  refused.
- An item of a collection of relations has a key. An item that the server
  embeds without one, such as `{"name": "x"}` for a `list[User]`, raises
  `FieldConversionError` naming the field and the item's position when the
  field is read, where `v1.0.0a5` read it as an object whose `pk` was `None`.
  Declare items that have no key as a `TypedDict` or a dataclass. The read JSON
  schema of such an item requires the related model's key.
- A query that compares the model's own key under two names, as in
  `filter(pk=7, id=8)`, `filter(pk__exact=7, id=8)`, `filter(pk=7, postId=8)`,
  `filter(pk__gt=7, postId__gt=8)`, or `filter(pk=7, ident=8)` for a key with
  `write_map={"id": "ident"}`, raises `ValueError` naming both parameters
  before any request, and so do two parameters that are sent under one name, as
  in `filter(id=7, postId=8)`. `create(id=7, postId=8)` raises `TypeError`
  naming both. The key's JSON key, `postId` for a field `id`, is otherwise an
  ordinary query name, sent as given, and so is it with a suffix that compares
  no value, as `postId__icontains`.
- The hooks `collection_items()`, `collection_items_from_response()`,
  `collection_page()`, and `bulk_get_by_pks()` must accept `api=` and
  `**kwargs`, and `object_json()`, `merge_response()`, and
  `API.raise_for_response()` must accept `**kwargs`. A class that overrides one
  of them without, also an undecorated base class, raises `TypeError`, naming
  the hook, when it is defined. `api` is required in `bulk_get_by_pks()` too,
  so code that calls it itself must pass it. It returns a `Mapping` of the
  model's objects.
- Annotate a hook's response as `eazyrest.Response` and its client as
  `eazyrest.Client`. An override annotated `requests.Response` or `API` fails
  type checking.
- `filter()` reads a Django REST Framework page, a `results` list with a `next`
  link, by default, where `v1.0.0a5` raised `ResponseShapeError`. An override
  of `collection_items()` that only read such pages can go.
- A class that overrides only `collection_page()` now reads `filter()` and
  `all()` through it, as it reads `get_page()`. The default
  `collection_items_from_response()` reads the response with
  `collection_page()` and the pages that it links to, where `v1.0.0a5` read
  only Django REST Framework's page shape and a list, and a class for another
  page shape had to override `collection_items_from_response()` too. A class
  that overrides `collection_items()` or `collection_items_from_response()` is
  read through it as before. A page that a hook builds with `Page(...)` and no
  source ends `filter()` after its items.
- The redirect checks apply to requests that go through the API's request
  methods, and a request that a subclass sends another way skips them.
- A request for a next page does not go through `API.get()`, and neither does
  one from `get_page(url=...)`, `next_page()`, or `previous_page()`, so an
  override of `get()` sees none of them. `v1.0.0a5` sent a next-page request
  through `get()`, so a subclass could count it, rewrite its URL, or add a
  credential there. Pagination now sends the request itself, with the session's
  headers, authentication, and hooks, the `request_options=` of a scoped query,
  and the API's limits, and checks the page's origin and each redirect as
  before. Move what such an override did to the session's configuration, its
  authentication handler, or a session hook. A subclass that sent a next-page
  request through the session directly now has the redirects of that request
  checked, and its session response hooks run.
- A field type that eazyrest has no way to convert is refused, where `v1.0.0a5`
  read and wrote its JSON as it was. That covers `bytes` without a conversion,
  `Hashable`, `type`, `Callable`, a bare `set`, `frozenset`, `Mapping`, or
  `Sequence`, their aliases in `typing`, and any class that no entry of
  `conversions` converts, a subclass of `str` or `int` among them, at the field
  and inside a collection, a mapping, a union, or a `TypedDict`, `NamedTuple`,
  or dataclass. A `NewType` of `str` or `int` serves for a key. Annotate the
  field `Any` to keep its JSON, give a bare collection its items, as `set[Any]`
  or `Mapping[str, Any]`, or register a `Conversion` for the class. A
  subclass's `conversions` decide for the fields that it inherits.
- A `Literal` or an enum with a value that is not a JSON scalar, such as
  `Literal[b"x"]` or an enum whose members are tuples, or a float that is not
  finite, as `Literal[math.inf]`, raises `TypeError` when the class is
  decorated. `v1.0.0a5` accepted it, though no JSON value read as such a member
  and its schema held a value that JSON cannot write. An enum with an entry of
  its own in `conversions` is not checked.
- An enum with a member whose value is `None`, or a `Literal` that holds such a
  member, raises `TypeError` naming the field and the member when the class is
  decorated, since every field reads `null` as `None`, so the member could
  never be read. `v1.0.0a5` accepted both.
- A set or frozenset whose items have no hash, as `set[dict[str, int]]`,
  `frozenset[list[int]]`, or a set of a dataclass that is not frozen, raises
  `TypeError` naming the field and the item type when the class is decorated.
  `v1.0.0a5` accepted it, read an empty array as an empty set, and refused any
  other value when the field was read. Use a list, a tuple, or a frozen
  dataclass.
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
- eazyrest has no Pydantic integration. The `pydantic` extra and the
  `eazyrest.pydantic` module, with its types `Datetime`, `Date`, `Time`,
  `Duration`, `UUID`, and `Decimal`, are gone, and a field converts by its
  declaration and its model's conversions alone, whether or not Pydantic is
  installed. A field whose type is a Pydantic model, a class derived from
  `BaseModel` or `RootModel`, or whose class has a Pydantic config, as
  `pydantic.with_config()` gives one, raises `TypeError` when the class is
  decorated, naming the class, unless an entry of the model's conversions
  converts that class. The same holds for such a class inside the type. A class
  with Pydantic's schema hook is refused the same way. `Annotated` metadata for
  Pydantic, such as `Field(gt=0)`, `Field(alias=...)`, `AfterValidator`, or the
  encoders in `Base64Bytes`, raises `TypeError` when the class is decorated, at
  the root of a field and at every member that eazyrest walks, and a conversion
  for the underlying type does not exempt it. Remove the metadata, or register
  a conversion for the type. The constraints of `annotated_types` are not
  refused. A native `TypedDict` keeps the keys that it does not declare, where
  a Pydantic model dropped them, and a dataclass refuses them. Validation
  belongs to the application: check a value before it is assigned, or in the
  `decode` and `encode` of a conversion, which report a bad value by raising
  `TypeError` or `ValueError`. To give a type its wire format, register a
  `Conversion` for it, as for bytes that the server sends as base64:

  ```python
  import base64

  from eazyrest import Conversion, JSONObject, json_object


  class APIObject(JSONObject):
      conversions = {
          bytes: Conversion(
              decode=lambda value, cls: base64.b64decode(value),
              encode=lambda value, cls: base64.b64encode(value).decode("ascii"),
              schema={"type": "string", "format": "base64"},
          ),
      }


  @json_object
  class Logo(APIObject):
      class_url = "/logos/"

      id: int
      image: bytes
  ```

- A plain `TypedDict`, `dict[str, T]`, fixed-length tuple, `NamedTuple`, or
  dataclass field converts its keys, items, or fields, where `v1.0.0a5` kept
  their JSON, so a `datetime.datetime` under a `TypedDict` key reads as a
  `datetime`. Annotate the JSON form, such as `dict[str, Any]`, to keep the
  JSON, or `Any` to keep any JSON, since `dict[str, Any]` now requires a JSON
  object. Types whose JSON would read as another type, such as a mapping whose
  keys are not strings, a model inside a mapping, or a union with a member that
  is not a JSON scalar, such as `list[str] | str` or
  `dict[str, Any] | list[Any]`, raise `TypeError` when the class is defined,
  naming the field, or `FieldConversionError` when the field is first used if
  they are members of a `TypedDict`, dataclass, or `NamedTuple`. Annotate such
  a union `Any`. A dataclass field reads an instance, where `v1.0.0a5` read a
  `dict`, and refuses a key that the dataclass lacks and a `dict` assigned to
  it. A `NamedTuple` with defaults refuses a shorter array. A parameterized
  generic `TypedDict`, and a parameterized type whose items eazyrest does not
  convert, such as `Counter[str]`, raise `TypeError` when the class is defined.
- A field annotated with a tuple class whose fields have no annotations, as
  `collections.namedtuple()` makes one, raises `TypeError` when the class is
  defined, unless a conversion converts the tuple class. Declare it with
  `typing.NamedTuple`.
- A field that is not the key, whose dotted annotation names an attribute that
  its module does not have yet, raises `FieldConversionError` when the field is
  used, not `AttributeError`. Every use of a field whose type cannot be read
  yet raises the same error, and the object's other fields still read and
  write.
- A plain `TypedDict`, dataclass, or `NamedTuple` whose field's annotation
  names a type that is imported only under `TYPE_CHECKING` raises
  `FieldConversionError` when the field is first used, where `v1.0.0a5` passed
  its JSON through. Import the type at run time.
- A field whose type is a `TypedDict` that is closed or has extra items (PEP
  728), or a subclass of one, which PEP 728 closes too, raises
  `FieldConversionError` naming the field when the field is first used, also
  inside an optional or a collection, where `v1.0.0a5` kept its JSON. Annotate
  the JSON form, such as `dict[str, Any]`.
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
  `datetime`, converts as that type, and a `NewType` as its supertype, also
  when the supertype is a named alias or an `Annotated`, where `v1.0.0a5` read
  their JSON values unchanged.
- A `Literal` field reads the JSON value of one of its enum members as the
  member, where `v1.0.0a5` read the JSON value.
- `decimal.Decimal` and `datetime.time` fields read as `Decimal` and `time`,
  where `v1.0.0a5` left the strings. A decimal string with spaces, such as
  `" 1.0 "`, is refused, and `Decimal("1E+2")` writes `"100"`. `deque[T]` reads
  a `deque`.
- `parse_duration()`, and so a `timedelta` field, refuses a string with
  whitespace before or after it, such as `" 1h "`, with `ValueError`, as a
  date, a time, and a decimal do. `v1.0.0a5` stripped it. Whitespace between
  the parts of a duration, as in `"1h 30m"`, is still allowed.
- A value that cannot be written as JSON, such as a float that is not finite,
  raises `FieldConversionError` when it is assigned, where `v1.0.0a5` accepted
  the assignment. Inside a type that eazyrest does not convert, such as an
  `Any` field or a value of `dict[str, Any]`, a tuple, which reads back as a
  list, and a mapping whose keys are not strings are such values. Assign a list
  instead of a tuple.
- A value that nests more than 128 levels deep, the default of
  `API.max_json_depth`, or that holds itself, raises `FieldConversionError`
  when it is assigned or given to `create()`, and `ValueError` from
  `encode_json()`, before anything is sent. `v1.0.0a5` accepted such a value up
  to Python's recursion limit, and a value between about 500 and 990 levels
  raised a bare `RecursionError`, from `create()` after its `POST`. What is
  written can be read back from a server that answers within the default limit.
- A set or frozenset field writes its items in a fixed order, sorted as JSON
  values, rather than in the set's order, and assigning a set equal to the
  server's sends no update, whatever order the server lists the items in. That
  holds for an optional set and a `NewType` of a set.
- `conversions` is a `JSONObject` attribute, so a model with a field of that
  name raises `ValueError` when it is decorated, and a class variable, method,
  or property of that name raises `TypeError` when the class is defined. Give
  the field another attribute name, with `field_map=` to keep its JSON key.
- `cls.api` is the class's registered API everywhere. While `filter()` called a
  collection hook, `v1.0.0a5` made `cls.api` the API that the query passed as
  `api=`, and the guide promised it. The promise ended when the hook returned,
  so the generator of a lazy hook already saw the registered API. A hook that
  reads `cls.api` to send a request must use its `api` argument.
- `api=` and `register_api()` take an `API`. Another object, such as a test
  double, raises `TypeError`.
- Requests send `Accept: application/json`, where `v1.0.0a5` sent requests'
  `*/*`. Set `api.session.headers["Accept"]` for a server that answers JSON
  only to another type.
- `json_schema()` describes fields that it gave as `{}`, such as `Decimal`,
  `time`, `int | str`, `dict[str, Any]`, and tuples, and names its dialect,
  JSON Schema 2020-12, in `$schema`, so a schema that a client compared with a
  saved copy changes.
- The schema of a `Flag` or `IntFlag` field is `{"type": "integer"}`, because
  the field reads and writes the combinations of its members, such as `3` for
  `READ | WRITE`, which a list of the single members would reject.
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
  `Page` leaves them out too. A message names a request by the URL that
  eazyrest was asked to send, such as the one that you passed to `get()` or a
  next-page link that was sent, and quotes no part of a URL that a response
  supplied. The one exception is a next-page link that eazyrest refuses to send
  or stops on, because it has another origin or names a page that was already
  read: the message quotes its origin alone, without its query, which can hold
  a signature or a token, and the whole link is on `Page.next_url`. After a
  followed redirect it names the URL that was asked for, and not the one that
  the redirect reached, and a refused redirect no longer names the origin that
  it was refused for, so code that looked in a message for either must read the
  error's `response`, whose `url` and `Location` header hold them. The `repr()`
  of a `Page` shows the URL that it was asked for. An exception that requests
  itself raises, such as `requests.ConnectionError`, quotes the URL of that
  host with its path and query.
- A 401 challenge whose body is larger than `max_response_bytes`, or that
  arrives past `total_timeout`, raises `LimitExceeded` when the session uses an
  authentication handler that reads the body, such as
  `requests.auth.HTTPDigestAuth`. Such a challenge was read without a limit.
  Its body also counts against the limit that the final body shares, so a
  challenge and a final body that are each within `max_response_bytes` raise
  `LimitExceeded` when they total more.
- The client that a query with `request_options=` passes to its hooks as `api`
  is a view of the API, not a copy of it. `v1.0.0a5` passed `copy.copy()` of
  the `API`, which shared its mutable attributes and copied its scalar ones, so
  a subclass that counted requests or refreshed a token in its own `get()` lost
  the requests of a scoped query. The view sends every request through the
  API's own `get()`, `post()`, `put()`, `patch()`, or `delete()` with the
  query's options, so the subclass sees them, though not a request for a page,
  which pagination sends directly. It is not an `API`, so
  `isinstance(api, API)` is false and a hook that checks it, or that casts
  `api` and assigns an attribute, changes: reading an attribute outside
  `Client`, such as `api.session` or a helper of your subclass, reads the
  API's, and assigning one raises `AttributeError`. A helper of your subclass
  that sends requests through its own `self` does not send the query's options.
  An object that a hook builds with this `api` keeps the view, and registering
  the view with `register_api()` raises `TypeError`. A query without
  `request_options=` still passes its API itself.
- The view of the client that a query with `request_options=` passes to its
  hooks adds its options and nothing else. `v1.0.0a5` raised `RedirectError`
  from it for a 3xx that the request did not follow, so a hook that sent
  `allow_redirects=False` and read the response itself failed there, and not in
  a query without options. It gets the 3xx back now, as from the API. A query
  and a next-page request still raise `RedirectError` for an unfollowed 3xx.
- A write or any request with a body follows no redirect. A request with any
  method but `GET`, from a model or from `API.post()`, `put()`, `patch()`, or
  `delete()`, and a `GET` with `data`, `json`, or `files`, is sent with
  `allow_redirects=False`, even if you pass `allow_redirects=True`, and a 3xx
  response to it raises `RedirectError`. `v1.0.0a5` followed a 307 or 308 of
  such a request to the request's origin, or to an origin in
  `API.redirect_origins`, and resent it with its body, and a raw write sent
  with `allow_redirects=False` returned the 3xx response. It refused a 301,
  302, or 303 of a `GET` with a body, as this release does. The message does
  not quote the `Location`, which is on the error's `response`. Send the
  request to the URL that the server expects, for example by setting
  `API.trailing_slash`.
- A raw `GET` sent with `allow_redirects=False` returns a 3xx response, where
  it raised `RedirectError` for a `Location` on another origin, though nothing
  was followed. A query and a next-page request raise `RedirectError` for an
  unfollowed 3xx. A next-page request to the API's origin that is answered with
  a redirect to another origin, when the query's client does not follow
  redirects, raised `PaginationError`, which advised
  `allow_other_origins=True`, though that option would follow nothing. It
  raises `RedirectError` now. A next-page URL that itself names another origin
  still raises `PaginationError`. A `Location` that cannot be parsed raises
  `RedirectError`, where `v1.0.0a5` let requests' `UnicodeDecodeError` or
  `ValueError` escape, so code that caught them must catch `RedirectError`. So
  does any other `Location` for which requests raises `ValueError` or
  `UnicodeError` while it prepares the next request, such as a port that is out
  of range when the request carries an `Authorization` header and the redirect
  keeps the host. A `Location` that requests prepares without error is
  returned. The body of an unfollowed redirect still counts against
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
  object's key or URL. So does a model object given for a primary key.
- Every object of a model that declares a key field has its key from
  construction. `User()`, `User(id=None)`, and `User(name="a")` raise
  `TypeError` naming `User.id` and pointing to `create()`, and `json=` without
  the key, with `null` for it, or with a key that is not of the key's type, as
  `User(json={"id": "7"})`, raises `FieldConversionError` naming `User.id`.
  `v1.0.0a5` built such objects, with the key `None` or a `pk` that raised. A
  caller that built one to create a resource calls `create()` instead, and one
  that built a resource at a fixed URL, such as `/me/`, reads it with the API
  and builds the object from the response, as
  `User(json=api.json(api.get("/me/")))`, whose `url` is the model's own. A
  class that overrides `url` needs the key too. A query or a page whose item
  names no key that the model reads raises `ResponseShapeError` naming the key
  field and the item's position, and `create()` and `get_or_create()` raise
  `ResponseShapeError` after the `POST` when the response names none, saying
  that the resource may have been created, with the response as the error's
  `response`. An embedded related object without its key, as
  `"owner": {"name": "a"}`, raises `FieldConversionError` naming the relation
  field when the field is read. An object therefore always equals the objects
  of its model with its key and hashes, and the refusals of an object without a
  key in relation writes, queries, and prefetch are gone with it.
- During `create()`, the object that `merge_response()` is called on is not the
  object that `create()` returns, and it has no key. Its `pk`, `url`, and
  `hash()` raise `UsageError`, and so does `==` with another object of its
  model. `v1.0.0a5` called the hook on the returned object, so a hook that kept
  something on `self`, such as an `ETag`, or read `self.pk`, no longer works
  for `create()`. Keep what is needed in the JSON that the hook returns, and
  read the key from the response.
- A pickle of an object of a keyed model that `v1.0.0a5` wrote does not load.
  It raises `UsageError`, because its state holds no key. Read the object from
  the server again. Pickles that this version writes load.
- A `UUID` key reads a string only in the hyphenated, compact, braced, or
  `urn:uuid:` form, in ASCII hex of either case, from the program, from JSON,
  in a query, and in a relation. Any other string raises `FieldConversionError`
  naming the field, where `v1.0.0a5` read every string that `uuid.UUID` reads,
  such as one with a `0x` prefix, a sign, surrounding whitespace, or digits
  that are not ASCII, as the UUID that its digits name.
- A `null` item in a collection of relations raises `FieldConversionError`
  naming the field and, for a sequence, the item's position, when the field is
  read, and before any request when a `None` item is assigned, added in place
  and saved, or given to `create()`. `v1.0.0a5` read it as `None`, kept it in a
  set, and sent it as `null`.
- `create()` and `get_or_create()` refuse a response that cannot be the created
  object with a `ResponseShapeError` that holds the response, because the
  resource may exist. A body that is not one JSON object, an empty list or a
  list of several objects among them, raises it, where `v1.0.0a5` raised
  `DoesNotExist` or `MultipleObjectsReturned` for a list. So does a response
  that names another key than the request sent, where `v1.0.0a5` returned the
  object of the key that the response named, and a `save()` already raised for
  it. A response that names no key takes the key that the request sent.
- An object of a model whose key annotation names a class that is not defined
  yet, as during a circular import, raises the error that resolving the name
  raises when the object is built, a `NameError`, an `AttributeError` for a
  module's attribute, or a `TypeError` for a type alias's string that names
  nothing. `v1.0.0a5` built the object, whose `==` was `False` and whose `url`
  and `hash()` raised until the name was defined. A key that is refused once
  its name resolves raises its `TypeError` when an object is built, where `==`
  and `hash()` raised it. A relation to such a model, or to a model that is not
  defined yet, still lets its owner be built, and raises when it is read.
- `json_schema()` gives a primary key the type that its builder sends, in read
  and write schemas alike, without `null` for a key whose annotation is
  optional, and a single relation's read schema requires the related model's
  key in its embedded object form, as a collection of relations already did.
  `v1.0.0a5` allowed `null` for an optional key and any object for an embedded
  related object.
- The read schema from `json_schema()` requires the primary key's JSON name,
  where `v1.0.0a5` listed no required property, and the write schema requires
  nothing. A validator that checks a payload with the schema then refuses an
  object that lacks its key, as reading it does.
- `create()` and `get_or_create()` resolve a name to its field before they
  apply its policy and conversion. `pk` and the key's JSON and write keys name
  the key, so `create(pk=7)` posts the key under its write key, where
  `v1.0.0a5` posted a member named `pk`. A read-only key is refused under any
  of its names, where `v1.0.0a5` posted it under its JSON key, and a key of
  another type, such as `"7"` for an `int` key, is refused before the `POST`
  under its write key too. The key is sent as it is built, so a `UUID` key
  given in compact uppercase is posted in its lowercase hyphenated form, as a
  full save puts it, where `v1.0.0a5` posted the spelling that it was given. A
  name that is the key's JSON or write key and also another field's raises
  `TypeError`. `get_or_create(pk=post)` for a `Post` object raises
  `FieldConversionError` naming the key field before the lookup, as
  `get_or_create(id=post)` does. The lookup of `get_or_create()` builds a key
  given under its JSON or write key as its creation does, so the query and the
  `POST` hold one spelling of a `UUID`, where the query sent the spelling that
  it was given. An upload in `files=` under any name of the key, or under a
  form key that a field of the request already writes, raises `TypeError`
  before the request, where `v1.0.0a5` sent it.
- A decorated subclass that redeclares the key field that it inherits with
  another annotation, such as `id: NewType("AID", int)` or `id: int | None`
  under `id: int`, raises `TypeError` when it is decorated, naming the
  subclass, the field, and the base's annotation as written. A subclass that
  chooses the field as its key again after a base chose another field is
  refused the same way. `v1.0.0a5` decorated it, and a relation to the base
  refused its objects only when they were written. Redeclare the field with the
  base's type, or leave it out.
- A primary key is a `str`, an `int`, or a `UUID`, alone or inside `T | None`,
  `Annotated`, or a `NewType`. Any other key raises `TypeError` when the class
  is decorated, naming the field and the types that a key may be, where
  `v1.0.0a5` admitted an enum, a `Literal`, a `Decimal`, a `datetime`, a
  `date`, a `time`, a `timedelta`, a `bool`, a `float`, any class that a
  conversion converts, and a relation. A type that a conversion in the model's
  `conversions` converts is refused too, even a `NewType` of `str`, `int`, or
  `UUID`, and so is a `UUID` when the model replaces eazyrest's conversion of
  it. A conversion may map two of the service's identifiers to one value, as an
  enum's `_missing_()` does for a value that it does not list or a `datetime`
  does for timestamps that differ past the microsecond, and then a write to one
  resource changed another. Declare the key as the type that the service sends,
  and expose the richer value as a property, as in `id: str` with a property
  `code` that returns `Code(self.id)`. A key that was a relation, as
  `pk="user"` with `user: User`, is declared as the related model's key type,
  as `user_id: int` with `field_map={"user_id": "user"}`, and a property `user`
  that returns `User(id=self.user_id)` gives the related object. A key whose
  type names a class that is not defined yet raises the same `TypeError` when
  the key is first used. The guide's "What a key may be" shows both patterns.
- A field whose annotation resolves and whose own type eazyrest does not
  convert, such as `Counter[str]`, `int | list[int]`, a type alias that refers
  to itself, raises `TypeError` when the class is decorated, naming the field,
  where earlier releases raised `FieldConversionError` when the field was first
  used. A name that is not defined yet, and a member of a `TypedDict`,
  dataclass, `NamedTuple`, still raise when the field is first used.
- A field whose own type is a union with a member that converts, such as
  `str | datetime.datetime`, also inside an optional, a collection, a mapping,
  a fixed-length tuple, or a `NewType`, raises `TypeError` when the class is
  decorated, naming the field, where `v1.0.0a5` raised `FieldConversionError`
  when the field was first used. The model's `conversions` decide which members
  convert, so a subclass whose `conversions` make an inherited field's union
  one that converts is refused when the subclass is decorated. A subclass that
  sets `conversions` and is not decorated raises `TypeError` when it is first
  used, naming the class and saying to decorate it. A union inside a
  `TypedDict`, dataclass, or `NamedTuple` still raises when the field is first
  used.
- A field typed by a dataclass with an init-only field, of type
  `dataclasses.InitVar`, raises `TypeError` when the class is decorated, naming
  the init-only field, where `v1.0.0a5` raised `FieldConversionError` on every
  read, blaming keys that the dataclass does not declare. Make it a field.
- A field annotated with something that is not a type, such as a function, a
  module, a value, a bare `LiteralString`, `Never`, `Self`, `Final`, or a
  `TypeVar`, raises `TypeError` when the class is decorated, naming the field,
  where `v1.0.0a5` passed its JSON through unconverted. `Literal[...]` is
  unchanged. On Python 3.14 and later, a method that has the name of a field's
  type, as `date` does beside `created: date`, hides the type, so such a class
  is refused too. Annotate the type by a name that the class does not shadow.
- A field whose annotation is `None` as a whole, as `date: date = None` gives,
  raises `TypeError` when the class is decorated, naming the shadowing of the
  type by the field, where `v1.0.0a5` read its JSON unconverted.
- `refresh()` raises `ValueError` for an object whose key is empty, `.`, or
  `..`, where `v1.0.0a5` reset it without an error. It raises before it
  discards anything.
- Bind an API with `Base.register_api(api)`. Assigning it, as in
  `Base.api = api`, would make `api=` ignored, so a class body that assigns
  `api` raises `TypeError`, and constructing an object of a class whose base
  had it assigned, or querying or creating one, raises `UsageError` before
  sending anything.
- A field whose type is a model without a primary-key field raises `TypeError`
  when the class is defined or, for a class defined later, when the field is
  first used, where `v1.0.0a5` defined the class and read the holder's other
  fields. Declare nested JSON without a key as a `TypedDict` or a dataclass.
- In eager mode, assigning a field a value equal to one of its updates that has
  not been sent sends the `PATCH`, where `v1.0.0a5` sent nothing and left the
  update pending. An update is unsent when it was made in lazy mode, or when a
  change made in place was found as the object loaded missing fields.
- Two objects of one model with the same known primary key are equal, where
  `v1.0.0a5` compared them by identity, so a set or dict of them keeps one. The
  key is the one that the object has from construction, given or read from the
  JSON that built it. Two spellings of one `UUID` are one key, and two `str`
  keys that differ in case are two keys. A key of type `int` or `str` refuses a
  value of another JSON type where it is given, with `FieldConversionError`, so
  `User(id="1")`, `User(id=1.0)`, and `User(id=True)` raise, where `v1.0.0a5`
  gave other keys that a load of the object then replaced and so changed its
  hash. A related object that a read builds from the server's JSON is checked
  too, and the error names the relation field. Give the key in its field's
  type. Once the key is known it never changes. A refresh leaves the key, the
  hash, and the `url` as they were. Assigning another key to an object that has
  its key raises `AttributeError`. A load, a write's response, and
  `update_from_json()` that name another key raise `ResponseShapeError`, a
  `ValueError`, and change nothing.
- `update_from_json()` keeps the related objects of a relation field whose JSON
  the payload leaves unchanged, as a load does, so that a related object keeps
  its unsaved changes when a payload repeats the field's JSON, where `v1.0.0a5`
  read new related objects and dropped the changes. A payload that names
  another key, or that embeds the same key with another member changed, or
  gives the bare key where an object was embedded, still gives new objects, and
  a change made in place to the field's own collection and a pending update of
  the field are still discarded.
- A query with `prefetch=` raises `TypeError` before any request if the model's
  `prefetch_batch_size` is not an `int`, and `ValueError` if it is below 1,
  where `v1.0.0a5` read no object for 0, and raised the `ValueError` of
  `itertools.islice()` after the request for another value. `None` reads the
  collection as one batch.
- `create()` raises `TypeError` before any request for a value under a name
  that is no field when the value is a tuple or a mapping with a key that is
  not a string, which `get_or_create()` already refused, where `v1.0.0a5` sent
  the tuple as a list and the key as its text.
- A `write_options()` block that ends while a block of the same object that
  began later is still open raises `UsageError`, where `v1.0.0a5` left the
  first block's options on the object for every later write.
- A keyed model that declares no `class_url` raises `UsageError` naming the
  class and `class_url`, before any request, from `url`, a read that loads the
  object, `refresh()`, `delete()`, `create()`, and the query methods, where
  `v1.0.0a5` raised an `AttributeError`, which `hasattr()` and `getattr()` with
  a default swallow.
- A string or bytes given as `API.redirect_origins` raises `TypeError` before
  the request, saying to give a collection of origins, where `v1.0.0a5` read it
  as a collection of its characters, so that every request raised the
  `Invalid URL` error of requests. Every request checks it, a request for a
  page that must stay on the API's origin included.
- A prefetched relation matches the objects that `bulk_get_by_pks()` returns by
  the key that each object holds, compared as `==` compares keys, not by the
  key of the returned dictionary, so `str` keys `"a"` and `"A"` get two
  objects.
- Prefetch raises `TypeError` when `bulk_get_by_pks()` returns an object that
  is not an instance of the related model or of a subclass of it, naming the
  hook, the model, the field, and the class returned. A parent whose key
  matched such an object used to read it, an object of another model and
  resource.
- A relation refuses an object of a subclass that chose another primary key
  with `pk=`, with `FieldConversionError` naming the field, in an assignment,
  `create()`, and a query. Prefetch raises `TypeError` for such an object that
  `bulk_get_by_pks()` returns. They used to write the subclass's key, which
  names another resource of the related model. Give the relation an object of
  the related model's own key.
- A relation builds the key of a related object that the program gives it with
  the related model's key constructor, as it builds one that it reads. An
  assignment, `save()`, `create()`, a query, and a lookup such as `owner__in`
  refuse an object whose key that model would not read, with
  `FieldConversionError` naming the relation field, where `v1.0.0a5` wrote the
  key's JSON as it was, which could be `true`, a list, or a mapping that a
  server may read as an embedded object. A `save(full=True)` that reads an
  embedded key of another JSON type from the object's own JSON raises the same
  error before the `PUT`, where it sent the key. A key that the program gives
  to a relation as it is, as in `post.owner = 7`, `create(owner=7)`, or
  `filter(owner=7)`, is built by the same constructor. Where `v1.0.0a5` sent
  `"7"`, `true`, or a list for an `int` key, and read a mapping back as an
  embedded object, they now raise `FieldConversionError` naming the relation
  field before any request, and in the `__in` and `__exact` lookups of a
  relation, which sent a bare value as it was given. A query that compares the
  model's own key, such as `User.get_or_create(id="7")`,
  `User.filter(pk__in=["7"])`, or `User.filter(id__range=["1", "9"])`, raises
  before its request, where it sent the request first or sent the text as
  given.
- A lookup on a field, such as `status__in`, `due__gte`, or `flag__exact`,
  encodes each value as the field's own parameter does, where `v1.0.0a5` sent
  its `str()`: an enum member is sent as its value, a `datetime` as ISO 8601
  text, and `True` as `true`, and a value that the field refuses raises
  `FieldConversionError`. The suffixes that compare values of the field are
  `exact`, `in`, `range`, `gt`, `gte`, `lt`, and `lte`. A lookup is also sent
  under its field's JSON key, so `type___in` on a field `type_` with the key
  `type` is sent as `type__in`, where it was sent under the Python name. A
  field whose conversion writes every value as a list or an object, such as a
  `NamedTuple`, has no query string for its value, so a lookup on it, such as
  `point__exact=Point(1, 2)`, raises `ValueError` naming the parameter, and
  text such as `point__exact="1,2"` raises `FieldConversionError`, where
  `v1.0.0a5` sent the text as given. Filter such a field through a server
  filter under another name, as in `Todo.filter(point_text="1,2")`.
- A lookup whose name ends in `__in` or `__range` sends its values as one
  comma-separated parameter, `?id__in=1,2`, where `v1.0.0a5` sent the parameter
  repeated, `?id__in=1&id__in=2`. django-filter reads the joined form, and read
  only the last of the repeated one, so `id__in=[1, 2]` matched the object with
  key 2, and refused a repeated `__range`. The new attribute
  `API.list_lookup_style`, `"comma"` by default, and `"repeat"` for a server
  that reads repeated values with `getlist()`, chooses the form, and `Client`
  has it as a property. The value must be an iterable, so `"1,2"` and `7` raise
  `ValueError`, as does one value that the field's conversion takes whole, and
  as an empty iterable, a `__range` that does not have two items, an item whose
  text is empty, and an item whose text holds a comma do. A set is sent in
  sorted order. Join no values yourself: the guide advised that for
  django-filter's lookups, and it is no longer needed.
- `copy.copy()` of a model object gives the same copy as `copy.deepcopy()`,
  which owns its related objects, pending updates, cached values, and JSON,
  where `v1.0.0a5` shared them with the original, so an assignment to the copy
  was sent by the original's `save()`. Code that relied on a copy sharing the
  related objects of the original must keep the original.
- Prefetch builds each key that the parent payloads hold with the related
  model's key constructor before it calls `bulk_get_by_pks()`, each distinct
  value once. A value that model cannot build as a key, such as `"7"` or `true`
  for an `int` key, or a list, raises `FieldConversionError` naming the
  relation field before the method and its request. `v1.0.0a5` passed `"7"` and
  `true` on, where `true` and `1` were one key, and raised `ResponseShapeError`
  for a list. The method receives the keys as the related model writes them.
- A field named twice in `prefetch=`, as in `prefetch=["owner", "owner"]`,
  raises `ValueError` naming the field before any request, where `v1.0.0a5`
  called `bulk_get_by_pks()` once for each copy with the same keys. Name each
  field once.
- `get_or_create()` refuses an argument that `create()` could not send before
  it sends the lookup. A model under a name that is no field, as in
  `get_or_create(x=User(id=1))`, raises `TypeError`, and a single item for a
  collection field raises `FieldConversionError`. The lookup's own checks come
  first, so a lookup parameter that requests would not send intact still raises
  `ValueError`. `v1.0.0a5` sent the lookup and raised only when nothing
  matched, so a call that a match satisfied is now refused. Pass such a value
  to `filter()` or `get()`.
- A model object given alone as the value of a collection of related objects,
  as in `Team.create(members=user)` or `team.members = user`, raises
  `FieldConversionError` even if the model defines `__iter__`. It was written
  as the members that its iteration yields, each as a key, so a model that
  iterates as `1, 2, 3` sent `{"members": [1, 2, 3]}` and related the wrong
  objects. `get_or_create()` refuses it before the lookup. Pass `[user]`.
  `filter()` and `get()` still take one model for a collection field and send
  its key.
- A key is built from the value that the program or the server gives, before
  any conversion of the field, so no conversion can change which resource it
  names. `pk` and the key field read the key as it is built: an `int` or a
  `str`, or a `UUID` for a `UUID` key. A key given as a subclass, such as an
  `IntEnum` member for an `int` key or a `str` subclass for a `str` key, reads
  as the plain value, where `v1.0.0a5` returned the value as it was given. A
  `UUID` subclass whose `str()` prints other text names its UUID, where
  `v1.0.0a5` refused it with `FieldConversionError`.
- A key that a server sends in another JSON type than its field's, such as
  `"7"` for an `int` key, is refused as a key that the program gives is. An
  embedded related object whose key has another type raises
  `FieldConversionError` naming the relation field when it is read, and one in
  the JSON of an object or of a collection item is refused as a missing key is.
- `create()` checks the key that it is given as the constructor does, before
  the `POST`. A key of another JSON type, such as `"1"` for an `int` key, or
  one that does not convert, such as `"not-a-uuid"` for a `UUID` key, raises
  `FieldConversionError` and sends nothing. It used to send the key and keep it
  as given.
- `create()` and the creation of `get_or_create()` refuse `None` for the key,
  under any of its names and in `fields=` or `defaults`, with
  `FieldConversionError` naming the key field and saying to leave the key out,
  before any request. `v1.0.0a5` posted `null`, and the creation then raised
  `ResponseShapeError` whatever the server answered, after the resource might
  exist.
- A query on a set field sends the field's distinct keys, sorted, so
  `watchers={User(id=7), Admin(id=7)}` sends `watchers=7`, where `v1.0.0a5`
  sent a repeated key as many times as it was given. A set field that the
  server lists with a repeated key, as `["a", "a"]`, is the set `{"a"}`, so
  assigning the field its own value sends nothing.
- `save()` and `save(full=True)` raise `FieldConversionError` before any
  request, naming the field and, for a sequence, the item's position, when a
  bare key was added in place to a collection of related objects, as in
  `post.tags.append("b")`. `v1.0.0a5` sent the key and kept the bare value in
  the list. Add the related object, or assign the keys.
- Assigning a relation, or a collection of relations, the object or the
  collection that it holds sends nothing when the server embedded the object,
  as `post.owner = post.owner` for `"owner": {"id": 7, "name": "a"}`, where
  `v1.0.0a5` sent `{"owner": 7}`. An assignment compares the keys. Another key
  is still sent.
- A write or a load of one object that the server answers with an empty list or
  a list of several objects raises `ResponseShapeError` with the response, as
  `create()` does, and a write's updates stay pending. `v1.0.0a5` raised
  `DoesNotExist` or `MultipleObjectsReturned`, which describe a query.
- A set given to the parameter of a field that is a sequence, as
  `filter(tags={Tag(slug="b"), Tag(slug="a")})` for a `list[Tag]`, is sent
  sorted, where `v1.0.0a5` sent it in hash order, so the URL differed between
  processes. So is any other `collections.abc.Set` in a query, as a dict's key
  view, which is sorted now where `v1.0.0a5` sent it in its own order.
- `get_or_create()` raises `ValueError` naming the parameter, before any
  request, for a JSON key or write key that holds `__`, as `doc__id` for a key
  `id` or `the__title` for a field `title`, where `v1.0.0a5` looked up the name
  and then posted a body without the field. Look the key up as `id` or `pk`,
  and any other field by its name. A key whose field name holds `__` is looked
  up and created by that name.
- A type alias whose value is `Annotated[list["datetime.datetime"], "a note"]`
  reads the string in its metadata as written. On Python 3.11 to 3.13, a string
  there that is not an expression, such as `"a note"`, made the alias raise
  `SyntaxError` when a field of it was read. On Python 3.14 and later the alias
  handed its metadata on as a forward reference, not as the string.
- A `NewType` whose supertype holds a string, as in
  `NewType("Stamps", list["datetime.datetime"])`, raises `TypeError` when a
  field of it is read or written, naming the `NewType` and saying to write the
  type itself. Python never resolves such a string, so it stayed a string in
  eazyrest's conversion.
- eazyrest requires Python 3.11 or later. Python 3.10 is no longer supported.
  The package no longer depends on `typing_extensions`.
- `json` returns a new mapping on each call, with the object's pending updates
  laid over the server's JSON and no write-only field, where `v1.0.0a5`
  returned the object's own mapping. Changing the mapping no longer changes the
  object. Code that changed it to change the object must assign fields. Its
  values are still the object's own, so a value in it must not be changed in
  place.
- A query argument is read the same way whether or not its name is a field's.
  An iterable other than a string, bytes, or a mapping repeats the parameter,
  and its items are encoded by the field's conversion. A generator, `deque`,
  `range`, set, or dictionary view for the name of a collection field now
  repeats the parameter where it raised `FieldConversionError`, and a
  collection of models for a collection of related objects sends each primary
  key. An instance of a class that the field's conversion registers is one
  value, not its items, even if the class is iterable, and is refused if the
  conversion writes it as a list, where before it repeated the parameter with
  each item. A query argument must be finite, because the whole of it is read
  before the request is sent.

### Migrating from 0.9.0

Much of the library changed between 0.9.0 and 1.0. Each item below is a change
that can break code written for 0.9.0, followed by what to do about it.

#### Installation

- eazyrest no longer depends on `certifi`, `dateparser`, `pytimeparse2`, or
  `tzlocal`, and it depends on `requests` without its `socks` extra. Install
  `eazyrest[socks]` to send requests through a SOCKS proxy. Without it, the
  first request through one raises `requests.exceptions.InvalidSchema`.
- Every dependency now has a minimum version: requests 2.33.0, urllib3 2.8.0,
  isodate 0.7.2, and python-dateutil 2.9.0.post0.
- Python 3.11 through 3.15 are supported.

#### Writes

- Assigning a field no longer sends a `PATCH` at once. By default, an object is
  in lazy write mode, and assignments wait for `save()`, which sends them in
  one `PATCH`. Call `save()` after changing an object, or construct the API
  with `API(url, default_write_mode="eager")`, or call
  `obj.set_write_mode("eager")`, to send each assignment as it is made.
- Assigning the key of an object that has its key raises `AttributeError` when
  the value names another key, where 0.9.0 sent a `PATCH` of the new key to the
  object's URL and, for an object that had not loaded, pointed the object at
  the new key. That covers the key field and `pk`. Assigning the key that the
  object has does nothing and sends nothing. Every object has its key from
  construction, as the 1.0.0a5 items say, so 0.9.0's object without a key,
  which took the key that it was given, no longer exists. To rename a resource
  on a server that allows it, send the write with the raw API, as
  `api.patch(obj.url, json={"id": 2})`, and build the object again from the
  response. A known key is also kept when the server names another one. A load,
  a write's response, and `update_from_json()` raise `ResponseShapeError`, a
  `ValueError`, and change nothing, where 0.9.0 replaced the object's JSON and
  so its key. A write's response that is refused leaves the pending updates. A
  key whose type is `int` or `str` refuses a value of another JSON type where
  it is given, as `User(id="1")` does for an `int` key, with
  `FieldConversionError`. So does a related object that a read builds from the
  server's JSON, and the error names the relation field.
- A write response is merged into the object through the new `merge_response()`
  hook, rather than replacing its JSON. A response without a body, such as
  `204 No Content`, keeps the values that were sent. `create()` keeps only the
  JSON-backed fields it sent, so a write-only key such as a password is not
  kept.
- A write follows no redirect. A 3xx response to a write, such as a 301 in
  answer to a `PATCH` to a URL without its trailing slash, raises
  `RedirectError`, and a lazy update stays pending. The message does not quote
  the `Location`, which is on the error's `response`. Send the write to the URL
  that the server expects, for example by setting `API.trailing_slash`.
- `create()` sends each field under its JSON key, as `field_map` names it, as a
  write does, where 0.9.0 sent the Python name.
- `create()` and `get_or_create()` on a model without a primary-key field raise
  `UsageError` before sending anything. 0.9.0 created such an object, and its
  `get_or_create()` returned one that matched if there was one. Look an object
  up with `get()` or `filter()`, which still work on such a model, and to add
  one, post to the endpoint with the API and build the object from the
  response's JSON with `json=`.
- `json` returns a new mapping on each call, with the object's pending updates
  laid over the server's JSON and no write-only field, where 0.9.0 returned the
  object's own mapping. Changing the mapping no longer changes the object. Code
  that changed it to change the object must assign fields. Its values are still
  the object's own, so a value in it must not be changed in place.
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
  the JSON value unchanged. Undeclared keys are kept.
- Fixed-length tuples and `NamedTuple`s convert as JSON arrays by position, and
  standard library dataclasses as JSON objects by field, where 0.9.0 returned
  the JSON value unchanged. `tuple[T]` is a tuple of one item, as Python's
  types say, so annotate a tuple of any length as `tuple[T, ...]`. A union
  converts only if each of its members passes through unchanged, as
  `int | str`'s do.
- A mapping whose keys are not strings, a model inside a mapping or a
  fixed-length tuple, and any parameterized type whose items eazyrest does not
  convert, such as `Counter[str]`, raise `TypeError` when the class is defined,
  naming the field, because eazyrest cannot read them as their types. So does a
  union with a member that converts, such as `str | datetime`. Inside a
  `TypedDict`, `NamedTuple`, or dataclass, any of these, and a model, raise
  `FieldConversionError` when the field is first used. Annotate their JSON
  form, such as `dict[str, Any]`. eazyrest does not convert a model inside one
  of these either, so annotate it with its primary key type, such as `int`.
  `MutableSequence`, `Collection`, `Set`, `MutableSet`, `deque`, and
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
- Two objects of one model with the same known primary key are equal, where
  0.9.0 compared them by identity, so a set or dict of them keeps one. The key
  is the one that the object has from construction, given or read from the JSON
  that built it. Two spellings of one `UUID` are one key, and two `str` keys
  that differ in case are two keys. A key of type `int` or `str` refuses a
  value of another JSON type where it is given, with `FieldConversionError`, so
  `User(id="1")`, `User(id=1.0)`, and `User(id=True)` raise, and so does a
  related object that a read builds from the server's JSON, with an error that
  names the relation field. Give the key in its field's type. A refresh leaves
  the key, the hash, and the `url` as they were.
- A primary key is a `str`, an `int`, or a `UUID`, and any other key raises
  `TypeError` when the class is decorated, as the 1.0.0a5 items say. That
  includes an enum, a `datetime`, a `Decimal`, and a relation, which 0.9.0
  accepted as keys. Declare the type that the service sends, and expose the
  richer value or the related object as a property.

#### Declarations

- A field whose type is a model without a primary-key field now raises
  `TypeError` when the class is defined or, for a class defined later, when the
  field is first used. 0.9.0 read such a field but could not write it. Declare
  nested JSON without a key as a `TypedDict` or a dataclass. For a model class
  that is not decorated with `@json_object`, the error says to decorate it and
  that it also needs a key field, or to declare nested JSON as a `TypedDict` or
  a dataclass instead.
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
- A detail response that is a list of several items raises
  `ResponseShapeError`, where 0.9.0 read the first item.
- `reset_session()` keeps the session's headers, cookies, and query parameters,
  which 0.9.0 dropped.
- Requests send `Accept: application/json`, where 0.9.0 sent requests' `*/*`.
  Set `api.session.headers["Accept"]` for a server that answers JSON only to
  another type.
- A relation, assigned, given to `create()` or a query, or in a collection of
  relations, refuses an object of a class that is neither the field's model nor
  a subclass of it with `FieldConversionError`. 0.9.0 sent that object's key on
  assignment, raised `TypeError` from `create()`, and sent the object's
  `repr()` as the parameter of a query. A model object given for a primary key
  is refused too.
- `url` percent-encodes the primary key as one path segment, and an object
  whose key is empty, `.`, or `..` has no URL. `refresh()` raises `ValueError`
  for such an object, where 0.9.0 reset it without an error. An object without
  a key cannot be built, as the 1.0.0a5 items say.
- The second positional argument of `API()` is `proxy`, not `pool_connections`.
  Pass connection-pool settings, and other `requests.adapters.HTTPAdapter`
  arguments such as `max_retries=`, as keyword arguments.
- `verify=` takes a CA bundle path as well as a boolean, and a path or `False`
  takes precedence over `REQUESTS_CA_BUNDLE`. `verify=False` no longer
  suppresses urllib3's `InsecureRequestWarning` for the whole process. Filter
  the warning yourself if you accept unverified connections.
- A redirect of a `GET` is followed only to the origin of the request it
  answers, or to an origin in `API.redirect_origins`, so that session headers
  such as API keys are not sent elsewhere. A `Location` that cannot be parsed,
  such as `http://[bad/` or one with a byte that is not UTF-8, raises
  `RedirectError`, where 0.9.0 let requests' `ValueError` or
  `UnicodeDecodeError` escape. So does a `Location` with a port that is out of
  range, when the redirect is followed or requests must read the port of one
  that is not, where 0.9.0 raised `ValueError` or
  `requests.exceptions.InvalidURL`. `RedirectError` is not a `ValueError`, so
  code that caught them must catch it.
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
  or a dataclass.
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
- Named type aliases, including `typing_extensions.TypeAliasType` and the
  `type` statement.
- `json_schema()`, the JSON schema of what a model reads or writes.
- Conversion tables: a model's `conversions` maps a type to a `Conversion` that
  reads and writes its JSON form, and gives its JSON schema, merged over its
  bases' and the default table, so a shared base model can convert a type of
  its own, such as `IPv4Address`, or replace a default, such as writing
  datetimes as timestamps. An entry can also replace the conversion of a
  dataclass, a `NamedTuple`, or a `TypedDict`. `DEFAULT_CONVERSIONS` holds the
  default table. A class checks its table when it is defined, and refuses a key
  that is a model or a type that eazyrest converts itself, such as `int`, whose
  entry would also convert `bool`.
- `encode_json(value, annotation, *, model=None)`, which encodes a request body
  or a query parameter as a field annotated `annotation` writes it, by the
  default conversion table or by `model`'s, for `API.post(json=...)` and the
  other request methods, which send `json=` as it is given. It refuses the
  annotations that a field refuses, such as `Collection[User]`,
  `Iterable[User]`, `list[User | None]`, and `list[list[User]]`, also optional
  or in a `NewType`, with `TypeError`.
- Eager and lazy write modes, `save()`, `refresh()`, `update_from_json()`,
  detection of in-place changes to field values, and `merge_response()`.
- For Django REST Framework APIs: `write_map=` for a relation read nested and
  written by key, `write_only=` for a field such as a password, `read_only=`
  for returned values that writes omit, file uploads with `files=` on
  `create()` and `save()`, full updates with `save(full=True)`, request options
  such as headers with `request_options=` on `save()`, `create()`, `delete()`,
  and the query methods, which send them with every request of the query, and
  `write_options()` for the writes of an object, eager ones included, the
  collection's size as `count`, and `APIError` with the parsed error body.
  Relation queries send primary keys.
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
  still returns a `requests.Response`, and the hooks receive an `API`, or a
  view of one for a query with `request_options=`. Annotate a hook override's
  parameters as `eazyrest.Response` and `eazyrest.Client`, because an override
  annotated `requests.Response` or `API` fails type checking, and cast them to
  use anything else, such as `resp.links` or `api.session`. `bulk_get_by_pks()`
  returns a `Mapping` of objects of the model, so an override may return a
  `dict` of its own class. The query methods, `create()`, and the model
  constructor take a `Client` as `api=`. The names that `request_options=`
  accepts and the exceptions for a failed connection are those of the client's
  HTTP library, requests for `API`.
- `API.json(response)` decodes JSON in application hooks with the same depth,
  numeric, and response-shape policy as model reads.
- `ResponseShapeError.response` holds the response that the error refuses, as
  `APIError.response` and `RedirectError.response` do. It may be `None`, as for
  an error raised from decoded JSON in a hook such as `object_json()`, so test
  it before use.
- Documentation, with a user guide and a guide to Django REST Framework APIs.

### Security

- A redirect is checked against the URL exactly as requests will send it, so a
  `Location` such as `https://api.example.com\@evil.example/` cannot hide
  another host from the origin check.
- A conversion error leaves out input values and mapping keys that the input
  chose.
- An error that quotes a URL, `APIError`, `NotFound`, `RedirectError`,
  `PaginationError`, `ResponseShapeError`, and `LimitExceeded`, leaves out the
  user and password of the URL in its message, `args`, and `repr()`, as
  `http://host/` for `http://user:pass@host/`. The URL's query is still quoted,
  as the URL that eazyrest was asked to send, which is the one that the caller
  gave or a page links to. The error's `request` and `response` keep the real
  URL.
- No error quotes a part of a URL that a response supplied, such as the
  signature of a download URL. An `APIError` for a failed download, a
  `LimitExceeded`, a `ResponseShapeError`, and a `RedirectError` name the
  request by the URL that eazyrest was asked to send, also when a followed
  redirect or a response hook led to another URL. The error's `response` holds
  the URL that was reached. An exception that requests itself raises, such as
  `requests.ConnectionError`, quotes the URL of that host as requests does,
  with its path and query.
- A `RedirectError` that refuses a redirect to another origin names the
  request, says that the redirect leads to another origin, and quotes no part
  of the target, not its origin, user, password, path, query, or fragment. The
  error's `response` holds the header.
- A `PaginationError` that refuses a redirect from a next-page request to
  another origin names the request by its link, with its query, and quotes no
  part of the target. A target that requests cannot prepare raises the API's
  `RedirectError`, which quotes none of it, and not a `PaginationError` that
  quoted it.
- A `PaginationError` for a next-page link or a `Link` header link that
  requests cannot prepare or send quotes none of the link, and has no
  `__cause__` or `__context__`, since the error that rejected the link, such as
  one for a port that is not a number, can quote it. This holds also with
  `allow_other_origins=True`. A link to another origin, or to a page already
  read, is quoted by its origin alone. `Page.next_url` and `Page.previous_url`
  hold the whole link. A link that is not an HTTP or HTTPS URL is not quoted,
  since its text can read as a user and password.
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
- `APIError` messages do not quote the error body, which can quote submitted
  values, and show at most 40 characters of the response's reason phrase, which
  the server chooses, as they show of each name of the body's members.
- Releases are built and published from a tag by a workflow that uses trusted
  publishing, with the workflow's actions pinned by commit.
