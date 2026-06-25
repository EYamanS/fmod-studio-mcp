"""fmod-studio-mcp — an MCP server that drives FMOD Studio live via its scripting
terminal (TCP, default 127.0.0.1:3663).

The server exposes the FMOD Studio Scripting API as **one tool per API member**,
generated from the crawled reference (``api_spec.json`` via :mod:`generation`). On
top of those it adds a small set of *generic* tools that reach the parts of the API
the static reference can't enumerate — the project-schema-defined managed properties
and relationships (e.g. an instrument's ``audioFile``, an event's ``timeline``) — plus
class introspection and a ``create_event`` composite for the common authoring path.

There is intentionally **no arbitrary-script / eval tool**: every capability is a
named, schema-validated operation over FMOD's object model.

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
from .generation import GeneratedTool, build_generated_tools, embed_value, _DESC

HOST = os.environ.get("FMOD_STUDIO_HOST", "127.0.0.1")
PORT = int(os.environ.get("FMOD_STUDIO_PORT", "3663"))

app = Server("fmod-studio-mcp")
TERMINAL = FmodTerminal(HOST, PORT)


def _q(value) -> str:
    return json.dumps(str(value))


def _run(script: str, overall: float = 30.0) -> str:
    try:
        reply = TERMINAL.run(script, overall=overall)
    except FmodTerminalError as exc:
        return f"ERROR: {exc}"
    return reply if reply else "(no output)"


# ---------------------------------------------------------------------------
# Generated tools — one per documented API member
# ---------------------------------------------------------------------------

_GENERATED: dict[str, GeneratedTool] = {gt.name: gt for gt in build_generated_tools()}


def _run_generated(gt: GeneratedTool, args: dict) -> str:
    # build() can take a while; give bank builds a generous window.
    overall = 180.0 if gt.spec["member"].lower().startswith("build") else 30.0
    return _run(gt.build_js(args), overall=overall)


# ---------------------------------------------------------------------------
# Generic managed-object tools — reach the dynamic, schema-defined members the
# static reference doesn't list (an object's per-class properties/relationships).
# ---------------------------------------------------------------------------

def _generic_tools() -> list[types.Tool]:
    obj = {"target": {"type": "string",
                      "description": "Object path ('event:/SFX/Hit', 'bank:/Master') or '{guid}'."}}
    return [
        types.Tool(
            name="fmod_get_property",
            description="Read any property of an object — including dynamic, per-class managed "
                        "properties not in the static reference (e.g. an event's 'timeline').",
            inputSchema={"type": "object", "required": ["target", "property"],
                         "properties": {**obj, "property": {"type": "string"}}},
        ),
        types.Tool(
            name="fmod_set_property",
            description="Set any property of an object (e.g. instrument 'audioFile' = an asset, or "
                        "'name'). Value is auto-embedded: numbers/booleans as literals, a path or "
                        "'{guid}' as an object reference, else a string.",
            inputSchema={"type": "object", "required": ["target", "property", "value"],
                         "properties": {**obj, "property": {"type": "string"},
                                        "value": {"type": ["string", "number", "boolean"]}}},
        ),
        types.Tool(
            name="fmod_add_relationship",
            description="Add an object to one of a target's relationships, e.g. relationship 'banks' "
                        "on an event -> a 'bank:/...'. (target.relationships[name].add(other))",
            inputSchema={"type": "object", "required": ["target", "relationship", "other"],
                         "properties": {**obj, "relationship": {"type": "string"},
                                        "other": {"type": "string", "description": "Path or '{guid}'."}}},
        ),
        types.Tool(
            name="fmod_remove_relationship",
            description="Remove an object from one of a target's relationships. "
                        "(target.relationships[name].remove(other))",
            inputSchema={"type": "object", "required": ["target", "relationship", "other"],
                         "properties": {**obj, "relationship": {"type": "string"},
                                        "other": {"type": "string", "description": "Path or '{guid}'."}}},
        ),
        types.Tool(
            name="fmod_class_names",
            description="List the project model's class/entity names (introspection: keys of studio.project.model).",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="fmod_describe_class",
            description="List a class's schema-defined property and relationship names — how to discover the "
                        "dynamic members (use with fmod_get_property/fmod_set_property/fmod_add_relationship).",
            inputSchema={"type": "object", "required": ["className"],
                         "properties": {"className": {"type": "string", "description": "e.g. 'Event', 'SingleSound'."}}},
        ),
        types.Tool(
            name="fmod_create_event",
            description="Composite: create an event, optionally import a one-shot sound onto a new audio track "
                        "and assign the event to a bank. Equivalent to a create + importAudioFile + addGroupTrack "
                        "+ addSound + set audioFile + add bank relationship.",
            inputSchema={"type": "object", "required": ["name"], "properties": {
                "name": {"type": "string"},
                "sound": {"type": "string", "description": "Absolute path to an audio file to place as a one-shot."},
                "bank_name": {"type": "string", "description": "Bank to assign to, e.g. 'Master'. Omit to skip."},
                "folder_path": {"type": "string", "description": "Existing folder lookup path, e.g. 'event:/SFX'."},
            }},
        ),
    ]


def _generic_dispatch(name: str, a: dict) -> str:
    if name == "fmod_get_property":
        return _run(f"{_DESC} var __o = studio.project.lookup({_q(a['target'])}); "
                    f"__o ? __desc(__o[{_q(a['property'])}]) : 'not found';")
    if name == "fmod_set_property":
        return _run(f"var __o = studio.project.lookup({_q(a['target'])}); "
                    f"if (!__o) 'not found'; else {{ __o[{_q(a['property'])}] = {embed_value(a['value'])}; "
                    f"'set ' + {_q(a['property'])}; }}")
    if name in ("fmod_add_relationship", "fmod_remove_relationship"):
        op = "add" if name.endswith("add_relationship") else "remove"
        return _run(f"var __o = studio.project.lookup({_q(a['target'])}); "
                    f"var __x = studio.project.lookup({_q(a['other'])}); "
                    f"if (!__o || !__x) 'not found'; else {{ __o.relationships[{_q(a['relationship'])}].{op}(__x); "
                    f"'{op}ed'; }}")
    if name == "fmod_class_names":
        return _run("JSON.stringify(Object.keys(studio.project.model).sort());")
    if name == "fmod_describe_class":
        return _run(
            f"var __e = studio.project.model[{_q(a['className'])}]; "
            "__e ? JSON.stringify({"
            "properties: Object.keys(__e.properties), "
            "relationships: Object.keys(__e.relationships)"
            "}, null, 1) : 'unknown class';")
    if name == "fmod_create_event":
        return _run(_js_create_event(a["name"], a.get("sound"), a.get("bank_name"), a.get("folder_path")))
    return f"ERROR: unknown tool {name}"


def _js_create_event(name: str, sound, bank_name, folder_path) -> str:
    lines = ["var __ev = studio.project.create('Event');", f"__ev.name = {_q(name)};"]
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
        lines += [f"var __bank = studio.project.lookup('bank:/' + {_q(bank_name)});",
                  "if (__bank) { __ev.relationships.banks.add(__bank); }"]
    lines.append("'created ' + __ev.getPath() + ' (' + __ev.id + ')';")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MCP wiring
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return _generic_tools() + [gt.tool() for gt in _GENERATED.values()]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    args = arguments or {}
    if name in _GENERATED:
        text = _run_generated(_GENERATED[name], args)
    else:
        text = _generic_dispatch(name, args)
    return [types.TextContent(type="text", text=text)]


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())
