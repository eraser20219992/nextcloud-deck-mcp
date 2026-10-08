from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from mcp.server.mcpserver import MCPServer

from .client import APP_API, DeckClient, DeckError
from .config import Config

AGENT_MARKER = "[agent]"
COMMENT_MAX_LEN = 1000
CHUNK_LIMIT = 960  # leaves room for the marker / continuation prefix

WORKFLOW = """\
You are an agent working through tasks on a Nextcloud Deck board.

Workflow:
1. Call get_next_task() to get the next open task assigned to you
   (list_tasks() gives an overview of all your tasks).
2. The card description contains the task instructions - follow them.
3. Card labels/tags provide context for how to solve the task. Use them:
   a tag like "coding" means deliver working code, a tag that looks like a
   document or ticket id (e.g. ARCHI-29022) refers to material you should
   consult or cite, tags such as "urgent" or "review" change priorities.
4. Read the comments: comments starting with "[agent]" are your own previous
   work, all other comments are from users. If a user posted a request or a
   reply after your last comment, treat it as a new follow-up task: solve it
   and post your answer with reply_to set to that comment's id.
5. Do the actual work (analysis, writing, coding, research) with your own
   tools; the Deck tools only carry the instructions and the results.
6. Publish the result with add_comment(). A result longer than the comment
   limit is uploaded as a uniquely named Markdown file attachment and
   referenced from a short comment; a short result is posted as a comment
   directly.
7. Call mark_done(card_id) only when the task and every follow-up request are
   fully resolved. If anything remains open, leave the card open and say so in
   a comment (use mark_open() if you previously closed it too early).
8. If you cannot complete a task, explain why in a comment and leave the card
   open so a human can pick it up.
"""


def split_message(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Split text into chunks of at most `limit` chars, preferring line breaks."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks or [""]


def _assigned_uids(card: dict[str, Any]) -> set[str]:
    return {
        u.get("participant", {}).get("uid", "")
        for u in card.get("assignedUsers") or []
    }


def _brief(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": card.get("id"),
        "title": card.get("title"),
        "labels": [l.get("title") for l in card.get("labels") or []],
        "assigned_to": sorted(_assigned_uids(card)),
        "done": bool(card.get("done")),
        "due": card.get("duedate"),
        "order": card.get("order"),
    }


def _comment_out(comment: dict[str, Any]) -> dict[str, Any]:
    message = comment.get("message") or ""
    reply_to = comment.get("replyTo")
    return {
        "id": comment.get("id"),
        "author": comment.get("actorDisplayName") or comment.get("actorId"),
        "author_id": comment.get("actorId"),
        "created": comment.get("creationDateTime"),
        "from_agent": message.startswith(AGENT_MARKER),
        "reply_to": reply_to.get("id") if isinstance(reply_to, dict) else None,
        "message": message,
    }


def _attachment_out(attachment: dict[str, Any]) -> dict[str, Any]:
    extended = attachment.get("extendedData") or {}
    return {
        "id": attachment.get("id"),
        "type": attachment.get("type"),
        "name": attachment.get("data"),
        "size": extended.get("filesize"),
        "mimetype": extended.get("mimetype"),
    }


def _detail(card: dict[str, Any], comments: list[dict[str, Any]]) -> dict[str, Any]:
    out = _brief(card)
    out["description"] = card.get("description") or ""
    out["done_at"] = card.get("done")
    out["archived"] = bool(card.get("archived"))
    out["attachments"] = [
        _attachment_out(a)
        for a in card.get("attachments") or []
        if not a.get("deletedAt")
    ]
    out["attachment_count"] = int(card.get("attachmentCount") or 0)
    ordered = sorted(
        comments,
        key=lambda c: (str(c.get("creationDateTime") or ""), int(c.get("id") or 0)),
    )
    out["comments"] = [_comment_out(c) for c in ordered]
    return out


def _select(
    cards: list[dict[str, Any]],
    agent_uid: str,
    *,
    include_done: bool = False,
    include_unassigned: bool = False,
) -> list[dict[str, Any]]:
    selected = []
    for card in cards:
        if card.get("deletedAt") or card.get("archived"):
            continue
        if not include_done and card.get("done"):
            continue
        assigned = _assigned_uids(card)
        if agent_uid not in assigned:
            if not (include_unassigned and not assigned):
                continue
        selected.append(card)
    selected.sort(key=lambda c: (c.get("order") is None, c.get("order") or 0))
    return selected


def attachment_filename(card_id: int, now: datetime | None = None) -> str:
    """Unique, human-readable name for a response attachment."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"response-card{card_id}-{stamp}-{uuid4().hex[:8]}.md"


def _as_agent_comment(message: str) -> str:
    return message if message.startswith(AGENT_MARKER) else f"{AGENT_MARKER} {message}"


def _check_comment_len(body: str) -> None:
    if len(body) > COMMENT_MAX_LEN:
        raise ValueError(
            f"comment of {len(body)} chars exceeds the {COMMENT_MAX_LEN} char limit"
        )


def _post_chunks(
    client: DeckClient,
    card_id: int,
    message: str,
    reply_to: int | None,
) -> list[dict[str, Any]]:
    chunks = split_message(message)
    total = len(chunks)
    posted = []
    for index, part in enumerate(chunks):
        if index == 0:
            body = _as_agent_comment(part)
        else:
            body = f"{AGENT_MARKER} (continued {index + 1}/{total}) {part}"
        _check_comment_len(body)
        posted.append(client.add_comment(card_id, body, parent_id=reply_to))
    return posted


def publish_response(
    client: DeckClient,
    card_id: int,
    message: str,
    reply_to: int | None = None,
) -> dict[str, Any]:
    """Publish a result on a card.

    Short results are posted as a single '[agent]' comment. Results longer
    than COMMENT_MAX_LEN are uploaded as a uniquely named Markdown file
    attachment and referenced from a short comment; if the upload fails,
    they fall back to numbered '[agent]' comment chunks.
    """
    if not message.strip():
        raise ValueError("message must not be empty")
    body = _as_agent_comment(message)

    if len(body) <= COMMENT_MAX_LEN:
        comment = client.add_comment(card_id, body, parent_id=reply_to)
        return {
            "card_id": card_id,
            "comments_posted": 1,
            "comment_ids": [comment.get("id")],
            "reply_to": reply_to,
            "attachment": None,
        }

    filename = attachment_filename(card_id)
    attachment: dict[str, Any] | None = None
    upload_error: str | None = None
    try:
        board_id = client.board_id()
        stack_id = client.stack_id(board_id)
        uploaded = client.upload_attachment(board_id, stack_id, card_id, filename, body)
        attachment_id = uploaded.get("id")
        if attachment_id is None:
            raise DeckError("attachment upload returned no id")
        attachment = {
            "id": attachment_id,
            "filename": filename,
            "type": uploaded.get("type"),
            "bytes": len(body),
            "url": client.attachment_url(card_id, attachment_id),
        }
    except DeckError as exc:
        upload_error = str(exc)

    if attachment is not None:
        reference = _as_agent_comment(
            f"The full response ({attachment['bytes']} chars) is too long for a "
            f"comment and is attached as [{filename}]({attachment['url']}). "
            f"Open the attachment to read it."
        )
        _check_comment_len(reference)
        comment = client.add_comment(card_id, reference, parent_id=reply_to)
        return {
            "card_id": card_id,
            "comments_posted": 1,
            "comment_ids": [comment.get("id")],
            "reply_to": reply_to,
            "attachment": attachment,
        }

    posted = _post_chunks(client, card_id, message, reply_to)
    return {
        "card_id": card_id,
        "comments_posted": len(posted),
        "comment_ids": [c.get("id") for c in posted],
        "reply_to": reply_to,
        "attachment": None,
        "note": f"attachment upload failed, posted as comment chunks ({upload_error})",
    }


def build_server(config: Config, client: DeckClient) -> MCPServer:
    mcp = MCPServer("nextcloud-deck", instructions=WORKFLOW)

    def _lane_cards() -> list[dict[str, Any]]:
        board_id = client.board_id()
        return client.cards(board_id, client.stack_id(board_id))

    @mcp.tool()
    def ping() -> dict[str, Any]:
        """Verify the connection and configuration: resolves board and lane,
        checks authentication and counts open tasks assigned to you."""
        board_id = client.board_id()
        stack_id = client.stack_id(board_id)
        boards = {b["id"]: b for b in client.boards()}
        stacks = {s["id"]: s for s in client.stacks(board_id)}
        open_tasks = _select(_lane_cards(), config.agent_uid)
        return {
            "connected": True,
            "server": config.url,
            "user": config.username,
            "board": {"id": board_id, "title": boards.get(board_id, {}).get("title")},
            "lane": {"id": stack_id, "title": stacks.get(stack_id, {}).get("title")},
            "agent_uid": config.agent_uid,
            "open_assigned_tasks": len(open_tasks),
        }

    @mcp.tool()
    def list_tasks(
        include_done: bool = False, include_unassigned: bool = False
    ) -> list[dict[str, Any]]:
        """List the cards in the configured lane. By default only open cards
        assigned to the agent are returned; labels and assignees are included
        so you can judge the type of work before picking a task."""
        return [_brief(c) for c in _select(
            _lane_cards(),
            config.agent_uid,
            include_done=include_done,
            include_unassigned=include_unassigned,
        )]

    @mcp.tool()
    def get_next_task() -> dict[str, Any]:
        """Return the next open task assigned to you, including its full
        description (instructions), attachments and the whole comment
        history. Returns {"task": null} when no open task remains."""
        open_tasks = _select(_lane_cards(), config.agent_uid)
        if not open_tasks:
            return {"task": None, "message": "No open tasks assigned to you."}
        board_id = client.board_id()
        stack_id = client.stack_id(board_id)
        # Full card fetch: the stacks list embeds cards without attachments.
        card = client.card(board_id, stack_id, int(open_tasks[0]["id"]))
        detail = _detail(card, client.comments(int(card["id"])))
        return {"task": detail}

    @mcp.tool()
    def get_task(card_id: int) -> dict[str, Any]:
        """Fetch one card in full: description (task instructions), labels,
        assignees, attachments and all comments in chronological order.
        Comments tagged with from_agent=true were written by you, others by
        users - read them to detect follow-up requests."""
        board_id = client.board_id()
        stack_id = client.stack_id(board_id)
        card = client.card(board_id, stack_id, card_id)
        return _detail(card, client.comments(card_id))

    @mcp.tool()
    def add_comment(
        card_id: int, message: str, reply_to: int | None = None
    ) -> dict[str, Any]:
        """Post your result or an answer as a comment on the card. If the
        result exceeds the comment length limit it is uploaded as a uniquely
        named Markdown file attachment instead and referenced from a short
        comment (the response reports the attachment name and url). Set
        reply_to to a comment id to answer a user in their thread."""
        if not message.strip():
            raise ValueError("message must not be empty")
        return publish_response(client, card_id, message, reply_to)

    @mcp.tool()
    def mark_done(card_id: int) -> dict[str, Any]:
        """Mark a card as completed (sets the Deck 'done' flag). Only call
        this after the task and all follow-up requests are resolved."""
        card = client.set_card_done(card_id, True)
        return {
            "card_id": card_id,
            "done": True,
            "done_at": card.get("done"),
        }

    @mcp.tool()
    def mark_open(card_id: int) -> dict[str, Any]:
        """Reopen a card that was marked as done by mistake."""
        client.set_card_done(card_id, False)
        return {"card_id": card_id, "done": False}

    return mcp
