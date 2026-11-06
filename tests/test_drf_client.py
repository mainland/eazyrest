"""Tests of the client against a live Django REST Framework server.

These tests need Django and Django REST Framework, which the drf tox
environment installs. Other environments skip them. The server runs in a
thread on a local port, and the client sends real HTTP requests to it.
"""

from __future__ import annotations

import contextlib
import datetime
import decimal
import threading
from collections.abc import Generator, Iterator
from typing import Annotated, Any, ClassVar
from urllib.parse import parse_qs, urlsplit
from wsgiref.simple_server import WSGIRequestHandler, make_server

import pytest

pytest.importorskip("django")
pytest.importorskip("rest_framework")
pytest.importorskip("django_filters")

from drf_settings import configure

configure()

from django.conf import settings
from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.core.handlers.wsgi import WSGIHandler
from django.test import Client
from django.urls import include, path
from django.utils.crypto import get_random_string
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import pagination, routers, serializers, viewsets
from rest_framework.authentication import (
    SessionAuthentication,
    TokenAuthentication,
)
from rest_framework.authtoken.models import Token
from rest_framework.parsers import JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from test_docs import DRF, run_guide_example

from eazyrest import (
    API,
    APIError,
    DoesNotExist,
    Hyperlinked,
    JSONObject,
    NotFound,
    follow_next_links,
    json_object,
)

# The server: serializers, views, and URLs in DRF's usual shapes.


class ContentTypeSerializer(serializers.ModelSerializer):
    """Content type, which permissions relate to."""

    class Meta:
        """Model and fields."""

        model = ContentType
        fields = ("id", "app_label", "model")


class PermissionSerializer(serializers.ModelSerializer):
    """Permission whose content type is read nested and written by key."""

    content_type = ContentTypeSerializer(read_only=True)
    content_type_id = serializers.PrimaryKeyRelatedField(
        source="content_type",
        queryset=ContentType.objects.all(),
        write_only=True,
    )

    class Meta:
        """Model and fields."""

        model = Permission
        fields = ("id", "name", "codename", "content_type", "content_type_id")


class HyperPermissionSerializer(serializers.HyperlinkedModelSerializer):
    """Permission whose content type is a hyperlink."""

    class Meta:
        """Model and fields."""

        model = Permission
        fields = ("id", "name", "codename", "content_type")


class UserSerializer(serializers.ModelSerializer):
    """User whose password is write-only."""

    password = serializers.CharField(write_only=True)

    class Meta:
        """Model and fields."""

        model = User
        fields = ("id", "username", "password")

    def create(self, validated_data: dict[str, Any]) -> User:
        """Create a user, hashing the password."""
        return User.objects.create_user(**validated_data)

    def update(self, instance: User, validated_data: dict[str, Any]) -> User:
        """Update a user, hashing a new password."""
        password = validated_data.pop("password", None)
        if password is not None:
            instance.set_password(password)
        return super().update(instance, validated_data)


class Pages(pagination.PageNumberPagination):
    """Small pages, so that collections span several."""

    page_size = 5


class ContentTypeViewSet(viewsets.ReadOnlyModelViewSet):
    """Content types."""

    queryset = ContentType.objects.order_by("id")
    serializer_class = ContentTypeSerializer


class PermissionViewSet(viewsets.ModelViewSet):
    """Permissions, paginated."""

    queryset = Permission.objects.order_by("id")
    serializer_class = PermissionSerializer
    pagination_class = Pages


class HyperPermissionViewSet(viewsets.ModelViewSet):
    """Permissions with hyperlinked relations."""

    queryset = Permission.objects.order_by("id")
    serializer_class = HyperPermissionSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_fields = ("content_type",)


class UserViewSet(viewsets.ModelViewSet):
    """Users."""

    queryset = User.objects.order_by("id")
    serializer_class = UserSerializer


class UploadSerializer(serializers.Serializer):
    """Writable upload fields, including DRF's file validation."""

    name = serializers.CharField()
    public = serializers.CharField()
    image = serializers.FileField(required=False)


class UploadView(APIView):
    """Accept a multipart upload and describe what arrived."""

    parser_classes = (JSONParser, MultiPartParser)
    stored: ClassVar[dict[str, Any]] = {}

    def get(self, request: Request) -> Response:
        """Return the stored upload's fields."""
        return Response(self.stored)

    def post(self, request: Request) -> Response:
        """Describe the uploaded file and form fields."""
        return self._write(request, partial=False, status=201)

    def put(self, request: Request) -> Response:
        """Replace writable values, preserving a file when none is sent."""
        return self._write(request, partial=False)

    def patch(self, request: Request) -> Response:
        """Update supplied values, including explicit file uploads."""
        return self._write(request, partial=True)

    def _write(
        self, request: Request, *, partial: bool, status: int = 200
    ) -> Response:
        """Validate and store a write's fields.

        Args:
            request: JSON or multipart request.
            partial: Whether missing ordinary fields are accepted.
            status: Response status.

        Returns:
            The stored representation with its file URL.
        """
        serializer = UploadSerializer(data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        values = dict(serializer.validated_data)
        upload = values.pop("image", None)
        payload = {**self.stored, "id": 1, **values}
        if upload is not None:
            payload.update(image=f"/media/{upload.name}", size=upload.size)
        type(self).stored = payload
        return Response(payload, status=status)


class PriceSerializer(serializers.Serializer):
    """A price and an opening time, as DRF's decimal and time fields."""

    amount = serializers.DecimalField(max_digits=8, decimal_places=2)
    opens = serializers.TimeField()


class PriceView(APIView):
    """Validate a price and return it as DRF represents it."""

    stored: ClassVar[dict[str, Any]] = {}

    def post(self, request: Request) -> Response:
        """Create the price."""
        return self._write(request, partial=False, status=201)

    def patch(self, request: Request) -> Response:
        """Update the supplied values."""
        return self._write(request, partial=True)

    def _write(
        self, request: Request, *, partial: bool, status: int = 200
    ) -> Response:
        """Validate, store, and represent a write's values.

        Args:
            request: JSON request.
            partial: Whether missing values are accepted.
            status: Response status.

        Returns:
            The stored price, with its decimal as a string.
        """
        serializer = PriceSerializer(data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        values = {**self.stored, **serializer.validated_data}
        type(self).stored = values
        return Response(
            {"id": 1, **PriceSerializer(values).data}, status=status
        )


# The DRF guide's two server-side bulk recipes, run as the guide writes them:
# a list view that filters by repeated id parameters, and a bulk action.
FilteredUserViewSet = run_guide_example(
    "queryset = queryset.filter(pk__in=ids)",
    doc=DRF,
    User=User,
    UserSerializer=UserSerializer,
)["UserViewSet"]
BulkActionUserViewSet = run_guide_example(
    'url_path="bulk"', doc=DRF, User=User, UserSerializer=UserSerializer
)["UserViewSet"]


class TeamSerializer(serializers.ModelSerializer):
    """Group whose members are a list of user keys."""

    members = serializers.PrimaryKeyRelatedField(
        source="user_set", many=True, read_only=True
    )

    class Meta:
        """Model and fields."""

        model = Group
        fields = ("id", "name", "members")


class TeamViewSet(viewsets.ReadOnlyModelViewSet):
    """Groups, as teams whose members can be prefetched."""

    queryset = Group.objects.order_by("id")
    serializer_class = TeamSerializer
    filter_backends = (DjangoFilterBackend,)
    filterset_fields: ClassVar[dict[str, list[str]]] = {
        "name": ["exact", "startswith"]
    }


class WhoAmIView(APIView):
    """Report the user whom DRF's stock token authentication names."""

    authentication_classes = (TokenAuthentication,)
    permission_classes = (IsAuthenticated,)

    def get(self, request: Request) -> Response:
        """Return the authenticated user's name."""
        return Response({"username": request.user.username})


class SessionWriteView(APIView):
    """Accept a write from a user whom a session cookie authenticates.

    DRF's session authentication enforces Django's CSRF check on unsafe
    methods.
    """

    authentication_classes = (SessionAuthentication,)
    permission_classes = (IsAuthenticated,)

    def post(self, request: Request) -> Response:
        """Return the authenticated user's name."""
        return Response({"username": request.user.username}, status=201)


router = routers.DefaultRouter()
router.register("contenttypes", ContentTypeViewSet, basename="contenttype")
router.register("permissions", PermissionViewSet, basename="permission")
router.register(
    "hyper-permissions", HyperPermissionViewSet, basename="hyperpermission"
)
router.register("users", UserViewSet, basename="user")
router.register("filtered-users", FilteredUserViewSet, basename="filtereduser")
router.register("teams", TeamViewSet, basename="team")

urlpatterns = [
    path("uploads/", UploadView.as_view()),
    path("uploads/1/", UploadView.as_view()),
    path("prices/", PriceView.as_view()),
    path("prices/1/", PriceView.as_view()),
    # The guide's bulk action, at the URL that its client recipe requests.
    # It comes before the router, whose detail route would read "bulk" as a
    # key.
    path("users/bulk/", BulkActionUserViewSet.as_view({"get": "bulk"})),
    path("whoami/", WhoAmIView.as_view()),
    path("session-writes/", SessionWriteView.as_view()),
    path("", include(router.urls)),
]


class QuietHandler(WSGIRequestHandler):
    """Request handler that does not log each request."""

    def log_message(self, format: str, *args: Any) -> None:
        """Discard the log line."""


@pytest.fixture(scope="module")
def api() -> Iterator[API]:
    """Serve the views on a local port, and register a client for them.

    Yields:
        Client for the server.
    """
    server = make_server(
        "127.0.0.1", 0, WSGIHandler(), handler_class=QuietHandler
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = API(f"http://127.0.0.1:{server.server_port}/")
    DRFBase.register_api(client)
    yield client
    server.shutdown()
    server.server_close()


# The client: models in the shapes that the DRF guide recommends.


class DRFBase(JSONObject):
    """Base model that reads every page of a DRF collection."""

    @classmethod
    def collection_items(cls, payload: Any, *, api: API) -> Any:
        """Read every page, following next links."""
        return follow_next_links(api, payload)


@json_object
class ContentTypeModel(DRFBase):
    """Content type."""

    class_url = "/contenttypes/"

    id: int
    app_label: str
    model: str


@json_object(write_map={"content_type": "content_type_id"})
class PermissionModel(DRFBase):
    """Permission whose content type is read nested and written by key."""

    class_url = "/permissions/"

    id: int
    name: str
    codename: str
    content_type: ContentTypeModel


@json_object
class HyperPermissionModel(DRFBase):
    """Permission whose content type is a hyperlink."""

    class_url = "/hyper-permissions/"

    id: int
    name: str
    codename: str
    content_type: Annotated[ContentTypeModel, Hyperlinked()]


@json_object(write_only={"password"})
class UserModel(DRFBase):
    """User whose password is write-only."""

    class_url = "/users/"

    id: int
    username: str
    password: str


@json_object(read_only={"id", "image", "size"})
class UploadModel(DRFBase):
    """Upload whose file field reads as a URL."""

    class_url = "/uploads/"

    id: int
    name: str
    public: str
    image: str
    size: int


@json_object
class PriceModel(DRFBase):
    """Price whose decimal and time convert as DRF writes them."""

    class_url = "/prices/"

    id: int
    amount: decimal.Decimal
    opens: datetime.time


# The DRF guide's two client recipes for bulk_get_by_pks(), run as the guide
# writes them. The first is a model whose hook filters by repeated ids. The
# second is a hook that requests the bulk action.
FilteredUserRecipe = run_guide_example(
    "users = list(cls.filter(id=sorted(pks), api=api))",
    doc=DRF,
    DRFObject=DRFBase,
    json_object=json_object,
)["User"]
bulk_action_recipe = run_guide_example(
    'resp = api.get("/users/bulk/"', doc=DRF
)["bulk_get_by_pks"]


@json_object
class FilteredUserModel(FilteredUserRecipe):  # type: ignore[misc,valid-type]
    """The guide's filtering client model, against the guide's list view."""

    class_url = "/filtered-users/"

    username: str


@json_object
class ActionUserModel(DRFBase):
    """User whose bulk lookup is the guide's bulk-action hook."""

    class_url = "/users/"

    id: int
    username: str

    bulk_get_by_pks = bulk_action_recipe


@json_object
class FilteredTeamModel(DRFBase):
    """Team whose members prefetch through the filtering recipe."""

    class_url = "/teams/"

    id: int
    name: str
    members: list[FilteredUserModel]


@json_object
class ActionTeamModel(DRFBase):
    """Team whose members prefetch through the bulk-action recipe."""

    class_url = "/teams/"

    id: int
    name: str
    members: list[ActionUserModel]


def _content_types() -> tuple[int, int]:
    """Return the keys of two content types.

    Returns:
        The keys of the group and user content types.
    """
    return (
        ContentType.objects.get_for_model(Group).pk,
        ContentType.objects.get_for_model(User).pk,
    )


def test_count_reports_the_whole_collection(api: API) -> None:
    """``filter()`` reports DRF's count and reads every page."""
    permissions = PermissionModel.filter()

    assert permissions.count == Permission.objects.count()
    assert permissions.count is not None
    assert permissions.count > Pages.page_size
    assert len(list(permissions)) == permissions.count


def test_write_map_writes_the_key_and_reads_the_nested_object(
    api: API,
) -> None:
    """A nested relation is written as ``content_type_id``."""
    group_type, user_type = _content_types()
    permission = PermissionModel.create(
        name="Can test",
        codename="can_test",
        content_type=ContentTypeModel(id=group_type),
    )
    assert permission.content_type.model == "group"

    permission.content_type = ContentTypeModel(id=user_type)
    permission.save()

    assert permission.content_type.model == "user"
    assert (
        Permission.objects.get(pk=permission.id).content_type_id == user_type
    )

    permission.name = "Can test fully"
    permission.save(full=True)

    assert Permission.objects.get(pk=permission.id).name == "Can test fully"


def test_hyperlinked_relation_reads_and_writes_urls(api: API) -> None:
    """A hyperlinked relation loads from its URL and writes one."""
    group_type, user_type = _content_types()
    stored = Permission.objects.create(
        name="Can link",
        codename="can_link",
        content_type_id=group_type,
    )
    permission = HyperPermissionModel(id=stored.pk)

    assert permission.content_type.model == "group"
    assert permission.content_type.url.startswith(api.base_url)

    permission.content_type = ContentTypeModel(id=user_type)
    permission.save()

    assert Permission.objects.get(pk=stored.pk).content_type_id == user_type

    for value in (user_type, str(user_type), ContentTypeModel(id=user_type)):
        results = list(HyperPermissionModel.filter(content_type=value))
        assert stored.pk in {result.pk for result in results}
        assert all(result.content_type.pk == user_type for result in results)


def test_write_only_password_is_set_but_never_read(api: API) -> None:
    """A write-only password reaches the server and is not kept."""
    user = UserModel.create(username="ada", password="first-password")

    assert User.objects.get(pk=user.id).check_password("first-password")
    assert "password" not in user.json

    user.password = "second-password"
    user.save()

    assert User.objects.get(pk=user.id).check_password("second-password")
    with pytest.raises(AttributeError, match="write-only"):
        _ = user.password


def test_validation_errors_and_missing_objects(api: API) -> None:
    """DRF's error bodies arrive as ``APIError.data``."""
    with pytest.raises(APIError) as info:
        PermissionModel.create(name="No codename")

    assert isinstance(info.value.data, dict)
    assert set(info.value.data) == {"codename", "content_type_id"}
    assert "codename" in str(info.value)

    with pytest.raises(DoesNotExist) as missing:
        _ = PermissionModel(id=999_999).name

    assert isinstance(missing.value, NotFound)


def test_decimal_and_time_fields_match_drf(api: API) -> None:
    """Decimals and times are written as DRF reads them, and read back."""
    price = PriceModel.create(
        amount=decimal.Decimal("12.3"), opens=datetime.time(9, 30)
    )

    assert price.json["amount"] == "12.30"
    assert str(price.amount) == "12.30"
    assert price.opens == datetime.time(9, 30)

    price.amount = decimal.Decimal("1E+2")
    price.opens = datetime.time(17, 0, 30)
    price.save()

    assert price.json == {"id": 1, "amount": "100.00", "opens": "17:00:30"}
    assert price.amount == decimal.Decimal("100.00")


def test_upload_sends_multipart(api: API) -> None:
    """``create(files=...)`` uploads through DRF's multipart parser."""
    upload = UploadModel.create(
        name="avatar",
        public="true",
        files={"image": ("me.txt", b"hello")},
    )

    assert upload.image == "/media/me.txt"
    assert upload.size == 5
    assert upload.name == "avatar"

    upload.name = "renamed"
    upload.save(full=True)

    assert upload.name == "renamed"
    assert upload.image == "/media/me.txt"

    upload.save(files={"image": ("new.txt", b"replacement")})

    assert upload.image == "/media/new.txt"
    assert upload.size == len(b"replacement")


@pytest.fixture(scope="module")
def team_member_keys() -> list[int]:
    """Create two teams whose members overlap.

    Returns:
        The keys of the three members, sorted.
    """
    users = [User.objects.create(username=f"bulk-{name}") for name in "abc"]
    first = Group.objects.create(name="bulk-first")
    first.user_set.add(users[0], users[1])
    second = Group.objects.create(name="bulk-second")
    second.user_set.add(users[1], users[2])
    return sorted(user.pk for user in users)


@contextlib.contextmanager
def _recorded_urls(api: API) -> Generator[list[str], None, None]:
    """Record the URL of every response that the API receives.

    Args:
        api: Client whose responses to record.

    Yields:
        The URLs so far, in order.
    """
    urls: list[str] = []

    def record(resp: Any, *args: Any, **kwargs: Any) -> None:
        """Record one response's URL."""
        urls.append(resp.request.url)

    api.session.hooks["response"].append(record)
    try:
        yield urls
    finally:
        api.session.hooks["response"].remove(record)


@pytest.mark.parametrize(
    ("team_model", "bulk_path"),
    [
        (FilteredTeamModel, "/filtered-users/"),
        (ActionTeamModel, "/users/bulk/"),
    ],
)
def test_bulk_recipes_prefetch_members_in_one_request(
    api: API, team_member_keys: list[int], team_model: Any, bulk_path: str
) -> None:
    """Each bulk recipe, client and server, loads every member at once.

    Args:
        team_model: Team model whose member model uses one recipe.
        bulk_path: Path that the recipe's bulk request reads.
    """
    with _recorded_urls(api) as urls:
        teams = list(
            team_model.filter(name__startswith="bulk-", prefetch="members")
        )
        members = {
            member.username for team in teams for member in team.members
        }

    assert members == {"bulk-a", "bulk-b", "bulk-c"}
    assert len(urls) == 2
    bulk = urlsplit(urls[1])
    assert bulk.path == bulk_path
    assert sorted(map(int, parse_qs(bulk.query)["id"])) == team_member_keys


def test_token_recipe_authenticates(api: API) -> None:
    """The guide's ``Authorization`` header passes DRF token authentication."""
    user = User.objects.create(username="token-user")
    token = Token.objects.create(user=user)

    with API(api.base_url) as client:
        with pytest.raises(APIError) as refused:
            client.get("/whoami/")
        assert refused.value.response.status_code == 401

        client.session.headers.update({"Authorization": f"Token {token.key}"})

        response = client.get("/whoami/")

    assert client.json(response) == {"username": "token-user"}


def test_session_csrf_recipe_allows_unsafe_requests(api: API) -> None:
    """The guide's CSRF header lets a session-authenticated write through.

    DRF's session authentication refuses an unsafe request whose
    ``X-CSRFToken`` header does not match the CSRF cookie.
    """
    user = User.objects.create_user(username="session-user")
    browser = Client()
    browser.force_login(user)
    session_id = browser.cookies[settings.SESSION_COOKIE_NAME].value
    csrf_token = get_random_string(32)

    with API(api.base_url) as client:
        client.cookies.set(settings.SESSION_COOKIE_NAME, session_id)
        client.cookies.set(settings.CSRF_COOKIE_NAME, csrf_token)
        with pytest.raises(APIError) as refused:
            client.post("/session-writes/")
        assert refused.value.response.status_code == 403
        assert "CSRF" in str(refused.value.data)

        client.session.headers.update({"X-CSRFToken": csrf_token})

        response = client.post("/session-writes/")

    assert response.status_code == 201
    assert client.json(response) == {"username": "session-user"}
