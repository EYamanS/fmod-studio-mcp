"""What a tool call hands back to the model.

A call that failed must not look like a call that worked. The terminal reports a
script error as an ``error():`` message, and the MCP SDK passes a
``CallToolResult`` straight through, so the failure can be marked as one.
"""

from __future__ import annotations

import asyncio

import pytest

from fmod_studio_mcp import server
from fmod_studio_mcp.client import FmodTerminalError


class FakeTerminal:
    """Stands in for the socket-backed terminal."""

    def __init__(self, reply: str = "", error: Exception | None = None):
        self.reply = reply
        self.error = error
        self.scripts: list[str] = []

    def run(self, script: str, **kwargs) -> str | None:
        self.scripts.append(script)
        if self.error is not None:
            raise self.error
        return self.reply


@pytest.fixture
def terminal(monkeypatch):
    def install(reply: str = "", error: Exception | None = None) -> FakeTerminal:
        fake = FakeTerminal(reply=reply, error=error)
        monkeypatch.setattr(server, "TERMINAL", fake)
        return fake

    return install


def test_a_value_reply_is_a_successful_result(terminal):
    terminal(reply="event:/Hit")
    result = server._run("x")
    assert result.isError is False
    assert result.content[0].text == "event:/Hit"


def test_a_transport_failure_is_a_failed_result(terminal):
    terminal(error=FmodTerminalError("Cannot reach FMOD Studio's scripting terminal at 127.0.0.1:3663."))
    result = server._run("x")
    assert result.isError is True
    assert "Cannot reach FMOD Studio" in result.content[0].text


def test_a_script_error_is_a_failed_result(terminal):
    """A runtime error inside FMOD is a failed call, not a successful one."""
    terminal(error=FmodTerminalError("ReferenceError: 'x' is not defined"))
    result = server._run("x")
    assert result.isError is True
    assert "ReferenceError" in result.content[0].text


def test_an_empty_value_is_reported_as_empty(terminal):
    """A property that holds an empty string is a value, not silence. Reading one
    used to answer "(no output)", which reads as a call that did nothing."""
    terminal(reply="")
    result = server._run("x")
    assert result.isError is False
    assert result.content[0].text == ""


def test_no_reply_at_all_says_so(terminal):
    terminal(reply=None)
    result = server._run("x")
    assert result.isError is False
    assert result.content[0].text == "(no output)"


def test_call_tool_returns_the_result_object(terminal):
    terminal(reply="42")
    result = asyncio.run(server.call_tool("fmod_project_filePath", {}))
    assert result.isError is False
    assert result.content[0].text == "42"


def test_call_tool_marks_a_failed_generic_call(terminal):
    terminal(error=FmodTerminalError("ReferenceError: 'y' is not defined"))
    result = asyncio.run(server.call_tool("fmod_class_names", {}))
    assert result.isError is True


def test_call_tool_reports_an_unknown_tool_as_a_failure(terminal):
    terminal(reply="unused")
    result = asyncio.run(server.call_tool("fmod_not_a_tool", {}))
    assert result.isError is True
    assert "unknown tool" in result.content[0].text
