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


def test_every_registered_tool_is_annotated():
    tools = _tools()
    missing = [t.name for t in tools if not t.annotations]
    assert not missing, f"unannotated: {missing}"
    assert len(tools) >= 39


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
    assert len(_tools()) >= 39


def test_no_tool_declares_the_boilerplate_output_schema():
    """
    Every tool here returns markdown prose, and FastMCP infers
    {"result": {"type": "string"}} from the `-> str` annotation -- then honours
    it by sending the payload a second time as structuredContent.

    Measured on get_ohlcv before this was dropped: 657 characters of content
    and 690 of an identical copy. 105% overhead on every call, plus 4,641
    characters of the same boilerplate across tools/list. A schema saying "this
    returns a string" is what the annotation already said.
    """
    for tool in _tools():
        assert tool.output_schema is None, (
            f"{tool.name} declares an output schema; its responses will be "
            "sent twice")


def test_a_response_is_not_sent_twice():
    import asyncio as _asyncio

    async def call():
        return await srv.mcp._call_tool_mcp(
            "get_journal_summary", {})          # local-only: no network

    result = _asyncio.run(call())
    structured = getattr(result, "structuredContent", None)
    assert structured is None, (
        "structuredContent duplicates the text content verbatim")


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

