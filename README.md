# gpt-image-2-mcp

Local MCP server for generating images through a ChatGPT browser session.

## Architecture

- `browser-daemon`: owns the long-lived Patchright/Chrome ChatGPT session.
- `serve-mcp`: exposes MCP tools and talks to the browser daemon over localhost.
- Generated images are written under `output/chatgpt-images/` and ignored by Git.

## Setup

```powershell
uv sync
```

## Run

Start the browser daemon first:

```powershell
uv run python chatgpt_image.py browser-daemon
```

Log in to ChatGPT in the opened browser, then press Enter.

Start the MCP server:

```powershell
uv run python chatgpt_image.py serve-mcp --transport streamable-http --port 8005
```

The MCP endpoint is:

```text
http://127.0.0.1:8005/mcp
```

## Tools

- `generate_image(prompt, conversation_mode="new")`
- `chatgpt_image_status()`

Use `conversation_mode="new"` for a new image, or `conversation_mode="continue"` to keep editing in the current ChatGPT conversation.
