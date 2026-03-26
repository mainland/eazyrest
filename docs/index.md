# eazyrest

`eazyrest` is a small library for consuming REST APIs with less boilerplate. It combines a session-based API client with a lightweight, type-directed JSON object mapping layer for typed Python models.

```{toctree}
:maxdepth: 2
:caption: Guide

quickstart
guide
reference
```

## Overview

`eazyrest` gives you two main building blocks:

- `API` for session-based HTTP access
- `JSONObject` plus `@json_object` for typed REST resource models

The library is designed around lazy object loading, typed related-object resolution, and a small amount of configurable behavior for JSON response shapes and write semantics.
