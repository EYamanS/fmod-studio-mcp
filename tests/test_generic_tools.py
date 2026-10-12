"""The generic tools, tested by running the JavaScript they generate.

``_generic_script`` builds the script; node runs it against a stand-in FMOD
object graph. That keeps the test on real generated code rather than on a string
that merely looks right.
"""

from __future__ import annotations

import json

from fmod_studio_mcp.server import _generic_script, _generic_tools

from conftest import requires_node

pytestmark = requires_node

# Tools whose reply is a value read out of the PROJECT, so nothing the server
# controls bounds its size. Each one must route through a capped entry point.
VALUE_REPLY_TOOLS = {
    "fmod_get_property": {"target": "event:/A", "property": "pad"},
}

# Tools whose reply size is fixed by the API surface or is a constant sentence.
# There is nothing to bound: whatever the project contains, they answer with the
# same shape. fmod_class_names is here rather than above on purpose: its list
# comes from FMOD's entity registry, it measured 3,684 characters on 2.03.13,
# and truncating it would break class discovery.
FIXED_REPLY_TOOLS = {
    "fmod_class_names": {},
    "fmod_describe_class": {"className": "Event"},
    "fmod_set_property": {"target": "event:/A", "property": "name", "value": "x"},
    "fmod_add_relationship": {"target": "event:/A", "relationship": "banks", "other": "bank:/Master"},
    "fmod_remove_relationship": {"target": "event:/A", "relationship": "banks", "other": "bank:/Master"},
    "fmod_create_event": {"name": "X"},
}

# A stand-in for the slice of the FMOD model these tools touch. It records the
# calls it receives, so a test can prove which method actually ran.
FAKE_STUDIO = """
var __calls = [];
function __obj(path) {
  return {
    path: path,
    getPath: function () { return path; },
    relationships: { banks: {
      add: function (o) { __calls.push("add " + o.path); },
      remove: function (o) { __calls.push("remove " + o.path); }
    } }
  };
}
var studio = { project: { lookup: function (path) {
  return path === "event:/missing" ? undefined : __obj(path);
} } };
"""


def run_script(run_node, script: str):
    source = FAKE_STUDIO + f"\nconsole.log(JSON.stringify(eval({json.dumps(script)})));"
    return json.loads(run_node(source))


def run_generic(run_node, name: str, **args):
    return run_script(run_node, _generic_script(name, args))


def test_adding_a_relationship_reports_added(run_node):
    assert run_generic(run_node, "fmod_add_relationship",
                       target="event:/A", relationship="banks", other="bank:/Master") == "added"


def test_removing_a_relationship_reports_removed(run_node):
    """The reply used to read 'removeed'."""
    assert run_generic(run_node, "fmod_remove_relationship",
                       target="event:/A", relationship="banks", other="bank:/Master") == "removed"


def test_removing_a_relationship_calls_remove(run_node):
    """The wording follows the call, so check the call itself too."""
    script = _generic_script("fmod_remove_relationship",
                             {"target": "event:/A", "relationship": "banks", "other": "bank:/Master"})
    source = FAKE_STUDIO + f"\neval({json.dumps(script)});\nconsole.log(JSON.stringify(__calls));"
    assert json.loads(run_node(source)) == ["remove bank:/Master"]


def test_adding_a_relationship_calls_add(run_node):
    script = _generic_script("fmod_add_relationship",
                             {"target": "event:/A", "relationship": "banks", "other": "bank:/Master"})
    source = FAKE_STUDIO + f"\neval({json.dumps(script)});\nconsole.log(JSON.stringify(__calls));"
    assert json.loads(run_node(source)) == ["add bank:/Master"]


def test_a_missing_object_is_reported_not_guessed(run_node):
    assert run_generic(run_node, "fmod_add_relationship",
                       target="event:/missing", relationship="banks", other="bank:/Master") == "not found"


def test_an_unknown_tool_builds_no_script():
    assert _generic_script("fmod_nonexistent", {}) is None


def test_every_generic_tool_is_classified():
    """A new generic tool has to be added to one of the two sets above. Without
    this it could return an unbounded value and nothing would notice."""
    advertised = {tool.name for tool in _generic_tools()}
    assert advertised == set(VALUE_REPLY_TOOLS) | set(FIXED_REPLY_TOOLS)


def test_every_generic_tool_that_returns_a_value_bounds_it():
    for name, args in VALUE_REPLY_TOOLS.items():
        script = _generic_script(name, args)
        assert "__render(" in script or "__cap(" in script, name


def test_a_large_property_is_bounded(run_node):
    """``fmod_get_property`` can return a whole relationship list. The bound has
    to cover that, not just the single-object case."""
    fake = """
    var studio = { project: { lookup: function () {
      var a = [];
      for (var i = 0; i < 50; i++) a.push({ pad: new Array(500).join("z") });
      return { pad: a };
    } } };
    """
    script = _generic_script("fmod_get_property", {"target": "event:/A", "property": "pad"})
    out = run_node(fake + f"\nconsole.log(String(eval({json.dumps(script)})));")
    assert "chars total" in out, "the list was not bounded"
    assert len(out.strip()) < 4200, f"rendered {len(out.strip())} characters"
