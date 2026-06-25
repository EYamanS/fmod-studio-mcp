# fmod-studio-mcp

An [MCP](https://modelcontextprotocol.io) server that drives **FMOD Studio live**
through its built-in **scripting terminal** (TCP, default `127.0.0.1:3663`). Unlike
file-based approaches that edit a project's XML on disk (and require closing/reopening
Studio), this talks to the *running* editor: changes appear immediately, and Studio
itself writes them — so there's no clobbering and no reload dance.

It exposes the [FMOD Studio Scripting API](https://www.fmod.com/docs/2.02/studio/scripting-api-reference.html)
as **one MCP tool per API member** — generated from the crawled reference, not
hand-written — plus a few generic tools for the parts the static docs can't enumerate.
There is **no arbitrary-script / eval tool**: every capability is a named,
schema-validated operation over FMOD's object model.

> ⚠️ **Alpha.** Built for an AI agent (Claude Code) to author game audio. It edits the
> *live* project — call `fmod_project_save` to persist, and keep it version-controlled.

## How it works

FMOD Studio's scripting console (open it in Studio with **Ctrl + 0**) listens on a TCP
port and evaluates anything it receives as UTF-8 JavaScript, returning the result as
text. This server keeps one connection and uses *read-until-idle* framing, so it doesn't
depend on a particular prompt string.

Each tool generates the small piece of scripting-API JavaScript for its member, runs it,
and returns the result as a string. Anything that returns an object reports that object's
path or `{guid}`, so results chain straight back in as another tool's `target`.

## The tool set

**Generated — one per documented member (~148).** Named `fmod_<Owner>_<member>`, e.g.
`fmod_project_create`, `fmod_project_importAudioFile`, `fmod_Event_addGroupTrack`,
`fmod_GroupTrack_addSound`, `fmod_Bank_getPath`, `fmod_system_getText`. A member's
`target_kind` decides how it's reached:

| kind | reached as | tool inputs |
|---|---|---|
| `module` | `studio.project.*`, `studio.system.*`, `console.*` … | the member's args |
| `global` | `alert(...)` | the member's args |
| `entity` | `studio.project.model[<className>].*` (class introspection) | `className` + args |
| `instance` | `studio.project.lookup(target).*` | `target` (path or `{guid}`) + args |

Method args are auto-embedded: numbers/booleans as JS literals, a path
(`event:/…`, `bank:/Master`) or `{guid}` as an object reference (`lookup(...)`), else a
string. Settable properties take an optional `value` (omit to read).

**Generic — reach the dynamic, schema-defined members the static docs don't list**
(an event's `timeline`, an instrument's `audioFile`, a sound's `pitch`/`looping`, …):

| Tool | Purpose |
|---|---|
| `fmod_get_property` | Read any property of an object, including per-class managed properties. |
| `fmod_set_property` | Set any property (e.g. instrument `audioFile` = an asset). |
| `fmod_add_relationship` / `fmod_remove_relationship` | Edit a relationship (e.g. event `banks` → a bank). |
| `fmod_class_names` | List the project model's class names. |
| `fmod_describe_class` | List a class's schema-defined property + relationship names (discovery). |
| `fmod_create_event` | Composite: create event → import one-shot → add track/sound → assign bank. |

## Requirements

- **FMOD Studio 2.02+**, with a project open and the **scripting console enabled** (Ctrl+0,
  which starts the TCP listener on port 3663 — the console shows the IP/port).
- Python 3.10+.

## Install

```bash
git clone https://github.com/EYamanS/fmod-studio-mcp
cd fmod-studio-mcp
python3 -m venv .venv && ./.venv/bin/pip install -e .
```

Add it to Claude Code (stdio):

```bash
claude mcp add fmod-studio -- "$(pwd)/.venv/bin/python" -m fmod_studio_mcp
```

Configure host/port if needed via env: `FMOD_STUDIO_HOST` (default `127.0.0.1`),
`FMOD_STUDIO_PORT` (default `3663`).

## Regenerating for a new FMOD version

The tool set is generated from `fmod_studio_mcp/api_spec.json`, which is built by crawling
the official Scripting API reference:

```bash
python tools/build_spec.py fmod_studio_mcp/api_spec.json
```

A new FMOD release means re-running that crawler — not editing a tool registry by hand.

## Example

Create an event with a one-shot sound and route it to the Master bank, step by step
(or use the `fmod_create_event` composite to do it in one call):

```
fmod_project_create          { entityName: "Event" }            -> event:/New Event
fmod_set_property            { target: "event:/New Event", property: "name", value: "Hit" }
fmod_project_importAudioFile { filePath: "/abs/path/hit.wav" }  -> {asset-guid}
fmod_Event_addGroupTrack     { target: "event:/Hit", name: "Audio 1" } -> {track-guid}
fmod_get_property            { target: "event:/Hit", property: "timeline" } -> {tl-guid}
fmod_GroupTrack_addSound     { target: "{track-guid}", parameter: "{tl-guid}",
                               soundType: "SingleSound", start: 0, length: 2.5 } -> {inst-guid}
fmod_set_property            { target: "{inst-guid}", property: "audioFile", value: "{asset-guid}" }
fmod_add_relationship        { target: "event:/Hit", relationship: "banks", other: "bank:/Master" }
fmod_project_save            {}
```

## Safety

- Edits the **live** project. Run `fmod_project_save` to persist; commit the project to git.
- Avoid editing the same project in the Studio GUI and via this server simultaneously in
  conflicting ways.

## License

MIT — see [LICENSE](LICENSE).
