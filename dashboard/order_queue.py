"""
The staged-order queue, as one thing rather than a file two processes guess at.

The MCP server drafts orders and the Streamlit dashboard approves them. They are
separate processes sharing `dashboard/order_drafts.json`, and both used to do
read-all, modify-in-memory, write-all -- so a draft added between one process's
read and its write disappeared. That was survivable while the server was the
only writer; adding a Cancel button to the dashboard made it a third.

Every write here re-reads first and patches one draft by id, so a concurrent
append survives. Every write is atomic, so a crash cannot truncate the queue
into something that parses as fewer orders than exist.
"""
import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_path() -> str:
    """Where the queue lives when nobody says otherwise."""
    return os.path.join(BASE_DIR, "dashboard", "order_drafts.json")


def restrict_to_owner(file_path: str):
    """
    Restrict a file to the current user. True if applied, False if it could not
    be, None where the platform makes it unnecessary.

    Staged orders name what an account is about to buy, and on Windows this file
    inherits the repository's ACL -- which on a default checkout under C:\\
    grants Authenticated Users Modify, so any process on the machine can read
    pending trades or rewrite their quantities before a human approves them.

    `os.chmod` is not the answer there. Python defers its mode to the C runtime,
    which honours exactly one bit -- the read-only attribute -- so 0o600 is a
    no-op that looks like a permission. The real mechanism is a DACL, applied
    here with icacls because it ships with Windows and needs no extra
    dependency. On POSIX the mode is real and is used.
    """
    if not os.path.exists(file_path):
        return False
    if os.name != "nt":
        try:
            os.chmod(file_path, 0o600)
            return True
        except OSError:
            return False

    import subprocess
    user = os.environ.get("USERNAME") or ""
    if not user:
        return False
    try:
        # /inheritance:r drops the inherited repository ACEs; without it the
        # grant is added beside them and Authenticated Users keeps Modify.
        result = subprocess.run(
            ["icacls", file_path, "/inheritance:r", "/grant:r", f"{user}:(F)"],
            capture_output=True, timeout=15)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def load(p: str = None) -> list:
    """Every draft on disk, or an empty list if there is no queue yet."""
    p = p or default_path()
    if not os.path.exists(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            content = f.read().strip()
        return json.loads(content) if content else []
    except (OSError, ValueError):
        return []


def _write(p: str, drafts: list) -> None:
    """Replace the queue atomically, then restrict it."""
    os.makedirs(os.path.dirname(p), exist_ok=True)
    temp = p + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(drafts, f, indent=2)
    os.replace(temp, p)
    restrict_to_owner(p)


def append(draft: dict, p: str = None) -> dict:
    """Add a draft, keeping anything another process wrote in the meantime."""
    p = p or default_path()
    drafts = load(p)
    drafts.append(draft)
    _write(p, drafts)
    return draft


def update(draft_id: str, **fields):
    """
    Patch one draft by id and write the queue back.

    Re-reads first: the caller's copy may be minutes old, and on this queue a
    stale copy written back whole is a deleted order. Returns the updated draft,
    or None if it is no longer there -- which is a real outcome, not an error.
    A draft can be cancelled in the dashboard while the model is looking at it.
    """
    p = fields.pop("_path", None) or default_path()
    drafts = load(p)
    for draft in drafts:
        if draft.get("draft_id") == draft_id:
            draft.update(fields)
            _write(p, drafts)
            return draft
    return None
