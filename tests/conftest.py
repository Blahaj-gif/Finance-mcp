"""
Test-wide isolation.

The on-disk bar cache is shared between the MCP server and the dashboard, which
means it is also shared with the test suite unless something says otherwise.
Without this, a frame cached by a real run was served to a test that had stubbed
the feed to return something stale -- the test passed or failed depending on what
had been fetched on that machine earlier in the day, which is the worst kind of
flake because it looks like a real regression.

Redirecting the cache to a per-session temp directory keeps every test reading
only what it wrote itself.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Placeholder broker credentials, set before any test module imports the server.
#
# The tool surface is gated on whether the configured broker has credentials at
# all (dashboard/capabilities.missing_credentials), and that is decided once, at
# import, because `tools/list` must not open a socket. So without this a CI
# runner with an empty environment registers 28 tools and a developer with a
# real `.env` registers 36, and every count assertion in the suite measures the
# machine rather than the server.
#
# Deliberately fake, and it does not matter that they are: nothing in the suite
# makes an authenticated call. `envfile.load_env` never overwrites a variable
# already in the environment, so these also stop a real `.env` from reaching the
# tests -- which is the point. A test that wants the unconfigured case patches
# `webull_client.WEBULL_APP_KEY` directly (see test_credential_gating.py).
os.environ.setdefault("WEBULL_APP_KEY", "test-app-key-not-a-real-credential")
os.environ.setdefault("WEBULL_APP_SECRET", "test-app-secret-not-a-real-credential")


@pytest.fixture(autouse=True, scope="session")
def _isolated_bar_cache():
    from dashboard import barcache
    with tempfile.TemporaryDirectory(prefix="finmcp-test-barcache-") as tmp:
        original = barcache.CACHE_DIR
        barcache.CACHE_DIR = tmp
        try:
            yield
        finally:
            barcache.CACHE_DIR = original


@pytest.fixture(autouse=True)
def _empty_bar_cache_between_tests():
    """Each test starts with nothing cached, so order never changes an outcome."""
    from dashboard import barcache
    barcache.clear()
    yield
    barcache.clear()


#: Captured the first time the stub is installed, so the tests that exercise
#: the real lookup can still reach it.
_REAL_SCHEDULED_EVENTS = None


@pytest.fixture(autouse=True)
def _no_calendar_lookups(monkeypatch):
    """
    No test reaches a calendar over the network.

    The event disclosure added to get_market_analysis, calculate_position_size
    and get_options_analytics calls two live calendars -- Yahoo for the issuer's
    earnings date and the BLS/Fed schedule pages for releases. The suite is
    offline by contract: the comment on the tests workflow says every test runs
    against recorded payloads or stubs, and that a test needing the network is a
    bug in the test.

    Measured before this existed: 3.9s per call, and test_risk_tools alone went
    from 21s to 32s, all of it real HTTP.

    Stubbed to "nothing scheduled", which is the quiet default. A test that
    wants an event in the window sets its own return value, and that assignment
    wins because it runs after this fixture.
    """
    global _REAL_SCHEDULED_EVENTS
    import finance_mcp as srv
    if _REAL_SCHEDULED_EVENTS is None:
        _REAL_SCHEDULED_EVENTS = srv.scheduled_events
    monkeypatch.setattr(srv, "scheduled_events", lambda *a, **k: ([], []))

    # The real lookup memoises on (symbol, horizon, anchor) for 15 minutes, so
    # the tests that exercise it -- all of which use the same symbol and the
    # same explicit anchor -- would serve each other's stubbed calendars. Same
    # reasoning as the bar cache above: a test must read only what it wrote.
    srv.EVENT_CACHE.clear()
    yield
    srv.EVENT_CACHE.clear()


@pytest.fixture
def real_scheduled_events():
    """
    The unstubbed scheduled_events, for the tests that exercise it directly.

    They stub its two calendars instead, so nothing here opens a socket either.
    """
    assert _REAL_SCHEDULED_EVENTS is not None, (
        "the autouse stub has not run yet, so the original was never captured")
    return _REAL_SCHEDULED_EVENTS
