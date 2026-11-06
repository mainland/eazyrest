# Django REST Framework

This guide covers how to use `eazyrest` against a Django REST Framework API when you care about lazy iteration, DRF's built-in pagination classes, and explicit related-object prefetch.

## Keep collection iteration page-shaped

`eazyrest` can keep object creation lazy, but it does not invent pagination on top of a single large API response. DRF's built-in pagination classes are `PageNumberPagination`, `LimitOffsetPagination`, and `CursorPagination`. For DRF apps, the best setup is:

- Paginate collection endpoints in DRF.
- Let each `eazyrest` request consume one page at a time.
- Iterate through pages lazily in model code if you need a full stream.

In practice, all three built-in DRF pagination classes can work with `eazyrest` because they expose `next`, `previous`, and `results` in the response body. `CursorPagination` is usually the best choice for large, append-heavy tables because it gives stable traversal without large offsets. `LimitOffsetPagination` is useful when clients need explicit window sizes and offsets. `PageNumberPagination` is useful for smaller or more user-facing collections where page numbers are part of the API contract.

## Unwrap DRF collections with `collection_items()`

Many DRF list endpoints return an envelope such as:

```json
{
  "next": "https://api.example.com/users/?cursor=abc",
  "previous": null,
  "results": [
    {"id": 1, "name": "Ada"}
  ]
}
```

If DRF pagination is not configured, list endpoints usually return a plain JSON list, which `JSONObject.collection_items()` already handles. If pagination is configured, each response holds one page, so return `follow_next_links()` from `collection_items()` to read every page:

```python
from eazyrest import JSONObject, follow_next_links, json_object


class DRFModel(JSONObject):
    @classmethod
    def collection_items(cls, payload, *, api):
        return follow_next_links(api, payload)


@json_object
class User(DRFModel):
    class_url = "/users/"

    id: int
    name: str
```

`follow_next_links()` handles unpaginated list responses as well as paginated responses from `PageNumberPagination`, `LimitOffsetPagination`, and `CursorPagination`. It requests the next page only when iteration reaches it. With `prefetch=`, iteration reads ahead by up to `prefetch_batch_size` objects, so a batch can span pages.

To show one page at a time, call `get_page()`, whose default `collection_page()` hook reads the same page shapes. The page's `count`, `next_url`, and `previous_url` come from DRF's `count`, `next`, and `previous` members, and `next_page()` and `previous_page()` follow them. `CursorPagination` reports no `count`.

With `PageNumberPagination`, a response usually looks like:

```json
{
  "count": 1234,
  "next": "https://api.example.com/users/?page=2",
  "previous": null,
  "results": [
    {"id": 1, "name": "Ada"}
  ]
}
```

With `LimitOffsetPagination`, a response usually looks like:

```json
{
  "count": 1234,
  "next": "https://api.example.com/users/?limit=100&offset=100",
  "previous": null,
  "results": [
    {"id": 1, "name": "Ada"}
  ]
}
```

With `CursorPagination`, a response usually looks like:

```json
{
  "next": "https://api.example.com/users/?cursor=cD0yMDI2LTA0LTAxKzEyJTNBMDAlM0EwMC4wMDAwMDAlMkIwMCUzQTAw",
  "previous": null,
  "results": [
    {"id": 1, "name": "Ada"}
  ]
}
```

`follow_next_links()` works the same way for all three pagination styles because DRF exposes the next page as a URL in each case. The only difference is how DRF encodes that URL.

## Add a base model for DRF conventions

It usually helps to centralize DRF-specific behavior on one base class. Start with pagination and response-shape handling there, then add serializer field conversions or API-specific helpers as needed:

```python
from eazyrest import API, JSONObject, follow_next_links


class DRFObject(JSONObject):
    @classmethod
    def collection_items(cls, payload, *, api):
        return follow_next_links(api, payload)


api = API("https://api.example.com/")
DRFObject.register_api(api)
```

Then put model-specific logic, such as field maps or bulk-prefetch support, on individual subclasses.

## Configure authentication and CSRF

`API` wraps a `requests.Session`, so DRF authentication usually belongs on the API object. For token or bearer authentication, set the session headers once:

```python
api = API("https://api.example.com/")
api.session.headers.update({"Authorization": "Bearer TOKEN"})
DRFObject.register_api(api)
```

For cookie-backed session authentication, assign cookies to the API session. If you make unsafe requests such as `POST`, `PATCH`, or `DELETE` against a CSRF-protected Django view, include the CSRF header expected by Django:

```python
api.session.headers.update({"X-CSRFToken": csrf_token})
```

Authentication, permission, and throttling failures should still flow through `API.raise_for_response()`, so keep DRF-specific error translation in the API layer.

## Use absolute URLs for hyperlinked APIs

DRF APIs sometimes expose full resource URLs, especially when using serializers such as `HyperlinkedModelSerializer`. `JSONObject.url` returns the model's resource URL, while `JSONObject.absolute_url` resolves that URL against the registered API base URL:

```python
todo = Todo(id=1)

print(todo.url)
# /todos/1/

print(todo.absolute_url)
# https://api.example.com/todos/1/
```

This uses the same URL resolution as `API.get()`, `API.post()`, and the other request helpers. If a model's `class_url` is already absolute, `absolute_url` leaves it unchanged.

`HyperlinkedModelSerializer` puts each object's own URL in a JSON field named `url`. That name is reserved for `JSONObject.url`, so map the field to another attribute name:

```python
@json_object(field_map={"resource_url": "url"})
class Todo(DRFObject):
    class_url = "/todos/"

    id: int
    resource_url: str
```

A `HyperlinkedRelatedField`, which `HyperlinkedModelSerializer` uses for every relation, represents a related object by its URL rather than its primary key. Annotate such a field with `Hyperlinked()`:

```python
from typing import Annotated

from eazyrest import Hyperlinked


@json_object
class Todo(DRFObject):
    class_url = "/todos/"

    id: int
    owner: Annotated[User, Hyperlinked()]
    watchers: list[Annotated[User, Hyperlinked()]]


todo.owner.name  # GET https://api.example.com/users/2/
todo.owner = User(id=3)
todo.save()  # PATCH {"owner": "https://api.example.com/users/3/"}
```

Reading the field gives a related object that loads from the URL when one of its other fields is read. Writing it sends the related object's absolute URL. A query sends its primary key, as django-filter's relation filters expect. Pass a related model instance or a bare primary key to the query. A relative URL is resolved against the related model's API. A URL whose origin is neither that API's nor one of its `redirect_origins` raises `FieldConversionError` when the field is read, so a server cannot send the client, with its credentials, to another host. A hyperlinked field can be prefetched when the related model implements `bulk_get_by_urls()`, which receives the URLs rather than primary keys, as [Prefetch hyperlinked relations](#prefetch-hyperlinked-relations) shows.

## Match DRF serializer fields

DRF serializer fields usually emit JSON-friendly primitives. Most of those can be represented directly with annotations, but a few common DRF fields benefit from explicit model conventions.

DRF `DateTimeField` emits ISO 8601 strings by default, and `eazyrest` decodes and encodes them without an override, including the `Z` suffix that DRF uses for UTC. A string without an offset, which DRF emits when Django's `USE_TZ` setting is `False`, decodes to a naive `datetime`.

DRF `DurationField` emits Django's duration format, such as `1 02:03:04`, unless it is configured to emit ISO 8601. `eazyrest` parses both formats into `datetime.timedelta` and encodes `timedelta` values as ISO 8601 durations, which DRF accepts as input. Override `to_json()` if your API requires a different wire format.

A serializer field with `write_only=True`, such as a password, appears in requests but never in responses. Declare it, and list it in `json_object(write_only=...)`, so that it can be assigned and saved like any other field without being kept or read back.

DRF `ChoiceField` maps well to `enum.Enum` annotations, and `MultipleChoiceField` maps well to collection annotations such as `set[MyEnum]` or `list[MyEnum]`. DRF `FileField` and `ImageField` return the file's URL, so declare them as `str` and list them in `json_object(read_only=...)` to omit their returned URLs from full updates. Upload a file with `files=` on `create()` or `save()`, which sends a multipart request that DRF's `MultiPartParser` reads.

## Map serializer names and relation shapes

Use `field_map` when a DRF serializer's JSON field name does not match the Python attribute name you want on the model:

```python
@json_object(field_map={"user": "user_id"})
class Todo(DRFObject):
    class_url = "/todos/"

    id: int
    user: User
    title: str
```

Related fields can be represented as primary keys, embedded objects, or hyperlinks depending on the serializer. Primary keys and embedded objects work naturally with `JSONObject` annotations, and hyperlinks with [`Hyperlinked()`](#use-absolute-urls-for-hyperlinked-apis).

A common serializer reads a relation nested and writes it as a primary key under another name:

```python
class TodoSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    user_id = serializers.PrimaryKeyRelatedField(
        source="user", queryset=User.objects.all(), write_only=True
    )
```

Use `write_map` to send such a field under its write key. Reads, queries, and the object's JSON keep using `user`, and `create()`, `save()`, and eager assignments send the related object's primary key as `user_id`. The response's nested object then replaces the assigned one:

```python
@json_object(write_map={"user": "user_id"})
class Todo(DRFObject):
    class_url = "/todos/"

    id: int
    user: User
    title: str


todo.user = User(id=3)
todo.save()  # PATCH {"user_id": 3}
```

For read-only serializer fields that you still want to read, annotate them normally. For server-computed values that should not be JSON-backed on the client, use `exclude=`. For write-only serializer fields such as passwords, declare them and list them in `json_object(write_only=...)`.

## Pass through filtering, search, and ordering

DRF filtering backends are query-parameter based, so `eazyrest` can pass them through with `filter()` keyword arguments. This works for django-filter fields, `SearchFilter`, and `OrderingFilter`:

```python
active_users = User.filter(is_active=True)
matching_users = User.filter(search="ada")
ordered_users = User.filter(ordering="-created")
```

For query parameters that are not valid Python identifiers, pass a dictionary with `**`:

```python
users = User.filter(**{"created__gte": "2026-01-01T00:00:00Z"})
```

On the DRF side, keep sensitive filtering and ordering fields explicit. For example, prefer explicit `filterset_fields`, `search_fields`, and `ordering_fields` so clients cannot accidentally query or order by fields that should not be exposed.

## Reject unknown filter parameters

django-filter passes every query parameter to a Django form whose fields are the view's filters, and a form ignores data that it has no field for. A parameter that names no filter, such as a misspelled field or a field missing from `filterset_fields`, therefore filters nothing. `filter()` then returns more objects than it should. `get()` and `get_or_create()` rely on the server to apply every lookup parameter, so `get()` can return an object that does not match, and `get_or_create()` can return one with `created=False` instead of creating it. django-filter has no setting that rejects unknown parameters. Its strict mode, which django-filter 2.0 removed, applied to invalid values, not unknown names.

This filter backend answers a request that has an unknown parameter with a 400 response:

```python
from django import forms
from django_filters.rest_framework import DjangoFilterBackend
from django_filters.widgets import QueryArrayWidget, SuffixedMultiWidget
from rest_framework.exceptions import ValidationError
from rest_framework.settings import api_settings


class StrictDjangoFilterBackend(DjangoFilterBackend):
    """A DjangoFilterBackend that rejects query parameters nothing reads."""

    def filter_queryset(self, request, queryset, view):
        filterset = self.get_filterset(request, queryset, view)
        known = self.get_known_parameters(view, filterset)
        unknown = sorted(set(request.query_params) - known)
        if unknown:
            raise ValidationError(
                {name: ["Unknown query parameter."] for name in unknown}
            )

        return super().filter_queryset(request, queryset, view)

    def get_known_parameters(self, view, filterset):
        known = set(getattr(view, "extra_query_params", ()))
        if api_settings.URL_FORMAT_OVERRIDE:
            known.add(api_settings.URL_FORMAT_OVERRIDE)

        # The paginator and the other filter backends name their parameters
        # in attributes such as page_query_param, search_param, and
        # ordering_param.
        components = [getattr(view, "paginator", None), *view.filter_backends]
        for component in components:
            for attr in dir(component):
                value = getattr(component, attr, None)
                if attr.endswith("_param") and isinstance(value, str):
                    known.add(value)

        if filterset is not None:
            form = filterset.form
            for name, field in form.fields.items():
                key = form.add_prefix(name)
                widget = field.widget
                # Range and lookup filters read suffixed parameters, such as
                # price_min and price_max.
                if isinstance(widget, SuffixedMultiWidget):
                    known.update(
                        widget.suffixed(key, suffix)
                        for suffix in widget.suffixes
                    )
                elif isinstance(widget, forms.MultiWidget):
                    known.update(
                        key + suffix for suffix in widget.widgets_names
                    )
                elif isinstance(widget, QueryArrayWidget):
                    known.update((key, f"{key}[]"))
                else:
                    known.add(key)

        return known
```

It accepts the parameters that the view's filters read, including suffixed ones such as `price_min` and `price_max` for a `RangeFilter` and `created_after` for a `DateFromToRangeFilter`. It also accepts the parameters of the view's paginator and other filter backends, such as `page`, `cursor`, `search`, and `ordering`, and the `format` parameter. Use it in place of `DjangoFilterBackend`, in a view's `filter_backends` or in the settings:

```python
REST_FRAMEWORK = {
    "DEFAULT_FILTER_BACKENDS": [
        "myapp.filters.StrictDjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
}
```

List a parameter that the view reads itself, such as `expand`, or that a custom widget reads under another name, in the view's `extra_query_params` attribute. The backend was tested with Django 5.2 and 6.1, DRF 3.15 and 3.18, and django-filter 24.3 and 26.2, with page-number, limit-offset, and cursor pagination.

## Match DRF write behavior

Lazy field assignment and `save()` use `PATCH`, which matches DRF viewsets that support partial updates. `create()` uses `POST` against `class_url`, and `delete()` uses `DELETE` against the object URL.

If a DRF endpoint does not allow partial updates, call `save(full=True)`, which sends a `PUT` holding every writable declared field present after an unloaded or partial object is loaded, encoded as an assignment would encode it. If your serializer has fields that are required on create but read-only afterward, pass them to `create()` and let normal field assignment handle later PATCHable fields.

## Unwrap DRF error responses

DRF error payloads are often structured JSON such as:

```json
{"detail": "Not found."}
```

or validation errors such as:

```json
{"email": ["This field must be unique."]}
```

A failed request raises `APIError`, or `NotFound` for a 404. Both are `requests.HTTPError`, and `data` holds the parsed body, so a client can report each field's errors:

```python
from eazyrest import APIError

try:
    user.save()
except APIError as err:
    if isinstance(err.data, dict):
        for field, messages in err.data.items():
            print(field, messages)
```

The message names the status and the members of the body, as in `400 Client Error: Bad Request for PATCH https://api.example.com/users/1/ (body names email)`, but not their text, because DRF's messages can quote the submitted values, such as `"'abc' is not a valid choice."`. `NotFound` is also `DoesNotExist`, so `except DoesNotExist` catches an object that was deleted on the server when it is lazily loaded, as it catches a lookup that matched nothing.

For exception types of your own, override `API.raise_for_response()` and translate DRF's error payload:

```python
import requests

from eazyrest import API, ResponseShapeError


class DRFAPIError(requests.HTTPError):
    pass


class DRFAPI(API):
    def raise_for_response(self, resp):
        if resp.ok:
            return

        try:
            payload = self.json(resp)
        except ResponseShapeError:
            resp.raise_for_status()

        if isinstance(payload, dict):
            if "detail" in payload:
                raise DRFAPIError(payload["detail"], response=resp)
            raise DRFAPIError(str(payload), response=resp)

        resp.raise_for_status()
```

That keeps DRF-specific error unwrapping in the API layer instead of spreading it across your models.

## Use explicit bulk endpoints for `prefetch`

`eazyrest` prefetch is intentionally explicit. A related model must implement `bulk_get_by_pks()`, and that should map to a real DRF endpoint shape rather than guessing.

For example, if your DRF viewset supports repeated query parameters:

```python
@json_object
class User(DRFObject):
    class_url = "/users/"

    id: int
    name: str

    @classmethod
    def bulk_get_by_pks(cls, pks, *, api):
        users = list(cls.filter(id=sorted(pks), api=api))
        return {user.pk: user for user in users}
```

Pass `api` on to `filter()`. Prefetch passes the query's `api=` here, or else this class's registered API, so that the related objects load from the same server, such as the same tenant's. They keep the query's API.

That works well when the DRF endpoint accepts requests like:

```text
/users/?id=2&id=3
```

If your API uses a different shape, such as `id__in=2,3` or a custom bulk action, implement that shape directly in `bulk_get_by_pks()`.

On the DRF server side, the simplest pattern is to expose a normal list endpoint that accepts repeated query parameters and filters by primary key:

```python
from rest_framework import viewsets


class UserViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = UserSerializer
    queryset = User.objects.all()

    def get_queryset(self):
        queryset = super().get_queryset()
        ids = self.request.query_params.getlist("id")
        if ids:
            queryset = queryset.filter(pk__in=ids)
        return queryset
```

That gives the client a bulk lookup shape like:

```text
/users/?id=2&id=3&id=5
```

If you need tighter control, you can also expose a dedicated DRF action for bulk fetches and point `bulk_get_by_pks()` at that endpoint. For example:

```python
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework import viewsets


class UserViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = UserSerializer
    queryset = User.objects.all()

    @action(detail=False, methods=["get"], url_path="bulk")
    def bulk(self, request):
        ids = request.query_params.getlist("id")
        queryset = self.get_queryset().filter(pk__in=ids)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)
```

Then your client model can implement:

```python
@classmethod
def bulk_get_by_pks(cls, pks, *, api):
    resp = api.get("/users/bulk/", params={"id": sorted(pks)})
    return {
        user.pk: user
        for user in (cls(json=item, api=api) for item in api.json(resp))
    }
```

Server-side, continue using Django ORM `select_related()` and `prefetch_related()` inside the bulk endpoint when it helps serializers avoid database N+1 queries.

## Prefetch hyperlinked relations

A field annotated with `Hyperlinked()` holds URLs, so prefetching it calls `bulk_get_by_urls()` on the related model instead of `bulk_get_by_pks()`. With DRF's default `lookup_field`, each URL ends with the primary key, so the hook can take the keys from the URLs and reuse the bulk lookup by key:

```python
@classmethod
def bulk_get_by_urls(cls, urls, *, api):
    prefix = api.resolve_url(cls.class_url)
    pks = {
        url.removeprefix(prefix).strip("/")
        for url in urls
        if url.startswith(prefix)
    }
    objects = cls.bulk_get_by_pks(pks, api=api)
    return {obj.absolute_url: obj for obj in objects.values()}
```

Return the objects keyed by URL. eazyrest matches the keys after resolving them against the API's base URL and normalizing their case and default ports. A URL outside the model's `class_url`, such as one of a serializer with another `lookup_field`, is not loaded here, and the object loads lazily when it is read.

## Preserve laziness with batch prefetch

When you call:

```python
todos = Todo.filter(prefetch="user")
```

`eazyrest` can still yield parent objects lazily in batches. The related objects are bulk-loaded for the current batch, and relation access falls back to normal lazy loading when a related object was not prefetched.

This works best when:

- The parent DRF endpoint is paginated.
- The related model has a real bulk lookup path.
- The related field is represented as IDs or embedded objects.

## Choose a DRF pagination class

For large DRF APIs, `CursorPagination` is usually the cleanest built-in choice:

- Parent collections stay naturally page-oriented.
- Page traversal stays stable as rows are inserted.
- `eazyrest` can prefetch related objects for each batch of parent objects.

`LimitOffsetPagination` is a good fit when clients need explicit slices, such as `?limit=100&offset=300`, and the result set is not so large that high offsets become expensive. `PageNumberPagination` is a good fit when the API is intentionally page-oriented, such as `?page=4`, and clients benefit from stable page-number links.

With `PageNumberPagination` and `LimitOffsetPagination`, the iterator that `filter()` returns has the collection's size as `count`, from the first page. `CursorPagination` does not report a size, so `count` is `None`.

Follow `next` links in `collection_items()`, as `follow_next_links()` does, rather than in related-field conversion. Related fields should describe one object's payload. Page traversal should describe how to walk a collection endpoint.

## Recommended DRF server-side setup

For the best experience with `eazyrest`, a DRF app should usually provide:

- Paginated list endpoints.
- Stable primary keys in relation fields.
- A documented bulk lookup shape for related resources.
- Embedded objects only when you intentionally want payload expansion.
- Next-page links with the public scheme and host.

DRF builds next-page links from the request that Django receives. Behind a reverse proxy, that request names the proxy's upstream address and scheme unless Django is told to trust the proxy's headers. Set `USE_X_FORWARDED_HOST = True` if the proxy sends the public host in `X-Forwarded-Host` rather than `Host`, and set `SECURE_PROXY_SSL_HEADER` if TLS ends at the proxy. Otherwise a link such as `http://127.0.0.1:8000/users/?page=2` reaches the client, and `follow_next_links()` raises `ValueError` instead of following it.

On the Django side, continue using ORM-level `select_related()` and `prefetch_related()` inside DRF views and serializers. That solves database N+1 problems inside the server. `eazyrest` prefetch solves HTTP-level N+1 problems in the client. They complement each other and do not replace each other.

## Suggested pattern

Use this split:

- DRF: Optimize database access with queryset prefetching and paginate list endpoints.
- `eazyrest`: Model typed resources, keep collection iteration lazy, and use explicit `bulk_get_by_pks()` for client-side relation prefetch.

That gives you a clean separation:

- Django/DRF controls query efficiency and pagination on the server.
- `eazyrest` controls typed object conversion and HTTP-side relation loading on the client.
