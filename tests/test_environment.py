"""
Paper vs live, and telling a throttled feed from a dead symbol.

Two things here decide whether someone loses money by accident: which broker
surface a submit button is pointed at, and whether "no data" means the ticker
is wrong or that we are being rate-limited.
"""
import datetime
import os
import re
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import webull_client as wc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# =====================================================================
# Sandbox / paper environment
# =====================================================================

def test_the_default_environment_is_live_not_paper(monkeypatch):
    """
    Deliberately the opposite of Webull's own MCP server, which defaults to the
    sandbox. This project reads a real account for everything else, so a silent
    switch to simulated balances would be the more dangerous surprise.
    """
    monkeypatch.setattr(wc, "WEBULL_ENVIRONMENT", "prod")
    assert wc.is_paper_environment() is False
    assert wc.environment_label() == "LIVE"


@pytest.mark.parametrize("value", ["uat", "UAT", "sandbox", "paper", " Simulated "])
def test_every_paper_alias_is_recognised(monkeypatch, value):
    monkeypatch.setattr(wc, "WEBULL_ENVIRONMENT", value)
    assert wc.is_paper_environment() is True
    assert wc.environment_label() == "PAPER"


@pytest.mark.parametrize("value", ["prod", "production", "live", ""])
def test_anything_else_is_treated_as_live(monkeypatch, value):
    """An unrecognised value must not quietly become paper -- or vice versa."""
    monkeypatch.setattr(wc, "WEBULL_ENVIRONMENT", value)
    assert wc.is_paper_environment() is False


def test_every_sdk_region_has_a_sandbox_endpoint():
    """
    The SDK ships production hosts for twelve regions. If we offer paper mode
    at all, it has to cover the same set, or a user in an uncovered region gets
    a confusing failure instead of a clear one.
    """
    import json
    from webull.core.endpoint.local_config_regional_endpoint_resolver import ENDPOINT_JSON
    prod_regions = set(json.load(open(ENDPOINT_JSON))["region_mapping"])
    assert prod_regions <= set(wc.SANDBOX_ENDPOINTS), (
        f"no sandbox host for {sorted(prod_regions - set(wc.SANDBOX_ENDPOINTS))}")


def test_every_sandbox_entry_defines_all_three_api_types():
    for region, cfg in wc.SANDBOX_ENDPOINTS.items():
        assert set(cfg) == {"api", "quotes-api", "events-api"}, region
        for key, host in cfg.items():
            assert host and "." in host, f"{region}.{key} is not a hostname"


def test_no_sandbox_host_is_a_production_host():
    """
    The whole point is that a paper order never reaches production. A copy-paste
    slip here would route simulated orders to the live broker.
    """
    import json
    from webull.core.endpoint.local_config_regional_endpoint_resolver import ENDPOINT_JSON
    prod = json.load(open(ENDPOINT_JSON))["region_mapping"]
    prod_hosts = {h for cfg in prod.values() for h in cfg.values()}
    for region, cfg in wc.SANDBOX_ENDPOINTS.items():
        for key, host in cfg.items():
            assert host not in prod_hosts, f"{region}.{key} points at production: {host}"
            assert ("sandbox" in host or "uat" in host), \
                f"{region}.{key} is neither a sandbox nor a uat host: {host}"


def test_the_broken_uat_import_is_gone():
    """
    This branch used to `import webull_openapi_mcp`, a package the project does
    not depend on and does not ship, so WEBULL_ENVIRONMENT=uat raised
    ModuleNotFoundError at client construction -- a documented setting that
    broke the app instead of switching it to paper.
    """
    src = open(os.path.join(ROOT, "dashboard", "webull_client.py"), encoding="utf-8").read()
    # The comment credits upstream by path, which is fine; an import is not.
    assert not re.search(r"^\s*(from|import)\s+webull_openapi_mcp", src, re.M)
    assert "SANDBOX_ENDPOINTS" in src, "the endpoints must be defined locally"


def test_an_unknown_region_in_paper_mode_refuses_rather_than_falling_through(monkeypatch, tmp_path):
    """
    Falling back to production while the user believes they are on paper is the
    one outcome that must never happen.

    `conf/` is redirected to a tmp dir: it holds the token and the SDK log so it
    is gitignored, and this test used to pass only because the developer machine
    already had one. On a clean checkout it died in the file logger with
    FileNotFoundError before ever reaching the assertion — a green test locally
    and a red one in CI, which is how the fresh-install bug below was found.
    """
    monkeypatch.setattr(wc, "WEBULL_ENVIRONMENT", "paper")
    monkeypatch.setattr(wc, "WEBULL_REGION_ID", "atlantis")
    monkeypatch.setattr(wc, "_API_CLIENT", None)
    monkeypatch.setattr(wc, "WEBULL_APP_KEY", "k")
    monkeypatch.setattr(wc, "WEBULL_APP_SECRET", "s")
    monkeypatch.setattr(wc, "WEBULL_TOKEN_DIR", str(tmp_path / "conf"))
    with pytest.raises(RuntimeError, match="no sandbox endpoint"):
        wc.get_api_client()


def test_the_token_directory_is_created_rather_than_assumed(monkeypatch, tmp_path):
    """
    A fresh clone or a fresh install has no `conf/` -- it is gitignored, the
    installer does not create it, and the SDK's token manager does not either.
    The file logger opened a path inside it without creating the directory, so
    the very first Webull call on a new machine died with FileNotFoundError
    before doing anything. Found by running the suite on a clean checkout, not
    on the machine that had been using it for weeks.
    """
    target = tmp_path / "nested" / "conf"
    assert not target.exists()

    monkeypatch.setattr(wc, "WEBULL_ENVIRONMENT", "paper")
    monkeypatch.setattr(wc, "WEBULL_REGION_ID", "atlantis")   # bail before the network
    monkeypatch.setattr(wc, "_API_CLIENT", None)
    monkeypatch.setattr(wc, "WEBULL_APP_KEY", "k")
    monkeypatch.setattr(wc, "WEBULL_APP_SECRET", "s")
    monkeypatch.setattr(wc, "WEBULL_TOKEN_DIR", str(target))

    with pytest.raises(RuntimeError, match="no sandbox endpoint"):
        wc.get_api_client()

    assert target.is_dir(), "the token/log directory should have been created"


# =====================================================================
# Throttled vs delisted
# =====================================================================

@pytest.fixture(autouse=True)
def clear_canary():
    wc._canary_state.update(checked_at=0.0, alive=None)
    yield
    wc._canary_state.update(checked_at=0.0, alive=None)


def test_an_empty_result_with_a_healthy_canary_is_a_bad_symbol(monkeypatch):
    monkeypatch.setattr(wc, "_canary_is_alive", lambda: True)
    err = wc._explain_empty_yahoo_result("NOPE")
    assert isinstance(err, wc.SymbolNotFoundError)
    assert "NOPE" in str(err) and "delisted" in str(err)


def test_an_empty_result_with_a_dead_canary_is_throttling(monkeypatch):
    """
    Yahoo answers a burst with 200 and an empty body as often as with 429. Read
    literally that says "this symbol does not exist", which sends the caller off
    to check a ticker that was never the problem.
    """
    monkeypatch.setattr(wc, "_canary_is_alive", lambda: False)
    err = wc._explain_empty_yahoo_result("AAPL")
    assert isinstance(err, wc.YahooThrottledError)
    assert "rate-limiting" in str(err)


def test_the_two_failures_are_different_exception_types():
    """Callers have to be able to branch on this, not grep a message."""
    assert not issubclass(wc.YahooThrottledError, wc.SymbolNotFoundError)
    assert not issubclass(wc.SymbolNotFoundError, wc.YahooThrottledError)
    assert issubclass(wc.SymbolNotFoundError, ValueError)


def test_the_canary_result_is_cached(monkeypatch):
    """A watchlist sweep of dead tickers must not fire one probe per name."""
    calls = {"n": 0}

    class Probe:
        def history(self, **kw):
            calls["n"] += 1
            return pd.DataFrame({"close": [1.0]})

    monkeypatch.setattr(wc, "yahoo_ticker", lambda s: Probe())
    for _ in range(5):
        assert wc._canary_is_alive() is True
    assert calls["n"] == 1


def test_a_canary_that_raises_counts_as_dead(monkeypatch):
    def boom(_):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(wc, "yahoo_ticker", boom)
    assert wc._canary_is_alive() is False


# =====================================================================
# Observed feed delay
# =====================================================================

class _MetaTicker:
    def __init__(self, meta):
        self.history_metadata = meta


def test_feed_delay_is_measured_from_yahoos_own_last_print(monkeypatch):
    """
    Yahoo does not publish exchangeDataDelayedBy on the chart endpoint, so the
    lag is measured rather than asserted: its last regular-session print
    against now.
    """
    five_min_ago = datetime.datetime.utcnow() - datetime.timedelta(minutes=5)
    monkeypatch.setattr(wc, "yahoo_ticker", lambda s: _MetaTicker({
        "regularMarketTime": int(five_min_ago.replace(
            tzinfo=datetime.timezone.utc).timestamp()),
        "fullExchangeName": "NasdaqGS",
        "exchangeTimezoneName": "America/New_York",
        "currency": "USD",
    }))
    d = wc.yahoo_feed_delay("AAPL")
    assert 4.0 <= d["observed_lag_minutes"] <= 6.0
    assert d["exchange"] == "NasdaqGS"
    assert d["market_open"] is True


def test_a_long_lag_is_reported_as_a_closed_market_not_a_delay(monkeypatch):
    """Overnight the gap is the session, not the feed. Saying otherwise would
    read as a 20-hour data delay."""
    yesterday = datetime.datetime.utcnow() - datetime.timedelta(hours=20)
    monkeypatch.setattr(wc, "yahoo_ticker", lambda s: _MetaTicker({
        "regularMarketTime": int(yesterday.replace(
            tzinfo=datetime.timezone.utc).timestamp())}))
    d = wc.yahoo_feed_delay("AAPL")
    assert d["market_open"] is False
    assert d["observed_lag_minutes"] > 1000


def test_missing_metadata_returns_nothing_rather_than_a_guess(monkeypatch):
    monkeypatch.setattr(wc, "yahoo_ticker", lambda s: _MetaTicker({}))
    assert wc.yahoo_feed_delay("AAPL") == {}

    def boom(_):
        raise RuntimeError("no metadata")
    monkeypatch.setattr(wc, "yahoo_ticker", boom)
    assert wc.yahoo_feed_delay("AAPL") == {}


def test_paper_mode_injects_the_sandbox_endpoints_for_a_real_region(monkeypatch, tmp_path):
    """
    The last unverified link in the paper-trading path: that setting
    WEBULL_ENVIRONMENT=paper actually repoints the client, rather than just
    changing a label while every call still goes to production.

    Asserted by capturing add_endpoint rather than by connecting — the sandbox
    needs its own credentials, and a test that silently no-ops without them
    would be exactly the kind of never-fires assertion this project has been
    burned by before.
    """
    calls = []

    class _Recorder:
        def set_token_dir(self, _d): pass
        def set_stream_logger(self, **_k): pass
        def set_file_logger(self, **_k): pass
        def add_endpoint(self, region, endpoint, api_type):
            calls.append((region, endpoint, api_type))

    monkeypatch.setattr(wc, "WEBULL_ENVIRONMENT", "paper")
    monkeypatch.setattr(wc, "WEBULL_REGION_ID", "th")
    monkeypatch.setattr(wc, "WEBULL_APP_KEY", "k")
    monkeypatch.setattr(wc, "WEBULL_APP_SECRET", "s")
    monkeypatch.setattr(wc, "WEBULL_TOKEN_DIR", str(tmp_path / "conf"))
    monkeypatch.setattr(wc, "_API_CLIENT", None)

    import webull.core.client as sdk_client
    monkeypatch.setattr(sdk_client, "ApiClient", lambda *a, **k: _Recorder())
    monkeypatch.setattr(wc, "_API_CLIENT", None)
    wc.get_api_client()
    monkeypatch.setattr(wc, "_API_CLIENT", None)          # don't leak the stub

    got = {api_type: host for _region, host, api_type in calls}
    expected = wc.SANDBOX_ENDPOINTS["th"]
    assert got == expected, f"paper mode did not repoint the client: {got}"
    assert all(region == "th" for region, _h, _t in calls)
    # And nothing production-shaped slipped through.
    assert not any("api.webull.co.th" == host for _r, host, _t in calls)


# =====================================================================
# Config discovery for an INSTALLED copy
# =====================================================================
# Resolving .env against the package's own directory is right for a git
# checkout and wrong for an installed package, where it points into
# site-packages and silently finds nothing -- so the SEC and broker tools would
# refuse with "not set" on a machine where the user had filled in a .env
# perfectly well, just not in a directory they could guess.

def test_an_explicit_env_var_beats_every_guess(tmp_path, monkeypatch):
    from dashboard import envfile
    target = tmp_path / "custom.env"
    target.write_text("FM_PROBE=explicit\n", encoding="utf-8")
    monkeypatch.setenv("FINANCE_MCP_ENV", str(target))
    assert envfile.candidate_paths()[0] == str(target)
    assert envfile.resolve() == str(target)


def test_the_current_directory_is_searched(tmp_path, monkeypatch):
    """Someone standing in a project expects the file they can see to win."""
    from dashboard import envfile
    monkeypatch.delenv("FINANCE_MCP_ENV", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("FM_PROBE=cwd\n", encoding="utf-8")
    assert envfile.resolve() == str(tmp_path / ".env")


def test_the_user_config_directory_is_platform_correct(monkeypatch):
    import sys
    from dashboard import envfile
    import unittest.mock as mock

    with mock.patch.object(sys, "platform", "darwin"):
        assert "Library/Application Support" in envfile.user_config_dir().replace("\\", "/")
    with mock.patch.object(sys, "platform", "linux"):
        monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg")
        assert envfile.user_config_dir().replace("\\", "/").startswith("/tmp/xdg")
    with mock.patch.object(sys, "platform", "win32"):
        monkeypatch.setenv("APPDATA", r"C:\Users\x\AppData\Roaming")
        assert "finance-mcp" in envfile.user_config_dir()


def test_the_search_order_is_stable_and_deduplicated(monkeypatch):
    from dashboard import envfile
    monkeypatch.delenv("FINANCE_MCP_ENV", raising=False)
    paths = envfile.candidate_paths()
    import os
    keys = [os.path.normcase(os.path.abspath(p)) for p in paths]
    assert len(keys) == len(set(keys)), "a path must not be searched twice"
    assert paths[-1].endswith(os.path.join("finance-mcp", ".env")), (
        "the per-user config directory is the last resort, not the first guess")


def test_quoted_values_are_unquoted(tmp_path):
    """Editors that do not know this is not shell add quotes."""
    from dashboard import envfile
    env = tmp_path / ".env"
    env.write_text('FM_Q1="quoted"\nFM_Q2=\'single\'\nFM_Q3=bare\n', encoding="utf-8")
    import os
    for k in ("FM_Q1", "FM_Q2", "FM_Q3"):
        os.environ.pop(k, None)
    envfile.load_env(str(env))
    assert os.environ["FM_Q1"] == "quoted"
    assert os.environ["FM_Q2"] == "single"
    assert os.environ["FM_Q3"] == "bare"


def test_console_entry_points_are_declared():
    """
    The MCP registry distributes from PyPI and has no way to describe a
    git-clone install, so being launchable as a command is what makes listing
    possible at all.
    """
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    toml = open(os.path.join(root, "pyproject.toml"), encoding="utf-8").read()
    assert "[project.scripts]" in toml
    for cmd in ("finance-mcp =", "finance-mcp-dashboard =", "finance-mcp-config ="):
        assert cmd in toml, f"missing console script: {cmd}"


def test_server_json_matches_the_package_version():
    """A registry entry pointing at a version that does not exist is worse than none."""
    import json, os, re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    manifest = json.load(open(os.path.join(root, "server.json"), encoding="utf-8"))
    toml = open(os.path.join(root, "pyproject.toml"), encoding="utf-8").read()
    version = re.search(r'^version = "([^"]+)"', toml, re.M).group(1)
    assert manifest["version"] == version
    assert manifest["packages"][0]["version"] == version
    # The identifier is asserted against pyproject rather than hardcoded, so a
    # rename cannot leave the two claiming different packages.
    dist = re.search(r'^name = "([^"]+)"', toml, re.M).group(1)
    assert manifest["packages"][0]["identifier"] == dist


def test_we_do_not_claim_a_pypi_name_that_belongs_to_someone_else():
    """
    server.json first claimed `finance-mcp`, which is an active Alibaba project
    (flowllm-ai/finance-mcp) with a different scope. A registry entry pointing
    at somebody else's package is worse than no entry: it is a claim on their
    name, and it would install their code for anyone who followed it.
    """
    import json, os, re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    manifest = json.load(open(os.path.join(root, "server.json"), encoding="utf-8"))
    taken = {"finance-mcp", "finance_mcp", "finance-mcp-server"}
    identifier = manifest["packages"][0]["identifier"]
    assert identifier not in taken, f"{identifier} is taken on PyPI by another project"

    toml = open(os.path.join(root, "pyproject.toml"), encoding="utf-8").read()
    dist = re.search(r'^name = "([^"]+)"', toml, re.M).group(1)
    assert dist == identifier, (
        f"pyproject name {dist!r} and server.json identifier {identifier!r} must "
        "match, or the registry entry points at a package that is never published")
    assert dist not in taken


def test_the_console_commands_are_stable_across_a_rename():
    """
    The distribution name changed; the command an MCP client runs did not. Every
    config we have published says `finance-mcp`, and breaking that would silently
    unhook the server from anyone who already installed it.
    """
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    toml = open(os.path.join(root, "pyproject.toml"), encoding="utf-8").read()
    assert 'finance-mcp = "dashboard.cli:serve"' in toml
