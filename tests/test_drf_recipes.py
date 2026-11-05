"""Tests of the server-side recipes in the Django REST Framework guide.

These tests need Django, Django REST Framework, and django-filter, which the
drf tox environment installs. Other environments skip them.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("django")
pytest.importorskip("rest_framework")
pytest.importorskip("django_filters")

from drf_settings import configure

configure()

import django_filters
from django.contrib.auth.models import Group
from rest_framework import (
    filters,
    pagination,
    serializers,
    viewsets,
)
from rest_framework.test import APIRequestFactory
from test_docs import DRF, run_guide_example

backend = run_guide_example("class StrictDjangoFilterBackend", doc=DRF)[
    "StrictDjangoFilterBackend"
]


class GroupFilter(django_filters.FilterSet):
    """Filters of several kinds, including ones that read suffixed names."""

    name = django_filters.CharFilter()
    id = django_filters.RangeFilter()
    ids = django_filters.BaseInFilter(field_name="id")

    class Meta:
        """The model and fields that the filters apply to."""

        model = Group
        fields = ("name", "id", "ids")


class GroupSerializer(serializers.ModelSerializer):
    """Serializer of the groups that the view lists."""

    class Meta:
        """The model and fields that the serializer writes."""

        model = Group
        fields = ("id", "name")


class Pages(pagination.PageNumberPagination):
    """Paginator that lets the client choose the page size."""

    page_size = 10
    page_size_query_param = "page_size"


class GroupViewSet(viewsets.ReadOnlyModelViewSet):
    """View that filters with the guide's strict backend."""

    queryset = Group.objects.order_by("id")
    serializer_class = GroupSerializer
    filterset_class = GroupFilter
    filter_backends = (backend, filters.SearchFilter, filters.OrderingFilter)
    search_fields = ("name",)
    ordering_fields = ("name",)
    pagination_class = Pages
    extra_query_params = ("expand",)


@pytest.fixture(scope="module", autouse=True)
def _groups() -> None:
    """Create three groups."""
    for name in ["a", "b", "c"]:
        Group.objects.get_or_create(name=name)


def _list(query: dict[str, Any]) -> Any:
    """Return the view's response to a list request with ``query``.

    Args:
        query: Query parameters.

    Returns:
        The rendered response.
    """
    request = APIRequestFactory().get("/groups/", query)
    response = GroupViewSet.as_view({"get": "list"})(request)
    response.render()
    return response


@pytest.mark.parametrize(
    "query",
    [
        {"name": "a"},
        {"id_min": "1", "id_max": "2"},
        {"ids": "1,2"},
        {"search": "a", "ordering": "name"},
        {"page": "1", "page_size": "2"},
        {"format": "json"},
        {"expand": "members"},
    ],
)
def test_strict_backend_accepts_parameters_that_something_reads(
    query: dict[str, Any],
) -> None:
    """Filters, suffixed filters, paginators, and other backends are known.

    Args:
        query: Query parameters that the view reads.
    """
    assert _list(query).status_code == 200


def test_strict_backend_rejects_unknown_parameters() -> None:
    """A parameter that nothing reads is a 400 that names it."""
    response = _list({"title": "a"})

    assert response.status_code == 400
    assert response.data == {"title": ["Unknown query parameter."]}
