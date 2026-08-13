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
