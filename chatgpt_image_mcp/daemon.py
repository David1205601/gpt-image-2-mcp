from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from chatgpt_image_mcp.browser import ChatGPTBrowserSession


DEFAULT_DAEMON_HOST = "127.0.0.1"
DEFAULT_DAEMON_PORT = 8765


class DaemonUnavailable(RuntimeError):
    """Raised when the browser daemon is not reachable."""


class DaemonToolError(RuntimeError):
    """Raised when the browser daemon reports a command error."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


async def _write_response(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    await writer.drain()


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    session: ChatGPTBrowserSession,
) -> None:
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=30)
        if not line:
            return
        request = json.loads(line.decode("utf-8"))
        action = request.get("action")

        if action == "status":
            result = await session.status()
        elif action == "generate":
            result = (
                await session.generate(
                    str(request.get("prompt") or ""),
                    timeout_seconds=int(request.get("timeout_seconds") or 420),
                    max_images=int(request.get("max_images") or 1),
                    conversation_mode=str(request.get("conversation_mode") or "new"),
                )
            ).to_dict()
        else:
            raise ValueError(f"Unsupported daemon action: {action!r}")

        await _write_response(writer, {"ok": True, "result": result})
    except Exception as exc:
        await _write_response(
            writer,
            {
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
    finally:
        writer.close()
        await writer.wait_closed()


async def run_browser_daemon(
    *,
    host: str = DEFAULT_DAEMON_HOST,
    port: int = DEFAULT_DAEMON_PORT,
    wait_for_enter: bool = True,
    login_timeout_seconds: int = 900,
) -> None:
    session = ChatGPTBrowserSession()
    await session.start(
        wait_for_enter=wait_for_enter,
        login_timeout_seconds=login_timeout_seconds,
    )
    server = await asyncio.start_server(
        lambda reader, writer: _handle_client(reader, writer, session),
        host,
        port,
    )
    print(f"Browser daemon ready at {host}:{port}. Keep this terminal open.", file=sys.stderr)
    try:
        async with server:
            await server.serve_forever()
    finally:
        await session.close()


async def daemon_request(
    action: str,
    payload: dict[str, Any] | None = None,
    *,
    host: str = DEFAULT_DAEMON_HOST,
    port: int = DEFAULT_DAEMON_PORT,
    response_timeout_seconds: int = 900,
) -> dict[str, Any]:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=5,
        )
    except OSError as exc:
        raise DaemonUnavailable(f"Browser daemon is not running at {host}:{port}.") from exc

    request = {"action": action, **(payload or {})}
    writer.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
    await writer.drain()

    try:
        line = await asyncio.wait_for(reader.readline(), timeout=response_timeout_seconds)
    finally:
        writer.close()
        await writer.wait_closed()

    if not line:
        raise DaemonUnavailable("Browser daemon closed the connection without a response.")

    response = json.loads(line.decode("utf-8"))
    if not response.get("ok"):
        raise DaemonToolError(
            str(response.get("error_type") or "DaemonToolError"),
            str(response.get("error") or "Browser daemon command failed."),
        )
    return response["result"]


async def daemon_status(
    *,
    host: str = DEFAULT_DAEMON_HOST,
    port: int = DEFAULT_DAEMON_PORT,
) -> dict[str, Any]:
    return await daemon_request("status", host=host, port=port, response_timeout_seconds=30)


async def daemon_generate(
    prompt: str,
    *,
    host: str = DEFAULT_DAEMON_HOST,
    port: int = DEFAULT_DAEMON_PORT,
    timeout_seconds: int = 420,
    max_images: int = 1,
    conversation_mode: str = "new",
) -> dict[str, Any]:
    return await daemon_request(
        "generate",
        {
            "prompt": prompt,
            "timeout_seconds": timeout_seconds,
            "max_images": max_images,
            "conversation_mode": conversation_mode,
        },
        host=host,
        port=port,
        response_timeout_seconds=timeout_seconds + 120,
    )
