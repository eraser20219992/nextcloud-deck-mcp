"""Live test for the deck-mcp server over a real MCP stdio connection.

Read-only by default; pass --write to also publish the solution (uploaded as
a .md attachment + reference comment because it exceeds the comment limit)
and mark the test card as done.

    uv run python scripts/mcp_smoke.py
    uv run python scripts/mcp_smoke.py --write
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CARD_ID = 162

SOLUTION = """\
Solution for the task in this card: a Python script that generates the
Fibonacci number series (the card carries the label "coding", so the deliverable
is code).

```python
#!/usr/bin/env python3
\"\"\"Generate the Fibonacci number series.\"\"\"

def fibonacci(count: int) -> list[int]:
    \"\"\"Return the first `count` Fibonacci numbers: 0, 1, 1, 2, 3, 5, ...\"\"\"
    if count <= 0:
        return []
    series: list[int] = []
    a, b = 0, 1
    for _ in range(count):
        series.append(a)
        a, b = b, a + b
    return series


if __name__ == "__main__":
    import sys

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    print(fibonacci(n))
```

Usage:
- `python fibonacci.py` prints the first 10 numbers: [0, 1, 1, 2, 3, 5, 8, 13, 21, 34]
- `python fibonacci.py 20` prints the first 20 numbers
- `fibonacci(0)` and negative input return an empty list instead of failing

Notes:
- Iterative generation, O(n) time and O(n) memory for the returned list.
- For very large sequences use a generator instead of building the full list:

```python
def fibonacci_gen():
    a, b = 0, 1
    while True:
        yield a
        a, b = b, a + b
```

Verified: `fibonacci(10) == [0, 1, 1, 2, 3, 5, 8, 13, 21, 34]`.
"""


def _payload(result) -> dict:
    data: dict = {
        "isError": bool(
            getattr(result, "is_error", False) or getattr(result, "isError", False)
        )
    }
    structured = getattr(result, "structured_content", None) or getattr(
        result, "structuredContent", None
    )
    if structured is not None:
        data["structured"] = structured
    texts = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text is None:
            continue
        try:
            texts.append(json.loads(text))
        except (json.JSONDecodeError, ValueError):
            texts.append(text)
    data["content"] = texts
    return data


def _show(label: str, data: dict) -> None:
    rendered = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    print(f"\n=== {label} ===\n{rendered}")


async def main(write: bool) -> int:
    params = StdioServerParameters(
        command="uv",
        args=["run", "deck-mcp"],
        env=dict(os.environ),
    )
    async with stdio_client(params) as (read, write_stream):
        async with ClientSession(read, write_stream) as session:
            await session.initialize()

            tools = await session.list_tools()
            _show("tools", {"names": [t.name for t in tools.tools]})

            for label, tool, args in [
                ("ping", "ping", {}),
                ("list_tasks", "list_tasks", {}),
                ("get_task(162)", "get_task", {"card_id": CARD_ID}),
                ("get_next_task", "get_next_task", {}),
            ]:
                result = await session.call_tool(tool, args)
                data = _payload(result)
                _show(label, data)
                if data["isError"]:
                    return 1

            if not write:
                print("\nRead-only run complete. Re-run with --write to post the "
                      "solution and mark the card done.")
                return 0

            result = await session.call_tool(
                "add_comment", {"card_id": CARD_ID, "message": SOLUTION}
            )
            _show("add_comment (solution)", _payload(result))

            result = await session.call_tool("mark_done", {"card_id": CARD_ID})
            _show("mark_done", _payload(result))
            if _payload(result)["isError"]:
                return 1

            result = await session.call_tool("get_task", {"card_id": CARD_ID})
            data = _payload(result)
            _show("get_task after mark_done", data)

            result = await session.call_tool("list_tasks", {})
            _show("list_tasks after mark_done (162 must be gone)", _payload(result))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    cli = parser.parse_args()
    raise SystemExit(asyncio.run(main(cli.write)))
