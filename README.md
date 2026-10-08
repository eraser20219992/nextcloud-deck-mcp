# deck-mcp

MCP server (stdio) that lets an AI agent read tasks from a Nextcloud Deck board,
solve them, post the results as comments, and mark cards done.

## Tools

| Tool | Purpose |
|------|---------|
| `ping` | Verify connectivity, board/lane lookup, count of open assigned tasks |
| `list_tasks` | Cards in the configured lane (by default only open cards assigned to the agent) |
| `get_next_task` | Next open assigned card incl. description and full comment history |
| `get_task` | One card in full (instructions, labels, assignees, attachments, comments with `from_agent` flag) |
| `add_comment` | Post the solution; if it exceeds the comment limit it is uploaded as a uniquely named `.md` attachment and referenced from a short comment |
| `add_comment` | Post the solution; long messages are split into numbered `[agent]` comments |
| `mark_done` | Set the Deck *done* flag (uses the dedicated `/cards/{id}/done` endpoint) |
| `mark_open` | Reopen a card that was closed too early |

The server ships a workflow instruction (visible to the agent as MCP server
instructions):

1. `get_next_task()` → read the description (the task) and the labels (the
   context, e.g. tag `coding` means "deliver working code").
2. Read the comments: `[agent]` comments are the agent's own previous work,
   anything else is from users. A user request posted after the agent's last
   comment is a follow-up task → answer it with `reply_to` set to that comment.
3. Do the actual work with your own tools, publish it with `add_comment()`:
   short results are posted as a comment; results longer than the 1000-char
   comment limit are uploaded as a uniquely named Markdown file attachment
   (e.g. `response-card162-20261008T161635Z-cf280140.md`) and referenced from
   a single short comment that links to it. If the upload fails, the result
   falls back to numbered `[agent]` comment chunks.
4. `mark_done()` only when everything is resolved; otherwise leave the card
   open and explain in a comment.

## Requirements

- Nextcloud with the Deck app (tested live against Deck 1.19.0 / API 1.0-1.1)
- Python ≥ 3.11 ([uv](https://docs.astral.sh/uv/) recommended)
- A dedicated Nextcloud account (or app password) for the agent
- The board must be **shared with the agent account with edit rights**, and the
  agent's lane (list) created on that board
- Only cards **assigned to the agent user** are picked up

## Setup

```sh
uv sync                      # install dependencies (creates .venv)
cp .env.example .env         # then fill in your credentials
```

`.env`:

```ini
NEXTCLOUD_URL=https://cloud.example.com
NEXTCLOUD_USERNAME=ai_agent
NEXTCLOUD_APP_PASSWORD=...       # Nextcloud -> Personal settings -> App passwords

DECK_BOARD=CMA                   # board name or numeric id
DECK_LANE=AI Agent               # lane (list) name or numeric id
#DECK_ASSIGNEE=ai_agent          # defaults to NEXTCLOUD_USERNAME
```

`.env` is gitignored; keep real credentials out of the repository.

Run the server (stdio transport):

```sh
uv run deck-mcp
```

## Registering with an MCP client

opencode (`opencode.json`):

```json
{
  "mcp": {
    "nextcloud-deck": {
      "type": "local",
      "command": ["uv", "run", "--directory", "C:/Users/devbc/Desktop/Nextcloud Deck", "deck-mcp"],
      "environment": { "NEXTCLOUD_URL": "https://cloud.example.com" }
    }
  }
}
```

Any other MCP client: run `uv run deck-mcp` as a stdio server and pass the
connection settings via environment variables (if they are not already in
`.env`, which is loaded automatically from the working directory).

## Testing

```sh
uv run pytest -q                     # unit tests (no network)
uv run python scripts/mcp_smoke.py           # live read-only test over MCP stdio
uv run python scripts/mcp_smoke.py --write   # live write test: uploads the solution
                                              # as a .md attachment, references it in
                                              # a comment, marks the card done
```

The smoke script connects to the server configured in `.env`, lists the tools,
reads the first assigned card and (with `--write`) performs one full
read → attachment/comment → `mark_done` cycle.

## Nextcloud Deck API notes (learned while building this)

- Board/stack/card reads: `GET /index.php/apps/deck/api/v1.0/...`
- Comments (read/write): `GET|POST /ocs/v2.php/apps/deck/api/v1.0/cards/{id}/comments`
  (max 1000 chars per comment → long results become attachments)
- Attachments: `GET|POST /index.php/apps/deck/api/v1.0/boards/{b}/stacks/{s}/cards/{c}/attachments`
  — upload is a **multipart** POST with a `type=deck_file` form field and a
  `file` part (JSON bodies are rejected); `GET .../attachments/{id}` streams
  the file back. That display route is `#[CORS]`, so it **rejects
  session-cookie auth** with `403 {"message":"CORS requires basic auth"}` —
  links meant for a logged-in browser therefore use the plain app route
  `GET /index.php/apps/deck/cards/{cardId}/attachment/{attachmentId}`
  (no CORS, `NoCSRFRequired`), which is what comment links point to
- All mutating requests must carry the `OCS-APIRequest: true` header, or the
  Deck routes answer `412 CSRF check failed`
- `GET .../stacks/{stackId}/cards` returns **405** — cards come embedded in the
  `GET .../boards/{id}/stacks` response instead (embedded cards carry
  `attachmentCount` but not the attachment list; single-card GET has both)
- The generic card update endpoint **rejects JSON request bodies** on Deck
  1.19 (HTTP 400); form-encoded works but easily wipes fields you omit.
  `mark_done`/`mark_open` therefore use the dedicated endpoints
  `PUT /index.php/apps/deck/cards/{id}/done` and `.../undone`, which need no
  body at all
- `done` cards have a `done` timestamp (ISO 8601); `null`/empty means open

## Project layout

```
src/deck_mcp/
  config.py      # .env loading and validation
  client.py      # DeckClient: authenticated HTTP, OCS unwrapping, endpoints
  server.py      # MCPServer, tools, workflow instructions, comment chunking
  __main__.py    # entry point (fail-fast config check, stdio run)
scripts/mcp_smoke.py   # live end-to-end test over MCP stdio
tests/                 # offline unit tests
```
