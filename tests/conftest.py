"""Shared test helpers.

The only things faked in this suite are the outside edges we do not own: the
socket to Studio's terminal, and the FMOD object graph that generated JavaScript
runs against. The code under test is always the real thing.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(
    NODE is None, reason="node is needed to execute the generated JavaScript"
)


@pytest.fixture(scope="session")
def run_node():
    """Run a JavaScript program under node and return its stdout."""

    def _run(source: str) -> str:
        assert NODE is not None
        proc = subprocess.run([NODE, "-e", source], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    return _run
