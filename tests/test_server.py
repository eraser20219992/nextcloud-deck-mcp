import asyncio
import re
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from deck_mcp.client import DeckError
from deck_mcp.config import Config
from deck_mcp.server import (
    AGENT_MARKER,
    COMMENT_MAX_LEN,
    WORKFLOW,
    _brief,
    _comment_out,
    _detail,
    _select,
    attachment_filename,
    build_server,
    publish_response,
    split_message,
)


def card(
    card_id,
    *,
    title="t",
    order=0,
    done=None,
    archived=False,
    assignees=(),
    labels=(),
    attachments=(),
    attachment_count=None,
):
    return {
        "id": card_id,
        "title": title,
        "order": order,
        "done": done,
        "archived": archived,
        "deletedAt": 0,
        "labels": [{"title": l} for l in labels],
        "assignedUsers": [{"participant": {"uid": u}} for u in assignees],
        "attachments": list(attachments),
        "attachmentCount": (
            len(list(attachments)) if attachment_count is None else attachment_count
        ),
    }


# ---------------------------------------------------------------- split_message


def test_split_short_message_single_chunk():
    assert split_message("hello") == ["hello"]


def test_split_respects_limit_and_line_boundaries():
    text = "\n".join(f"line {i} " + "x" * 20 for i in range(100))
    chunks = split_message(text, limit=200)
    assert all(len(c) <= 200 for c in chunks)
    assert "".join(chunks) == text
    assert len(chunks) > 1


def test_split_handles_very_long_single_line():
    text = "y" * 2500
    chunks = split_message(text, limit=960)
    assert all(len(c) <= 960 for c in chunks)
    assert "".join(chunks) == text


# ------------------------------------------------------------------------- select


def test_select_only_own_assigned_open_cards():
    cards = [
        card(1, assignees=["ai_agent"], order=2),
        card(2, assignees=["someone_else"], order=1),
        card(3, assignees=[], order=0),
        card(4, assignees=["ai_agent"], order=3, done="2026-01-01T00:00:00+00:00"),
        card(5, assignees=["ai_agent"], order=4, archived=True),
    ]
    result = _select(cards, "ai_agent")
    assert [c["id"] for c in result] == [1]


def test_select_sorted_by_order():
    cards = [card(1, order=30, assignees=["a"]), card(2, order=10, assignees=["a"])]
    assert [c["id"] for c in _select(cards, "a")] == [2, 1]


def test_select_include_done_and_unassigned():
    cards = [
        card(1, assignees=["a"], done="2026-01-01T00:00:00+00:00"),
        card(2, assignees=[]),
        card(3, assignees=["b"]),
    ]
    result = _select(cards, "a", include_done=True, include_unassigned=True)
    assert sorted(c["id"] for c in result) == [1, 2]


# ----------------------------------------------------------------------- comments


def test_comment_out_flags_agent_messages():
    user = _comment_out({"id": 1, "message": "please do X", "actorId": "eraser"})
    agent = _comment_out({"id": 2, "message": f"{AGENT_MARKER} done", "actorId": "ai_agent"})
    assert user["from_agent"] is False
    assert agent["from_agent"] is True


def test_detail_orders_comments_chronologically():
    comments = [
        {"id": 9, "message": "second", "creationDateTime": "2026-10-08T12:00:00+00:00"},
        {"id": 8, "message": "first", "creationDateTime": "2026-10-08T10:00:00+00:00"},
    ]
    out = _detail(card(1, labels=["coding"]), comments)
    assert [c["message"] for c in out["comments"]] == ["first", "second"]
    assert out["labels"] == ["coding"]


def test_brief_contains_labels_and_assignees():
    out = _brief(card(1, labels=["coding", "urgent"], assignees=["ai_agent"]))
    assert out["labels"] == ["coding", "urgent"]
    assert out["assigned_to"] == ["ai_agent"]
    assert out["done"] is False


def test_detail_lists_attachments_and_hides_deleted():
    attachments = [
        {
            "id": 1,
            "type": "deck_file",
            "data": "report.md",
            "deletedAt": 0,
            "extendedData": {"filesize": 123, "mimetype": "text/markdown"},
        },
        {"id": 2, "type": "deck_file", "data": "gone.md", "deletedAt": 1712345678},
    ]
    out = _detail(card(1, attachments=attachments, attachment_count=2), [])
    assert out["attachments"] == [
        {
            "id": 1,
            "type": "deck_file",
            "name": "report.md",
            "size": 123,
            "mimetype": "text/markdown",
        }
    ]
    assert out["attachment_count"] == 2


class FakeClient:
    def __init__(self, fail_upload=False):
        self.posted = []
        self.uploads = []
        self.fail_upload = fail_upload
        self.config = SimpleNamespace(url="https://cloud.example.com")

    def board_id(self):
        return 11

    def stack_id(self, board_id=None):
        return 49

    def add_comment(self, card_id, message, parent_id=None):
        self.posted.append({"card_id": card_id, "message": message, "parent": parent_id})
        return {"id": len(self.posted)}

    def upload_attachment(self, board_id, stack_id, card_id, filename, content):
        if self.fail_upload:
            raise DeckError("upload failed", status=500)
        self.uploads.append(
            {
                "board_id": board_id,
                "stack_id": stack_id,
                "card_id": card_id,
                "filename": filename,
                "content": content,
            }
        )
        return {"id": len(self.uploads), "type": "deck_file", "data": filename}

    def attachment_url(self, card_id, attachment_id):
        return (
            f"{self.config.url}/index.php/apps/deck/cards/{card_id}"
            f"/attachment/{attachment_id}"
        )


# --------------------------------------------------------------- publish_response


def test_publish_short_message_is_single_comment():
    client = FakeClient()
    result = publish_response(client, 162, "short result", reply_to=55)
    assert result["comments_posted"] == 1
    assert result["attachment"] is None
    assert client.uploads == []
    comment = client.posted[0]
    assert comment["message"] == f"{AGENT_MARKER} short result"
    assert comment["parent"] == 55
    assert comment["card_id"] == 162


def test_publish_does_not_double_prefix():
    client = FakeClient()
    publish_response(client, 1, f"{AGENT_MARKER} already tagged")
    assert client.posted[0]["message"] == f"{AGENT_MARKER} already tagged"


def test_publish_rejects_empty():
    with pytest.raises(ValueError):
        publish_response(FakeClient(), 1, "   ")


def test_publish_long_message_becomes_attachment():
    client = FakeClient()
    long_message = "\n".join(f"result line {i}" for i in range(200))
    result = publish_response(client, 162, long_message, reply_to=7)

    # full response went into exactly one uniquely named markdown file
    assert len(client.uploads) == 1
    upload = client.uploads[0]
    assert upload["card_id"] == 162
    assert upload["board_id"] == 11
    assert upload["stack_id"] == 49
    assert re.fullmatch(r"response-card162-\d{8}T\d{6}Z-[0-9a-f]{8}\.md", upload["filename"])
    assert upload["content"] == f"{AGENT_MARKER} {long_message}"

    # a single short comment references the file
    assert result["comments_posted"] == 1
    assert len(client.posted) == 1
    comment = client.posted[0]
    assert comment["message"].startswith(AGENT_MARKER)
    assert len(comment["message"]) <= COMMENT_MAX_LEN
    assert upload["filename"] in comment["message"]
    assert comment["parent"] == 7

    attachment = result["attachment"]
    assert attachment["filename"] == upload["filename"]
    assert attachment["id"] == 1
    assert attachment["url"].endswith("/cards/162/attachment/1")
    assert attachment["bytes"] == len(f"{AGENT_MARKER} {long_message}")


def test_publish_long_message_keeps_preexisting_marker():
    client = FakeClient()
    publish_response(client, 1, f"{AGENT_MARKER} " + "x" * 2000)
    assert client.uploads[0]["content"].startswith(f"{AGENT_MARKER} ")
    assert client.uploads[0]["content"].count(AGENT_MARKER) == 1


def test_publish_attachment_names_are_unique():
    assert attachment_filename(162) != attachment_filename(162)


def test_publish_falls_back_to_chunks_when_upload_fails():
    client = FakeClient(fail_upload=True)
    long_message = "\n".join(f"result line {i}" for i in range(200))
    result = publish_response(client, 162, long_message, reply_to=55)

    assert result["attachment"] is None
    assert "attachment upload failed" in result["note"]
    assert result["comments_posted"] == len(client.posted) > 1
    for index, comment in enumerate(client.posted):
        assert comment["message"].startswith(AGENT_MARKER)
        assert len(comment["message"]) <= COMMENT_MAX_LEN
        assert comment["parent"] == 55
        assert comment["card_id"] == 162
        if index:
            assert "continued" in comment["message"]


# ------------------------------------------------------- tool usage / error text


class ServerClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.done = {}

    def boards(self):
        return [{"id": 11, "title": "Board"}]

    def stacks(self, board_id):
        return [{"id": 49, "title": "Lane"}]

    def cards(self, board_id, stack_id):
        return [
            card(162, assignees=["u"], labels=["coding"]),
            card(163, assignees=["someone_else"]),
        ]

    def card(self, board_id, stack_id, card_id):
        return card(card_id, assignees=["u"])

    def comments(self, card_id):
        return []

    def set_card_done(self, card_id, done):
        self.done[card_id] = done
        return {"id": card_id, "done": "2026-10-08T12:00:00+00:00" if done else None}

    def attach_file(
        self, board_id, stack_id, card_id, filename, content, mime_type="text/markdown"
    ):
        self.uploads.append({"card_id": card_id, "filename": filename, "content": content})
        return {"id": len(self.uploads), "type": "deck_file", "data": filename}


def make_server():
    config = Config(
        url="https://cloud.example.com",
        username="u",
        password="p",
        board="Board",
        lane="Lane",
        board_id=11,
        lane_id=49,
        assignee=None,
    )
    return build_server(config, ServerClient())


def call_tool(name, arguments):
    return asyncio.run(make_server().call_tool(name, arguments))


def test_workflow_requires_tool_name():
    assert '"name"' in WORKFLOW
    assert "arguments" in WORKFLOW


def test_tool_descriptions_include_call_shape():
    tools = {t.name: t for t in asyncio.run(make_server().list_tools())}
    assert '{"name": "get_task", "arguments": {"card_id": 162}}' in tools["get_task"].description
    assert "Call shape" in tools["attach_files"].description


def test_validation_error_repeats_usage():
    with pytest.raises(ToolError) as info:
        call_tool("get_task", {})
    message = str(info.value)
    assert "card_id" in message
    assert "How to call the 'get_task' tool" in message
    assert '{"name": "get_task"' in message


def test_anticipated_body_error_repeats_usage():
    with pytest.raises(ToolError) as info:
        call_tool("add_comment", {"card_id": 162, "message": "   "})
    message = str(info.value)
    assert "message must not be empty" in message
    assert "How to call the 'add_comment' tool" in message
    assert '{"name": "add_comment"' in message


def test_attach_files_accepts_single_object():
    result = call_tool(
        "attach_files",
        {"card_id": 162, "files": {"filename": "a.md", "content": "A"}},
    )
    assert result.structured_content["result"][0]["filename"] == "a.md"


def test_attach_files_missing_content_repeats_usage():
    with pytest.raises(ToolError) as info:
        call_tool("attach_files", {"card_id": 162, "files": [{"filename": "a.md"}]})
    assert "files[0] is missing 'content'" in str(info.value)
    assert "How to call the 'attach_files' tool" in str(info.value)
