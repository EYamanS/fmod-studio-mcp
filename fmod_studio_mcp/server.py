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

import json
import os

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

from .client import FmodTerminal, FmodTerminalError
from .generation import GeneratedTool, build_generated_tools, embed_value, _DESC
from . import guardrails

HOST = os.environ.get("FMOD_STUDIO_HOST", "127.0.0.1")
PORT = int(os.environ.get("FMOD_STUDIO_PORT", "3663"))

# Seconds to keep reading a reply before giving up. A project build is the one
# call that legitimately runs long, so it gets its own window.
_REPLY_WINDOW = 30.0
_BUILD_WINDOW = 180.0
_NO_OUTPUT = "(no output)"

# Tool name -> (method to call on the relationship, what the reply says happened).
# A table, so the word reported back can never drift from the method called.
_RELATIONSHIP_OPS = {
    "fmod_add_relationship": ("add", "added"),
    "fmod_remove_relationship": ("remove", "removed"),
}

app = Server("fmod-studio-mcp")
TERMINAL = FmodTerminal(HOST, PORT)


def _q(value) -> str:
    return json.dumps(str(value))


def _result(text: str, *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], isError=is_error
    )


def _run(script: str, overall: float = _REPLY_WINDOW) -> types.CallToolResult:
    """Run a script on the live terminal and report it as a tool result.

    A script error or an unreachable terminal is a failed call, marked as one, so
    a failure cannot be mistaken for a value. An empty string is a value and is
    passed through; only a terminal that sent nothing says "(no output)".
    """
    try:
        reply = TERMINAL.run(script, overall=overall)
    except FmodTerminalError as exc:
        return _result(f"ERROR: {exc}", is_error=True)
    return _result(_NO_OUTPUT if reply is None else reply)


# ---------------------------------------------------------------------------
# Generated tools — one per documented API member
# ---------------------------------------------------------------------------

_GENERATED: dict[str, GeneratedTool] = {gt.name: gt for gt in build_generated_tools()}


def _run_generated(gt: GeneratedTool, args: dict) -> types.CallToolResult:
    # build() can take a while; give bank builds a generous window.
    overall = _BUILD_WINDOW if gt.spec["member"].lower().startswith("build") else _REPLY_WINDOW
    # A member that can move or destroy data answers with a read-back instead of
    # its ordinary reply, so the caller sees which object it touched. Everything
    # else keeps the plain generated script.
    script = guardrails.generated_script(gt, args) or gt.build_js(args)
    return _run(script, overall=overall)


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


def _generic_script(name: str, a: dict) -> str | None:
    """Build the JavaScript for a generic tool, or None if the name is unknown.

    Kept separate from running it so the generated script can be executed and
    checked on its own.
    """
    if name == "fmod_get_property":
        return (f"{_DESC} var __o = studio.project.lookup({_q(a['target'])}); "
                f"__o ? __render(__o[{_q(a['property'])}]) : 'not found';")
    if name == "fmod_set_property":
        return (f"var __o = studio.project.lookup({_q(a['target'])}); "
                f"if (!__o) 'not found'; else {{ __o[{_q(a['property'])}] = {embed_value(a['value'])}; "
                f"'set ' + {_q(a['property'])}; }}")
    if name in _RELATIONSHIP_OPS:
        op, done = _RELATIONSHIP_OPS[name]
        return (f"var __o = studio.project.lookup({_q(a['target'])}); "
                f"var __x = studio.project.lookup({_q(a['other'])}); "
                f"if (!__o || !__x) 'not found'; else {{ __o.relationships[{_q(a['relationship'])}].{op}(__x); "
                f"'{done}'; }}")
    if name == "fmod_class_names":
        # Deliberately uncapped. This list is set by FMOD's entity registry, not
        # by project content, so there is nothing here to flood with. It measured
        # 3,684 characters on 2.03.13, near enough to the 4,000 bound that capping
        # it would break class discovery outright on a fatter FMOD build.
        return "JSON.stringify(Object.keys(studio.project.model).sort());"
    if name == "fmod_describe_class":
        # Uncapped for the same reason: the property and relationship names come
        # from the class schema, which is fixed. It measured 551-822 characters.
        return (
            f"var __e = studio.project.model[{_q(a['className'])}]; "
            "__e ? JSON.stringify({"
            "properties: Object.keys(__e.properties), "
            "relationships: Object.keys(__e.relationships)"
            "}, null, 1) : 'unknown class';")
    if name == "fmod_create_event":
        return _js_create_event(a["name"], a.get("sound"), a.get("bank_name"), a.get("folder_path"))
    return None


def _generic_dispatch(name: str, a: dict) -> types.CallToolResult:
    script = _generic_script(name, a)
    if script is None:
        return _result(f"ERROR: unknown tool {name}", is_error=True)
    # `fmod_set_property` is the wide door: it writes any property on any object.
    # Its guarded form reports the value before and after, so a wrong target is
    # visible in the reply rather than discovered later.
    return _run(guardrails.generic_script(name, a) or script)


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

# The advertised tool set is fixed once the spec is loaded, so build it once
# rather than rebuilding 155 schemas on every tools/list request.
_TOOLS: list[types.Tool] = _generic_tools() + [gt.tool() for gt in _GENERATED.values()]


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return _TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
    args = arguments or {}
    if name in _GENERATED:
        return _run_generated(_GENERATED[name], args)
    return _generic_dispatch(name, args)


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())
