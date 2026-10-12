"""The crawler that builds api_spec.json.

``tools/build_spec.py`` is a script rather than an installed module, so it is
loaded from its path. The expectations for ``parse_params`` are hand-written from
the bracketed-argument convention the FMOD reference uses, not read back out of
the spec the crawler produced.
"""

from __future__ import annotations

import http.server
import importlib.util
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load_build_spec():
    module_spec = importlib.util.spec_from_file_location("build_spec", ROOT / "tools" / "build_spec.py")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


build_spec = load_build_spec()


class FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_a_successful_fetch_returns_the_page(monkeypatch):
    monkeypatch.setattr(build_spec.subprocess, "run",
                        lambda *a, **k: FakeCompleted(0, "<html>ok</html>"))
    assert build_spec.fetch("globals") == "<html>ok</html>"


def test_a_failed_fetch_is_an_error_not_an_empty_page(monkeypatch):
    """Silently crawling nothing would drop members from the spec without a word."""
    monkeypatch.setattr(build_spec.subprocess, "run",
                        lambda *a, **k: FakeCompleted(22, "", "curl: (22) 404"))
    with pytest.raises(RuntimeError) as caught:
        build_spec.fetch("globals")
    assert "globals" in str(caught.value)


class NotFoundHandler(http.server.BaseHTTPRequestHandler):
    """Answers every request with 404, like the CDN does for a bad page name."""

    def do_GET(self):  # noqa: N802 - the name is fixed by BaseHTTPRequestHandler
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"<html>not found</html>")

    def log_message(self, *args):  # keep the test output quiet
        pass


@pytest.fixture
def not_found_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), NotFoundHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_a_missing_page_stops_the_crawl(monkeypatch, not_found_server):
    """curl exits 0 on an HTTP 404, so a mistyped slug used to parse to zero
    members and quietly drop tools from the spec.

    The mocked test above only proves the raise happens once curl reports
    failure. This one runs the real curl against a real 404, which is the only
    way to show curl is being asked to report it at all.
    """
    monkeypatch.setattr(build_spec, "BASE", not_found_server + "/page-{}.html")
    with pytest.raises(RuntimeError) as caught:
        build_spec.fetch("globals")
    assert "globals" in str(caught.value)


@pytest.mark.parametrize("signature, expected", [
    ("system.getText(msg[, defaultText])", [("msg", False), ("defaultText", True)]),
    ("f(a, b)", [("a", False), ("b", False)]),
    ("f()", []),
    ("f([a, b])", [("a", True), ("b", True)]),
    ("project.importAudioFile(filePath)", [("filePath", False)]),
])
def test_optional_arguments_follow_the_bracket_nesting(signature, expected):
    parsed = build_spec.parse_params(signature)
    assert [(p["name"], p["optional"]) for p in parsed] == expected
