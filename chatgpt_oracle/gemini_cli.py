"""Dedicated Gemini entry point; never loads the ChatGPT broker configuration."""
from __future__ import annotations

import argparse
import asyncio
import json
import os

from .config import Settings


def main(argv=None):
    parser = argparse.ArgumentParser(prog="gemini-oracle")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "browser-status", "config"])
    parser.add_argument("--transport", choices=["stdio", "http", "streamable-http"])
    args = parser.parse_args(argv)
    os.environ["ORACLE_PROVIDER"] = "gemini"
    if args.transport:
        os.environ["GEMINI_ORACLE_TRANSPORT"] = args.transport
    if args.command == "serve":
        from .server import run
        run()
        return 0
    settings = Settings.from_env(provider="gemini")
    if args.command == "config":
        print(json.dumps({"provider": "gemini", "root": str(settings.root),
                          "jobs_dir": str(settings.jobs_dir), "profile_dir": str(settings.browser_profile_dir),
                          "cdp_url": settings.browser_cdp_url, "port": settings.port,
                          "transport": settings.transport}, ensure_ascii=False))
        return 0
    from .gemini_browser import GeminiBrowser

    async def status():
        browser = GeminiBrowser(settings)
        try:
            value = await browser.browser_status()
            print(json.dumps(value, ensure_ascii=False))
            return 0 if value["logged_in"] else 2
        finally:
            await browser.close()
    return asyncio.run(status())


if __name__ == "__main__":
    raise SystemExit(main())
