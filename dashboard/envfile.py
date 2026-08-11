"""
Load .env into os.environ, from wherever the caller happens to have started.

Extracted because it used to live only in webull_client, and three modules read
environment variables at import time. econ_calendar's SEC_USER_AGENT and
central_banks' FRED/BEA keys were therefore empty unless webull_client had
already been imported — so `from dashboard import econ_calendar` on its own
produced a module that could not talk to the SEC, while the same module worked
fine inside finance_mcp purely because of import order there.

Nothing was silently sent anonymously: _sec_headers() raises when the contact
address is missing, which is the behaviour the SEC's fair-access policy calls
for. But a module whose correctness depends on which other module was imported
first is a trap, and the fix is for each of them to load their own settings.

Idempotent, and never overwrites a variable already set in the real environment
— an explicitly exported value should win over a checked-in file.
"""
import os
import sys

_loaded = False

APP_DIR_NAME = "finance-mcp"


def user_config_dir() -> str:
    """
    Where a *installed* copy keeps its .env, per-platform convention.

    A git checkout keeps it next to the code, which is convenient and obvious.
    An installed package cannot: site-packages is not a place anyone edits, and
    is wiped on upgrade.
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config")
    return os.path.join(base, APP_DIR_NAME)


def candidate_paths() -> list:
    """
    Every place a .env may live, in precedence order.

    The order matters and is deliberate:

      1. FINANCE_MCP_ENV -- an explicit answer beats every guess.
      2. The current directory -- someone standing in a project expects the
         file they can see to win.
      3. The repo root -- the git-checkout case, and where the installer writes.
      4. The per-user config directory -- the installed case, which is the only
         one of these that survives `pip install --upgrade`.

    Resolving against the package's own location used to be the *only* rule.
    That is correct for a checkout and wrong for an installed package, where it
    points into site-packages and silently finds nothing -- so the SEC and
    broker tools would refuse with "not set" on a machine where the user had
    filled in a .env perfectly well, just not in a directory they could guess.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(here)
    paths = []
    explicit = os.environ.get("FINANCE_MCP_ENV", "").strip()
    if explicit:
        paths.append(explicit)
    paths.append(os.path.join(os.getcwd(), ".env"))
    paths.append(os.path.join(repo_root, ".env"))
    paths.append(os.path.join(user_config_dir(), ".env"))

    seen, ordered = set(), []
    for p in paths:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            ordered.append(p)
    return ordered


def resolve() -> str | None:
    """The first candidate that exists, or None."""
    for path in candidate_paths():
        if os.path.isfile(path):
            return path
    return None


def load_env(env_path=None, override=False) -> int:
    """
    Read KEY=VALUE lines into os.environ. Returns how many were set.

    Never overwrites a variable already present in the real environment unless
    `override` is set: an explicitly exported value should beat a checked-in
    file.
    """
    global _loaded
    if env_path is None:
        if _loaded and not override:
            return 0
        env_path = resolve()
        if env_path is None:
            _loaded = True
            return 0

    if not os.path.exists(env_path):
        _loaded = True
        return 0

    count = 0
    with open(env_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not key:
                continue
            # Values are often quoted by editors that do not know this is not
            # shell. Strip a matched pair; leave anything else exactly as typed.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key in os.environ and not override:
                continue
            os.environ[key] = value
            count += 1

    _loaded = True
    return count
