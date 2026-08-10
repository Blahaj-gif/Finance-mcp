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
