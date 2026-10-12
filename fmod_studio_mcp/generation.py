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

# A path that identifies its subject ends in a name segment, as in `bank:/Master`.
# One that stops at the separator carries no name at all and is therefore the
# parent's path restated. FMOD renders a nameless object exactly that way:
# observed on a live 2.03.13 terminal, three `studio.project.create('Bank')`
# calls each answered `bank:/` and two `MixerGroup` calls each answered `bus:/`.
# Two distinct objects must not render the same text, or a caller can neither
# tell them apart nor address the one it wants.
_PATH_PREFIX_RE = re.compile(r"^[a-z]+:/$")

# A JS helper, prepended to every generated script, that renders any result as a
# stable string: objects -> their path or id (so results can be chained back in as
# a `target`), arrays -> a JSON list of the same, primitives -> String().
#
# getPath() is NOT on every object. Only four entity types have it (Bank, Event,
# MixerStrip, ParameterPreset); a folder, track, sound or asset does not. The
# live terminal 2.03.13 answers `studio.project.lookup("event:/").getPath()` with
# "TypeError: not a function", so the member must be type-checked, never assumed.
#
# getPath() is also not always the *object's* path. A newly created object has an
# empty name, so FMOD reports the parent's path, and the id is the only thing
# left that identifies it. The id is added in that one case, and it is what makes
# the reply usable: `{guid}` is an addressing form `project.lookup` accepts, so a
# caller can name the object it just made without a save-and-read trip to disk.
#
# A path can also fail to address its subject while looking perfectly ordinary.
# An event's own mixer strips report paths in the *bus* namespace, and
# project.lookup does not answer them. Observed on a live 2.03.13 terminal,
# 2026-09-15, sweeping every model instance whose class answers findInstances and
# whose object answers getPath: of 2179 checked, 1058 could not be resolved by
# their own getPath(), and every one of them was an EventMixerGroup (535) or a
# MixerInput (523). Both are MixerStrip subclasses owned by an event: 523 event
# timelines each hold an EventMixerGroup reporting `bus:/Audio`, and no two are
# the same object. So that path addresses none of them, and two of them render
# identically. This is the reported ghost bus: fmod_get_property(bus:/, 'input')
# listed a `bus:/voice` that resolves to nothing and answers `undefined` to
# `name`, `color` and `volume`.
#
# So a path is not trusted for being present and well-formed. It is used when
# project.lookup answers it, which is exactly the question "can a caller hand
# this back as a target?". When it cannot, the id is the answer instead. The
# answer is not cached: a cached miss would be wrong the moment the object is
# renamed, and the check measured 2 ms for a 17-member relationship live.
#
# Objects that are neither managed (no getPath, no id) nor primitive are the gap:
# `studio.version` is one, and it carries a useful toString(), while a schema map
# like Event.relationships carries none and is only readable as JSON. So: prefer
# toString() when it says something, otherwise fall back to JSON.
#
# The bound is applied to the WHOLE result by __cap, at the end. __desc's own
# limit only stops one object from building a huge string; it does not bound a
# list, whose members are mapped individually. A list of 50 large objects
# measured 25,751 characters before __cap existed.
#
# __render is the single entry point for a value; __cap takes text that is
# already serialised (fmod_describe_class pretty-prints its JSON).
_DESC = (
    "var __DESC_MAX = 4000;"
    "var __PATH_PREFIX=" + json.dumps(_PATH_PREFIX_RE.pattern) + ";"
    "function __pathOrId(x,p){"
    "if(!p)return String(x.id);"
    "try{if(!studio.project.lookup(p))return String(x.id);}catch(e){return p;}"
    "if(new RegExp(__PATH_PREFIX).test(p))return p+' '+String(x.id);"
    "return p;}"
    "function __desc(x){"
    "if(x===null||x===undefined)return String(x);"
    "if(Array.isArray(x))return JSON.stringify(x.map(__desc));"
    "if(typeof x==='object'){"
    "if(typeof x.getPath==='function'){"
    "var __p=x.getPath();"
    "if(typeof __p==='string'&&x.id!==undefined)return __pathOrId(x,__p);"
    "return __p;}"
    "if(x.id!==undefined)return String(x.id);"
    "var __s=String(x);"
    "if(__s!=='[object Object]')return __s;"
    "try{var __j=JSON.stringify(x);"
    "if(__j!==undefined)return __j.length>__DESC_MAX?"
    "__j.slice(0,__DESC_MAX)+'\u2026 ('+__j.length+' chars total)':__j;}catch(e){}"
    "return __s;}"
    "return String(x);}"
    "function __cap(s){s=String(s);return s.length>__DESC_MAX?"
    "s.slice(0,__DESC_MAX)+'\u2026 ('+s.length+' chars total)':s;}"
    "function __render(x){return __cap(__desc(x));}"
)

_PATH_RE = re.compile(r"^(event|bank|bus|vca|snapshot|parameter|tag|preset):/")
_GUID_RE = re.compile(r"^\{[0-9a-fA-F-]+\}$")
_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")

# `project.lookup` is the one member whose parameter *is* the identifier the
# member exists to resolve, so the conversion below must not run for it. Its
# receiver is already `studio.project`, and converting the argument produced a
# lookup of a lookup:
#
#     __render(studio.project.lookup(studio.project.lookup("event:/SFX/footstep")))
#
# Observed on a live 2.03.13 terminal, against a path that resolves:
#
#     studio.project.lookup("event:/Bedtime/vo/emb_vo/vo_emb_bingo_yeah")
#         -> (ManagedObject:Event); typeof result.lookup === "undefined"
#     <that result>.lookup(...)
#         -> ERROR TypeError: not a function
#
# Every valid input failed the same way, and the cause is not the receiver: the
# inner call is correct, and the outer call cannot exist. It is the argument that
# has to stay text. No ManagedObject has `.lookup`, so no object this conversion
# can produce is ever the right argument here.
_NO_CONVERSION = {("project", "lookup")}


def _takes_identifier_verbatim(spec: dict) -> bool:
    """True for the member that resolves the identifier itself.

    Named as a question about the member rather than as a check in
    :meth:`GeneratedTool.build_js`, so the escape is one idea in one place and a
    second member needing it is a one-line change with a test to argue against.
    """
    return (spec["owner"], spec["member"]) in _NO_CONVERSION


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
            # The member belongs to one entity type, not to ManagedObject at large:
            # `getPath` exists on Event and Bank but not on a folder or a track, and
            # calling it on the wrong one throws "TypeError: not a function" inside
            # Studio. Naming the expected type in the schema is what lets the caller
            # pick a target that can actually answer.
            props["target"] = {
                "type": "string",
                "description": f"Object to act on: a path (e.g. 'event:/SFX/Hit', 'bank:/Master') "
                               f"or a '{{guid}}'. This member is defined on "
                               f"{self.spec['owner']}, not on every object, so the target "
                               f"has to be one.",
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
        literal = _takes_identifier_verbatim(s)
        if s["kind"] == "method":
            arg_js = []
            for p in s["params"]:
                if p["name"] in args and args[p["name"]] is not None:
                    raw = str(args[p["name"]]).strip()
                    arg_js.append(json.dumps(raw) if literal else embed_value(args[p["name"]]))
                elif not p["optional"]:
                    arg_js.append("undefined")
            call = (s["member"] if s["target_kind"] == "global"
                    else f"{recv}.{s['member']}")
            expr = f"{call}({', '.join(arg_js)})"
        else:
            # Only a global member has no receiver, and it is handled above.
            access = s["member"] if s["target_kind"] == "global" else f"{recv}.{s['member']}"
            if not s["immutable"] and args.get("value") is not None:
                expr = f"({access} = {embed_value(args['value'])})"
            else:
                expr = access
        return f"{_DESC} __render({expr});"


def load_spec(path: str = SPEC_PATH) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_generated_tools(path: str = SPEC_PATH) -> list[GeneratedTool]:
    return [GeneratedTool(s) for s in load_spec(path)]
