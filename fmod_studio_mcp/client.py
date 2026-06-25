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


class FmodTerminalError(RuntimeError):
    """Raised when the scripting terminal can't be reached or a call fails."""


class FmodTerminal:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 connect_timeout: float = 5.0):
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
        self._read_until_idle(idle=0.3, overall=2.0)

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
        assert self._sock is not None
        chunks: list[bytes] = []
        deadline = time.monotonic() + overall
        self._sock.settimeout(idle)
        while time.monotonic() < deadline:
            try:
                data = self._sock.recv(65536)
            except socket.timeout:
                break
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks).decode("utf-8", errors="replace")

    def run(self, script: str, idle: float = 0.4, overall: float = 30.0) -> str:
        """Send `script` to the terminal and return its reply text.

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
            self._sock.sendall(payload)  # type: ignore[union-attr]
        return self._read_until_idle(idle=idle, overall=overall).strip()
