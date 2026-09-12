"""
Console entry points, so an installed copy is launchable without knowing where
pip put it.

A git checkout runs `python finance_mcp.py` and `streamlit run dashboard/app.py`
because the paths are visible. An installed package has neither: site-packages
is not a path anyone types, and the MCP registry has no way to describe a
git-clone install anyway -- it distributes from PyPI, npm, OCI, NuGet, Cargo or
a remote endpoint, and nothing else. So being installable *is* the listing.
"""
import os
import subprocess
import sys


def serve() -> int:
    """The MCP server, over stdio. This is what an MCP client runs."""
    # Import here rather than at module scope: the dashboard entry point below
    # must not pay for pandas and the broker SDK just to spawn Streamlit.
    import finance_mcp
    finance_mcp.mcp.run(transport="stdio")
    return 0


def dashboard() -> int:
    """Launch the Streamlit dashboard against the installed app.py."""
    app = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
    if not os.path.isfile(app):
        print(f"Dashboard app not found at {app}", file=sys.stderr)
        return 1

    try:
        import streamlit  # noqa: F401
    except ImportError:
        print("The dashboard needs Streamlit and Plotly, which are an optional "
              "extra so the MCP server alone stays light:\n"
              "    pip install 'finance-mcp[dashboard]'\n"
              "    uvx --from 'finance-mcp[dashboard]' finance-mcp-dashboard",
              file=sys.stderr)
        return 1

    # Bind loopback unless told otherwise. Streamlit leaves `server.address`
    # unset and unset means every interface: it prints a Network URL on the LAN
    # address, and because this app runs headless, an External URL as well. What
    # is being served is a live brokerage account and a button that submits
    # orders, with no authentication in front of it, so on any shared network
    # the default is the wrong one. A config.toml key would not be enough --
    # Streamlit resolves that against the working directory, so an installed
    # copy launched from elsewhere would never see it.
    args = list(sys.argv[1:])
    if not any(a == "--server.address" or a.startswith("--server.address=")
               for a in args):
        args = ["--server.address", "127.0.0.1", *args]

    # Run from the app's own directory so it finds .streamlit/config.toml --
    # Streamlit resolves that relative to the working directory, and launching
    # from anywhere else silently drops the theme.
    return subprocess.call(
        [sys.executable, "-m", "streamlit", "run", app, *args],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def where() -> int:
    """Print where configuration is read from. For 'why is my key not working'."""
    from dashboard import envfile
    found = envfile.resolve()
    print("Config search order (first match wins):")
    for path in envfile.candidate_paths():
        mark = "  <-- using this" if path == found else ""
        print(f"  {'[found]' if os.path.isfile(path) else '[     ]'} {path}{mark}")
    if found is None:
        print("\nNo .env found. Create one at:")
        print(f"  {os.path.join(envfile.user_config_dir(), '.env')}")
        print("or set FINANCE_MCP_ENV to point at one.")
    return 0
