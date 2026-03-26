# Quickstart

## Installation

```bash
pip install eazyrest
```

Requires Python 3.10+.

## Example

The example below uses the public demo API at `https://jsonplaceholder.typicode.com/`.

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
    first.completed = True
    first.save()
```
