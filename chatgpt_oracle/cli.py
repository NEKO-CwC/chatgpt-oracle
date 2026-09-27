from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from .browser import ChatGPTBrowser
from .config import Settings


async def _browser_setup(settings: Settings) -> int:
    browser = ChatGPTBrowser(settings)
    await browser.start()
    page = await browser._page()
    print(f"专用 ChatGPT 浏览器已启动：{settings.browser_profile_dir}")
    print(f"当前页面：{page.url}")
    print("请在该窗口完成登录；完成后按 Enter 让初始化命令检查登录状态。")
    try:
        await asyncio.to_thread(input)
    except EOFError:
        # Non-interactive runners cannot wait on stdin. The standard Chrome
        # process stays alive, so the user can finish login and run the status
        # check again from a normal terminal.
        print("当前终端没有可用的 stdin；请完成浏览器登录后重新运行 browser-status。")
    status = await browser.browser_status()
    print(status)
    await browser.close()
    return 0 if status["logged_in"] else 2


async def _browser_status(settings: Settings) -> int:
    browser = ChatGPTBrowser(settings)
    try:
        status = await browser.browser_status()
        print(status)
        return 0 if status["logged_in"] else 2
    finally:
        await browser.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chatgpt-oracle")
    parser.add_argument(
        "command",
        choices=["serve", "browser-setup", "browser-status", "config"],
        nargs="?",
        default="serve",
    )
    parser.add_argument("--transport", choices=["stdio", "http", "streamable-http"])
    args = parser.parse_args(argv)
    if args.transport:
        os.environ["CHATGPT_ORACLE_TRANSPORT"] = args.transport
    settings = Settings.from_env()
    if args.command == "browser-setup":
        return asyncio.run(_browser_setup(settings))
    if args.command == "browser-status":
        return asyncio.run(_browser_status(settings))
    if args.command == "config":
        print({
            "root": str(settings.root),
            "jobs_dir": str(settings.jobs_dir),
            "browser_profile_dir": str(settings.browser_profile_dir),
            "host": settings.host,
            "port": settings.port,
            "transport": settings.transport,
            "mcp_path": settings.mcp_path,
            "headless": settings.browser_headless,
        })
        return 0
    from .server import run

    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
