import asyncio
import json
import os
import sys
import urllib.parse
from typing import Any, Optional

import httpx

SERVER_NAME = "yggdrasil-cosmos-mcp"
SERVER_VERSION = "2.0.0"
PROTOCOL_VERSION = "2024-11-05"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "commit_to_memory",
        "description": (
            "Commit a distilled technical decision, summary, and optional full topic "
            "conversation into Project Cosmos (Yggdrasil GitHub memory)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "Topic category slug, e.g. 'kubernetes'."},
                "summary": {"type": "string", "description": "Distilled summary or decision."},
                "source_agent": {"type": "string", "description": "Calling AI assistant name."},
                "title": {"type": "string", "description": "Optional entry title."},
                "conversation": {
                    "type": "string",
                    "description": "Optional full topic conversation transcript.",
                },
                "scope": {
                    "type": "string",
                    "enum": ["private", "career-public", "commercial"],
                    "default": "private",
                },
                "realm": {"type": "string", "description": "Optional folder namespace."},
            },
            "required": ["tag", "summary", "source_agent"],
        },
    },
    {
        "name": "recall_memory",
        "description": "Read a topic neuron dossier from Yggdrasil by tag (and optional realm/scope).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "Topic category slug to recall."},
                "realm": {"type": "string", "description": "Optional realm namespace."},
                "scope": {
                    "type": "string",
                    "enum": ["private", "career-public", "commercial"],
                },
                "include_conversation": {"type": "boolean", "default": True},
            },
            "required": ["tag"],
        },
    },
    {
        "name": "search_memories",
        "description": "Search across all topic neurons in Yggdrasil and return relevant matching entries.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Search keywords."},
                "tag": {"type": "string", "description": "Optional tag filter."},
                "realm": {"type": "string", "description": "Optional realm namespace filter."},
                "scope": {
                    "type": "string",
                    "enum": ["private", "career-public", "commercial"],
                },
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
            },
            "required": ["q"],
        },
    },
    {
        "name": "list_memory_tags",
        "description": "List all available topic neuron tags and realms stored in Yggdrasil.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _jsonrpc_result(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _jsonrpc_error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


async def _call_tool(
    name: str, arguments: dict[str, Any], client: httpx.AsyncClient, api_key: str
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        if name == "commit_to_memory":
            resp = await client.post("/memory/add", json=arguments, headers=headers)
            resp.raise_for_status()
            return {
                "content": [{"type": "text", "text": json.dumps(resp.json(), indent=2)}],
                "isError": False,
            }

        if name == "recall_memory":
            tag = str(arguments["tag"])
            encoded_tag = urllib.parse.quote(tag, safe="")
            params: dict[str, Any] = {}
            if arguments.get("realm"):
                params["realm"] = arguments["realm"]
            if arguments.get("scope"):
                params["scope"] = arguments["scope"]
            if "include_conversation" in arguments:
                params["include_conversation"] = arguments["include_conversation"]
            resp = await client.get(
                f"/memory/read/{encoded_tag}", params=params, headers=headers
            )
            resp.raise_for_status()
            data = resp.json()
            text = data.get("markdown") or f"No entries found for tag '{tag}'."
            return {"content": [{"type": "text", "text": text}], "isError": False}

        if name == "search_memories":
            params = {"q": arguments["q"]}
            for key in ("tag", "realm", "scope", "limit"):
                if arguments.get(key) is not None:
                    params[key] = arguments[key]
            resp = await client.get("/memory/search", params=params, headers=headers)
            resp.raise_for_status()
            return {
                "content": [{"type": "text", "text": json.dumps(resp.json(), indent=2)}],
                "isError": False,
            }

        if name == "list_memory_tags":
            resp = await client.get("/memory/tags", headers=headers)
            resp.raise_for_status()
            return {
                "content": [{"type": "text", "text": json.dumps(resp.json(), indent=2)}],
                "isError": False,
            }

        return {
            "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
            "isError": True,
        }
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        return {
            "content": [{"type": "text", "text": f"Tool error: {exc}"}],
            "isError": True,
        }


async def handle_jsonrpc_message(
    message: Any, client: httpx.AsyncClient, api_key: str
) -> Optional[dict[str, Any]]:
    """Handle a single MCP JSON-RPC 2.0 request or notification."""
    if not isinstance(message, dict):
        return _jsonrpc_error(None, -32600, "Invalid Request: expected JSON object")

    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _jsonrpc_error(msg_id, -32602, "Invalid params: expected object")

    if method == "notifications/initialized" or msg_id is None:
        return None

    if method == "initialize":
        return _jsonrpc_result(
            msg_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )

    if method == "ping":
        return _jsonrpc_result(msg_id, {})

    if method == "tools/list":
        return _jsonrpc_result(msg_id, {"tools": TOOLS})

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments") or {}
        if not isinstance(tool_name, str) or not isinstance(arguments, dict):
            return _jsonrpc_error(msg_id, -32602, "Invalid tools/call arguments")
        result = await _call_tool(tool_name, arguments, client=client, api_key=api_key)
        return _jsonrpc_result(msg_id, result)

    return _jsonrpc_error(msg_id, -32601, f"Method not found: {method}")


async def _stdio_loop() -> None:
    base_url = os.getenv("YGGDRASIL_URL", "http://localhost:8000").rstrip("/")
    api_key = os.getenv("YGGDRASIL_API_KEY") or os.getenv("YGGDRASIL_PUBLIC_READ_KEY") or ""

    async with httpx.AsyncClient(base_url=base_url, timeout=20.0) as client:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                err = _jsonrpc_error(None, -32700, f"Parse error: {exc}")
                sys.stdout.write(json.dumps(err) + "\n")
                sys.stdout.flush()
                continue

            response = await handle_jsonrpc_message(msg, client=client, api_key=api_key)
            if response is not None:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()


def main() -> None:
    asyncio.run(_stdio_loop())


if __name__ == "__main__":
    main()
