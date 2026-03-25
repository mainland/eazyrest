# eazyrest

`eazyrest` is a small library for consuming REST APIs with less boilerplate. It combines a session-based API client with a lightweight, type-directed JSON object mapping layer that models REST resources as typed Python classes.

## Installation

```bash
pip install eazyrest
```

Requires Python 3.10+.

## Example

The example uses the public REST demo API available at [https://jsonplaceholder.typicode.com/](https://jsonplaceholder.typicode.com/).

```python
from eazyrest import API, JSONObject, json_object


with API("https://jsonplaceholder.typicode.com/") as demo_api:
    class JSONPlaceholderObject(JSONObject):
        """A JSON object from the JSONPlaceholder API."""


    # Register the API instance for this model hierarchy
    JSONPlaceholderObject.register_api(demo_api)

    @json_object
    class User(JSONPlaceholderObject):
        """Represents one user object from JSONPlaceholder."""

        class_url = "/users/"

        id: int
        name: str
        username: str
        email: str
        address: dict
        phone: str
        website: str
        company: dict

        def __str__(self) -> str:
            return self.name

    @json_object(field_map={"user": "userId"})
    class Todo(JSONPlaceholderObject):
        """Represents one todo object from JSONPlaceholder."""

        class_url = "/todos/"

        id: int
        user: User
        title: str
        completed: bool

    # Load one object by primary key (GET /todos/1/)
    todo = Todo(id=1)
    print(todo.title, todo.completed)

    # Demonstrate loading of a related object
    print(todo.user)

    # Query a collection (GET /todos/?userId=1)
    todos_for_user_1 = list(Todo.filter(userId=1))
    print(len(todos_for_user_1))

    # Access typed fields
    first = todos_for_user_1[0]
    print(first.id, first.user.id, first.user, first.title)

    # Lazy writes are the default; call save() to persist batched changes
    first.completed = True
    first.save()
```

## License

MIT. See `LICENSE`.

## Development

Set up a local virtual environment and install the project with development dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
```

Install the Git pre-commit hook so `ruff check`, `docformatter --check`, and `mypy` run automatically on every commit:

```bash
pre-commit install
```

You can run the hooks manually across the whole tree with:

```bash
pre-commit run --all-files
```

Run the test suite with:

```bash
pytest
```

GitHub Actions runs `ruff`, `mypy`, and `pytest` for Python 3.10, 3.11, and 3.12 on every push and pull request.

To build and validate a distribution:

```bash
python -m pip install -U build twine
python -m build
python -m twine check dist/*
```

### Publishing

To publish to PyPI:

```bash
python -m twine upload dist/*
```

To publish to Test PyPI:

```bash
python -m twine upload --repository testpypi dist/*
```
