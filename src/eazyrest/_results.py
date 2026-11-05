"""The iterator type of collection queries."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Generic, TypeVar

T = TypeVar("T")


class Results(Iterator[T], Generic[T]):
    """An iterator over a collection's items that knows the collection's size.

    It iterates once, lazily, as the iterator it wraps does. ``count`` is the
    number of items in the whole collection when the response says so, as the
    ``count`` member of a page of Django REST Framework's
    ``PageNumberPagination`` and ``LimitOffsetPagination`` does, and ``None``
    otherwise.
    """

    count: int | None
    """Number of items in the whole collection, or ``None`` if unknown."""

    def __init__(self, items: Iterable[T], count: int | None = None) -> None:
        """Wrap an iterable of a collection's items.

        Args:
            items: The items, which are read once and lazily.
            count: Number of items in the whole collection, if known.
        """
        self._items = iter(items)
        self.count = count

    def __iter__(self) -> Results[T]:
        """Return this iterator.

        Returns:
            This iterator.
        """
        return self

    def __next__(self) -> T:
        """Return the next item.

        Returns:
            The next item of the collection.

        Raises:
            StopIteration: When the items are exhausted.
        """
        return next(self._items)


def _count_of(items: object) -> int | None:
    """Return the collection size that an iterable of items carries.

    Args:
        items: Iterable that a collection hook returned.

    Returns:
        Its ``count`` attribute, if that is an integer other than a boolean,
        and ``None`` otherwise.
    """
    count = getattr(items, "count", None)
    if isinstance(count, int) and not isinstance(count, bool):
        return count
    return None
