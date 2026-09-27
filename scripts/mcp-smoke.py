from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def main() -> None:
    root = Path(__file__).resolve().parents[1]
    python = root / ".venv" / "Scripts" / "python.exe"
    params = StdioServerParameters(
        command=str(python),
        args=["-m", "chatgpt_oracle.server"],
        env={
            **os.environ,
            "CHATGPT_ORACLE_ROOT": str(root),
            "CHATGPT_ORACLE_TRANSPORT": "stdio",
            "PYTHONPATH": str(root),
        },
    )
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            tools = await session.list_tools()
            health = await session.call_tool("chatgpt_health", {})
            print(json.dumps({
                "tools": [tool.name for tool in tools.tools],
                "health": [item.text for item in health.content if hasattr(item, "text")],
            }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

