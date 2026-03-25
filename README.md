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

Create the development environment in `.venv` with [uv](https://docs.astral.sh/uv/). It installs the project in editable mode with the `dev` dependency group, at the versions that `uv.lock` pins:

```bash
uv sync
```

Without uv, create the environment with pip 25.1 or later, which does not read the lock:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -e . --group dev
```

Install the Git pre-commit hooks, which run ruff, docformatter, mypy, and the other checks on every commit:

```bash
.venv/bin/python -m pre_commit install
```

You can run the hooks manually across the whole tree with:

```bash
.venv/bin/python -m pre_commit run --all-files
```

Run the test suite with:

```bash
.venv/bin/python -m pytest
```

Run the multi-environment test matrix locally with:

```bash
.venv/bin/python -m tox
```

Run the pre-commit hooks across the whole tree, as CI does, with:

```bash
.venv/bin/python -m tox -e lint
```

On every push and pull request, GitHub Actions runs the test suite on Python 3.10, 3.11, and 3.12, runs the pre-commit hooks, checks that `uv.lock` is current, and audits the locked dependencies for known vulnerabilities.

To build and validate a distribution:

```bash
uv build
uvx twine check dist/eazyrest-*
```

### Publishing

To publish to PyPI:

```bash
uvx twine upload dist/*
```

To publish to Test PyPI:

```bash
uvx twine upload --repository testpypi dist/*
```
