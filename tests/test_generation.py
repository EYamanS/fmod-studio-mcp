"""Script generation for the spec-driven tools.

Every documented member must produce a runnable script, and how a member is
reached has to follow its ``target_kind``. This walks the whole committed spec,
so it covers all 148 generated tools rather than a sample.
"""

from __future__ import annotations

from fmod_studio_mcp.generation import _DESC, build_generated_tools, embed_value
from fmod_studio_mcp.server import _GENERATED


def args_for(tool) -> dict:
    """Minimal arguments that satisfy a tool's required inputs."""
    args = {}
    for name in tool.input_schema().get("required", []):
        args[name] = {"target": "event:/SFX/Hit", "className": "Event"}.get(name, "0")
    return args


def expression_of(tool) -> str:
    """The part of a script after the helper, which is what does the work."""
    return tool.build_js(args_for(tool))[len(_DESC):].strip()


# A value shaped like a path, so a member that converts its arguments is forced
# to convert this one. Used to tell "this member converts" from "it does not".
PATH_ARG = "event:/SFX/Hit"


def expression_of_any_arg(tool) -> str:
    """The expression a tool renders when every parameter is handed a path.

    ``args_for`` fills parameters with ``"0"``, which never triggers the
    object conversion, so it cannot answer "does this member convert?". This
    hands every parameter a path-shaped string instead.
    """
    tk = tool.spec["target_kind"]
    args = {name: PATH_ARG for name in tool.input_schema().get("required", [])}
    if tk == "instance":
        args["target"] = PATH_ARG
    elif tk == "entity":
        args["className"] = "Event"
    return tool.build_js(args)[len(_DESC):].strip()


def test_every_member_in_the_spec_generates_a_script():
    tools = build_generated_tools()
    assert len(tools) == 148
    for tool in tools:
        script = tool.build_js(args_for(tool))
        assert script.strip(), tool.name
        assert "__render(" in script, tool.name


def test_every_generated_tool_bounds_its_result():
    """__cap is what bounds a result, and it runs at the end of the pipeline. A
    call site that reaches for __desc directly is unbounded, so this walks all
    148 of them rather than trusting the one that was spot-checked."""
    for tool in build_generated_tools():
        expression = expression_of(tool)
        assert "__render(" in expression, tool.name
        assert "__desc(" not in expression, tool.name


def test_every_tool_name_is_unique_and_namespaced():
    names = [tool.name for tool in build_generated_tools()]
    assert len(names) == len(set(names))
    assert all(name.startswith("fmod_") for name in names)


def test_a_module_member_is_reached_on_its_fixed_receiver():
    script = _GENERATED["fmod_project_filePath"].build_js({})
    assert "studio.project.filePath" in script


def test_an_instance_member_is_reached_by_lookup():
    script = _GENERATED["fmod_Bank_getPath"].build_js({"target": "bank:/Master"})
    assert 'studio.project.lookup("bank:/Master")' in script


def test_an_instance_target_says_which_entity_it_must_be():
    """``getPath`` is defined on Event and Bank, not on every object — calling it
    on a folder throws inside Studio. The schema has to name the expected type,
    otherwise the caller cannot tell 'event:/SFX/Hit' from 'event:/'."""
    for name in ("fmod_Event_getPath", "fmod_Bank_getPath", "fmod_Timeline_getCursorPosition"):
        target = _GENERATED[name].input_schema()["properties"]["target"]["description"]
        owner = _GENERATED[name].spec["owner"]
        assert owner in target, f"{name} does not name {owner}"


def test_an_entity_member_is_reached_through_the_model():
    script = _GENERATED["fmod_entity_findInstances"].build_js({"className": "Event"})
    assert 'studio.project.model["Event"]' in script


def test_a_global_member_is_a_bare_call():
    assert _GENERATED["fmod_global_alert"].build_js({"msg": "hi"}).startswith("var __DESC_MAX")


def test_a_property_reads_when_no_value_is_given():
    script = _GENERATED["fmod_project_filePath"].build_js({})
    assert script.endswith("__render(studio.project.filePath);")


def test_a_settable_property_writes_when_a_value_is_given():
    script = _GENERATED["fmod_project_distanceRolloffType"].build_js({"value": 1})
    assert "studio.project.distanceRolloffType = 1" in script


def test_embedding_turns_paths_and_guids_into_object_references():
    assert embed_value("event:/SFX/Hit") == 'studio.project.lookup("event:/SFX/Hit")'
    assert embed_value("bank:/Master") == 'studio.project.lookup("bank:/Master")'
    assert embed_value("{1234-5678}") == 'studio.project.lookup("{1234-5678}")'


def test_embedding_keeps_numbers_booleans_and_strings_typed():
    assert embed_value(2.5) == "2.5"
    assert embed_value(True) == "true"
    assert embed_value("hello") == '"hello"'


# --- the member that takes the identifier itself ---------------------------------
#
# `project.lookup` is the one member whose parameter is the identifier the whole
# method exists to resolve. Its receiver is already `studio.project`, and
# embed_value turned the argument into `studio.project.lookup(...)`, so the script
# came out as a lookup of a lookup:
#
#     __render(studio.project.lookup(studio.project.lookup("event:/SFX/footstep")))
#
# Observed on a live 2.03.13 terminal against a path that resolves:
#
#     studio.project.lookup("event:/Bedtime/vo/emb_vo/vo_emb_bingo_yeah")
#         -> (ManagedObject:Event), typeof result.lookup === "undefined"
#     <that result>.lookup(...)
#         -> ERROR TypeError: not a function   <- no ManagedObject has lookup()
#
# So the argument is passed as text, and the receiver still does the resolving.

def test_the_lookup_tool_passes_its_identifier_as_text_not_a_second_lookup():
    """The regression: fmod_project_lookup failed with `TypeError: not a function`
    for every valid input, because the identifier was resolved twice."""
    expr = expression_of_any_arg(_GENERATED["fmod_project_lookup"])
    assert expr.count("studio.project.lookup(") == 1, expr
    assert 'studio.project.lookup(studio.project.lookup(' not in expr, expr


def test_the_lookup_tool_still_reaches_the_member_on_its_receiver():
    """The fix must remove the duplicated argument, not the call itself."""
    expr = expression_of_any_arg(_GENERATED["fmod_project_lookup"])
    assert expr == '__render(studio.project.lookup("event:/SFX/Hit"));'


def test_a_guid_reaches_the_lookup_tool_as_text_too():
    """A `{guid}` is the other addressing form the member accepts, and it is the
    form a create reply hands back, so it has to survive the same way a path does."""
    tool = _GENERATED["fmod_project_lookup"]
    expr = tool.build_js({"idOrPath": "{907bcc24-689f-4fae-a8c4-7ebb5012eb83}"})[len(_DESC):].strip()
    assert expr == '__render(studio.project.lookup("{907bcc24-689f-4fae-a8c4-7ebb5012eb83}"));', expr


def test_no_other_generated_tool_passes_a_resolved_object():
    """`project.lookup` is the only member whose argument is the identifier
    itself, so it must be the only one that skips the conversion. If a second
    member ever needs the escape, this test is where that decision gets argued."""
    offenders = [t.name for t in build_generated_tools()
                 if t.spec["member"] == "lookup"]
    assert offenders == ["fmod_project_lookup"], offenders


def test_every_other_member_still_converts_a_path_argument():
    """The guard against the fix over-reaching. The conversion is what makes an
    object-reference parameter work, and 29 members depend on it: a path argument
    must still arrive as an object everywhere except project.lookup itself."""
    converted = [t.name for t in build_generated_tools()
                 if t.spec["member"] != "lookup"
                 and "studio.project.lookup(" in expression_of_any_arg(t)]
    assert "fmod_Folder_getItem" in converted, converted
    assert "fmod_project_deleteObject" in converted, converted
    assert "fmod_window_navigateTo" in converted, converted
