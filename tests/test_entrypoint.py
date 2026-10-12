"""The server started the way a client starts it.

``fmod-studio-mcp`` has two entry points, the console script and ``python -m``.
Both must start the same server, so this drives the real process over stdio and
reads the real handshake. It passes before and after a refactor of the entry
points, which is what makes it a safety net rather than a bug reproduction.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading

EXPECTED_TOOL_COUNT = 155
GENERIC_TOOLS = {
    "fmod_get_property",
    "fmod_set_property",
    "fmod_add_relationship",
    "fmod_remove_relationship",
    "fmod_class_names",
    "fmod_describe_class",
    "fmod_create_event",
}


INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "test", "version": "1.0"}},
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def _drive(*messages: dict, expected: int = 2, timeout: float = 25.0) -> dict:
    """Start the server, send these messages, and return the replies by id.

    Replies are collected until every expected id has arrived, so a slow start
    cannot truncate the run the way a fixed sleep can.
    """
    payload = "\n".join(json.dumps(msg) for msg in messages) + "\n"

    proc = subprocess.Popen(
        [sys.executable, "-m", "fmod_studio_mcp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert proc.stdin and proc.stdout
    replies: dict = {}
    arrived = threading.Event()

    def collect() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if line.startswith("{"):
                message = json.loads(line)
                if "id" in message:
                    replies[message["id"]] = message
                    if len(replies) >= expected:
                        break
        arrived.set()

    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    proc.stdin.write(payload)
    proc.stdin.flush()
    # Closing stdin is what lets the server exit, so it happens once every
    # expected reply has landed rather than after a guessed delay.
    arrived.wait(timeout)
    proc.stdin.close()
    proc.wait(timeout=15)
    reader.join(timeout=5)
    return replies


def handshake() -> dict:
    """Start the server, list its tools, and return the parsed replies."""
    return _drive(INITIALIZE, INITIALIZED,
                  {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})


def test_python_dash_m_starts_a_server_that_lists_its_tools():
    replies = handshake()
    assert 1 in replies and 2 in replies, f"missing replies: {sorted(replies)}"
    assert replies[1]["result"]["serverInfo"]["name"] == "fmod-studio-mcp"

    tools = replies[2]["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert len(tools) == EXPECTED_TOOL_COUNT
    assert all(name.startswith("fmod_") for name in names)
    assert GENERIC_TOOLS <= names


def test_every_listed_tool_has_a_usable_schema():
    tools = handshake()[2]["result"]["tools"]
    for tool in tools:
        assert tool["description"], tool["name"]
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        for required in schema.get("required", []):
            assert required in schema["properties"], f"{tool['name']} requires unknown {required}"


def call_tool(name: str, arguments: dict) -> dict:
    """Start the server and call one tool, returning the raw JSON-RPC reply."""
    return _drive(INITIALIZE, INITIALIZED,
                  {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}})


def test_a_call_result_reaches_the_client_intact():
    """Our handler returns a ``types.CallToolResult``, and the MCP SDK only
    recognises that type from 1.19.0 — see the bounds in ``pyproject.toml``.
    Older versions feed it to ``list(results)`` instead, which turns the
    pydantic model into ``(field, value)`` tuples, so every call fails
    validation and the failure flag is lost.

    Stubbing the terminal never reaches that code, so this drives a real call
    over stdio. An unknown tool name is used because the server answers it
    without a live FMOD Studio, and it still exercises the whole result path.
    """
    result = call_tool("fmod_nope", {})[2]["result"]
    assert result["isError"] is True
    # The exact text matters: a framework validation error would also be marked
    # as an error, so only our own message proves the result survived intact.
    assert result["content"][0]["text"] == "ERROR: unknown tool fmod_nope"
