"""Sphinx configuration for the eazyrest documentation."""

from importlib.metadata import version as package_version

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
}

# Warn about every reference that does not resolve.
nitpicky = True
nitpick_ignore = [
    # Sphinx cannot link to a type variable.
    ("py:class", "eazyrest.eazyrest.T"),
]
