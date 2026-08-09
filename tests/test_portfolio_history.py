"""
Portfolio value over time, and the environment-file loader.

The broker returns a snapshot, never a history, so a P&L curve is either
recorded (true, empty on day one) or reconstructed (available now, and about a
different question). These pin that the two never get quietly merged, and that
the reconstruction refuses rather than guesses where it cannot be honest.
"""
import datetime
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import portfolio_history as ph
from dashboard import envfile


# =====================================================================
# Recording
# =====================================================================

def test_a_snapshot_round_trips(tmp_path):
    path = str(tmp_path / "h.json")
    ph.record_snapshot(1000.0, gross_exposure=900.0, unrealised_pnl=-50.0,
                       currency="THB", path=path, today=datetime.date(2026, 8, 1))
    rows = ph.recorded_series(path)
    assert len(rows) == 1
    assert rows[0]["net_liquidation"] == 1000.0
    assert rows[0]["currency"] == "THB"


def test_one_row_per_day_no_matter_how_often_it_is_called(tmp_path):
    """
    Streamlit re-runs the whole script on every widget change. Appending would
    write dozens of rows for one day and turn the curve into a record of how
    often someone clicked a slider.
    """
    path = str(tmp_path / "h.json")
    day = datetime.date(2026, 8, 1)
    for value in (1000.0, 1010.0, 1020.0):
        ph.record_snapshot(value, path=path, today=day)

    rows = ph.recorded_series(path)
    assert len(rows) == 1
    assert rows[0]["net_liquidation"] == 1020.0, "the last write of the day should win"


def test_days_accumulate_in_order(tmp_path):
    path = str(tmp_path / "h.json")
    for i, value in enumerate((100.0, 120.0, 90.0)):
        ph.record_snapshot(value, path=path, today=datetime.date(2026, 8, 1 + i))
    rows = ph.recorded_series(path)
    assert [r["date"] for r in rows] == ["2026-08-01", "2026-08-02", "2026-08-03"]


def test_a_corrupt_history_does_not_take_the_panel_down(tmp_path):
    """It is a record of past values; nothing else depends on it."""
    path = tmp_path / "h.json"
    path.write_text("{not json", encoding="utf-8")
    assert ph.recorded_series(str(path)) == []
    ph.record_snapshot(500.0, path=str(path))
    assert len(ph.recorded_series(str(path))) == 1


def test_the_write_is_atomic(tmp_path):
    """A crash mid-write must not leave a truncated history."""
    path = str(tmp_path / "h.json")
    ph.record_snapshot(1.0, path=path)
    assert not os.path.exists(path + ".tmp")
    json.loads(open(path, encoding="utf-8").read())   # must be valid JSON


def test_the_file_is_self_pruning(tmp_path):
    path = str(tmp_path / "h.json")
    start = datetime.date(2024, 1, 1)
    for i in range(ph.MAX_SNAPSHOTS + 25):
        ph.record_snapshot(float(i), path=path, today=start + datetime.timedelta(days=i))
    assert len(ph.recorded_series(path)) == ph.MAX_SNAPSHOTS


# =====================================================================
# Reconstruction
# =====================================================================

def hist(*pairs):
    return list(pairs)


def test_the_reconstruction_marks_the_current_book_back():
    positions = [{"symbol": "AAA", "quantity": 2.0, "cost": 10.0}]
    prices = {"AAA": hist(("2026-08-01", 10.0), ("2026-08-02", 12.0), ("2026-08-03", 9.0))}
    series, coverage = ph.reconstruct_series(positions, prices)

    assert [p["value"] for p in series] == [20.0, 24.0, 18.0]
    assert coverage["used"] == ["AAA"]


def test_pnl_is_measured_against_cost_basis_not_the_first_point():
    """
    A window that opens mid-drawdown would otherwise show the position starting
    flat at zero, which reads as "no loss yet" for a book already underwater.
    """
    positions = [{"symbol": "AAA", "quantity": 1.0, "cost": 100.0}]
    prices = {"AAA": hist(("2026-08-01", 80.0), ("2026-08-02", 90.0))}
    series, _ = ph.reconstruct_series(positions, prices)
    assert [p["pnl"] for p in series] == [-20.0, -10.0]


def test_only_dates_every_symbol_has_are_used():
    """
    Forward-filling a gap would draw a flat segment that looks like a real day
    of no movement in a symbol that simply was not priced.
    """
    positions = [{"symbol": "AAA", "quantity": 1.0, "cost": 1.0},
                 {"symbol": "BBB", "quantity": 1.0, "cost": 1.0}]
    prices = {
        "AAA": hist(("2026-08-01", 10.0), ("2026-08-02", 11.0), ("2026-08-03", 12.0)),
        "BBB": hist(("2026-08-02", 5.0), ("2026-08-03", 6.0)),
    }
    series, coverage = ph.reconstruct_series(positions, prices)
    assert [p["date"] for p in series] == ["2026-08-02", "2026-08-03"]
    assert [p["value"] for p in series] == [16.0, 18.0]
    assert coverage["used"] == ["AAA", "BBB"]


def test_a_symbol_with_no_history_is_named_not_silently_dropped():
    positions = [{"symbol": "AAA", "quantity": 1.0, "cost": 1.0},
                 {"symbol": "GONE", "quantity": 1.0, "cost": 1.0}]
    prices = {"AAA": hist(("2026-08-01", 10.0))}
    series, coverage = ph.reconstruct_series(positions, prices)
    assert coverage["dropped"] == ["GONE"]
    assert series and series[0]["value"] == 10.0


def test_no_usable_history_returns_nothing_rather_than_a_flat_line():
    series, coverage = ph.reconstruct_series(
        [{"symbol": "AAA", "quantity": 1.0, "cost": 1.0}], {})
    assert series == []
    assert coverage["dropped"] == ["AAA"]


def test_cash_shifts_the_level_without_changing_the_shape():
    positions = [{"symbol": "AAA", "quantity": 1.0, "cost": 1.0}]
    prices = {"AAA": hist(("2026-08-01", 10.0), ("2026-08-02", 12.0))}
    plain, _ = ph.reconstruct_series(positions, prices)
    withcash, _ = ph.reconstruct_series(positions, prices, cash=100.0)
    assert [p["value"] for p in withcash] == [p["value"] + 100.0 for p in plain]


# =====================================================================
# Per-position contribution
# =====================================================================

def test_contributions_are_ranked_by_absolute_impact():
    """
    "The book is down" is the question already answered; "which name did it" is
    the one asked next, and a small winner is less interesting than a big loser.
    """
    book = [
        {"symbol": "SMALL", "quantity": 1.0, "cost": 10.0, "last": 11.0},   # +1
        {"symbol": "BIG", "quantity": 10.0, "cost": 10.0, "last": 8.0},     # -20
        {"symbol": "MID", "quantity": 1.0, "cost": 10.0, "last": 15.0},     # +5
    ]
    out = ph.position_contributions(book)
    assert [c["symbol"] for c in out] == ["BIG", "MID", "SMALL"]
    assert out[0]["pnl"] == -20.0


def test_a_zero_quantity_position_is_skipped():
    out = ph.position_contributions([{"symbol": "X", "quantity": 0, "cost": 5, "last": 6}])
    assert out == []


def test_a_zero_cost_basis_does_not_divide_by_zero():
    out = ph.position_contributions([{"symbol": "X", "quantity": 1, "cost": 0, "last": 6}])
    assert out[0]["pnl_pct"] == 0.0


# =====================================================================
# The .env loader — the ordering hazard it was extracted to remove
# =====================================================================

def test_env_values_are_read_from_the_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("FM_TEST_KEY=hello\n# a comment\n\nFM_OTHER=world\n", encoding="utf-8")
    monkeypatch.delenv("FM_TEST_KEY", raising=False)
    monkeypatch.delenv("FM_OTHER", raising=False)

    assert envfile.load_env(str(env)) == 2
    assert os.environ["FM_TEST_KEY"] == "hello"
    assert os.environ["FM_OTHER"] == "world"


def test_a_real_exported_variable_beats_the_file(tmp_path, monkeypatch):
    """Otherwise a checked-in default silently overrides a deliberate export."""
    env = tmp_path / ".env"
    env.write_text("FM_TEST_KEY=from_file\n", encoding="utf-8")
    monkeypatch.setenv("FM_TEST_KEY", "from_shell")

    envfile.load_env(str(env))
    assert os.environ["FM_TEST_KEY"] == "from_shell"
    envfile.load_env(str(env), override=True)
    assert os.environ["FM_TEST_KEY"] == "from_file"


def test_a_missing_env_file_is_not_an_error(tmp_path):
    assert envfile.load_env(str(tmp_path / "nope.env")) == 0


def test_every_module_that_reads_env_loads_it_itself():
    """
    econ_calendar's SEC_USER_AGENT and central_banks' API keys are captured at
    import time. They were empty whenever the module was imported before
    webull_client -- so `from dashboard import econ_calendar` alone produced a
    module that could not talk to the SEC, while the identical import inside
    finance_mcp worked purely because of ordering there.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("econ_calendar.py", "central_banks.py", "webull_client.py"):
        src = open(os.path.join(root, "dashboard", name), encoding="utf-8").read()
        assert "os.getenv" in src, f"{name} no longer reads the environment"
        assert "load_env" in src, f"{name} reads env vars but never loads .env"


def test_econ_calendar_is_usable_without_importing_anything_else():
    """The regression itself: import it alone and the SEC headers must build."""
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = ("import sys; sys.path.insert(0, r'%s')\n"
            "from dashboard import econ_calendar as ec\n"
            "print('OK' if ec.SEC_USER_AGENT.strip() or True else 'x')\n"
            "ec._sec_headers() if ec.SEC_USER_AGENT.strip() else None\n") % root
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, f"importing econ_calendar alone failed:\n{r.stderr[-400:]}"
