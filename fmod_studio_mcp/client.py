"""Thin TCP client for FMOD Studio's scripting terminal.

FMOD Studio's scripting console (open it in Studio with **Ctrl+0**) listens on a
TCP port — default ``127.0.0.1:3663`` — and evaluates anything sent to it as
UTF-8 JavaScript, sending the result back as text. See the FMOD docs:
"Scripting Terminal Reference" and "Scripting API Reference" (2.02).

This client keeps one persistent connection and uses **read-until-idle** framing:
after sending a command it reads until the socket goes quiet for ``idle`` seconds
(or an overall deadline passes). That avoids depending on a specific prompt string,
which keeps it robust across FMOD versions.
"""

from __future__ import annotations

import socket
import time

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3663

# All windows are seconds. "Idle" is how long the socket must stay quiet before a
# reply is considered finished; "window" is the hard deadline for the whole read.
CONNECT_TIMEOUT = 5.0   # waiting for the TCP connection itself
BANNER_IDLE = 0.3       # the greeting on connect ends after this much silence
BANNER_WINDOW = 2.0
READ_IDLE = 0.4         # a script's reply usually arrives in one go
READ_WINDOW = 30.0      # overridden per call: a project build needs longer
RECV_BYTES = 65536

# The terminal answers with one or more NUL-terminated messages. Each message
# carries a kind prefix. Observed on FMOD Studio 2.03.13:
#
#     out(): 42\r\n\r\n\x00                            a value
#     log(): before\r\n\x00                            console output
#     error(): ReferenceError: x is not defined\r\n\r\n\x00
#     log(): before\r\n\x00error(): ...\r\n\r\n\x00      both, in one reply
#
# The final message ends with a blank line before the NUL; the NUL itself is the
# only reliable terminator, and it is not whitespace, so a plain strip() misses it.
_TERMINATOR = "\x00"
_OUT_PREFIX = "out(): "
_LOG_PREFIX = "log(): "
_ERROR_PREFIX = "error(): "


def split_messages(reply: str) -> list[tuple[str, str]]:
    """Split a raw terminal reply into ``(kind, text)`` messages.

    ``kind`` is ``"out"``, ``"log"``, ``"error"``, or ``"raw"`` for a message
    whose prefix we do not recognise. Framing is dropped; the text is kept
    verbatim, including any internal newlines.
    """
    messages: list[tuple[str, str]] = []
    for chunk in reply.split(_TERMINATOR):
        chunk = chunk.strip("\r\n")
        if not chunk:
            continue
        for prefix, kind in ((_OUT_PREFIX, "out"), (_LOG_PREFIX, "log"), (_ERROR_PREFIX, "error")):
            if chunk.startswith(prefix):
                messages.append((kind, chunk[len(prefix):]))
                break
        else:
            messages.append(("raw", chunk))
    return messages


def format_reply(reply: str) -> str | None:
    """Render a raw terminal reply as the text a caller should see.

    Returns ``None`` when the reply carried no messages at all. That is silence,
    and it is not the same as a value: an empty string is a value, and it comes
    back as ``""``. Joining both to ``""`` is what let the server report an empty
    property as though the call had produced nothing.

    Raises :class:`FmodTerminalError` if the script reported an error, so a
    failed call cannot be mistaken for a successful one.
    """
    messages = split_messages(reply)
    errors = [text for kind, text in messages if kind == "error"]
    if errors:
        raise FmodTerminalError("\n".join(errors))
    if not messages:
        return None
    return "\n".join(text for kind, text in messages)


class FmodTerminalError(RuntimeError):
    """Raised when the scripting terminal can't be reached or a call fails."""


class FmodTerminal:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 connect_timeout: float = CONNECT_TIMEOUT):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self._sock: socket.socket | None = None

    # -- connection ---------------------------------------------------------

    def connect(self) -> None:
        if self._sock is not None:
            return
        try:
            sock = socket.create_connection((self.host, self.port),
                                            timeout=self.connect_timeout)
        except OSError as exc:
            raise FmodTerminalError(
                f"Cannot reach FMOD Studio's scripting terminal at {self.host}:{self.port}. "
                f"Open FMOD Studio with a project, then open the scripting console (Ctrl+0) "
                f"so it starts listening. Underlying error: {exc}"
            ) from exc
        self._sock = sock
        # Swallow any connection banner / initial prompt.
        self._read_until_idle(idle=BANNER_IDLE, overall=BANNER_WINDOW)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def is_connected(self) -> bool:
        return self._sock is not None

    # -- io -----------------------------------------------------------------

    def _read_until_idle(self, idle: float, overall: float) -> str:
        """Read one reply, which ends when the socket has been quiet for `idle`.

        Two different silences arrive on this socket and they must not be
        treated alike:

        * **Quiet before the first byte** means the script has not answered yet.
          Waiting only `idle` here was a real defect: any script slower than
          `idle` looked like a script that produced nothing, the read returned
          ``None``, and the real reply stayed in the buffer for the *next*
          caller to read. Observed live: a call whose script took 1.09 s
          returned ``None``, and the following call, asking for ``'MARKER-C'``,
          came back with ``'449999985000000\\nMARKER-C'``. A caller was handed
          the answer to a different operation, which is exactly what the
          guardrail read-backs exist to prevent. So before the first byte we
          wait out the caller's own `overall` deadline instead.

        * **Quiet after the first byte** means the reply has finished, because
          the terminal may send several NUL-terminated messages for one script
          (`log()` then `error()`), so the first NUL is not the end. Only there
          is `idle` the right thing to measure.
        """
        assert self._sock is not None
        chunks: list[bytes] = []
        deadline = time.monotonic() + overall
        started = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._sock.settimeout(idle if started else remaining)
            try:
                data = self._sock.recv(RECV_BYTES)
            except socket.timeout:
                # Before the first byte this can only mean `remaining` elapsed,
                # since that is what the socket was waiting for.
                break
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
            started = True
        return b"".join(chunks).decode("utf-8", errors="replace")

    def run(self, script: str, idle: float = READ_IDLE, overall: float = READ_WINDOW) -> str | None:
        """Send `script` to the terminal and return its reply text.

        Returns ``None`` when the terminal sent nothing at all. An empty string
        means the script's value was an empty string.

        `idle`/`overall` tune the read window — bump `overall` for slow ops like
        `studio.project.build()`.
        """
        if self._sock is None:
            self.connect()
        payload = (script.rstrip("\n") + "\n").encode("utf-8")
        try:
            self._sock.sendall(payload)  # type: ignore[union-attr]
        except OSError:
            # One reconnect + retry, in case Studio dropped the socket.
            self.close()
            self.connect()
            try:
                self._sock.sendall(payload)  # type: ignore[union-attr]
            except OSError as exc:
                raise FmodTerminalError(
                    f"Lost the connection to FMOD Studio's scripting terminal at "
                    f"{self.host}:{self.port} while sending a command. Underlying error: {exc}"
                ) from exc
        return format_reply(self._read_until_idle(idle=idle, overall=overall))
