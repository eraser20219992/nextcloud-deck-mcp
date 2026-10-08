from __future__ import annotations

import functools
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

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

How to format every tool call (read carefully - malformed calls are rejected
before they reach this server):
- A tool call MUST be a JSON object shaped like
  {"name": "<tool name>", "arguments": { ... }}.
  The "name" field is mandatory and must be exactly one of the tool names
  used below (ping, list_tasks, get_next_task, get_task, add_comment,
  attach_file, attach_files, mark_done, mark_open). A call that omits "name",
  or sends a bare arguments object with no tool name, is invalid.
- Put ALL inputs inside "arguments" as a JSON object keyed by the argument
  names documented in each tool's description. Never place arguments next to
  "name" at the top level, and never invent argument names.
- Respect the documented types: card and comment ids are integers (write
  162, not "162"); message/filename/content are strings. Omit optional
  arguments you do not need instead of sending null.
- Each tool description ends with the exact call shape and an example. If a
  call fails, the error response repeats that tool's usage description: read
  it, correct the call, and retry.
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


def _with_usage(registry: dict[str, str]) -> Any:
    """Wrap a tool so anticipated failures reach the model as a `ToolError`.

    The SDK turns any other exception into a generic crash message, so
    `ValueError`/`DeckError` raised by a tool body (e.g. invalid arguments)
    would otherwise be swallowed. The tool's description is recorded in
    `registry` so `DeckMCPServer.call_tool` can append it to every error,
    letting the agent re-read the correct usage and self-correct.
    """

    def decorator(fn: Any) -> Any:
        registry[fn.__name__] = (fn.__doc__ or "").strip()

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except ToolError:
                raise
            except (ValueError, DeckError) as exc:
                raise ToolError(str(exc)) from None

        return wrapper

    return decorator


_USAGE_HEADER = "--- How to call the '{name}' tool (correct usage) ---"


class DeckMCPServer(MCPServer):
    """`MCPServer` that appends a tool's usage description to its errors.

    Every failed call (argument validation or an anticipated tool error)
    carries a trailing copy of that tool's description, so a model that
    produced a malformed call can see the expected shape and retry.
    """

    def __init__(
        self, *args: Any, tool_usage: dict[str, str] | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self._tool_usage = tool_usage if tool_usage is not None else {}

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Any = None
    ) -> Any:
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as exc:
            raise ToolError(self._append_usage(name, exc)) from exc

    def _append_usage(self, name: str, exc: ToolError) -> str:
        message = str(exc)
        usage = self._tool_usage.get(name)
        if not usage:
            return message
        header = _USAGE_HEADER.format(name=name)
        if header in message:
            return message
        return f"{message}\n\n{header}\n{usage}"


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
        # prefer attach_file if available (handles bytes/str), fallback to upload_attachment
        if hasattr(client, "attach_file"):
            uploaded = client.attach_file(board_id, stack_id, card_id, filename, body)
        else:
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
    tool_usage: dict[str, str] = {}
    mcp = DeckMCPServer("nextcloud-deck", instructions=WORKFLOW, tool_usage=tool_usage)

    def _lane_cards() -> list[dict[str, Any]]:
        board_id = client.board_id()
        return client.cards(board_id, client.stack_id(board_id))

    @mcp.tool()
    @_with_usage(tool_usage)
    def ping() -> dict[str, Any]:
        """Verify the connection and configuration: resolves board and lane,
        checks authentication and counts open tasks assigned to you.

        Arguments: none.
        Call shape: {"name": "ping", "arguments": {}}
        Example: {"name": "ping", "arguments": {}}"""
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
    @_with_usage(tool_usage)
    def list_tasks(
        include_done: bool = False, include_unassigned: bool = False
    ) -> list[dict[str, Any]]:
        """List the cards in the configured lane. By default only open cards
        assigned to you are returned; labels and assignees are included so you
        can judge the type of work before picking a task.

        Arguments (all optional, omit to use the default):
          include_done (boolean, default false): also return completed cards.
          include_unassigned (boolean, default false): also return open cards
            that have no assignee.
        Call shape: {"name": "list_tasks", "arguments": {"include_done": false, "include_unassigned": false}}
        Example: {"name": "list_tasks", "arguments": {}}"""
        return [_brief(c) for c in _select(
            _lane_cards(),
            config.agent_uid,
            include_done=include_done,
            include_unassigned=include_unassigned,
        )]

    @mcp.tool()
    @_with_usage(tool_usage)
    def get_next_task() -> dict[str, Any]:
        """Return the next open task assigned to you, including its full
        description (instructions), attachments and the whole comment
        history. Returns {"task": null} when no open task remains.

        Arguments: none.
        Call shape: {"name": "get_next_task", "arguments": {}}
        Example: {"name": "get_next_task", "arguments": {}}"""
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
    @_with_usage(tool_usage)
    def get_task(card_id: int) -> dict[str, Any]:
        """Fetch one card in full: description (task instructions), labels,
        assignees, attachments and all comments in chronological order.
        Comments tagged with from_agent=true were written by you, others by
        users - read them to detect follow-up requests.

        Arguments:
          card_id (integer, required): the card id as returned by list_tasks,
            get_next_task or get_task. Write it as a number, e.g. 162.
        Call shape: {"name": "get_task", "arguments": {"card_id": 162}}
        Example: {"name": "get_task", "arguments": {"card_id": 162}}"""
        board_id = client.board_id()
        stack_id = client.stack_id(board_id)
        card = client.card(board_id, stack_id, card_id)
        return _detail(card, client.comments(card_id))

    @mcp.tool()
    @_with_usage(tool_usage)
    def add_comment(
        card_id: int, message: str, reply_to: int | None = None
    ) -> dict[str, Any]:
        """Post your result or an answer as a comment on the card. If the
        result exceeds the comment length limit it is uploaded as a uniquely
        named Markdown file attachment instead and referenced from a short
        comment (the response reports the attachment name and url). Set
        reply_to to a comment id to answer a user in their thread.

        Arguments:
          card_id (integer, required): the card to comment on, e.g. 162.
          message (string, required): the comment text or your full result.
            Must not be empty.
          reply_to (integer, optional): the id of the comment you are
            answering, to keep the reply in that thread. Omit for a new
            top-level comment.
        Call shape: {"name": "add_comment", "arguments": {"card_id": 162, "message": "text", "reply_to": 55}}
        Example: {"name": "add_comment", "arguments": {"card_id": 162, "message": "Done: ..."}}"""
        if not message.strip():
            raise ValueError("message must not be empty")
        return publish_response(client, card_id, message, reply_to)

    @mcp.tool()
    @_with_usage(tool_usage)
    def attach_file(
        card_id: int,
        filename: str,
        content: str,
        mime_type: str = "text/markdown",
    ) -> dict[str, Any]:
        """Attach one file to a card (as a deck_file attachment). Returns
        attachment metadata including id, filename, type and url. Use this for
        extra files such as code or reports; use add_comment for the main
        answer.

        Arguments:
          card_id (integer, required): the card to attach the file to, e.g. 162.
          filename (string, required): the file name including extension,
            e.g. "report.md".
          content (string, required): the full text content of the file. Send
            the text itself, not a path or a base64 blob.
          mime_type (string, optional, default "text/markdown"): MIME type of
            the content, e.g. "text/plain" or "application/json".
        Call shape: {"name": "attach_file", "arguments": {"card_id": 162, "filename": "report.md", "content": "# Title", "mime_type": "text/markdown"}}
        Example: {"name": "attach_file", "arguments": {"card_id": 162, "filename": "report.md", "content": "# Report"}}"""
        board_id = client.board_id()
        stack_id = client.stack_id(board_id)
        if hasattr(client, "attach_file"):
            uploaded = client.attach_file(board_id, stack_id, card_id, filename, content, mime_type)
        else:
            uploaded = client.upload_attachment(board_id, stack_id, card_id, filename, content, mime_type)
        attachment_id = uploaded.get("id")
        if attachment_id is None:
            raise DeckError("attachment upload returned no id")
        return {
            "id": attachment_id,
            "filename": filename,
            "type": uploaded.get("type"),
            "url": client.attachment_url(card_id, attachment_id),
        }

    @mcp.tool()
    @_with_usage(tool_usage)
    def attach_files(
        card_id: int,
        files: list[dict[str, Any]] | dict[str, Any] | None = None,
        filename: str | None = None,
        content: str | None = None,
        mime_type: str = "text/markdown",
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Attach several files to a card in one call, or a single file. Use
        this instead of attach_file when attaching more than one file.

        Arguments:
          card_id (integer, required): the card to attach the files to, e.g. 162.
          files (array, optional): the list of files to attach. Each entry is
            an object with a "filename" (string, e.g. "report.md") and a
            "content" (string, the full text of the file); an optional
            "mime_type" (string, default "text/markdown") may be given. Do NOT
            JSON-encode file names or contents and do NOT pass a single object
            at the top level.
          filename (string, optional): single-file alternative to "files".
          content (string, optional): single-file alternative to "files".
          mime_type (string, optional, default "text/markdown"): MIME type for
            the single-file form.

        Provide EITHER "files", OR both "filename" and "content".
        Call shape: {"name": "attach_files", "arguments": {"card_id": 162, "files": [{"filename": "a.md", "content": "A"}]}}
        Example: {"name": "attach_files", "arguments": {"card_id": 162, "files": [{"filename": "report.md", "content": "# Report"}, {"filename": "data.json", "content": "{}", "mime_type": "application/json"}]}}"""
        board_id = client.board_id()
        stack_id = client.stack_id(board_id)
        if isinstance(files, dict):
            files = [files]
        results = []
        if files:
            if not isinstance(files, list):
                raise ValueError(
                    "'files' must be a list of {filename, content} objects"
                )
            for index, f in enumerate(files):
                if not isinstance(f, dict):
                    raise ValueError(
                        f"files[{index}] must be an object with 'filename' and "
                        f"'content', got {type(f).__name__}"
                    )
                fname = f.get("filename") or f.get("name") or "attachment"
                fcontent = f.get("content")
                if fcontent is None:
                    fcontent = f.get("data")
                if fcontent is None:
                    raise ValueError(
                        f"files[{index}] is missing 'content'"
                    )
                fmime = f.get("mime_type") or f.get("mimetype") or "text/markdown"
                if hasattr(client, "attach_file"):
                    uploaded = client.attach_file(board_id, stack_id, card_id, fname, fcontent, fmime)
                else:
                    uploaded = client.upload_attachment(board_id, stack_id, card_id, fname, fcontent, fmime)
                aid = uploaded.get("id")
                if aid is None:
                    raise DeckError("attachment upload returned no id")
                results.append({
                    "id": aid,
                    "filename": fname,
                    "type": uploaded.get("type"),
                    "url": client.attachment_url(card_id, aid),
                })
            return results
        if filename is not None and content is not None:
            return attach_file(card_id, filename, content, mime_type)
        raise ValueError(
            "'files' was not provided; to attach one file pass both 'filename' "
            "and 'content'"
        )

    # aliases
    add_attachment = attach_file
    add_attachments = attach_files

    @mcp.tool()
    @_with_usage(tool_usage)
    def mark_done(card_id: int) -> dict[str, Any]:
        """Mark a card as completed (sets the Deck 'done' flag). Only call
        this after the task and all follow-up requests are resolved.

        Arguments:
          card_id (integer, required): the card to complete, e.g. 162.
        Call shape: {"name": "mark_done", "arguments": {"card_id": 162}}
        Example: {"name": "mark_done", "arguments": {"card_id": 162}}"""
        card = client.set_card_done(card_id, True)
        return {
            "card_id": card_id,
            "done": True,
            "done_at": card.get("done"),
        }

    @mcp.tool()
    @_with_usage(tool_usage)
    def mark_open(card_id: int) -> dict[str, Any]:
        """Reopen a card that was marked as done by mistake.

        Arguments:
          card_id (integer, required): the card to reopen, e.g. 162.
        Call shape: {"name": "mark_open", "arguments": {"card_id": 162}}
        Example: {"name": "mark_open", "arguments": {"card_id": 162}}"""
        client.set_card_done(card_id, False)
        return {"card_id": card_id, "done": False}

    return mcp
