"""A load queues what was changed in place before it drops cached values."""

from __future__ import annotations

import requests_mock

from eazyrest import API, JSONObject, json_object

api = API("https://load-queue.example/")


@json_object(pk="slug")
class Tag(JSONObject):
    """A related model keyed by text."""

    class_url = "/tags/"
    slug: str


@json_object
class Post(JSONObject):
    """A model with a collection of relations and a field that loads."""

    class_url = "/posts/"
    id: int
    title: str
    tags: list[Tag]


Tag.register_api(api)
Post.register_api(api)


def test_a_load_that_changes_a_field_keeps_a_change_made_in_place() -> None:
    """The server's new list does not hide the item added to the old one."""
    post = Post(id=1)
    post.update_from_json({"tags": ["a"]})
    post.tags.append(Tag(slug="b"))
    with requests_mock.Mocker() as mocker:
        mocker.get(
            "https://load-queue.example/posts/1/",
            json={"id": 1, "title": "t", "tags": ["a", "c"]},
        )
        mocker.patch("https://load-queue.example/posts/1/", json={})
        assert post.title == "t"
        assert [tag.pk for tag in post.tags] == ["a", "b"]
        post.save()
        assert mocker.last_request.method == "PATCH"
        assert mocker.last_request.json() == {"tags": ["a", "b"]}
