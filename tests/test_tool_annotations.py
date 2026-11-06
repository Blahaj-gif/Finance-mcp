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
    destructive = sorted(t.name for t in _tools() if t.annotations.destructive_hint)
    assert destructive == ["cancel_order"], destructive


def test_drafting_an_order_is_not_marked_destructive():
    """
    It writes to a local queue and reaches no market. Marking it destructive
    would train a reader to click through the warning that matters.
    """
    draft = next(t for t in _tools() if t.name == "draft_order")
    assert draft.annotations.read_only_hint is False, "it does write something"
    assert draft.annotations.destructive_hint is False


def test_previewing_an_order_is_read_only():
    """
    The broker prices it and consents to nothing. If preview were marked as a
    write, the one safe way to check an order before approving it would look
    as dangerous as sending it.
    """
    preview = next((t for t in _tools() if t.name == "preview_order"), None)
    if preview is None:
        pytest.skip("preview_order not registered for this broker")
    assert preview.annotations.read_only_hint is True


def test_the_writing_tools_are_the_ones_that_write():
    """
    Derived from the registered server rather than trusted: a fortieth tool
    that opens a file or posts to a broker without being declared would be
    advertised as read-only, which is the one lie these annotations must not
    tell.
    """
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
            assert tool.annotations.open_world_hint is False, tool.name
        else:
            assert tool.annotations.open_world_hint is True, tool.name


def test_read_only_tools_are_idempotent_and_writing_ones_are_not():
    """
    Repeating a price lookup costs a request and changes nothing. Repeating a
    draft queues a second order, and repeating a cancel hits an order that is
    already gone.
    """
    for tool in _tools():
        if tool.annotations.read_only_hint:
            assert tool.annotations.idempotent_hint is True, tool.name
        else:
            assert tool.annotations.idempotent_hint is False, tool.name


def test_most_of_this_server_is_read_only():
    """
    The shape of the claim. If a change inverts this the README is wrong, and
    so is the pitch.
    """
    tools = _tools()
    read_only = [t for t in tools if t.annotations.read_only_hint]
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
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    readme = open(os.path.join(root, "README.md"), encoding="utf-8").read()
    match = re.search(r"\*\*(\d+) of\s*\n?(\d+) are annotated read-only", readme)
    assert match, "the README no longer states the read-only ratio"

    tools = _tools()
    read_only = sum(1 for t in tools if t.annotations.read_only_hint)
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
    narrower than standard, that claim would be a lie the model acts on.

    Counted in sections rather than characters: the first version measured real
    payloads, which passed here and failed in CI, where there is no upstream to
    fetch. A unit test that needs the internet is not a unit test.
    """
    brief = srv.profile_sections(detail="brief")
    standard = srv.profile_sections(detail="standard")
    full = srv.profile_sections(detail="full")

    assert len(brief) < len(standard) < len(full)
    assert len(brief) * 2 <= len(standard),         "brief has to be worth choosing, not a rounding error"
    assert len(srv.profile_sections(sections="business")) == 1


# =====================================================================
# The description IS the prompt
# =====================================================================
# A tool's description and its parameter descriptions are the only things a
# model reads when choosing whether to call it. Everything the server knows
# that does not reach that text is knowledge the caller does not have.

#: Families that do overlapping jobs. Every member must name every other, so a
#: model landing anywhere in a family learns what the alternatives are for.
OVERLAPPING_FAMILIES = (
    ("get_ohlcv", "get_market_analysis", "get_multi_timeframe"),
    ("get_options_chain", "get_options_analytics", "get_unusual_options"),
    ("get_edgar_filings", "read_filing", "get_company_financials"),
)


@pytest.mark.parametrize("family", OVERLAPPING_FAMILIES, ids=lambda f: f[0])
def test_overlapping_tools_route_to_each_other(family):
    """
    Routing was one-directional and covered five tools of thirty-six.
    get_options_chain named both its siblings; neither named it back. So a
    model that entered the family at the expensive end -- a full greeks and
    skew report when it wanted a chain -- got no hint that a narrower tool
    existed, and one that entered at the cheap end got no hint of the richer.

    A dead end costs a wrong call and a second round trip. The fix is a
    sentence; the point of asserting it is that a fourth tool added to a family
    has to join the routing rather than sit outside it.
    """
    for name in family:
        desc = _tool_description(name)
        missing = [other for other in family if other != name and other not in desc]
        assert not missing, (
            f"{name} does not mention {missing}. A model reading only {name}'s "
            "description cannot know those alternatives exist.")


#: Claims no feed here carries. "smart money positioning" was the live one:
#: volume above open interest and an implied vol above 50% are two threshold
#: tests, and who is on the other side of the trade is not in the data.
UNEARNED_CLAIMS = (
    "smart money", "institutional positioning", "whale",
    "guaranteed", "will rise", "will fall", "sure thing", "can't lose",
)


def test_no_tool_description_claims_to_know_what_it_cannot_see():
    """
    This server strips unearned inference everywhere else -- the consensus
    score ships with its backtest, quotes carry their provenance, unverified
    adapters say so in every line of output. A tool description that announces
    it detects "smart money positioning" undoes that in the one place the model
    is guaranteed to read.
    """
    offenders = []
    for tool in _tools():
        lowered = (tool.description or "").lower()
        offenders += [f"{tool.name}: {phrase!r}"
                      for phrase in UNEARNED_CLAIMS if phrase in lowered]
    assert not offenders, (
        "tool descriptions assert what the data does not support:\n  "
        + "\n  ".join(offenders))


def _tools_that_compute_the_consensus_score():
    """
    Derived from the source, not listed here: every tool that appends
    HEURISTIC_NOTE is a tool that computes the fixed-weight score. A fourth one
    must not be able to appear without this test seeing it.
    """
    import ast

    source = open(srv.__file__, encoding="utf-8").read()
    lines = source.splitlines()
    registered = {t.name for t in _tools()}
    found = []
    for node in ast.parse(source).body:
        if not isinstance(node, ast.FunctionDef) or node.name not in registered:
            continue
        body = "\n".join(lines[node.lineno - 1:node.end_lineno])
        if "HEURISTIC_NOTE" in body:
            found.append(node.name)
    return found


def test_the_consensus_score_is_called_a_heuristic_where_it_is_advertised():
    """
    Three tools compute the fixed-weight consensus score and all three append
    HEURISTIC_NOTE to their output, which is the right behaviour and was never
    the problem. The problem was upstream of it: two of the three advertised
    the score in their description -- a "ranked verdict table", a "Confluence
    Score" -- with nothing to say it is a heuristic that underperformed
    buy-and-hold in backtest.

    A model picks the tool from the description and reads the caveat only after
    it has already decided the number was worth asking for. By then the anchor
    is set, which is the exact effect get_market_analysis's own description
    gives as the reason it withholds a verdict by default.
    """
    scoring = _tools_that_compute_the_consensus_score()
    assert len(scoring) >= 3, (
        f"expected at least three consensus-score tools, found {scoring}")

    silent = [name for name in scoring
              if "heuristic" not in _tool_description(name).lower()]
    assert not silent, (
        f"these advertise the consensus score without calling it a heuristic: "
        f"{silent}")
