"""
IV history tests.

IV rank is defined against implied-volatility history. Nothing free publishes
that series, so this module accumulates one. These tests pin the two things
that matter: the rank is only reported once there is enough history to support
it, and the arithmetic is right when there is.
"""
import datetime
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import iv_history as ivh


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Never touch the real history file."""
    monkeypatch.setattr(ivh, "HISTORY_PATH", str(tmp_path / "iv_history.json"))
    yield


def seed(symbol, values, end=None):
    """Write `values` as consecutive daily observations ending today."""
    end = end or datetime.date.today()
    for i, v in enumerate(reversed(values)):
        ivh.record_snapshot(symbol, v, today=end - datetime.timedelta(days=i))


# =====================================================================
# Recording
# =====================================================================

def test_records_one_observation_per_day():
    assert ivh.record_snapshot("MU", 0.55) == 1
    assert ivh.record_snapshot("MU", 0.58) == 1, "same day must overwrite, not append"
    assert ivh.observation_count("MU") == 1


def test_records_accumulate_across_days():
    seed("MU", [0.50, 0.52, 0.54])
    assert ivh.observation_count("MU") == 3


def test_ignores_a_nonsense_reading():
    ivh.record_snapshot("MU", 0.5)
    ivh.record_snapshot("MU", None)
    ivh.record_snapshot("MU", -1.0)
    ivh.record_snapshot("MU", 0.0)
    assert ivh.observation_count("MU") == 1


def test_symbols_are_kept_separate_and_case_insensitive():
    ivh.record_snapshot("mu", 0.5)
    ivh.record_snapshot("NVDA", 0.7)
    assert ivh.observation_count("MU") == 1
    assert ivh.observation_count("nvda") == 1


def test_store_is_bounded():
    seed("MU", [0.4 + i * 0.001 for i in range(ivh.MAX_PER_SYMBOL + 50)])
    assert ivh.observation_count("MU") <= ivh.MAX_PER_SYMBOL


def test_a_corrupt_store_does_not_break_recording(monkeypatch):
    with open(ivh.HISTORY_PATH, "w", encoding="utf-8") as f:
        f.write("{ this is not json")
    assert ivh.record_snapshot("MU", 0.5) == 1


# =====================================================================
# Ranking
# =====================================================================

def test_no_rank_before_the_minimum_history_exists():
    seed("MU", [0.5] * (ivh.MIN_OBSERVATIONS - 1))
    assert ivh.iv_rank("MU", 0.5) is None


def test_rank_appears_once_there_is_enough_history():
    seed("MU", [0.5] * ivh.MIN_OBSERVATIONS)
    assert ivh.iv_rank("MU", 0.5) is not None


def test_rank_positions_current_iv_inside_its_own_range():
    # 0.20 .. 0.60 recorded; today's 0.40 sits exactly halfway.
    values = [0.20 + (0.40 * i / 39) for i in range(40)]
    seed("MU", values)
    r = ivh.iv_rank("MU", 0.40)
    assert r["rank"] == pytest.approx(50.0, abs=1.0)
    assert r["low"] == pytest.approx(0.20, abs=0.001)
    assert r["high"] == pytest.approx(0.60, abs=0.001)


def test_rank_is_100_at_the_high_and_0_at_the_low():
    seed("MU", [0.20 + (0.40 * i / 39) for i in range(40)])
    assert ivh.iv_rank("MU", 0.60)["rank"] == pytest.approx(100.0, abs=0.5)
    assert ivh.iv_rank("MU", 0.20)["rank"] == pytest.approx(0.0, abs=0.5)


def test_rank_is_clamped_outside_the_recorded_range():
    seed("MU", [0.30] * 20 + [0.50] * 20)
    assert ivh.iv_rank("MU", 5.0)["rank"] == 100.0
    assert ivh.iv_rank("MU", 0.01)["rank"] == 0.0


def test_percentile_counts_observations_below_current():
    seed("MU", [0.10] * 30 + [0.90] * 10)   # 40 obs, 30 below 0.50
    assert ivh.iv_rank("MU", 0.50)["percentile"] == pytest.approx(75.0)


def test_flat_history_reports_the_midpoint_not_a_divide_by_zero():
    seed("MU", [0.42] * 40)
    assert ivh.iv_rank("MU", 0.42)["rank"] == 50.0


def test_observations_older_than_the_lookback_are_excluded():
    old = datetime.date.today() - datetime.timedelta(days=ivh.LOOKBACK_DAYS + 40)
    seed("MU", [0.9] * 40, end=old)
    assert ivh.iv_rank("MU", 0.5) is None, "stale observations must not support a rank"


# =====================================================================
# Coverage reporting
# =====================================================================

def test_coverage_reports_progress_toward_a_usable_rank():
    seed("MU", [0.5] * 10)
    c = ivh.coverage()["MU"]
    assert c["observations"] == 10
    assert c["usable"] is False
    assert c["needs"] == ivh.MIN_OBSERVATIONS - 10


def test_coverage_marks_a_symbol_usable_once_it_qualifies():
    seed("NVDA", [0.5] * ivh.MIN_OBSERVATIONS)
    assert ivh.coverage()["NVDA"]["usable"] is True
    assert ivh.coverage()["NVDA"]["needs"] == 0
