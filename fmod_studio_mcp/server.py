"""fmod-studio-mcp — an MCP server that drives FMOD Studio live via its
scripting terminal (TCP, default 127.0.0.1:3663).

Design:
  * `fmod_run_script` / `fmod_eval` expose the **entire** FMOD Studio Scripting
    API (anything you can type in the console), so nothing is out of reach.
  * Introspection tools (`fmod_class_names`, `fmod_dump`) make that API
    discoverable at runtime.
  * Ergonomic wrappers (`fmod_create_event`, `fmod_import_audio`, `fmod_create`,
    `fmod_lookup`, `fmod_build`, `fmod_save`, …) generate the JS for common ops.

Config via env: FMOD_STUDIO_HOST (default 127.0.0.1), FMOD_STUDIO_PORT (3663).
"""

from __future__ import annotations

import asyncio
import json
import os

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

from .client import FmodTerminal, FmodTerminalError

HOST = os.environ.get("FMOD_STUDIO_HOST", "127.0.0.1")
PORT = int(os.environ.get("FMOD_STUDIO_PORT", "3663"))

app = Server("fmod-studio-mcp")
TERMINAL = FmodTerminal(HOST, PORT)


def _q(value: str) -> str:
    """JSON-encode a string for safe embedding in generated JavaScript."""
    return json.dumps(value)


def _run(script: str, overall: float = 30.0) -> str:
    try:
        reply = TERMINAL.run(script, overall=overall)
    except FmodTerminalError as exc:
        return f"ERROR: {exc}"
    return reply if reply else "(no output)"


# ---------------------------------------------------------------------------
# JS builders for the high-level wrappers
# ---------------------------------------------------------------------------

def _js_create_event(name: str, sound: str | None, bank_name: str | None,
                     folder_path: str | None) -> str:
    lines = [
        "var __ev = studio.project.create('Event');",
        f"__ev.name = {_q(name)};",
    ]
    if folder_path:
        lines.append(f"var __f = studio.project.lookup({_q(folder_path)}); if (__f) __ev.folder = __f;")
    if sound:
        lines += [
            f"var __asset = studio.project.importAudioFile({_q(sound)});",
            "var __track = __ev.addGroupTrack();",
            "var __inst = __track.addSound(__ev.timeline, 'SingleSound', 0, __asset.length);",
            "__inst.audioFile = __asset;",
        ]
    if bank_name:
        lines += [
            f"var __bank = studio.project.lookup('bank:/' + {_q(bank_name)});",
            "if (__bank) { __ev.relationships.banks.add(__bank); }",
        ]
    lines.append("'created ' + __ev.getPath() + ' (' + __ev.id + ')';")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="fmod_run_script",
            description=(
                "Run arbitrary FMOD Studio Scripting API JavaScript in the live Studio and return "
                "its terminal output. This is the full-power escape hatch — the entire scripting API "
                "is reachable (studio.project, studio.system, ManagedObjects, etc.). End with an "
                "expression to echo a result."
            ),
            inputSchema={
                "type": "object",
                "required": ["script"],
                "properties": {
                    "script": {"type": "string", "description": "JavaScript to evaluate in FMOD Studio."},
                    "timeout": {"type": "number", "description": "Max seconds to wait for output (default 30; raise for build())."},
                },
            },
        ),
        types.Tool(
            name="fmod_eval",
            description="Evaluate a single JS expression and return its value (convenience over run_script).",
            inputSchema={
                "type": "object",
                "required": ["expression"],
                "properties": {"expression": {"type": "string"}},
            },
        ),
        types.Tool(
            name="fmod_project_info",
            description="Report whether a project is open and its file path.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="fmod_save",
            description="Save the FMOD Studio project (studio.project.save()).",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="fmod_build",
            description="Build the project's banks (studio.project.build()). May take a while.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="fmod_class_names",
            description="List the scripting API's managed class names (introspection: studio.system.getClassNames()).",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="fmod_lookup",
            description="Look up an object by path or GUID (e.g. 'event:/SFX/Hit', 'bank:/Master') and report its path + id.",
            inputSchema={
                "type": "object",
                "required": ["path"],
                "properties": {"path": {"type": "string", "description": "Lookup path or {guid}."}},
            },
        ),
        types.Tool(
            name="fmod_dump",
            description="Dump an object's properties + relationships (object.dump()) by path or GUID.",
            inputSchema={
                "type": "object",
                "required": ["path"],
                "properties": {"path": {"type": "string"}},
            },
        ),
        types.Tool(
            name="fmod_create",
            description="Create a managed object of a class (studio.project.create(class)) and optionally set its name.",
            inputSchema={
                "type": "object",
                "required": ["class_name"],
                "properties": {
                    "class_name": {"type": "string", "description": "e.g. 'Event', 'Bank', 'EventFolder'."},
                    "name": {"type": "string", "description": "Optional name to set."},
                },
            },
        ),
        types.Tool(
            name="fmod_import_audio",
            description="Import an audio file into the project (studio.project.importAudioFile(path)); returns the asset path/id.",
            inputSchema={
                "type": "object",
                "required": ["path"],
                "properties": {"path": {"type": "string", "description": "Absolute path to the .wav/.ogg/etc."}},
            },
        ),
        types.Tool(
            name="fmod_create_event",
            description=(
                "Create an event in the live project, optionally importing a sound onto a single "
                "instrument and assigning it to a bank. Generates the scripting-API calls "
                "(create → name → addGroupTrack → addSound → importAudioFile → relationships.banks.add)."
            ),
            inputSchema={
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "sound": {"type": "string", "description": "Absolute path to an audio file to place as a one-shot."},
                    "bank_name": {"type": "string", "description": "Bank to assign to, e.g. 'Master'. Omit to skip."},
                    "folder_path": {"type": "string", "description": "Existing folder lookup path, e.g. 'event:/SFX'."},
                },
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    text = _dispatch(name, arguments or {})
    return [types.TextContent(type="text", text=text)]


def _dispatch(name: str, args: dict) -> str:
    match name:
        case "fmod_run_script":
            return _run(args["script"], overall=float(args.get("timeout", 30)))
        case "fmod_eval":
            return _run(f"({args['expression']});")
        case "fmod_project_info":
            return _run("'open=' + studio.project.isOpen + ' path=' + studio.project.filePath;")
        case "fmod_save":
            return _run("studio.project.save(); 'saved';")
        case "fmod_build":
            return _run("studio.project.build(); 'build complete';", overall=180.0)
        case "fmod_class_names":
            return _run("JSON.stringify(studio.system.getClassNames());")
        case "fmod_lookup":
            return _run(
                f"var __o = studio.project.lookup({_q(args['path'])}); "
                "__o ? (__o.getPath() + ' (' + __o.id + ')') : 'not found';"
            )
        case "fmod_dump":
            return _run(f"var __o = studio.project.lookup({_q(args['path'])}); __o ? __o.dump() : 'not found';")
        case "fmod_create":
            nm = args.get("name")
            js = f"var __o = studio.project.create({_q(args['class_name'])});"
            if nm:
                js += f" __o.name = {_q(nm)};"
            js += " __o.id;"
            return _run(js)
        case "fmod_import_audio":
            return _run(f"var __a = studio.project.importAudioFile({_q(args['path'])}); __a ? __a.id : 'failed';")
        case "fmod_create_event":
            return _run(_js_create_event(
                args["name"], args.get("sound"), args.get("bank_name"), args.get("folder_path")))
        case _:
            return f"ERROR: unknown tool {name}"


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())
