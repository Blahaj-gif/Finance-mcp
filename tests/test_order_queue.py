"""The staged-order queue as shared state, not as a file two processes guess at."""
import datetime
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import order_queue as oq


def test_updating_one_draft_keeps_what_another_process_added(tmp_path, monkeypatch):
    """
    The MCP server and the dashboard are separate processes sharing one file.
    Both used read-all / modify-in-memory / write-all, so a draft added between
    the dashboard's read and its write vanished. Cancelling made that worse by
    adding a third writer.
    """
    path = tmp_path / "order_drafts.json"
    monkeypatch.setattr(oq, "default_path", lambda: str(path))
    oq.append({"draft_id": "A", "status": "PENDING_APPROVAL"})

    stale = oq.load()                                   # the dashboard reads
    oq.append({"draft_id": "B", "status": "PENDING_APPROVAL"})   # server appends
    oq.update("A", status="CANCELLED")                  # dashboard writes back

    ids = {d["draft_id"]: d["status"] for d in oq.load()}
    assert ids == {"A": "CANCELLED", "B": "PENDING_APPROVAL"}, \
        f"the concurrent append must survive, got {ids}"
    assert len(stale) == 1


def test_updating_a_draft_that_is_gone_reports_it_rather_than_recreating_it(tmp_path, monkeypatch):
    path = tmp_path / "order_drafts.json"
    monkeypatch.setattr(oq, "default_path", lambda: str(path))
    oq.append({"draft_id": "A", "status": "PENDING_APPROVAL"})

    assert oq.update("NOPE", status="CANCELLED") is None
    assert len(oq.load()) == 1


def test_a_half_written_queue_is_never_left_behind(tmp_path, monkeypatch):
    """The write is atomic, so a crash mid-write cannot truncate the queue."""
    path = tmp_path / "order_drafts.json"
    monkeypatch.setattr(oq, "default_path", lambda: str(path))
    oq.append({"draft_id": "A", "status": "PENDING_APPROVAL"})
    oq.update("A", status="EXECUTED")

    assert not os.path.exists(str(path) + ".tmp")
    assert json.loads(path.read_text(encoding="utf-8"))[0]["status"] == "EXECUTED"


def test_the_queue_file_is_not_readable_by_other_users(tmp_path, monkeypatch):
    """
    Staged orders name what an account is about to buy. On Windows the file
    inherited the repository's ACL, which on a default checkout under C:\ grants
    Authenticated Users Modify -- so any process on the box could read pending
    trades or rewrite their quantities before approval.
    """
    path = tmp_path / "order_drafts.json"
    monkeypatch.setattr(oq, "default_path", lambda: str(path))
    oq.append({"draft_id": "A", "status": "PENDING_APPROVAL"})

    assert oq.restrict_to_owner(str(path)) is not False, \
        "the queue must be restricted to its owner, or say it could not be"
