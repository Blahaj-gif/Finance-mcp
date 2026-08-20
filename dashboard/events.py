"""What happened, recorded once, so everything else can read it.

Three things want to know that a filing landed or a macro number printed: the
desktop notifier, the dashboard, and `get_updates` on the next assistant turn.
Wiring three watchers to three consumers is nine edges. Wiring both sides to one
append-only log is six, and the log is also the thing that survives a restart.

**Nothing here pushes to a language model, because MCP cannot.** A server may
send a notification to a *client* over an open session, and may ask a client to
run a completion via sampling, but there is no mechanism in the protocol that
makes an assistant begin a turn. So the split is: the **person** is notified
within seconds through a channel they actually watch, and the **assistant**
learns on its very next turn by reading this file. Anything claiming to wake an
assistant is using a side channel outside MCP or overstating what it does.

The format is one JSON object per line, fsynced per write, same as
`alert_manager`'s store and for the same reason: a crash halfway through must
not lose the events before it, and a partial last line is discardable without
touching anything else.
"""
import datetime
import json
import os
import sys
import threading

# One writer at a time. The watcher runs on its own thread and the MCP tools
# read from request threads; appends are short but they are not atomic.
_LOCK = threading.Lock()

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "events.jsonl")

# Kinds, named rather than free text so a reader can filter without guessing.
FILING = "filing"
MACRO = "macro"
BUYBACK = "buyback"
MOVE = "move"


def path() -> str:
    """Where the log lives. Overridable so tests do not touch the real one."""
    return os.getenv("FINANCE_EVENTS_PATH", DEFAULT_PATH)


def _seen_keys(limit: int = 5000) -> set:
    """Keys already recorded, newest first, bounded.

    Bounded because this is read on every candidate event and an unbounded scan
    of a log that grows for years would make the watcher slower the longer it
    ran. Five thousand is far more than any dedupe window needs: a filing seen
    an hour ago is thousands of events ago only on a very busy day.
    """
    keys = set()
    try:
        with open(path(), "r", encoding="utf-8") as handle:
            for line in handle.readlines()[-limit:]:
                try:
                    keys.add(json.loads(line).get("key"))
                except (ValueError, AttributeError):
                    # A half-written last line from a killed process. The rest
                    # of the file is still good, which is the point of the
                    # format.
                    continue
    except FileNotFoundError:
        pass
    return keys


def record(kind: str, title: str, key: str, detail: str = "", symbol: str = "",
           url: str = "", at=None, notifier=None) -> bool:
    """Append an event unless its key has been seen. Returns whether it is new.

    `key` is what makes the watcher restartable: it polls a feed that still
    contains everything it saw last time, and without a key every restart would
    re-notify the whole window. An accession number, a release identifier, an
    operation date -- whatever the source already uses to mean "this one".

    `notifier` is injected so a test can capture what would have been shown
    without a desktop session, and so a notifier that is missing or broken
    cannot lose the event: the write happens first.
    """
    if key in _seen_keys():
        return False

    row = {
        "seen_at": (at or datetime.datetime.now(datetime.timezone.utc)).isoformat(),
        "kind": kind,
        "key": key,
        "title": title,
        "detail": detail,
        "symbol": symbol,
        "url": url,
    }
    with _LOCK:
        # Re-checked inside the lock: two watcher passes can race on the same
        # filing, and the cost of the second read is nothing next to a duplicate
        # notification at four in the morning.
        if key in _seen_keys():
            return False
        with open(path(), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    # After the write, always. A notifier that raises, or a machine with no
    # notification daemon, must not turn a recorded event into a lost one.
    if notifier is not None:
        try:
            notifier(title, detail or symbol or kind)
        except Exception as exc:
            print(f"[events] notifier failed: {exc}", file=sys.stderr)
    return True


def recent(since=None, kinds=None, limit: int = 200) -> list:
    """Events newer than `since`, newest last, oldest dropped past `limit`."""
    out = []
    try:
        with open(path(), "r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if kinds and row.get("kind") not in kinds:
                    continue
                if since is not None and row.get("seen_at", "") <= since:
                    continue
                out.append(row)
    except FileNotFoundError:
        return []
    return out[-limit:]


def describe(rows: list) -> str:
    """The log as lines a person or a model can read without parsing JSON."""
    if not rows:
        return "Nothing recorded."
    lines = []
    for row in rows:
        when = row.get("seen_at", "")[:19].replace("T", " ")
        mark = row.get("symbol") or row.get("kind")
        lines.append(f"  {when}  {mark:<10} {row.get('title', '')}")
        if row.get("detail"):
            lines.append(f"                                 {row['detail']}")
    return "\n".join(lines)
