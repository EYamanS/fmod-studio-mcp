"""Value rendering for generated tools.

Every generated tool prepends the ``__desc`` helper to its script inside FMOD.
That helper is JavaScript, so these tests run the real thing under node against
stand-in values shaped like the ones the API actually returns.

The shapes and the expected text come from probing a live FMOD Studio 2.03.13
terminal, not from reading the code:

    studio.version                      object, no getPath, no id,
                                        String() -> "Version 2.03.13, 64-bit, ..."
    Event.properties                    object, String() -> "[object Object]",
                                        JSON -> 450 chars
    Event.relationships                 object, JSON -> 1911 chars
    Event.properties['note']            object, JSON -> 39 chars
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from fmod_studio_mcp.generation import _DESC

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is needed to execute the generated JavaScript")

VERSION_OBJECT = '{ toString: function () { return "Version 2.03.13, 64-bit, Qt 6.5.3 under LGPL, Build #162576"; } }'
MANAGED_OBJECT = '{ getPath: function () { return "event:/SFX/Hit"; } }'
# What studio.project.lookup("event:/") actually returns: a folder, which has an
# id and an entity but NO getPath. getPath() is defined on Event, Bank,
# MixerStrip and ParameterPreset only, so the helper must not assume it.
FOLDER_OBJECT = '{ id: "{a1935f59-5c41-439f-a224-d6517b1fa236}", entity: "MasterEventFolder" }'
# An object that answers getPath with something that is not a function.
NOT_A_FUNCTION = '{ getPath: 42, id: "{b}" }'
# A newly created object has an empty name, and FMOD renders its path with no
# name segment at all, so it is the parent's path. Observed on a live 2.03.13
# terminal: three ``studio.project.create('Bank')`` calls each returned the
# identical string "bank:/", and two ``MixerGroup`` calls each returned "bus:/".
# Two distinct objects, therefore, that this helper must not render identically.
NAMELESS_BANK_A = '{ getPath: function () { return "bank:/"; }, id: "{1111-aaaa}", entity: "Bank" }'
NAMELESS_BANK_B = '{ getPath: function () { return "bank:/"; }, id: "{2222-bbbb}", entity: "Bank" }'

CASES = {
    # a managed object still reports its path, which is what makes results chain
    "managed": (MANAGED_OBJECT, "event:/SFX/Hit"),
    "managed_array": (f"[{MANAGED_OBJECT}, 2]", '["event:/SFX/Hit","2"]'),
    "id_only": ('{ id: "{1234-5678}" }', "{1234-5678}"),
    # the regression: a folder has no getPath, and asking for one used to throw
    # "TypeError: not a function" on a live 2.03.13 terminal.
    "folder_without_getpath": (FOLDER_OBJECT, "{a1935f59-5c41-439f-a224-d6517b1fa236}"),
    "folder_in_array": (f"[{FOLDER_OBJECT}]", '["{a1935f59-5c41-439f-a224-d6517b1fa236}"]'),
    # a getPath that is not callable must fall through, not throw
    "non_callable_getpath": (NOT_A_FUNCTION, "{b}"),
    # an object with its own toString, like studio.version, keeps that text
    "custom_tostring": (VERSION_OBJECT, "Version 2.03.13, 64-bit, Qt 6.5.3 under LGPL, Build #162576"),
    # a plain object has no useful String(), so its contents are the answer
    "plain_object": ('{ note: "hello", color: "0,0,0" }', '{"note":"hello","color":"0,0,0"}'),
    "nested_object": ('{ a: { b: 2 } }', '{"a":{"b":2}}'),
    "empty_object": ("{}", "{}"),
    # Array members go through the same rule as a single value, so they come out
    # as strings. That is the existing contract, kept here deliberately.
    "primitive_array": ("[1, 2.5, true, false, null, \"hi\"]", '["1","2.5","true","false","null","hi"]'),
    # a nameless object has the same path as its parent, so the path alone cannot
    # tell two of them apart. The id is what makes the reply usable, and
    # '{guid}' is an addressing form lookup already accepts.
    "nameless_object": (NAMELESS_BANK_A, "bank:/ {1111-aaaa}"),
    # two banks made by two create calls, which used to render identically
    "two_nameless_objects": (f"[{NAMELESS_BANK_A}, {NAMELESS_BANK_B}]",
                             '["bank:/ {1111-aaaa}","bank:/ {2222-bbbb}"]'),
    # a bank with a real name keeps reporting just its path, so results still
    # chain back in as a target
    "a_named_object_is_not_padded": ('{ getPath: function () { return "bank:/Master"; }, id: "{3333-cccc}" }',
                                     "bank:/Master"),
}

# A getPath that is *present but throws* is not a shape FMOD produces: of the 63
# model classes, 53 have no getPath and 0 of the 63 throw when it is called. The
# helper therefore does not paper over one. Swallowing the throw would hand back
# the object's id as if it were the path, which is a wrong answer; letting it
# out means the call fails where the caller can see it.
THROWING_GETPATH = '{ getPath: function () { throw new Error("boom"); }, id: "{c}" }'


# Stand-ins for the project's identifier space. A rendered string is only usable
# as a `target` when ``studio.project.lookup`` answers it, so the fake has to
# answer, and the set it answers is what each test is about.
#
# _GRAPH_ORDINARY: a real project resolves the paths its own objects report.
# Every rendering test that is not about an unaddressable path uses this, so
# those cases keep testing what they tested before the resolution check existed.
_GRAPH_ORDINARY = """
if (typeof studio === "undefined") { var studio = {}; }
studio.project = { lookup: function (p) { return {}; } };
"""

# _GRAPH_SET: answers exactly the listed paths and nothing else, so a test can
# say whether the path it renders is supposed to resolve.
_GRAPH_SET = """
var PATHS = new Set(%s);
if (typeof studio === "undefined") { var studio = {}; }
studio.project = { lookup: function (p) { return PATHS.has(p) ? {} : null; } };
"""


def render_raw(cases_js: str):
    """Run the helper and hand back the raw result, so a failure can be seen.

    The helper prepends the ordinary-project stub: these cases are about how a
    value is rendered, not about whether its path resolves, and a project that
    cannot answer lookup would push every one of them down the fallback.
    """
    script = _GRAPH_ORDINARY + "\n" + _DESC + "\n" + f"""
var __cases = {cases_js};
var __out = {{}};
Object.keys(__cases).forEach(function (k) {{ __out[k] = __desc(__cases[k]); }});
console.log(JSON.stringify(__out));
"""
    return subprocess.run([NODE, "-e", script], capture_output=True, text=True)


def render(cases_js: str) -> dict:
    proc = render_raw(cases_js)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def render_value(expr_js: str, graph: str = _GRAPH_ORDINARY) -> str:
    """Render one bare JavaScript value under a stand-in project and return the
    text ``__render`` produced.

    ``__render`` is the single entry point every generated and generic tool
    uses, so this exercises the same path the server does rather than a copy of
    it. The subject is passed bare, not wrapped, so a reply that is itself JSON
    -- a list of objects -- is not mistaken for the harness's own framing.

    JSON.stringify rather than console.log is what makes there be exactly one
    decoding step: console.log on a string prints the string, and on an object
    prints node's inspector, which is neither the server's rendering nor a
    stable thing to assert against.
    """
    script = graph + "\n" + _DESC + "\n" + f"console.log(JSON.stringify(__render({expr_js})));"
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def render_against(expr_js: str, paths) -> str:
    """The same, against a project that resolves exactly ``paths``.

    Whether a rendered string can be handed back as a `target` is decided by
    whether ``studio.project.lookup`` answers it, so a test about that decision
    has to supply the lookup, not merely a shape of object.
    """
    return render_value(expr_js, _GRAPH_SET % json.dumps(sorted(paths)))


@pytest.mark.parametrize("name", sorted(CASES))
def test_values_render_as_observed(name):
    js, expected = CASES[name]
    assert render(f"{{ v: {js} }}")["v"] == expected


def test_undefined_and_null_are_named_not_blank():
    assert render("{ a: undefined, b: null }") == {"a": "undefined", "b": "null"}


def test_a_huge_object_is_truncated_and_says_so():
    """An unbounded dump would pour into the model's context; it must be bounded."""
    out = render('{ v: (function () { var o = {}; for (var i = 0; i < 5000; i++) o["k" + i] = i; return o; })() }')["v"]
    assert out.startswith('{"k0":0')
    assert "chars total" in out
    assert len(out) < 6000


def test_a_getpath_that_throws_is_reported_not_hidden():
    """Silently falling back to the id would report a path that does not exist,
    which is worse than failing. The typeof guard covers every real shape; a
    getPath that exists and throws is a genuine fault and must surface."""
    proc = render_raw(f"{{ v: {THROWING_GETPATH} }}")
    assert proc.returncode != 0, f"the throw was swallowed: {proc.stdout!r}"
    assert "boom" in proc.stderr


def test_a_large_list_is_bounded_not_only_a_large_object():
    """The bound has to cover the whole result. It used to sit inside the object
    branch, and the array branch maps over its members with no total limit, so a
    list of 50 large objects rendered to 243,351 characters."""
    big = "[" + ",".join('{ a: "%s" }' % ("x" * 500) for _ in range(50)) + "]"
    out = render_value(big)
    assert "chars total" in out, "the list was not bounded"
    assert len(out) < 4200, f"rendered {len(out)} characters"


def test_a_small_result_is_not_truncated():
    """A bound that mangles ordinary answers is worse than none: results are
    chained back in as a `target`, so they have to stay usable."""
    assert render_value('"event:/SFX/Hit"') == "event:/SFX/Hit"
    assert render_value("42") == "42"


# --- a path that does not address its subject ------------------------------------
#
# `getPath()` is callable and returns a string, and the string can still fail to
# address the object. That is not a guess: an event's own mixer strips report
# paths in the *bus* namespace, and those paths do not resolve.
#
# Observed on a live 2.03.13 terminal, 2026-09-15, sweeping every model instance
# whose class answers findInstances and whose object answers getPath:
#
#     checked 2179, unaddressable by their own getPath() 1058
#     byEntity: { EventMixerGroup: 535, MixerInput: 523 }
#
# Both are MixerStrip subclasses owned by an event: 523 event timelines each hold
# an EventMixerGroup reporting `bus:/Audio`, and no two of them are the same
# object. So `bus:/Audio` addresses none of them.
#
# This is the mechanism behind the reported ghost bus. `fmod_get_property(bus:/,
# 'input')` listed a `bus:/voice` that resolves to nothing, is absent from
# Metadata/Group on disk, and answers `undefined` to `name`, `color` and
# `volume`. The master bus really does hold that member -- it is in the
# relationship list, with an id and `isValid === "true"` -- but its path does not
# address it, so it cannot be reached by the name it reports.
#
# The contract every reply already claims: what comes back can go back in as a
# `target`. A path that fails that is not a usable answer, and the guid is.

# An event-owned mixer strip: entity MixerInput, id present, path in the bus
# namespace. The name is absent, which is why `name` answers `undefined`.
EVENT_STRIP = ('{ getPath: function () { return "bus:/nested_sfx_grn_driver_car_loop"; }, '
               'id: "{9bf7a999-a6a6-4e90-a5d6-d6035b6dfa09}", entity: "MixerInput" }')
# The same shape, on the class that supplies the other 535. Two events each hold
# one, both reporting `bus:/Audio`, so the path cannot tell them apart either.
EVENT_MIXER_A = ('{ getPath: function () { return "bus:/Audio"; }, '
                 'id: "{aaaa1111-0000-0000-0000-000000000001}", entity: "EventMixerGroup" }')
EVENT_MIXER_B = ('{ getPath: function () { return "bus:/Audio"; }, '
                 'id: "{bbbb2222-0000-0000-0000-000000000002}", entity: "EventMixerGroup" }')


def test_a_strip_whose_path_does_not_resolve_is_reported_by_id():
    """The ghost. `bus:/voice` came back instead of something addressable."""
    out = render_against(EVENT_STRIP, paths=[])
    assert out == "{9bf7a999-a6a6-4e90-a5d6-d6035b6dfa09}", out


def test_two_event_mixer_groups_do_not_render_identically():
    """Both report `bus:/Audio` and neither resolves, so the path identifies
    neither of them. The ids do."""
    out = json.loads(render_against(f"[{EVENT_MIXER_A}, {EVENT_MIXER_B}]", paths=[]))
    assert out == ["{aaaa1111-0000-0000-0000-000000000001}",
                   "{bbbb2222-0000-0000-0000-000000000002}"], out


def test_a_path_that_does_resolve_is_still_reported_as_the_path():
    """The guard against the fix over-reaching. Real buses, tracks and events
    resolve, and their path is the short form a caller wants; replacing every
    one with a guid would make every reply unreadable."""
    out = render_against('{ getPath: function () { return "bus:/SFX"; }, '
                         'id: "{42012a64-43fc-44a6-9b65-932860ddffb7}" }',
                         paths=["bus:/SFX"])
    assert out == "bus:/SFX", out


_GRAPH_NO_LOOKUP = """
if (typeof studio === "undefined") { var studio = {}; }
studio.project = {};
"""


def test_replies_survive_a_project_that_cannot_answer_lookup():
    """The check must not turn a working reply into a failure. A project that
    cannot answer the question leaves the path alone, which is the behaviour
    before the check existed."""
    out = render_value('{ getPath: function () { return "bus:/SFX"; }, '
                       'id: "{42012a64-43fc-44a6-9b65-932860ddffb7}" }',
                       _GRAPH_NO_LOOKUP)
    assert out == "bus:/SFX", out


def test_the_nameless_object_id_is_still_added_to_a_path_that_resolves():
    """A bare type prefix has no name segment, so the path is the parent's path
    restated even though it resolves. That case still needs the id, or a create
    reply stays useless. Regression guard for the earlier fix."""
    out = render_against('{ getPath: function () { return "bank:/"; }, '
                         'id: "{1111-aaaa}" }', paths=["bank:/"])
    assert out == "bank:/ {1111-aaaa}", out


def test_a_resolvable_path_is_preferred_for_a_plain_managed_object():
    """The most common reply in the whole server, unchanged."""
    out = render_against('{ getPath: function () { return "event:/SFX/Hit"; }, '
                         'id: "{55fc1684-65c2-4bcd-9b8d-c97245599405}" }',
                         paths=["event:/SFX/Hit"])
    assert out == "event:/SFX/Hit", out
