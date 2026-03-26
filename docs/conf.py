"""Sphinx configuration for the eazyrest documentation."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

project = "eazyrest"
author = "Geoffrey Mainland"

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
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

autodoc_typehints = "description"
autodoc_member_order = "bysource"
autoclass_content = "both"
