"""Framing and error handling for the terminal client.

The socket is the outside edge we do not own, so it is faked here. Everything
above it, the framing rules and the decision about what counts as an error, is
our code under test.

The reply bytes in these tests are copies of real replies captured from a live
FMOD Studio 2.03.13 terminal, so the shapes are observed rather than invented:

    b'out(): 42\\r\\n\\r\\n\\x00'                         a value
    b'log(): before\\r\\n\\x00'                           console output
    b"error(): ReferenceError: 'x' is not defined\\r\\n\\r\\n\\x00"
    b'log(): before\\r\\n\\x00error(): ...\\r\\n\\r\\n\\x00'  both, in one reply
"""

from __future__ import annotations

import socket
import time

import pytest

from fmod_studio_mcp.client import FmodTerminal, FmodTerminalError

VALUE_REPLY = b"out(): 42\r\n\r\n\x00"
CONNECT_BANNER = b"log(): Connected to FMOD Studio on ::ffff:127.0.0.1:3663.\r\n\x00"
STRING_REPLY = b"out(): /Users/me/Project.fspro\r\n\r\n\x00"
MULTILINE_REPLY = b'out(): {\n "properties": [\n  "note"\n ]\n}\r\n\r\n\x00'
LOG_REPLY = b"log(): before\r\n\x00"
ERROR_REPLY = b"error(): ReferenceError: 'x' is not defined\r\n\r\n\x00"
LOG_THEN_ERROR_REPLY = b"log(): before\r\n\x00error(): ReferenceError: 'x' is not defined\r\n\r\n\x00"
EMPTY_REPLY = b"\x00"
# Captured from a live terminal: a property holding an empty string answers
# `out(): `, which is one message whose text is empty.
EMPTY_VALUE_REPLY = b"out(): \r\n\r\n\x00"


class FakeSocket:
    """Stands in for a socket connected to Studio's scripting terminal.

    Timing matters here. A real socket only has a reply available once a command
    has been sent, and only greets you with the banner on connect. The read loop
    stops as soon as the buffer runs dry, so handing out a reply too early would
    let ``connect()`` swallow it.
    """

    def __init__(self, replies=(), banner=b"", fail_on_send=()):
        self._replies = list(replies)
        self._pending = [banner] if banner else []
        self.sent: list[bytes] = []
        self.closed = False
        self.timeout = None
        self._fail_on_send = set(fail_on_send)
        self._sends = 0

    def settimeout(self, value) -> None:
        self.timeout = value

    def sendall(self, payload: bytes) -> None:
        self._sends += 1
        if self._sends in self._fail_on_send:
            raise OSError("broken pipe")
        self.sent.append(payload)
        if self._replies:
            self._pending.append(self._replies.pop(0))

    def recv(self, _n: int) -> bytes:
        return self._pending.pop(0) if self._pending else b""

    def close(self) -> None:
        self.closed = True


class SlowSocket(FakeSocket):
    """A socket whose reply only becomes available after `delay` seconds.

    `FakeSocket` hands its reply over instantly, so it cannot tell a client that
    waits for a slow script from one that gives up early. This one honours
    ``settimeout``: it blocks up to the timeout it was given, and raises
    :class:`socket.timeout` only if the reply is still not ready by then, which
    is what a real socket does.
    """

    def __init__(self, reply: bytes, delay: float):
        super().__init__()
        self._reply = reply
        self._delay = delay
        self._ready_at: float | None = None

    def sendall(self, payload: bytes) -> None:
        self.sent.append(payload)
        if self._ready_at is None:
            self._ready_at = time.monotonic() + self._delay

    def recv(self, _n: int) -> bytes:
        if self._ready_at is None:
            return b""
        wait = self._ready_at - time.monotonic()
        if wait > 0:
            time.sleep(min(wait, self.timeout or 0))
        if time.monotonic() < self._ready_at:
            raise socket.timeout()
        self._ready_at = None
        return self._reply


def terminal_replying(*replies: bytes) -> FmodTerminal:
    """A terminal already connected, with one reply per command sent."""
    terminal = FmodTerminal()
    terminal._sock = FakeSocket(replies=replies)
    return terminal


def test_a_value_reply_comes_back_as_plain_text():
    assert terminal_replying(VALUE_REPLY).run("1+1") == "42"


def test_a_string_value_is_not_altered():
    assert terminal_replying(STRING_REPLY).run("x") == "/Users/me/Project.fspro"


def test_a_multi_line_value_survives_intact():
    """JSON results must keep their internal newlines and lose only the framing."""
    assert terminal_replying(MULTILINE_REPLY).run("x") == '{\n "properties": [\n  "note"\n ]\n}'


def test_no_terminator_or_prefix_reaches_the_caller():
    text = terminal_replying(VALUE_REPLY).run("1+1")
    assert "\x00" not in text
    assert not text.startswith("out(): ")


def test_console_output_is_returned_as_text():
    assert terminal_replying(LOG_REPLY).run("console.log('before')") == "before"


def test_an_empty_string_value_comes_back_as_an_empty_string():
    """An empty string is a value. It must survive as one."""
    assert terminal_replying(EMPTY_VALUE_REPLY).run("x") == ""


def test_nothing_arriving_is_not_an_empty_value():
    """A read that returns no messages at all is silence, not a value.

    Both cases used to come back as ``""``, so a caller could not tell an empty
    property from a call that produced nothing, and the server reported the
    first as though it were the second.
    """
    assert terminal_replying(EMPTY_REPLY).run("x") is None


def test_an_error_reply_is_reported_as_an_error():
    with pytest.raises(FmodTerminalError) as caught:
        terminal_replying(ERROR_REPLY).run("x()")
    assert "ReferenceError" in str(caught.value)


def test_log_output_before_an_error_still_reports_the_error():
    with pytest.raises(FmodTerminalError) as caught:
        terminal_replying(LOG_THEN_ERROR_REPLY).run("x()")
    assert "ReferenceError" in str(caught.value)


def test_a_send_failure_reconnects_and_retries_once(monkeypatch):
    terminal = FmodTerminal()
    first = FakeSocket(replies=[VALUE_REPLY], fail_on_send={1})
    second = FakeSocket(replies=[VALUE_REPLY], banner=CONNECT_BANNER)
    terminal._sock = first
    monkeypatch.setattr("fmod_studio_mcp.client.socket.create_connection",
                        lambda *a, **k: second)
    assert terminal.run("1+1") == "42"
    assert first.closed
    assert second.sent


def test_a_failure_after_reconnect_is_still_a_readable_error(monkeypatch):
    """A dead socket on the retry must not escape as a bare OSError."""
    terminal = FmodTerminal()
    first = FakeSocket(replies=[VALUE_REPLY], fail_on_send={1})
    second = FakeSocket(replies=[VALUE_REPLY], banner=CONNECT_BANNER, fail_on_send={1})
    terminal._sock = first
    monkeypatch.setattr("fmod_studio_mcp.client.socket.create_connection",
                        lambda *a, **k: second)
    with pytest.raises(FmodTerminalError):
        terminal.run("1+1")


def test_a_slow_reply_goes_to_the_call_that_asked_for_it():
    """A script slower than the idle window must not lose its reply.

    Observed live against FMOD Studio 2.03.13: a call whose script took 1.09 s
    returned ``None`` after exactly 0.401 s, and the *next* call, asking for
    ``'MARKER-C'``, came back with ``'449999985000000\\nMARKER-C'``. The second
    caller was handed the first caller's answer.

    The delay here (0.6 s) is deliberately longer than READ_IDLE (0.4 s) and
    shorter than the overall window, which is the band the bug lived in.
    """
    terminal = FmodTerminal()
    terminal._sock = SlowSocket(VALUE_REPLY, delay=0.6)
    assert terminal.run("slow()") == "42"


def test_a_fast_reply_does_not_pay_the_overall_deadline():
    """The fix waits out `overall` only for the *first* byte.

    Once a reply has started, a quiet socket ends it, so an ordinary exchange
    still costs the idle window rather than the deadline.
    """
    terminal = FmodTerminal()
    terminal._sock = SlowSocket(VALUE_REPLY, delay=0.0)
    started = time.monotonic()
    assert terminal.run("fast()") == "42"
    assert time.monotonic() - started < 1.0
