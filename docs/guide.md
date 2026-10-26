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

## Collections

`filter()` and `all()` return iterables of model instances:

```python
users = list(User.all())
admins = list(User.filter(role="admin"))
```

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
