import json

import httpx
import pytest

from deck_mcp.client import APP_API, OCS_API, DeckClient, DeckError
from deck_mcp.config import Config

BOARDS = [
    {"id": 13, "title": "Welcome to Nextcloud Deck!", "deletedAt": 0},
    {"id": 11, "title": "CMA", "deletedAt": 0},
]
STACKS = [
    {"id": 41, "title": "Backlog", "deletedAt": 0},
    {"id": 49, "title": "AI Agent", "deletedAt": 0},
]
CARD = {
    "id": 162,
    "title": "Test Card",
    "description": "write fibonacci",
    "stackId": 49,
    "type": "plain",
    "order": 999,
    "archived": False,
    "done": None,
    "duedate": None,
    "labels": [{"id": 83, "title": "coding"}],
    "assignedUsers": [
        {"participant": {"uid": "ai_agent", "primaryKey": "ai_agent"}},
    ],
}


def make_client(handler, **overrides):
    values = dict(
        url="https://cloud.example",
        username="ai_agent",
        password="secret",
        board="CMA",
        lane="AI Agent",
        board_id=None,
        lane_id=None,
        assignee=None,
    )
    values.update(overrides)
    return DeckClient(
        Config(**values), transport=httpx.MockTransport(handler)
    )


def test_resolve_board_and_lane_by_title():
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        if request.url.path == f"{APP_API}/boards":
            return httpx.Response(200, json=BOARDS)
        if request.url.path == f"{APP_API}/boards/11/stacks":
            return httpx.Response(200, json=STACKS)
        raise AssertionError(f"unexpected path {request.url.path}")

    client = make_client(handler)
    assert client.board_id() == 11
    assert client.stack_id() == 49
    assert client.stack_id() == 49  # cached, no extra request
    assert requests == [
        f"GET {APP_API}/boards",
        f"GET {APP_API}/boards/11/stacks",
    ]


def test_unknown_lane_lists_available():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{APP_API}/boards":
            return httpx.Response(200, json=BOARDS)
        if request.url.path == f"{APP_API}/boards/11/stacks":
            return httpx.Response(200, json=STACKS)
        raise AssertionError("unexpected")

    client = make_client(handler, lane="Nope")
    client.board_id()
    with pytest.raises(DeckError) as exc:
        client.stack_id()
    assert "AI Agent" in str(exc.value)


def test_title_match_is_case_insensitive_fallback():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{APP_API}/boards":
            return httpx.Response(200, json=BOARDS)
        raise AssertionError("unexpected")

    client = make_client(handler, board="cma")
    assert client.board_id() == 11


def test_cards_embedded_in_stacks_list():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{APP_API}/boards/11/stacks":
            return httpx.Response(
                200,
                json=[
                    {"id": 41, "title": "Backlog", "cards": [{"id": 1}]},
                    {"id": 49, "title": "AI Agent", "cards": [CARD]},
                ],
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    client = make_client(handler)
    assert client.cards(11, 49) == [CARD]
    with pytest.raises(DeckError):
        client.cards(11, 99)


def test_comments_unwrap_and_pagination():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        query = request.url.params
        offset = int(query.get("offset", "0"))
        limit = int(query.get("limit", "50"))
        page = [
            {
                "id": offset + i,
                "message": f"m{offset + i}",
                "actorId": "ai_agent",
                "creationDateTime": "2026-10-08T10:00:00+00:00",
            }
            for i in range(limit if offset == 0 else 0)
        ]
        return httpx.Response(
            200, json={"ocs": {"meta": {"statuscode": 200, "status": "ok"}, "data": page}}
        )

    client = make_client(handler)
    comments = client.comments(162)
    assert len(comments) == 50
    assert len(seen) == 2
    assert seen[0].startswith(f"https://cloud.example{OCS_API}/cards/162/comments")


def test_add_comment_posts_ocs_payload():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "ocs": {
                    "meta": {"statuscode": 200, "status": "ok"},
                    "data": {"id": 777, "message": "hi"},
                }
            },
        )

    client = make_client(handler)
    result = client.add_comment(162, "hi", parent_id=12)
    assert result["id"] == 777
    assert captured["path"] == f"{OCS_API}/cards/162/comments"
    assert captured["body"] == {"message": "hi", "parentId": 12}


def test_add_comment_sends_json_content_type():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["content_type"] = request.headers.get("content-type")
        return httpx.Response(
            200,
            json={"ocs": {"meta": {"statuscode": 200, "status": "ok"}, "data": {}}},
        )

    client = make_client(handler)
    client.add_comment(162, "hi")
    assert captured["content_type"] == "application/json"


def test_upload_attachment_sends_multipart():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.content
        return httpx.Response(
            200,
            json={
                "id": 9,
                "cardId": 162,
                "type": "deck_file",
                "data": "response.md",
            },
        )

    client = make_client(handler)
    result = client.upload_attachment(11, 49, 162, "response.md", "# hello\n")
    assert captured["method"] == "POST"
    assert captured["path"] == f"{APP_API}/boards/11/stacks/49/cards/162/attachments"
    assert captured["content_type"].startswith("multipart/form-data; boundary=")
    assert b'name="type"' in captured["body"]
    assert b"deck_file" in captured["body"]
    assert b'filename="response.md"' in captured["body"]
    assert b"# hello" in captured["body"]
    assert result["id"] == 9


def test_upload_attachment_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"message": "File already exists."})

    client = make_client(handler)
    with pytest.raises(DeckError) as excinfo:
        client.upload_attachment(11, 49, 162, "response.md", "x")
    assert excinfo.value.status == 409


def test_attachment_url():
    client = make_client(lambda request: httpx.Response(200, json={}))
    url = client.attachment_url(162, 9)
    # plain app route (session-friendly), not the #[CORS] API route
    assert url == "https://cloud.example/index.php/apps/deck/cards/162/attachment/9"


def test_set_card_done_uses_dedicated_endpoints():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json={**CARD, "done": "2026-10-08T12:00:00+00:00"})

    client = make_client(handler)
    card = client.set_card_done(162, True)
    assert captured["method"] == "PUT"
    assert captured["path"] == "/index.php/apps/deck/cards/162/done"
    assert card["done"] == "2026-10-08T12:00:00+00:00"

    handler2_path = {}

    def handler2(request: httpx.Request) -> httpx.Response:
        handler2_path["path"] = request.url.path
        return httpx.Response(200, json={**CARD, "done": None})

    client2 = make_client(handler2)
    result = client2.set_card_done(162, False)
    assert handler2_path["path"] == "/index.php/apps/deck/cards/162/undone"
    assert result["done"] is None


def test_ocs_failure_raises_with_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ocs": {
                    "meta": {
                        "status": "failure",
                        "statuscode": 998,
                        "message": "Invalid query",
                    },
                    "data": [],
                }
            },
        )

    client = make_client(handler)
    with pytest.raises(DeckError) as exc:
        client.stacks(11)
    assert "998" in str(exc.value)
    assert "Invalid query" in str(exc.value)


def test_http_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Permission denied"})

    client = make_client(handler)
    with pytest.raises(DeckError) as exc:
        client.boards()
    assert exc.value.status == 403
    assert "Permission denied" in str(exc.value)
