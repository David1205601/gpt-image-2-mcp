from __future__ import annotations

import anyio
import json
import sys
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ImageContent, TextContent

from chatgpt_image_mcp.browser import image_as_base64
from chatgpt_image_mcp.daemon import (
    DEFAULT_DAEMON_HOST,
    DEFAULT_DAEMON_PORT,
    DaemonToolError,
    DaemonUnavailable,
    daemon_generate,
    daemon_status,
)


mcp = FastMCP("ChatGPT Images 2.0", log_level="ERROR")
_daemon_host = DEFAULT_DAEMON_HOST
_daemon_port = DEFAULT_DAEMON_PORT


@mcp.tool()
async def generate_image(prompt: str, conversation_mode: str = "new") -> CallToolResult:
    """Generate one image in ChatGPT from a text prompt and return the saved image.

    Use conversation_mode='new' for a new photo, or 'continue' to keep editing
    in the current ChatGPT conversation.
    """
    try:
        result = await daemon_generate(
            prompt,
            host=_daemon_host,
            port=_daemon_port,
            max_images=1,
            conversation_mode=conversation_mode,
        )
    except DaemonUnavailable as exc:
        return CallToolResult(
            isError=True,
            content=[
                TextContent(
                    type="text",
                    text=f"{exc}\nStart it with `uv run python chatgpt_image.py browser-daemon`, log in once, then retry.",
                )
            ],
        )
    except DaemonToolError as exc:
        if exc.error_type == "ImageGenerationRefused":
            return CallToolResult(
                isError=True,
                content=[
                    TextContent(
                        type="text",
                        text=f"Image generation refused by ChatGPT: {exc}",
                    )
                ],
                structuredContent={
                    "status": "refused",
                    "error_type": exc.error_type,
                    "message": str(exc),
                    "prompt": prompt,
                    "conversation_mode": conversation_mode,
                },
            )
        return CallToolResult(
            isError=True,
            content=[
                TextContent(
                    type="text",
                    text=f"Browser daemon error ({exc.error_type}): {exc}",
                )
            ],
        )
    except Exception as exc:
        return CallToolResult(
            isError=True,
            content=[
                TextContent(
                    type="text",
                    text=f"Unexpected MCP server error ({type(exc).__name__}): {exc}",
                )
            ],
        )

    primary = result["primary_image"]
    data, mime_type = image_as_base64(primary["path"])
    summary = {
        "status": "saved",
        "prompt": result["prompt"],
        "image_path": primary["path"],
        "output_dir": result["output_dir"],
        "mime_type": primary["mime_type"],
        "width": primary["width"],
        "height": primary["height"],
        "session_reused": result["session_reused"],
        "conversation_mode": result.get("conversation_mode", conversation_mode),
        "daemon_host": _daemon_host,
        "daemon_port": _daemon_port,
    }
    return CallToolResult(
        content=[
            TextContent(type="text", text=json.dumps(summary, ensure_ascii=False, indent=2)),
            ImageContent(type="image", data=data, mimeType=mime_type),
        ],
        structuredContent=summary,
    )


@mcp.tool()
async def chatgpt_image_status() -> dict:
    """Show the current ChatGPT image browser-session mode."""
    try:
        daemon = await daemon_status(host=_daemon_host, port=_daemon_port)
        return {
            "ready": True,
            "mcp_mode": "thin-client",
            "daemon_host": _daemon_host,
            "daemon_port": _daemon_port,
            "daemon": daemon,
        }
    except DaemonUnavailable as exc:
        return {
            "ready": False,
            "mcp_mode": "thin-client",
            "daemon_host": _daemon_host,
            "daemon_port": _daemon_port,
            "error": str(exc),
        }


async def run_mcp_server_async(
    *,
    transport: Literal["stdio", "sse", "streamable-http"] = "streamable-http",
    host: str = "127.0.0.1",
    port: int = 8005,
    daemon_host: str = DEFAULT_DAEMON_HOST,
    daemon_port: int = DEFAULT_DAEMON_PORT,
) -> None:
    global _daemon_host, _daemon_port
    _daemon_host = daemon_host
    _daemon_port = daemon_port
    mcp.settings.host = host
    mcp.settings.port = port
    if transport == "streamable-http":
        print(f"MCP server starting at http://{host}:{port}/mcp", file=sys.stderr)
    else:
        print(f"MCP server starting with {transport} transport.", file=sys.stderr)
    print(f"MCP tools will use browser daemon at {daemon_host}:{daemon_port}.", file=sys.stderr)
    match transport:
        case "stdio":
            await mcp.run_stdio_async()
        case "sse":
            await mcp.run_sse_async(None)
        case "streamable-http":
            await mcp.run_streamable_http_async()
        case _:
            raise ValueError(f"Unsupported MCP transport: {transport}")


def run_mcp_server(
    *,
    transport: Literal["stdio", "sse", "streamable-http"] = "streamable-http",
    host: str = "127.0.0.1",
    port: int = 8005,
    daemon_host: str = DEFAULT_DAEMON_HOST,
    daemon_port: int = DEFAULT_DAEMON_PORT,
) -> None:
    async def _runner() -> None:
        await run_mcp_server_async(
            transport=transport,
            host=host,
            port=port,
            daemon_host=daemon_host,
            daemon_port=daemon_port,
        )

    anyio.run(_runner)


if __name__ == "__main__":
    run_mcp_server()
