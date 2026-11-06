# Changelog

## 1.0.0 (unreleased)

eazyrest 1.0 is the first stable release. From 1.0 on, the names that the `eazyrest` package exports, the `eazyrest.pydantic` types, and the behavior that the guide documents follow semantic versioning. A breaking change waits for 2.0.

The tags `v1.0.0a1` through `v1.0.0a4` name commits from an earlier history that was later rewritten. They are not ancestors of 1.0.0.

### Migrating from 0.9.0

Much of the library changed between 0.9.0 and 1.0. Each item below is a change that can break code written for 0.9.0, followed by what to do about it.

#### Installation

- eazyrest no longer depends on `certifi`, `dateparser`, `pytimeparse2`, or `tzlocal`, and it depends on `requests` without its `socks` extra. Install `eazyrest[socks]` to send requests through a SOCKS proxy. Without it, the first request through one raises `requests.exceptions.InvalidSchema`.
- Every dependency now has a minimum version: requests 2.33.0, urllib3 2.8.0, isodate 0.7.2, python-dateutil 2.9.0.post0, and, before Python 3.11, typing_extensions 4.14.1. The new `pydantic` extra needs Pydantic 2.12, or 2.14.1 on Python 3.15, and below 3.
- Python 3.10 through 3.15 are supported.

#### Writes

- Assigning a field no longer sends a `PATCH` at once. By default, an object is in lazy write mode, and assignments wait for `save()`, which sends them in one `PATCH`. Call `save()` after changing an object, or construct the API with `API(url, default_write_mode="eager")`, or call `obj.set_write_mode("eager")`, to send each assignment as it is made.
- A write response is merged into the object through the new `merge_response()` hook, rather than replacing its JSON. A response without a body, such as `204 No Content`, keeps the values that were sent. `create()` keeps only the JSON-backed fields it sent, so a write-only key such as a password is not kept.
- A redirect that would change a write's method or drop its body, such as a 301 in answer to a `PATCH` to a URL without its trailing slash, raises `RedirectError` before it is followed, and a lazy update stays pending. Use the URL that the server expects, for example by setting `API.trailing_slash`.
- `delete()` takes request options in `request_options=`, as `save()` and `create()` do, rather than as positional and keyword arguments for `API.delete()`. Replace `obj.delete(headers=h)` with `obj.delete(request_options={"headers": h})`.

#### Queries

- `filter()` and `all()` return iterators rather than lists, and the request is sent when they are called. Call `list()` on the result to iterate it more than once. The iterator is a `Results`, whose `count` is the collection's size when the response reports it.
- A collection request answered with a redirect that is not followed, such as a `304 Not Modified`, raises `RedirectError` rather than reading its body as the collection.
- `filter(url=...)` is removed. Override `class_url`, or call the API directly.
- Query parameters named after JSON-backed fields are encoded as `create()` encodes them: renamed by `field_map`, converted to JSON, and with booleans sent as `true` and `false`.
- A query parameter whose value requests would drop or change raises `ValueError` before the request: `None`, an empty collection or iterator, a mapping, or a collection holding `None` or another collection. In 0.9.0, such a constraint was silently omitted, so `get()` could return an object that does not match it.
- The keyword arguments `api`, `prefetch`, `defaults`, and `params` of the query methods are their own options, and they reserve `request_options`. Pass a query parameter with one of these names in `params=`. `create()` likewise reserves `api`, `fields`, `files`, and `request_options`, and takes field values with those names in `fields=`.
- In `get_or_create()`, a key that contains `__` and is not a field, such as `name__iexact`, is sent only in the lookup, not in the creation payload. Every keyword argument is sent in the lookup's query string, so pass a value that only the creation needs, such as a password, in `defaults`.
- `DoesNotExist` and `MultipleObjectsReturned` derive from `EazyrestError` rather than directly from `Exception`, and their messages name the model and the lookup parameters, but not their values.
- A failed request raises `APIError`, or `NotFound` for a 404, instead of a plain `requests.HTTPError`. Both are `requests.HTTPError`, so existing handlers still catch them, but the message differs: it names the method, URL, status, and the members of a JSON body, and the parsed body is in `data`. `NotFound` is also a `DoesNotExist`, so a 404 on a lazy load is caught by `except DoesNotExist`.

#### Fields and conversion

- Fields convert their values by annotation. `datetime`, `date`, `timedelta`, `UUID`, enum, optional, collection, and related-object fields read and write their JSON forms, and so do their items, as in `list[datetime.datetime]`.
- A `datetime` field writes ISO 8601 rather than a timestamp, and reads ISO 8601 or a Unix timestamp. `parse_datetime()` no longer uses `dateparser` or the local time zone, so it reads only what a field reads. Override `to_json()` to write timestamps, as the guide shows. A `datetime` whose UTC offset is not a whole number of minutes raises `FieldConversionError` rather than writing an offset that ISO 8601 lacks.
- A `timedelta` field writes an ISO 8601 duration. `parse_duration()` rejects years and months, whose length varies, no longer reads the `:SS` and `D:HH:MM:SS` forms, and reads microseconds. A value out of range raises `ValueError` rather than `OverflowError`.
- A value that a field cannot convert raises `FieldConversionError`, which is both a `TypeError` and a `ValueError`. Its message names the model and field but not the value.
- Assigning a non-collection to a collection field, or a `datetime` to a `date` field, raises.
- `to_json()` is a classmethod. `create_related()` is private.

#### Declarations

- `json_object()` takes its options as keyword arguments only. It raises `ValueError` for a field that would replace a `JSONObject` attribute, for a `pk=`, `field_map=`, or `exclude=` name that is not a field, and for two fields with the same JSON key. A class with decorated ancestors that are not subclasses of one another raises `TypeError`.
- A decorated subclass keeps the primary key that it inherits.
- Redeclaring a field also preserves its read and write keys and, when those options are omitted, its write-only and read-only policies. Explicit mappings or policy sets override those defaults.

#### URLs and HTTP

- `url` percent-encodes the primary key as one path segment, and an object whose key is missing, empty, `.`, or `..` has no URL.
- The second positional argument of `API()` is `proxy`, not `pool_connections`. Pass connection-pool settings, and other `requests.adapters.HTTPAdapter` arguments such as `max_retries=`, as keyword arguments.
- `verify=` takes a CA bundle path as well as a boolean, and a path or `False` takes precedence over `REQUESTS_CA_BUNDLE`. `verify=False` no longer suppresses urllib3's `InsecureRequestWarning` for the whole process. Filter the warning yourself if you accept unverified connections.
- A redirect is followed only to the origin of the request it answers, or to an origin in `API.redirect_origins`, so that session headers such as API keys are not sent elsewhere.
- Assigning `API.cookies` replaces the session's cookies rather than merging into them. Call `api.cookies.update(jar)` to add cookies.
- Each API limits the size, time, and JSON depth of a response, and the pages and items of a traversal, and raises `LimitExceeded` at a limit. See the guide's Limits section to change or remove them.

#### Modules

- The module `eazyrest.eazyrest` is now the private `eazyrest._models`. Import every name from `eazyrest`, as its `__all__` lists them.

### New features

- Lazy loading of objects and related objects, and explicit bulk prefetch through `prefetch=` and `bulk_get_by_pks()`.
- Pagination helpers `follow_next_links()`, for Django REST Framework's page shapes, and `follow_link_headers()`, for RFC 8288 `Link` headers, with the `collection_items_from_response()` hook. The collection hooks receive the query's client as `api`, so a hook reads later pages with the API that the query used.
- Pydantic conversion of fields whose types use Pydantic, through the `pydantic` extra, and the `eazyrest.pydantic` types `Datetime`, `Date`, `Duration`, and `UUID`. A type that would drop the keys it does not declare is rejected, and a field must read back what it writes.
- Named type aliases, including `typing_extensions.TypeAliasType` and the `type` statement.
- Eager and lazy write modes, `save()`, `refresh()`, `update_from_json()`, detection of in-place changes to field values, and `merge_response()`.
- For Django REST Framework APIs: `write_map=` for a relation read nested and written by key, `write_only=` for a field such as a password, `read_only=` for returned values that writes omit, `Hyperlinked()` for relations represented by URLs, file uploads with `files=` on `create()` and `save()`, full updates with `save(full=True)`, request options such as headers with `request_options=` on `save()`, `create()`, and `delete()`, the collection's size as `count`, and `APIError` with the parsed error body. Hyperlinked writes send URLs and relation queries send primary keys.
- `API.put()`, use of `API` as a context manager, `raise_for_response()`, `resolve_url()`, `trailing_slash`, `redirect_origins`, `reset_session()` that keeps the session's configuration, `register_api()`, and `api=` on queries and `create()`.
- `absolute_url`, and a `repr()` that shows the class and primary key without loading the object.
- The exceptions `EazyrestError`, `APIError`, `NotFound`, `FieldConversionError`, `PrefetchNotSupported`, `RedirectError`, `PaginationError`, `ResponseShapeError`, `UsageError`, and `LimitExceeded`. Each one that replaces another exception type also derives from that type.
- `eazyrest.__version__`.
- `API.json(response)` decodes JSON in application hooks with the same depth, numeric, and response-shape policy as model reads.
- Documentation, with a user guide and a guide to Django REST Framework APIs.

### Security

- A redirect is checked against the URL exactly as requests will send it, so a `Location` such as `https://api.example.com\@evil.example/` cannot hide another host from the origin check.
- Pydantic error messages leave out input values, mapping keys that the input chose, and discriminated-union tags.
- Response limits also cover intermediate retry bodies and retry delays with the adapters that `API` creates. Model writes reject response streaming and unfollowed final 3xx statuses. A full save loads partial state first, and multipart writes reject empty lists rather than silently omitting them.
- Invalid JSON numbers raise `ResponseShapeError`, malformed error bodies give `APIError.data=None`, and JSON depth violations raise `LimitExceeded` for success and error responses alike. Bounded body reads preserve Requests' transport exceptions.
- A hyperlinked relation is followed only to an origin that the related model's API allows, and `APIError` messages do not quote the error body, which can quote submitted values.
- Releases are built and published from a tag by a workflow that uses trusted publishing, with the workflow's actions pinned by commit.
