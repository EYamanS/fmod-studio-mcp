"""Turn the crawled FMOD Scripting API spec (``api_spec.json``) into one MCP tool
per API member.

The spec is the single source of truth: re-crawl a newer FMOD reference, drop in a
new ``api_spec.json``, and the tool set regenerates — no hand-edited registry. Each
spec entry becomes a ``GeneratedTool`` that knows how to build the JavaScript for
its member and run it on the live Studio.

A member is reached differently depending on ``target_kind``:

* ``module``   — fixed receiver (``studio.project``, ``studio.system`` …); no target.
* ``global``   — a bare global call (``alert(...)``).
* ``entity``   — class introspection; receiver is ``studio.project.model[<className>]``.
* ``instance`` — the caller passes ``target`` (a path like ``event:/SFX/Hit`` or a
  ``{guid}``), resolved with ``studio.project.lookup(target)``.

Method arguments are embedded by :func:`embed_value`: numbers/booleans become JS
literals, anything that looks like a path or ``{guid}`` becomes a ``lookup(...)`` so
object-reference parameters work, and everything else is a quoted string.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from mcp import types

SPEC_PATH = os.path.join(os.path.dirname(__file__), "api_spec.json")

# A JS helper, prepended to every generated script, that renders any result as a
# stable string: objects -> their path or id (so results can be chained back in as
# a `target`), arrays -> a JSON list of the same, primitives -> String().
_DESC = (
    "function __desc(x){"
    "if(x===null||x===undefined)return String(x);"
    "if(Array.isArray(x))return JSON.stringify(x.map(__desc));"
    "if(typeof x==='object')return (typeof x.getPath==='function'?x.getPath():"
    "(x.id!==undefined?x.id:'[object]'));"
    "return String(x);}"
)

_PATH_RE = re.compile(r"^(event|bank|bus|vca|snapshot|parameter|tag|preset):/")
_GUID_RE = re.compile(r"^\{[0-9a-fA-F-]+\}$")
_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")


def embed_value(v) -> str:
    """Render a tool-argument value as a JavaScript expression."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, dict)):
        return json.dumps(v)
    s = str(v).strip()
    if _NUM_RE.match(s):
        return s
    if s in ("true", "false", "null"):
        return s
    if _PATH_RE.match(s) or _GUID_RE.match(s):
        return f"studio.project.lookup({json.dumps(s)})"
    return json.dumps(s)


def _sanitize(part: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", part).strip("_")


@dataclass
class GeneratedTool:
    spec: dict

    @property
    def owner(self) -> str:
        return "global" if self.spec["owner"] == "(global)" else self.spec["owner"]

    @property
    def name(self) -> str:
        return f"fmod_{_sanitize(self.owner)}_{_sanitize(self.spec['member'])}"

    # -- input schema -------------------------------------------------------

    def input_schema(self) -> dict:
        props: dict = {}
        required: list[str] = []
        tk = self.spec["target_kind"]
        if tk == "instance":
            props["target"] = {
                "type": "string",
                "description": "Object to act on: a path (e.g. 'event:/SFX/Hit', 'bank:/Master') or a '{guid}'.",
            }
            required.append("target")
        elif tk == "entity":
            props["className"] = {
                "type": "string",
                "description": "Model class name, e.g. 'Event', 'Bank', 'GroupTrack'.",
            }
            required.append("className")

        if self.spec["kind"] == "method":
            for p in self.spec["params"]:
                props[p["name"]] = {
                    "type": ["string", "number", "boolean"],
                    "description": (p.get("doc") or "")
                    + (" (optional)" if p["optional"] else ""),
                }
                if not p["optional"]:
                    required.append(p["name"])
        elif not self.spec["immutable"]:
            # settable property: optional value writes it, omitted reads it
            props["value"] = {
                "type": ["string", "number", "boolean"],
                "description": "New value to assign. Omit to read the current value.",
            }

        schema: dict = {"type": "object", "properties": props}
        if required:
            schema["required"] = required
        return schema

    def description(self) -> str:
        s = self.spec
        sig = f"{s['owner']}.{s['member']}"
        bits = [s["description"] or sig]
        if s["returns"]:
            bits.append(s["returns"])
        if s["kind"] == "method" and any(p["optional"] for p in s["params"]):
            bits.append("(bracketed args are optional)")
        bits.append(f"[{s['kind']} · {sig}]")
        return " ".join(b for b in bits if b)

    def tool(self) -> types.Tool:
        return types.Tool(name=self.name, description=self.description(),
                          inputSchema=self.input_schema())

    # -- JS generation ------------------------------------------------------

    def _receiver(self, args: dict) -> str:
        tk = self.spec["target_kind"]
        if tk == "module":
            return self.spec["receiver"]
        if tk == "global":
            return ""  # the member itself is the callable
        if tk == "entity":
            return f"studio.project.model[{json.dumps(str(args['className']))}]"
        return f"studio.project.lookup({json.dumps(str(args['target']))})"

    def build_js(self, args: dict) -> str:
        s = self.spec
        recv = self._receiver(args)
        if s["kind"] == "method":
            arg_js = []
            for p in s["params"]:
                if p["name"] in args and args[p["name"]] is not None:
                    arg_js.append(embed_value(args[p["name"]]))
                elif not p["optional"]:
                    arg_js.append("undefined")
            call = (s["member"] if s["target_kind"] == "global"
                    else f"{recv}.{s['member']}")
            expr = f"{call}({', '.join(arg_js)})"
        else:
            access = s["member"] if s["target_kind"] == "global" else (
                f"{recv}.{s['member']}" if recv else s["member"])
            if not s["immutable"] and args.get("value") is not None:
                expr = f"({access} = {embed_value(args['value'])})"
            else:
                expr = access
        return f"{_DESC} __desc({expr});"


def load_spec(path: str = SPEC_PATH) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_generated_tools(path: str = SPEC_PATH) -> list[GeneratedTool]:
    return [GeneratedTool(s) for s in load_spec(path)]
