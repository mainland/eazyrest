# Quickstart

## Installation

```bash
pip install eazyrest
```

To send requests through a SOCKS proxy, install the `socks` extra:

```bash
pip install "eazyrest[socks]"
```

Requires Python 3.10+.

## Example

The example below uses the public demo API at `https://jsonplaceholder.typicode.com/`. JSONPlaceholder accepts write requests for demonstration, but it does not persist those changes.

```python
from eazyrest import API, JSONObject, json_object


class JSONPlaceholderObject(JSONObject):
    """Base model for JSONPlaceholder resources."""


@json_object
class User(JSONPlaceholderObject):
    class_url = "/users/"

    id: int
    name: str


@json_object(field_map={"user": "userId"})
class Todo(JSONPlaceholderObject):
    class_url = "/todos/"

    id: int
    user: User
    title: str
    completed: bool


with API(
    "https://jsonplaceholder.typicode.com/",
    default_write_mode="lazy",
) as demo_api:
    JSONPlaceholderObject.register_api(demo_api)

    todo = Todo(id=1)
    print(todo.title)
    print(todo.user.name)

    todos = list(Todo.filter(userId=1))
    print(len(todos))

    first = todos[0]
    # JSONPlaceholder accepts this but does not persist it.
    first.completed = True
    first.save()
```

## Getting objects

- `Todo(id=1)` makes a lazy object without a request. The first read of a field other than the primary key sends a `GET` to the object's URL.
- `Todo.get(title="Write docs")` queries the collection and returns its one match. It raises `DoesNotExist` if nothing matches and `MultipleObjectsReturned` if more than one object does. It is a query, not a lookup by primary key.
- `todo.refresh()` discards the object's cached JSON and pending changes, so the next read loads it again.

The [guide](guide.md) describes loading, saving, and queries in full.
