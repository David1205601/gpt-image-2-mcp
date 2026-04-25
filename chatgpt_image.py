from __future__ import annotations

import argparse
import asyncio
import json
import sys

from chatgpt_image_mcp.browser import LoginRequired, generate, login_interactive, status
from chatgpt_image_mcp.daemon import (
    DEFAULT_DAEMON_HOST,
    DEFAULT_DAEMON_PORT,
    DaemonUnavailable,
    daemon_status,
    run_browser_daemon,
)


def _print_json(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate images through the ChatGPT Images 2.0 browser-controlled MCP server.")
    sub = parser.add_subparsers(dest="command")

    login_parser = sub.add_parser("login", help="Open a fresh browser and wait until ChatGPT is logged in.")
    login_parser.add_argument("--force", action="store_true", help="Accepted for compatibility; sessions are never reused.")
    sub.add_parser("status", help="Show the current browser-session mode.")

    generate_parser = sub.add_parser("generate", help="Generate an image from a prompt.")
    generate_parser.add_argument("prompt", nargs="+", help="Prompt text to send to ChatGPT.")
    generate_parser.add_argument("--max-images", type=int, default=1, help="Maximum images to export.")
    generate_parser.add_argument("--timeout-seconds", type=int, default=420, help="Generation timeout.")
    generate_parser.add_argument("--login-timeout-seconds", type=int, default=600, help="How long to wait for manual login/verification.")
    generate_parser.add_argument(
        "--conversation-mode",
        choices=["new", "continue"],
        default="new",
        help="Use 'new' for a new photo, or 'continue' to keep editing the current conversation.",
    )

    daemon_parser = sub.add_parser("browser-daemon", help="Open ChatGPT once and keep the browser alive for MCP calls.")
    daemon_parser.add_argument("--host", default=DEFAULT_DAEMON_HOST, help="Host for the local browser daemon.")
    daemon_parser.add_argument("--port", type=int, default=DEFAULT_DAEMON_PORT, help="Port for the local browser daemon.")
    daemon_parser.add_argument(
        "--login-confirm",
        choices=["enter", "auto"],
        default="enter",
        help="Use 'enter' for manual startup, or 'auto' to continue as soon as ChatGPT is ready.",
    )
    daemon_parser.add_argument("--login-timeout-seconds", type=int, default=900, help="How long to wait for login/verification.")

    daemon_status_parser = sub.add_parser("daemon-status", help="Check whether the browser daemon is reachable.")
    daemon_status_parser.add_argument("--host", default=DEFAULT_DAEMON_HOST, help="Browser daemon host.")
    daemon_status_parser.add_argument("--port", type=int, default=DEFAULT_DAEMON_PORT, help="Browser daemon port.")

    serve_parser = sub.add_parser("serve-mcp", help="Run the MCP server; it talks to a separate browser daemon.")
    serve_parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="streamable-http",
        help="MCP transport. Use streamable-http for manual CLI startup; use stdio when an MCP client launches this process.",
    )
    serve_parser.add_argument("--host", default="127.0.0.1", help="Host for HTTP transports.")
    serve_parser.add_argument("--port", type=int, default=8005, help="Port for HTTP transports.")
    serve_parser.add_argument("--daemon-host", default=DEFAULT_DAEMON_HOST, help="Browser daemon host.")
    serve_parser.add_argument("--daemon-port", type=int, default=DEFAULT_DAEMON_PORT, help="Browser daemon port.")
    serve_parser.add_argument(
        "--login-confirm",
        choices=["enter", "auto"],
        default="enter",
        help="Deprecated no-op. Login is handled by `browser-daemon` now.",
    )
    serve_parser.add_argument("--login-timeout-seconds", type=int, default=900, help="Deprecated no-op.")
    return parser


async def _run_async(args: argparse.Namespace) -> int:
    command = args.command or "login"

    if command == "login":
        _print_json(await login_interactive(force=args.force))
        return 0

    if command == "status":
        _print_json(await status())
        return 0

    if command == "generate":
        prompt = " ".join(args.prompt)
        try:
            result = await generate(
                prompt,
                timeout_seconds=args.timeout_seconds,
                max_images=args.max_images,
                login_timeout_seconds=args.login_timeout_seconds,
                conversation_mode=args.conversation_mode,
            )
        except LoginRequired as exc:
            print(str(exc), file=sys.stderr)
            print("Run the generate command again, log in in the opened browser, and wait for it to continue automatically.", file=sys.stderr)
            return 2
        _print_json(result.to_dict())
        return 0

    if command == "browser-daemon":
        await run_browser_daemon(
            host=args.host,
            port=args.port,
            wait_for_enter=args.login_confirm == "enter",
            login_timeout_seconds=args.login_timeout_seconds,
        )
        return 0

    if command == "daemon-status":
        try:
            _print_json(await daemon_status(host=args.host, port=args.port))
            return 0
        except DaemonUnavailable as exc:
            print(str(exc), file=sys.stderr)
            return 2

    return 1


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    command = args.command or "login"

    if command == "serve-mcp":
        from chatgpt_image_mcp.server import run_mcp_server

        run_mcp_server(
            transport=args.transport,
            host=args.host,
            port=args.port,
            daemon_host=args.daemon_host,
            daemon_port=args.daemon_port,
        )
        return 0

    return asyncio.run(_run_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
