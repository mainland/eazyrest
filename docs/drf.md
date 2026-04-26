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

If DRF pagination is not configured, list endpoints usually return a plain JSON list and `JSONObject.collection_items()` already handles that shape. If pagination is configured, your model can unwrap `results` directly:

```python
from eazyrest import JSONObject, json_object


class DRFModel(JSONObject):
    @classmethod
    def collection_items(cls, payload):
        return payload["results"]


@json_object
class User(DRFModel):
    class_url = "/users/"

    id: int
    name: str
```

That keeps `filter()` aligned with normal DRF list responses.

For a shared DRF base model, one `collection_items()` implementation can handle both unpaginated list responses and paginated responses from `PageNumberPagination`, `LimitOffsetPagination`, and `CursorPagination`:

```python
class DRFPaginatedObject(JSONObject):
    @classmethod
    def collection_items(cls, payload):
        if isinstance(payload, list):
            yield from payload
            return

        while True:
            yield from payload["results"]
            next_url = payload["next"]
            if next_url is None:
                return
            payload = cls.api.get(next_url).json()
```

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

The iterator logic is the same for all three pagination styles because DRF exposes the next page as a URL in each case. The only difference is how DRF encodes that URL.

## Add a base model for DRF conventions

It usually helps to centralize DRF-specific behavior on one base class. Start with pagination and response-shape handling there, then add serializer field conversions or API-specific helpers as needed:

```python
from eazyrest import API, JSONObject


class DRFObject(JSONObject):
    @classmethod
    def collection_items(cls, payload):
        if isinstance(payload, list):
            yield from payload
            return

        while True:
            yield from payload["results"]
            next_url = payload["next"]
            if next_url is None:
                return
            payload = cls.api.get(next_url).json()


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

## Match DRF serializer fields

DRF serializer fields usually emit JSON-friendly primitives. Most of those can be represented directly with annotations, but a few common DRF fields benefit from explicit model conventions.

DRF `DateTimeField` defaults to ISO 8601 strings. `eazyrest`'s built-in `datetime.datetime` conversion expects Unix timestamps, so add this override to your shared DRF base model if your serializers use the default ISO representation:

```python
import datetime
from typing import Any

from eazyrest import AnalyzedType, BaseType, JSONObject, parse_datetime


class DRFObject(JSONObject):
    def from_json(
        self,
        value: Any,
        conversion: AnalyzedType,
        *,
        field: str | None = None,
    ) -> Any:
        match conversion:
            case BaseType(datetime.datetime) if value is not None:
                return parse_datetime(value)

        return super().from_json(value, conversion, field=field)

    @classmethod
    def to_json(cls, value: Any, conversion: AnalyzedType) -> Any:
        match conversion:
            case BaseType(datetime.datetime) if value is not None:
                return value.isoformat()

        return super().to_json(value, conversion)
```

This also applies inside typed collections such as `list[datetime.datetime]`, because collection conversion recurses into each item.

DRF `DurationField` can emit either ISO 8601 durations or Django-style duration strings depending on serializer settings. `eazyrest` can parse common duration strings into `datetime.timedelta`, but you should override `to_json()` too if your API requires one exact wire format.

DRF `ChoiceField` maps well to `enum.Enum` annotations, and `MultipleChoiceField` maps well to collection annotations such as `set[MyEnum]` or `list[MyEnum]`. DRF `FileField` and `ImageField` are usually easiest to model as `str` URL fields unless you want a custom wrapper type.

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

Related fields can be represented as primary keys, embedded objects, or hyperlinks depending on the serializer. Primary keys and embedded objects work naturally with `JSONObject` annotations. Hyperlinked relationship fields are usually best modeled as `str` fields unless the API also exposes a stable primary key that `eazyrest` can use to build the related object URL.

For read-only serializer fields that you still want to read, annotate them normally. For server-computed values that should not be JSON-backed on the client, use `exclude=`. For write-only serializer fields such as passwords, either pass them directly to `create()` as extra JSON keys or keep them off the model class entirely.

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

## Match DRF write behavior

Lazy field assignment and `save()` use `PATCH`, which matches DRF viewsets that support partial updates. `create()` uses `POST` against `class_url`, and `delete()` uses `DELETE` against the object URL.

If a DRF endpoint does not allow partial updates, avoid field assignment plus `save()` for that model and call the API directly with the shape that endpoint expects. If your serializer has fields that are required on create but read-only afterward, pass them to `create()` and let normal field assignment handle later PATCHable fields.

## Unwrap DRF error responses

DRF error payloads are often structured JSON such as:

```json
{"detail": "Not found."}
```

or validation errors such as:

```json
{"email": ["This field must be unique."]}
```

If you want cleaner exceptions on the client side, override `API.raise_for_response()` and translate DRF's error payload into your own exception type:

```python
import requests

from eazyrest import API


class DRFAPIError(requests.HTTPError):
    pass


class DRFAPI(API):
    def raise_for_response(self, resp):
        if resp.ok:
            return

        try:
            payload = resp.json()
        except ValueError:
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
    def bulk_get_by_pks(cls, pks, *, api=None):
        users = list(cls.filter(id=sorted(pks)))
        return {user.pk: user for user in users}
```

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
def bulk_get_by_pks(cls, pks, *, api=None):
    api = cls.api if api is None else api
    resp = api.get("/users/bulk/", params={"id": sorted(pks)})
    return {
        user.pk: user
        for user in (cls(json=item, api=api) for item in resp.json())
    }
```

Server-side, continue using Django ORM `select_related()` and `prefetch_related()` inside the bulk endpoint when it helps serializers avoid database N+1 queries.

## Preserve laziness with batch prefetch

When you call:

```python
todos = Todo.filter(prefetch=["user"])
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
- `eazyrest` can layer batch prefetch on top of each page.

`LimitOffsetPagination` is a good fit when clients need explicit slices, such as `?limit=100&offset=300`, and the result set is not so large that high offsets become expensive. `PageNumberPagination` is a good fit when the API is intentionally page-oriented, such as `?page=4`, and clients benefit from stable page-number links.

If you need to follow `next` links across pages, keep that logic at the model or API-wrapper level rather than pushing it into related-field conversion. Related fields should describe one object's payload. Page traversal should describe how to walk a collection endpoint.

## Recommended DRF server-side setup

For the best experience with `eazyrest`, a DRF app should usually provide:

- Paginated list endpoints.
- Stable primary keys in relation fields.
- A documented bulk lookup shape for related resources.
- Embedded objects only when you intentionally want payload expansion.

On the Django side, continue using ORM-level `select_related()` and `prefetch_related()` inside DRF views and serializers. That solves database N+1 problems inside the server. `eazyrest` prefetch solves HTTP-level N+1 problems in the client. They complement each other; they do not replace each other.

## Suggested pattern

Use this split:

- DRF: Optimize database access with queryset prefetching and paginate list endpoints.
- `eazyrest`: Model typed resources, keep collection iteration lazy, and use explicit `bulk_get_by_pks()` for client-side relation prefetch.

That gives you a clean separation:

- Django/DRF controls query efficiency and pagination on the server.
- `eazyrest` controls typed object conversion and HTTP-side relation loading on the client.
