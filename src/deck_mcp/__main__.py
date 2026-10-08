from __future__ import annotations

import sys

from .client import DeckClient, DeckError
from .config import ConfigError, load_config
from .server import build_server


def main() -> None:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"deck-mcp configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    server = build_server(config, DeckClient(config))
    try:
        server.run()
    except DeckError as exc:
        print(f"deck-mcp API error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
