"""Sphinx configuration for the eazyrest documentation."""

from importlib.metadata import version as package_version
from typing import Any

from docutils import nodes

project = "eazyrest"
author = "Geoffrey Mainland"
# autodoc documents the installed package, so its version is the release.
release = package_version("eazyrest")
version = ".".join(release.split(".")[:2])

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.intersphinx",
    "sphinx.ext.napoleon",
]

templates_path = ["_templates"]
exclude_patterns = ["_build"]

html_theme = "furo"
html_static_path = ["_static"]
html_title = "eazyrest"

source_suffix = {
    ".md": "markdown",
}

myst_enable_extensions = [
    "colon_fence",
]

# Give ## and ### headings anchors, so links can name a section.
myst_heading_anchors = 3

autodoc_typehints = "description"
autodoc_member_order = "bysource"
autoclass_content = "both"

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "requests": ("https://requests.readthedocs.io/en/latest/", None),
    "dateutil": ("https://dateutil.readthedocs.io/en/stable/", None),
    "pydantic": ("https://docs.pydantic.dev/latest/", None),
}

# Warn about every reference that does not resolve.
nitpicky = True
nitpick_ignore = [
    # Sphinx cannot link to a type variable.
    ("py:class", "eazyrest._models.T"),
    # _models.py imports TypeAdapter only for type checking, so autodoc cannot
    # qualify the name.
    ("py:class", "TypeAdapter"),
]


def _drop_alias_values(app: Any, doctree: Any) -> None:
    """Drop the "alias of" line of the eazyrest.pydantic types.

    autodoc writes the value of an ``Annotated`` alias even with
    ``:no-value:``, and the types' metadata are eazyrest's private objects,
    which would print with their addresses.

    Args:
        app: The Sphinx application.
        doctree: A read document.
    """
    if app.env.docname != "reference/pydantic":
        return
    for paragraph in list(doctree.findall(nodes.paragraph)):
        if paragraph.astext().startswith("alias of Annotated["):
            paragraph.parent.remove(paragraph)


def setup(app: Any) -> None:
    """Install the document hooks.

    Args:
        app: The Sphinx application.
    """
    app.connect("doctree-read", _drop_alias_values)
