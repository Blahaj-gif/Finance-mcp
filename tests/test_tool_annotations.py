"""
MCP tool annotations: the safety claim, made machine-readable.

This server's whole argument is that reads are safe and the order path is not.
Until these existed that lived only in prose a model had to be persuaded by. A
client that respects annotations can wave through a price lookup and stop on
`cancel_order` without reading the README.

Hints, not permissions. The real guarantee stays structural: there is no code
path from a tool to a live submission, and no flag that creates one.
"""
import asyncio
import inspect
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import finance_mcp as srv


def _tools():
    return asyncio.run(srv.mcp._list_tools())


def _tool_description(name):
    """The description the model actually receives for one tool."""
    return next(t.description or "" for t in _tools() if t.name == name)


def test_every_registered_tool_is_annotated():
    tools = _tools()
    missing = [t.name for t in tools if not t.annotations]
    assert not missing, f"unannotated: {missing}"
    assert len(tools) >= 36


def test_the_only_destructive_tool_is_the_one_that_acts_on_the_market():
    """
    Pulling a resting order cannot be undone, and a second call after a fill is
    not a no-op -- it is a 404 on an order that already executed. Nothing else
    here destroys anything.
    """
    destructive = sorted(t.name for t in _tools() if t.annotations.destructiveHint)
    assert destructive == ["cancel_order"], destructive


def test_drafting_an_order_is_not_marked_destructive():
    """
    It writes to a local queue and reaches no market. Marking it destructive
    would train a reader to click through the warning that matters.
    """
    draft = next(t for t in _tools() if t.name == "draft_order")
    assert draft.annotations.readOnlyHint is False, "it does write something"
    assert draft.annotations.destructiveHint is False


def test_previewing_an_order_is_read_only():
    """
    The broker prices it and consents to nothing. If preview were marked as a
    write, the one safe way to check an order before approving it would look
    as dangerous as sending it.
    """
    preview = next((t for t in _tools() if t.name == "preview_order"), None)
    if preview is None:
        pytest.skip("preview_order not registered for this broker")
    assert preview.annotations.readOnlyHint is True


def test_the_writing_tools_are_the_ones_that_write():
    """
    Derived from the source rather than trusted: a fortieth tool that opens a
    file or posts to a broker without being declared would be advertised as
    read-only, which is the one lie these annotations must not tell.
    """
    source = open(srv.__file__, encoding="utf-8").read()
    declared = set(srv.WRITING_TOOLS)

    registered = {t.name for t in _tools()}
    suspicious = set()
    for name in registered:
        fn = getattr(srv, name, None)
        if fn is None:
            continue
        body = inspect.getsource(inspect.unwrap(fn))
        writes = ("atomic_write_json" in body or "add_alert(" in body
                  or ".place_order(" in body or ".cancel_order(" in body)
        if writes:
            suspicious.add(name)

    undeclared = suspicious - declared
    assert not undeclared, (
        f"these tools write but are advertised read-only: {sorted(undeclared)}")


def test_local_only_tools_do_not_claim_to_reach_the_world():
    for tool in _tools():
        if tool.name in srv.LOCAL_ONLY_TOOLS:
            assert tool.annotations.openWorldHint is False, tool.name
        else:
            assert tool.annotations.openWorldHint is True, tool.name


def test_read_only_tools_are_idempotent_and_writing_ones_are_not():
    """
    Repeating a price lookup costs a request and changes nothing. Repeating a
    draft queues a second order, and repeating a cancel hits an order that is
    already gone.
    """
    for tool in _tools():
        if tool.annotations.readOnlyHint:
            assert tool.annotations.idempotentHint is True, tool.name
        else:
            assert tool.annotations.idempotentHint is False, tool.name


def test_most_of_this_server_is_read_only():
    """
    The shape of the claim. If a change inverts this the README is wrong, and
    so is the pitch.
    """
    tools = _tools()
    read_only = [t for t in tools if t.annotations.readOnlyHint]
    assert len(read_only) / len(tools) > 0.8, (
        f"only {len(read_only)} of {len(tools)} tools are read-only")


def test_annotating_never_stops_the_server_from_starting():
    """
    Annotations are a startup nicety. A host that imports this module inside a
    running event loop must still get a working server, not an exception from
    asyncio.run.
    """
    async def inside_a_loop():
        return srv.annotate_tools()

    assert asyncio.run(inside_a_loop()) == 0, (
        "should decline rather than raise when a loop is already running")
    # And the tools are still there afterwards.
    assert len(_tools()) >= 36


def test_no_tool_declares_the_boilerplate_output_schema():
    """
    Every tool here returns markdown prose, and FastMCP infers
    {"result": {"type": "string"}} from the `-> str` annotation -- then honours
    it by sending the payload a second time as structuredContent.

    Measured on the serialised result before this was dropped: +64% on a
    114-character reply, +89% on get_ohlcv, +98% on get_data_sources. The
    overhead approaches 100% as the payload grows and the fixed JSON envelope
    stops diluting it -- an earlier note here said 105%, which compared the
    structuredContent field against the raw text rather than against the
    serialised content block, and no honest measurement exceeds 100% for a
    verbatim copy. Plus 4,641 characters of the same boilerplate across
    tools/list. A schema saying "this returns a string" is what the annotation
    already said.
    """
    for tool in _tools():
        assert tool.output_schema is None, (
            f"{tool.name} declares an output schema; its responses will be "
            "sent twice")


def test_a_response_is_not_sent_twice():
    """
    Every tool here returns text. Emitting the same payload again as
    `structuredContent` would double what the client pays to read one answer.

    Driven through the public `call_tool` rather than a private method: this
    test used to reach `_call_tool_mcp`, which a FastMCP release then renamed,
    so it failed in CI against an unpinned dependency while passing locally
    against an older one. A test that breaks on someone else's refactor of a
    private name is testing the wrong surface.
    """
    import asyncio as _asyncio

    result = _asyncio.run(srv.mcp.call_tool("get_journal_summary", {}))

    assert result.structured_content is None, (
        "structuredContent duplicates the text content verbatim")
    assert result.content, "the text content is the payload"


def test_the_tool_list_stays_within_a_reasonable_context_budget():
    """
    A client pays for this on every conversation before a question is asked.
    It was 41,832 characters; the boilerplate schemas were an eighth of that.
    This is a ceiling, not a target -- but a fortieth tool with a 900-character
    description should have to be a deliberate choice.
    """
    import json as _json

    total = sum(len(_json.dumps({
        "name": t.name, "description": t.description,
        "inputSchema": t.parameters,
        "annotations": t.annotations.model_dump() if t.annotations else None,
    })) for t in _tools())
    assert total < 45_000, f"tools/list is {total} characters"



def test_the_readme_states_the_real_read_only_count():
    """
    The README and the awesome-list entry both quote this ratio, and both said
    "35 of 39" for a day after three tools were removed. A number in prose
    drifts silently; deriving it from the server is the only way it stays true.
    """
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    readme = open(os.path.join(root, "README.md"), encoding="utf-8").read()
    match = re.search(r"\*\*(\d+) of\s*\n?(\d+) are annotated read-only", readme)
    assert match, "the README no longer states the read-only ratio"

    tools = _tools()
    read_only = sum(1 for t in tools if t.annotations.readOnlyHint)
    assert (int(match.group(1)), int(match.group(2))) == (read_only, len(tools)), (
        f"README says {match.group(1)} of {match.group(2)}; "
        f"the server registers {read_only} of {len(tools)}")


# =====================================================================
# A cheap path nobody is told about is not a cheap path
# =====================================================================

def test_the_profile_tells_the_model_how_to_ask_for_less():
    """
    get_company_profile spans 555 tokens for one section to 6,015 for all of
    them -- the field filtering that MCP guidance calls the highest-leverage
    saving, already built. But the summary line read "Everything worth knowing
    about a company, in one call. Start here", which steers every caller into
    the widest default. A model asked which sector Apple is in spent 3,161
    tokens on a question answerable in 555.

    The lever has to be named where the model reads, not only in the parameter
    list it may skim.
    """
    body = _tool_description("get_company_profile")

    assert "sections" in body, "the narrowing parameter must be named in the description"
    assert "detail" in body
    assert "brief" in body, "the cheap preset has to be named to be reachable"


def test_the_profile_presets_are_actually_cheaper_in_that_order():
    """
    The description now makes a cost claim. If brief were not materially
    cheaper than standard, that claim would be a lie the model acts on.
    """
    import finance_mcp as srv
    sizes = {}
    for level in ("brief", "standard", "full"):
        sizes[level] = len(srv.get_company_profile("AAPL", detail=level))

    assert sizes["brief"] < sizes["standard"] < sizes["full"]
    assert sizes["brief"] * 2 < sizes["standard"], \
        "brief has to be worth choosing, not a rounding error"
