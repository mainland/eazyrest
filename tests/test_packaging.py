"""Tests for how the package is installed and imported."""

from __future__ import annotations

import ast
import importlib.metadata
import os
import sys
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

import eazyrest

SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.mark.skipif(
    "TOX_ENV_NAME" not in os.environ,
    reason="Only tox installs the built package rather than the source tree",
)
def test_tox_imports_the_installed_package() -> None:
    """Under tox, the tests exercise the package that tox built."""
    module = Path(eazyrest.__file__).resolve()

    assert SRC not in module.parents


# A module that the package imports without declaring it, because a declared
# requirement brings it in and fixes its version: Pydantic depends on an exact
# release of pydantic-core.
_PROVIDED_BY = {"pydantic-core": "pydantic"}

# Extras whose modules are not part of the core, and the extra that installs
# each one's requirements.
_MODULE_EXTRAS = {"pydantic": "pydantic"}


def _taken_body(statement: ast.If) -> list[ast.stmt]:
    """Return the branch of an ``if`` that runs on this Python.

    Args:
        statement: Module-level ``if`` statement.

    Returns:
        The branch that a ``sys.version_info`` test selects, the ``else``
        branch of ``if TYPE_CHECKING``, or both branches of any other test.
    """
    test = statement.test
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return statement.orelse
    if "version_info" in ast.unparse(test):
        # The test is the package's own guard, such as
        # sys.version_info >= (3, 11), so evaluating it is safe.
        taken = eval(
            compile(ast.Expression(test), "<guard>", "eval"), {"sys": sys}
        )
        return statement.body if taken else statement.orelse
    return [*statement.body, *statement.orelse]


def _imported_distributions(path: Path) -> set[str]:
    """Return the distributions of the modules that importing a file needs.

    Only module-level imports count, following the branches that this Python
    takes, because a function's import runs only if the function does.

    Args:
        path: Python source file of the package.

    Returns:
        Canonical names of the third-party distributions that provide the
        modules imported at module level.
    """
    providers = importlib.metadata.packages_distributions()
    names: set[str] = set()
    pending = list(ast.parse(path.read_text()).body)
    while pending:
        statement = pending.pop()
        if isinstance(statement, ast.If):
            pending.extend(_taken_body(statement))
        elif isinstance(statement, ast.Import):
            names.update(alias.name.split(".")[0] for alias in statement.names)
        elif isinstance(statement, ast.ImportFrom) and statement.level == 0:
            names.add((statement.module or "").split(".")[0])
    third_party = names - set(sys.stdlib_module_names) - {"eazyrest"}
    return {
        canonicalize_name(providers.get(name, [name])[0])
        for name in third_party
    }


def test_each_imported_distribution_is_a_requirement() -> None:
    """The metadata requires every distribution that importing needs.

    The test environments install more than the package requires, such as
    typing_extensions on every Python, so an import that the metadata does
    not declare would pass the other tests. This test reads the metadata of
    the installed package instead, with the markers of this Python.
    """
    requirements = [
        Requirement(text)
        for text in importlib.metadata.requires("eazyrest") or []
    ]
    package = Path(eazyrest.__file__).parent
    for path in sorted(package.glob("*.py")):
        extra = _MODULE_EXTRAS.get(path.stem, "")
        required = {
            canonicalize_name(requirement.name)
            for requirement in requirements
            if requirement.marker is None
            or requirement.marker.evaluate({"extra": extra})
        }
        for name in _imported_distributions(path):
            provider = _PROVIDED_BY.get(name, name)
            assert provider in required, (
                f"{path.name} imports {name}, which eazyrest"
                + (f"[{extra}]" if extra else "")
                + " does not require"
            )
