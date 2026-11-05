# eazyrest

`eazyrest` is a small library for consuming REST APIs with less boilerplate. It combines a session-based API client with a lightweight, type-directed JSON object mapping layer that models REST resources as typed Python classes.

Typed fields are converted as objects are read and written, including nested items in collection annotations such as `list[datetime.datetime]`, `tuple[Enum, ...]`, and related-object collections.

## Installation

```bash
pip install eazyrest
```

To have Pydantic convert fields whose types use it, such as Pydantic models or `Annotated[int, pydantic.Field(gt=0)]`, install the `pydantic` extra:

```bash
pip install "eazyrest[pydantic]"
```

To send requests through a SOCKS proxy, install the `socks` extra:

```bash
pip install "eazyrest[socks]"
```

Requires Python 3.10+.

## Documentation

Sphinx documentation is configured under `docs/` using MyST Markdown and the `furo` theme.

Build the docs locally with:

```bash
.venv/bin/python -m tox -e docs
```

Read the Docs configuration is provided in `.readthedocs.yaml`.

## Example

The example uses the public REST demo API available at [https://jsonplaceholder.typicode.com/](https://jsonplaceholder.typicode.com/). JSONPlaceholder accepts write requests for demonstration, but it does not persist those changes.

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

    # Lazy writes are the default; JSONPlaceholder accepts this but does not persist it
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

Install the Git pre-commit hooks, which run ruff, mypy, and the other checks on every commit:

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

Build the documentation with warnings treated as errors:

```bash
.venv/bin/python -m tox -e docs
```

On every push and pull request, GitHub Actions runs the test suite on Python 3.10 through 3.15, at the minimum declared dependency versions, with the oldest supported Pydantic, and without Pydantic. It also runs the lint and documentation environments, checks that `uv.lock` is current, audits the locked dependencies for known vulnerabilities, and checks that the built wheel imports with and without each extra. The test environments install only the runtime dependencies and a test dependency group, so a runtime dependency missing from `pyproject.toml` fails the tests, and they report branch coverage. The lint environment includes `zizmor`, which audits the workflows.

To build and validate a distribution:

```bash
uv build
uvx twine check dist/eazyrest-*.tar.gz dist/eazyrest-*.whl
```

Start with no `build/` or `dist/` directory left from an earlier build. In
particular, a wheel-only build can copy removed modules from a stale `build/`
directory. Keep distributions you want to retain outside the checkout.

### Publishing

Releases are published by `.github/workflows/release.yml`, not from a workstation. Pushing a tag such as `v1.0.0` builds the distributions from the tag, checks that their version matches it, tests the wheel on the oldest and newest Python, and publishes the same files to TestPyPI and then, once a maintainer approves the `pypi` environment, to PyPI. Publishing uses PyPI's trusted publishing, so no API token is stored, and it uploads attestations of the files' origin. Before the first release, register the workflow as a trusted publisher on TestPyPI and PyPI, with the environments `testpypi` and `pypi`, and require approval for `pypi` in the repository's environment settings.

A file uploaded to PyPI can never be replaced, so tag only a clean, tested commit, and consider publishing a release candidate, such as `v1.0.0rc1`, first.

Use annotated release tags, for example `git tag -a v1.0.0rc1 -m "Release
1.0.0rc1"`. Tags have three numeric components, optionally followed by `aN`,
`bN`, `rcN`, or `.postN`, with no leading zeros. The workflow rejects local
and development versions before building. It uses the triggering tag's
validated version even if another tag names the same commit.
