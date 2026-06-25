# fmod-studio-mcp

An [MCP](https://modelcontextprotocol.io) server that drives **FMOD Studio live**
through its built-in **scripting terminal** (TCP, default `127.0.0.1:3663`). Unlike
file-based approaches that edit a project's XML on disk (and require closing/reopening
Studio), this talks to the *running* editor: changes appear immediately, and Studio
itself writes them — so there's no clobbering and no reload dance.

It exposes the **entire** [FMOD Studio Scripting API](https://www.fmod.com/docs/2.02/studio/scripting-api-reference.html)
via a `run_script` escape hatch, plus introspection and ergonomic wrappers for the
common authoring tasks.

> ⚠️ **Alpha.** Built for an AI agent (Claude Code) to author game audio. It edits the
> *live* project — call `fmod_save` to persist, and keep it version-controlled.

## How it works

FMOD Studio's scripting console (open it in Studio with **Ctrl + 0**) listens on a TCP
port and evaluates anything it receives as UTF-8 JavaScript, returning the result as
text. This server keeps one connection and uses *read-until-idle* framing, so it doesn't
depend on a particular prompt string.

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

## Tools

| Tool | Purpose |
|---|---|
| `fmod_run_script` | Run arbitrary Scripting-API JavaScript — **full API access**. |
| `fmod_eval` | Evaluate one expression, return its value. |
| `fmod_project_info` | Is a project open? Its file path. |
| `fmod_save` / `fmod_build` | Save the project / build banks. |
| `fmod_class_names` | List managed class names (introspection). |
| `fmod_lookup` | Resolve a path/GUID → path + id. |
| `fmod_dump` | Dump an object's properties + relationships. |
| `fmod_create` | Create a managed object of any class. |
| `fmod_import_audio` | Import an audio file. |
| `fmod_create_event` | Create an event, optionally with a one-shot sound + bank assignment. |

Anything not covered by a wrapper is reachable through `fmod_run_script` — e.g.:

```js
studio.project.lookup("event:/SFX/Hit").relationships.banks.add(
  studio.project.lookup("bank:/Master"));
```

## Safety

- Edits the **live** project. Run `fmod_save` to persist; commit the project to git.
- Avoid editing the same project in the Studio GUI and via this server simultaneously in
  conflicting ways.

## License

MIT — see [LICENSE](LICENSE).
