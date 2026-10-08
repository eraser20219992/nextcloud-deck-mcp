from __future__ import annotations

from typing import Any

import httpx

from .config import Config

# Verified against a live Nextcloud 1.19 server: board/stack/card endpoints only
# answer on the plain app route, while the comment endpoints only answer on the
# OCS route (the OCS route returns error 998 for /stacks).
APP_API = "/index.php/apps/deck/api/v1.0"
OCS_API = "/ocs/v2.php/apps/deck/api/v1.0"
INTERNAL_API = "/index.php/apps/deck"


class DeckError(RuntimeError):
    """Raised when the Nextcloud Deck API returns an error."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300] or f"HTTP {response.status_code}"
    if isinstance(body, dict):
        ocs = body.get("ocs")
        if isinstance(ocs, dict):
            meta = ocs.get("meta") or {}
            return str(meta.get("message") or meta.get("statuscode") or body)
        for key in ("message", "error", "status"):
            if key in body:
                return str(body[key])
    return str(body)


class DeckClient:
    """Thin synchronous client for the Nextcloud Deck REST API."""

    def __init__(self, config: Config, transport: httpx.BaseTransport | None = None):
        self.config = config
        self._http = httpx.Client(
            base_url=config.url,
            auth=(config.username, config.password),
            headers={
                "OCS-APIRequest": "true",
                "Accept": "application/json",
                # No default Content-Type: httpx sets application/json or
                # multipart/form-data per request depending on the payload.
            },
            timeout=30.0,
            follow_redirects=True,
            transport=transport,
        )
        self._board_id: int | None = config.board_id
        self._stack_id: int | None = config.lane_id

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ http

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        ocs: bool = False,
        internal: bool = False,
    ) -> Any:
        if ocs:
            base = OCS_API
        elif internal:
            base = INTERNAL_API
        else:
            base = APP_API
        url = f"{base}{path}"
        try:
            response = self._http.request(method, url, json=json_body)
        except httpx.HTTPError as exc:
            raise DeckError(f"Request to {url} failed: {exc}") from exc

        if response.status_code >= 400:
            raise DeckError(
                f"{method} {url} -> HTTP {response.status_code}: "
                f"{_error_detail(response)}",
                status=response.status_code,
            )
        try:
            body = response.json()
        except ValueError:
            raise DeckError(
                f"{method} {url} returned a non-JSON response: "
                f"{response.text[:200]}"
            ) from None

        if isinstance(body, dict) and "ocs" in body:
            ocs_body = body["ocs"]
            meta = ocs_body.get("meta") or {}
            statuscode = int(meta.get("statuscode") or 200)
            failed = meta.get("status") == "failure" or statuscode >= 400
            if failed:
                raise DeckError(
                    f"{method} {url} -> OCS error {statuscode}: "
                    f"{meta.get('message', 'unknown error')}",
                    status=statuscode,
                )
            return ocs_body.get("data")
        return body

    # --------------------------------------------------------------- endpoints

    def boards(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/boards")
        return [b for b in (data or []) if not b.get("deletedAt")]

    def stacks(self, board_id: int) -> list[dict[str, Any]]:
        data = self._request("GET", f"/boards/{board_id}/stacks")
        return [s for s in (data or []) if not s.get("deletedAt")]

    def cards(self, board_id: int, stack_id: int) -> list[dict[str, Any]]:
        # The card collection endpoint does not exist (405 on live servers) and
        # the single-stack endpoint embeds cards without their labels, so the
        # stacks list is the only response that carries cards with labels.
        stacks = self.stacks(board_id)
        for stack in stacks:
            if int(stack.get("id") or -1) == stack_id:
                return stack.get("cards") or []
        raise DeckError(f"Lane/stack {stack_id} not found on board {board_id}")

    def card(self, board_id: int, stack_id: int, card_id: int) -> dict[str, Any]:
        data = self._request(
            "GET", f"/boards/{board_id}/stacks/{stack_id}/cards/{card_id}"
        )
        if not isinstance(data, dict):
            raise DeckError(f"Card {card_id} not found")
        return data

    def set_card_done(self, card_id: int, done: bool) -> dict[str, Any]:
        """Set or clear the Deck 'done' flag of a card.

        Uses the dedicated /cards/{id}/done and /cards/{id}/undone endpoints
        (verified live on Deck 1.19): the generic card-update endpoint rejects
        JSON request bodies on this server.
        """
        action = "done" if done else "undone"
        data = self._request("PUT", f"/cards/{card_id}/{action}", internal=True)
        return data if isinstance(data, dict) else {}

    def comments(self, card_id: int, page_size: int = 50) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        offset = 0
        while offset < 500:
            page = self._request(
                "GET",
                f"/cards/{card_id}/comments?limit={page_size}&offset={offset}",
                ocs=True,
            )
            collected.extend(page or [])
            if len(page or []) < page_size:
                break
            offset += page_size
        return collected

    def add_comment(
        self,
        card_id: int,
        message: str,
        parent_id: int | None = None,
    ) -> dict[str, Any]:
        data = self._request(
            "POST",
            f"/cards/{card_id}/comments",
            json_body={"message": message, "parentId": parent_id},
            ocs=True,
        )
        return data if isinstance(data, dict) else {}

    def attachments(
        self, board_id: int, stack_id: int, card_id: int
    ) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            f"/boards/{board_id}/stacks/{stack_id}/cards/{card_id}/attachments",
        )
        return data if isinstance(data, list) else []

    def upload_attachment(
        self,
        board_id: int,
        stack_id: int,
        card_id: int,
        filename: str,
        content: str,
        mime_type: str = "text/markdown",
    ) -> dict[str, Any]:
        """Upload `content` to a card as a `deck_file` attachment.

        Multipart POST (the Deck API rejects JSON bodies for uploads);
        verified live on Deck 1.19. The file shows up in the card's
        attachment list and can be fetched via `attachments()`.
        """
        url = (
            f"{APP_API}/boards/{board_id}/stacks/{stack_id}"
            f"/cards/{card_id}/attachments"
        )
        try:
            response = self._http.post(
                url,
                data={"type": "deck_file"},
                files={"file": (filename, content.encode("utf-8"), mime_type)},
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise DeckError(f"Attachment upload to {url} failed: {exc}") from exc
        if response.status_code >= 400:
            raise DeckError(
                f"POST {url} -> HTTP {response.status_code}: "
                f"{_error_detail(response)}",
                status=response.status_code,
            )
        try:
            body = response.json()
        except ValueError:
            raise DeckError(
                f"POST {url} returned a non-JSON response: {response.text[:200]}"
            ) from None
        return body if isinstance(body, dict) else {}

    def attachment_url(self, card_id: int, attachment_id: int) -> str:
        """Browser-friendly download URL for an attachment.

        Uses the plain app route, not the API route: the API display route is
        #[CORS] and therefore rejects session-cookie auth with
        'CORS requires basic auth', while this route accepts the logged-in
        web session (NoCSRFRequired, no CORS). attachmentId may be a plain
        id for deck_file attachments.
        """
        return (
            f"{self.config.url}{INTERNAL_API}/cards/{card_id}"
            f"/attachment/{attachment_id}"
        )

    # -------------------------------------------------------------- resolution

    @staticmethod
    def _match_title(
        items: list[dict[str, Any]], title: str, kind: str
    ) -> dict[str, Any]:
        exact = [i for i in items if i.get("title") == title]
        if not exact:
            exact = [
                i
                for i in items
                if str(i.get("title", "")).casefold() == title.casefold()
            ]
        if not exact:
            available = ", ".join(sorted(str(i.get("title")) for i in items)) or "(none)"
            raise DeckError(
                f"No {kind} named {title!r} found. Available: {available}"
            )
        if len(exact) > 1:
            raise DeckError(
                f"Multiple {kind}s named {title!r}; use the numeric id instead"
            )
        return exact[0]

    def board_id(self) -> int:
        if self._board_id is None:
            board = self._match_title(self.boards(), self.config.board, "board")
            self._board_id = int(board["id"])
        return self._board_id

    def stack_id(self, board_id: int | None = None) -> int:
        if self._stack_id is None:
            bid = board_id if board_id is not None else self.board_id()
            stack = self._match_title(self.stacks(bid), self.config.lane, "lane/list")
            self._stack_id = int(stack["id"])
        return self._stack_id
