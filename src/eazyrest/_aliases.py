"""Named type aliases, expanded to the types that they stand for."""

from __future__ import annotations

import types
import typing
from collections.abc import Mapping
from typing import Annotated, Any, Literal, TypeVar, Union


def _is_type_alias(ty: Any) -> bool:
    """Return whether a type is a named type alias.

    A named alias is a ``typing.TypeAliasType``, which the ``type`` statement
    creates, or its ``typing_extensions`` backport. An ordinary assignment, as
    in ``Points = list[Point]``, binds the type itself and is not one.

    The test reads the object's class and nothing of its value. Reading the
    value evaluates it, which raises the error of a name that is not defined
    yet, whatever that error is, and a test must not hide or change it. The
    caller that needs the value reads it, and gets that error.

    Args:
        ty: Type to test.

    Returns:
        Whether ``ty`` is a named type alias.
    """
    return type(ty).__name__ == "TypeAliasType"


def _expand_alias(
    alias: Any, args: tuple[Any, ...], expanding: tuple[Any, ...]
) -> Any:
    """Return the type that a named alias, with type arguments, stands for.

    Args:
        alias: Named type alias.
        args: Type arguments for the alias's type parameters, already expanded.
        expanding: Aliases whose expansion is in progress.

    Returns:
        The alias's value, with ``args`` in place of its type parameters and
        every named alias inside it expanded.

    Raises:
        TypeError: If the alias refers to itself, or its type parameters are
            not type variables matching ``args``.
    """
    if alias in expanding:
        raise TypeError(
            f"Type alias {alias.__name__} refers to itself, which eazyrest "
            "cannot convert"
        )

    params = alias.__type_params__
    if len(params) != len(args) or not all(
        isinstance(param, TypeVar) for param in params
    ):
        raise TypeError(
            f"Type alias {alias.__name__} needs one type argument for each of "
            "its type parameters, which must be type variables"
        )

    return _expand_aliases(
        _resolve_alias_value(alias),
        dict(zip(params, args, strict=True)),
        (*expanding, alias),
    )


def _expand_aliases(
    ty: Any,
    substitutions: Mapping[Any, Any] | None = None,
    expanding: tuple[Any, ...] = (),
) -> Any:
    """Return a type with every named type alias in it expanded.

    Args:
        ty: Type to expand.
        substitutions: Types to put in place of type variables, while the value
            of a parameterized alias expands.
        expanding: Aliases whose expansion is in progress.

    Returns:
        ``ty`` with each named alias, also as a type argument, replaced by the
        type it stands for, or ``ty`` itself if it has none.

    Raises:
        TypeError: If an alias cannot be expanded.
    """
    if substitutions and isinstance(ty, TypeVar) and ty in substitutions:
        return substitutions[ty]
    if _is_type_alias(ty):
        return _expand_alias(ty, (), expanding)

    origin = typing.get_origin(ty)
    args = typing.get_args(ty)
    if _is_type_alias(origin):
        args = tuple(
            _expand_aliases(arg, substitutions, expanding) for arg in args
        )
        return _expand_alias(origin, args, expanding)
    if not args or origin is Literal:
        return ty

    new_args = tuple(
        _expand_aliases(arg, substitutions, expanding) for arg in args
    )
    if all(new is old for new, old in zip(new_args, args, strict=True)):
        return ty
    if origin is Annotated:
        params = new_args
        return Annotated[params]
    if origin is types.UnionType:
        origin = Union

    return origin[new_args]


def _has_forward_refs(ty: Any) -> bool:
    """Return whether a type names a type by a string, at any depth.

    Args:
        ty: Type to examine.

    Returns:
        Whether ``ty``, or a type argument within it, is a string or a
        ``ForwardRef``.
    """
    if isinstance(ty, (str, typing.ForwardRef)):
        return True
    if typing.get_origin(ty) is Literal:
        return False
    return any(_has_forward_refs(arg) for arg in typing.get_args(ty))


def _resolve_alias_value(alias: Any) -> Any:
    """Return the value of a named alias with its string references resolved.

    A string inside an alias's value, as in ``list["datetime.datetime"]``,
    names a type in the module that defines the alias. It is resolved there, as
    ``typing.get_type_hints()`` resolves an annotation, so that the alias
    converts as the type it names rather than as an unconverted value.

    Args:
        alias: Named type alias.

    Returns:
        The alias's value, with each string reference replaced by the type it
        names.

    Raises:
        _UnresolvedAliasError: If a string reference names nothing in the
            alias's module, which a ``TypeError`` reports.
        Exception: If evaluating the alias's value raises, as for a name that
            is not defined yet, whatever that raises.
    """
    value = alias.__value__
    if not _has_forward_refs(value):
        return value

    try:
        return _resolve_in_module(value, alias.__module__, alias.__name__)
    except Exception as error:
        raise _UnresolvedAliasError(
            f"Type alias {alias.__name__} names a type by a string that "
            f"cannot be resolved in module {alias.__module__}"
        ) from error


def _resolve_in_module(
    value: Any,
    module: str,
    holder_name: str,
    localns: Mapping[str, Any] | None = None,
) -> Any:
    """Resolve a value as the annotation of a class in a module.

    A string in ``value``, at any depth, names a type as it does in an
    annotation: first in ``localns`` if given, then in the module's names. This
    is the one place that builds the class for ``get_type_hints()``, so every
    resolution reads names alike.

    Args:
        value: Annotation, or any value that holds strings that name types.
        module: Name of the module whose names the strings use.
        holder_name: Name of the class that holds the annotation, which an
            error from resolving may quote.
        localns: Names that take precedence over the module's, or ``None`` for
            none.

    Returns:
        ``value``, with its ``Annotated`` metadata and each string replaced by
        the type that it names.

    Raises:
        Exception: If a string names something undefined, whatever resolving it
            raises.
    """
    holder = type(
        holder_name,
        (),
        {"__module__": module, "__annotations__": {"value": value}},
    )
    return typing.get_type_hints(holder, localns=localns, include_extras=True)[
        "value"
    ]


class _UnresolvedAliasError(TypeError):
    """A type alias names a type by a string that does not resolve yet."""
