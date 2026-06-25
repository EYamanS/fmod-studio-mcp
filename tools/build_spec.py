"""Parse the FMOD Studio 2.02 Scripting API reference (CloudFront member pages)
into api_spec.json — the single source of truth the MCP server turns into one
tool per member.

Member heading:  <h2 api="function" id="eventaddgrouptrack">Event.addGroupTrack</h2>
followed by prose, an optional <pre> signature, a <dt>/<dd> param list, and a
"Returns ..."/"Immutable." sentence.

kind:  method  -> has a <pre> signature OR the heading shows "(...)"
       property-> neither (a bare data accessor)
target_kind: module   (fixed receiver, no target)      e.g. studio.project.create
             global   (bare global call)               e.g. alert(msg)
             entity   (receiver studio.project.model[Class], needs className)
             instance (agent supplies target path/GUID, resolved via lookup)
"""
import json, re, subprocess, sys
from collections import Counter

BASE = "https://d1s9dnlmdewoh1.cloudfront.net/2.02/studio/scripting-api-reference-{}.html"
SLUGS = [
    "globals", "menu", "project", "project-entity", "project-managedobject",
    "project-model", "project-model-asset", "project-model-automatableobject",
    "project-model-automationcurve", "project-model-automator", "project-model-bank",
    "project-model-event", "project-model-folder", "project-model-mixereffect",
    "project-model-mixerstrip", "project-model-modulator", "project-model-parameter",
    "project-model-sound", "project-model-track", "project-model-triggerable",
    "project-workspace", "system", "ui", "window",
]

# owner token -> fixed JS receiver (no target arg needed)
MODULE_RECEIVER = {
    "console": "console", "system": "studio.system", "ui": "studio.ui",
    "window": "studio.window", "menu": "studio.menu", "project": "studio.project",
    "workspace": "studio.project.workspace", "studio": "studio",
}
GLOBAL_FUNCS = {"alert"}
# Skipped: dynamic/templated accessors (model.*, %name%) and the Managed* sub-object
# mechanism — the latter is redundant with the generic get/set/relationship tools and
# its instances aren't reachable by a stable path/GUID the agent can supply.
SKIP_OWNERS = {"model", "File", "ScriptProcess", "Process",
               "ManagedProperty", "ManagedRelationship",
               "ManagedPropertyMap", "ManagedRelationshipMap"}

HEAD = re.compile(r'<(h[2-4])\b[^>]*\bid="([^"]*)"[^>]*>(.*?)</\1>(.*?)(?=<h[2-4]\b|\Z)', re.S)
PRE = re.compile(r"<pre\b[^>]*>(.*?)</pre>", re.S)
PARAM_DT = re.compile(r"<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", re.S)


def fetch(slug):
    return subprocess.run(["curl", "-sL", "-A", "Mozilla/5.0", BASE.format(slug)],
                          capture_output=True, text=True, timeout=30).stdout


def text_of(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def parse_params(sig):
    """'system.getText(msg[, defaultText])' -> [{name:'msg',optional:False},
    {name:'defaultText',optional:True}].  Optionality follows [ ] bracket nesting."""
    m = re.search(r"\(([^)]*)\)", sig)
    if not m:
        return []
    depth, cur, cur_opt, out = 0, "", False, []

    def flush():
        nm = re.sub(r"[^A-Za-z0-9_]", "", cur)
        if nm:
            out.append({"name": nm, "optional": cur_opt})

    for ch in m.group(1):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        elif ch == ",":
            flush(); cur, cur_opt = "", False
        else:
            cur += ch
            if depth > 0:
                cur_opt = True
    flush()
    return out


def main():
    spec, skipped = [], []
    for slug in SLUGS:
        html = fetch(slug)
        for mm in HEAD.finditer(html):
            title = text_of(mm.group(3))
            block = mm.group(4)
            if not title or title.startswith(("Module:", "Class:")) or "%" in title:
                continue
            base = title.split("(")[0].strip()
            if "." in base:
                owner, member = base.split(".", 1)
            elif base in GLOBAL_FUNCS:
                owner, member = "", base
            else:
                continue  # bare class/section header
            member = member.strip()
            if not member or owner in SKIP_OWNERS:
                if owner in SKIP_OWNERS:
                    skipped.append(title)
                continue
            if member.split(".")[0][:1].isupper():
                skipped.append(title); continue  # e.g. model.Event accessor

            pre = PRE.search(block)
            sig_text = text_of(pre.group(1)) if pre else title
            # A method's signature shows "(...)"; a property accessor never does.
            kind = "method" if "(" in sig_text else "property"
            immutable = "Immutable." in text_of(block)
            params = parse_params(sig_text) if kind == "method" else []

            docs = {re.sub(r"[^A-Za-z0-9_]", "", text_of(dt)): text_of(dd)
                    for dt, dd in PARAM_DT.findall(block)}
            for p in params:
                p["doc"] = docs.get(p["name"], "")

            pm = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
            desc = text_of(pm.group(1)) if pm else ""
            rm = re.search(r"Returns?\b([^.]*\.)", text_of(block))
            ret = ("Returns" + rm.group(1)).strip() if rm else ""

            if owner == "":
                target_kind, receiver = "global", member  # alert(...)
            elif owner == "entity":
                target_kind, receiver = "entity", None     # studio.project.model[Class].<member>
            elif owner in MODULE_RECEIVER:
                target_kind, receiver = "module", MODULE_RECEIVER[owner]
            else:
                target_kind, receiver = "instance", None    # lookup(target).<member>

            spec.append({
                "module": slug, "owner": owner or "(global)", "member": member,
                "kind": kind, "immutable": immutable, "target_kind": target_kind,
                "receiver": receiver, "params": params, "returns": ret,
                "description": desc, "signature": sig_text,
            })

    seen, uniq = set(), []
    for s in spec:
        k = (s["owner"], s["member"])
        if k not in seen:
            seen.add(k); uniq.append(s)

    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/api_spec.json"
    with open(out, "w") as f:
        json.dump(uniq, f, indent=2)

    print(f"members: {len(uniq)}  kinds={dict(Counter(s['kind'] for s in uniq))}  "
          f"target={dict(Counter(s['target_kind'] for s in uniq))}  skipped={len(skipped)}")
    by = {(s["owner"], s["member"]): s for s in uniq}
    print("\n--- spot checks ---")
    for key in [("Event", "addGroupTrack"), ("Event", "isPlaying"), ("GroupTrack", "addSound"),
                ("ManagedObject", "id"), ("ManagedObject", "relationships"), ("project", "create"),
                ("project", "importAudioFile"), ("system", "getText"), ("(global)", "alert"),
                ("console", "log"), ("Bank", "getPath"), ("MarkerTrack", "addNamedMarker"),
                ("entity", "findInstances"), ("studio", "version"), ("Sound", "setFadeInCurve")]:
        s = by.get(key)
        if not s:
            print(f"  {key[0]}.{key[1]:<20} MISSING"); continue
        ps = ",".join(p["name"] + ("?" if p["optional"] else "") for p in s["params"])
        print(f"  {key[0]}.{key[1]:<20} {s['kind']:<8} {s['target_kind']:<9} "
              f"recv={s['receiver']} ({ps})")


if __name__ == "__main__":
    main()
