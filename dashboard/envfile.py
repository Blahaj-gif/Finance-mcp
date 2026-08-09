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

_loaded = False


def load_env(env_path=None, override=False) -> int:
    """
    Read KEY=VALUE lines into os.environ. Returns how many were set.

    Resolved relative to the repo root rather than the working directory, so
    launching the dashboard from `dashboard/` finds the same file as launching
    it from the root.
    """
    global _loaded
    if env_path is None:
        if _loaded and not override:
            return 0
        env_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")

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
            if key in os.environ and not override:
                continue          # a real exported value beats the file
            os.environ[key] = value
            count += 1

    _loaded = True
    return count
