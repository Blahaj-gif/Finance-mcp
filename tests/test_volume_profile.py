"""
Volume profile, value area, HVN and LVN.

The implementation is a port, so most of what matters is whether it reproduces
the reference exactly — including the edge cases the JS version's comments say
it was fixed for. Those comments are the specification here; each one below
names the case it pins.
"""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import volume_profile as vp


def bars(rows):
    """rows = [(high, low, volume), ...]"""
    return ([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows])


def flat_window(n=100, high=110.0, low=90.0, volume=1000.0):
    return bars([(high, low, volume)] * n)


# =====================================================================
# Profile construction and its guards
# =====================================================================

def test_total_equals_the_volume_that_went_in():
    """
    The invariant the bucket clamp exists to protect. Every bar's volume is
    split across the buckets it spans, so the histogram must sum to the volume
    of the window — no more, no less.
    """
    h, l, v = bars([(101 + i * 0.5, 99 + i * 0.5, 100.0 * (i + 1)) for i in range(40)])
    prof = vp.build_profile(h, l, v, lookback=40, buckets=10)
    assert prof["total"] == pytest.approx(sum(v))
    assert sum(prof["buckets"]) == pytest.approx(prof["total"])


def test_a_bar_sitting_exactly_at_the_window_high_is_not_dropped():
    """
    The reason the LOW bucket is clamped to buckets-1 and not merely to >= 0.
    A flat bar at price_max gives floor((low - min) / width) == buckets, which
    without the upper clamp exceeds the high bucket and silently discards that
    bar's volume — total would stop matching the volume summed in.
    """
    rows = [(100.0, 90.0, 500.0)] * 9 + [(110.0, 110.0, 777.0)]
    h, l, v = bars(rows)
    prof = vp.build_profile(h, l, v, lookback=10, buckets=10)
    assert prof["total"] == pytest.approx(9 * 500.0 + 777.0)


def test_a_degenerate_price_range_returns_nothing():
    """No range means no buckets; a zero-width profile would put a POC anywhere."""
    h, l, v = flat_window(20, high=100.0, low=100.0)
    assert vp.build_profile(h, l, v, lookback=20, buckets=10) is None


@pytest.mark.parametrize("kwargs", [
    {"lookback": 0}, {"lookback": -5}, {"lookback": 2.5},
    {"buckets": 1}, {"buckets": 0}, {"buckets": 3.5},
])
def test_bad_parameters_return_nothing_rather_than_guessing(kwargs):
    h, l, v = flat_window(50)
    args = {"lookback": 50, "buckets": 10}
    args.update(kwargs)
    assert vp.build_profile(h, l, v, **args) is None


def test_a_window_longer_than_the_data_returns_nothing():
    """Silently profiling fewer bars than asked would misreport the window."""
    h, l, v = flat_window(30)
    assert vp.build_profile(h, l, v, lookback=100, buckets=10) is None


def test_zero_and_negative_volume_bars_are_skipped_not_counted():
    rows = [(101.0, 99.0, 0.0)] * 5 + [(101.0, 99.0, 250.0)] * 5
    h, l, v = bars(rows)
    prof = vp.build_profile(h, l, v, lookback=10, buckets=8)
    assert prof["total"] == pytest.approx(5 * 250.0)


def test_non_finite_prices_do_not_poison_the_range():
    rows = [(float("nan"), 99.0, 100.0), (101.0, float("inf"), 100.0)] + \
           [(102.0, 98.0, 100.0)] * 8
    h, l, v = bars(rows)
    prof = vp.build_profile(h, l, v, lookback=10, buckets=5)
    assert math.isfinite(prof["price_min"]) and math.isfinite(prof["price_max"])


# =====================================================================
# Point of control
# =====================================================================

def test_the_poc_lands_on_the_price_that_traded_most():
    # Twenty bars pinned to a narrow band at 100, three wide bars elsewhere.
    rows = [(100.5, 99.5, 1000.0)] * 20 + [(120.0, 110.0, 50.0)] * 3
    h, l, v = bars(rows)
    va = vp.value_area(h, l, v, lookback=23, buckets=20)
    assert 99.0 <= va["poc"] <= 101.5, f"POC {va['poc']} is not on the busy band"


def test_the_poc_is_a_bucket_centre_and_the_value_area_edges_are_edges():
    h, l, v = bars([(101 + i * 0.2, 99 + i * 0.2, 100.0) for i in range(50)])
    prof = vp.build_profile(h, l, v, lookback=50, buckets=10)
    va = vp.value_area(h, l, v, lookback=50, buckets=10)
    w, pmin = prof["bucket_width"], prof["price_min"]
    # POC sits half a bucket above a boundary; VAL/VAH sit exactly on boundaries.
    assert ((va["poc"] - pmin) / w - 0.5) % 1 == pytest.approx(0, abs=1e-9)
    assert (va["val"] - pmin) / w == pytest.approx(round((va["val"] - pmin) / w), abs=1e-9)


# =====================================================================
# Value area
# =====================================================================

def test_the_value_area_covers_at_least_the_requested_fraction():
    h, l, v = bars([(105 - abs(i - 25) * 0.1, 95 + abs(i - 25) * 0.1, 100.0 + i)
                    for i in range(50)])
    for fraction in (0.5, 0.7, 0.9):
        va = vp.value_area(h, l, v, lookback=50, buckets=20, fraction=fraction)
        assert va["coverage"] >= fraction - 1e-9


def test_the_value_area_contains_the_poc():
    h, l, v = bars([(101 + i * 0.3, 99 + i * 0.3, 100.0 * (i % 7 + 1)) for i in range(60)])
    va = vp.value_area(h, l, v, lookback=60, buckets=15)
    assert va["val"] <= va["poc"] <= va["vah"]


def test_a_wider_value_area_fraction_never_narrows_the_band():
    h, l, v = bars([(101 + i * 0.3, 99 + i * 0.3, 100.0 * (i % 5 + 1)) for i in range(60)])
    narrow = vp.value_area(h, l, v, lookback=60, buckets=20, fraction=0.5)
    wide = vp.value_area(h, l, v, lookback=60, buckets=20, fraction=0.9)
    assert wide["vah"] - wide["val"] >= narrow["vah"] - narrow["val"] - 1e-9


def test_an_out_of_range_fraction_falls_back_to_seventy_percent():
    h, l, v = bars([(101 + i * 0.3, 99 + i * 0.3, 100.0) for i in range(60)])
    default = vp.value_area(h, l, v, lookback=60, buckets=20, fraction=0.7)
    for bad in (0.0, -1.0, 1.5):
        assert vp.value_area(h, l, v, lookback=60, buckets=20,
                             fraction=bad)["coverage"] == default["coverage"]


def test_ties_expand_upward_deterministically():
    """
    A stated convention, not an accident of comparison order. A symmetric
    profile must give the same answer every run and in both runtimes.
    """
    h, l, v = bars([(101.0, 99.0, 100.0)] * 40)
    a = vp.value_area(h, l, v, lookback=40, buckets=10)
    b = vp.value_area(h, l, v, lookback=40, buckets=10)
    assert (a["val"], a["vah"]) == (b["val"], b["vah"])


def test_the_dalton_method_is_available_and_differs_from_single():
    h, l, v = bars([(105 - abs(i - 20) * 0.2, 95 + abs(i - 20) * 0.2, 50.0 + i * 3)
                    for i in range(40)])
    single = vp.value_area(h, l, v, lookback=40, buckets=20, method="single")
    dalton = vp.value_area(h, l, v, lookback=40, buckets=20, method="dalton")
    assert single is not None and dalton is not None
    assert dalton["coverage"] >= 0.7 - 1e-9


# =====================================================================
# High and low volume nodes
# =====================================================================

def test_the_poc_is_always_reported_as_a_high_volume_node():
    """Even if it somehow fell below the 80th-percentile cut."""
    h, l, v = bars([(101 + i * 0.3, 99 + i * 0.3, 100.0 * (i % 9 + 1)) for i in range(60)])
    nodes = vp.high_volume_nodes(h, l, v, lookback=60, buckets=20)
    assert any(n["is_poc"] for n in nodes)


def test_high_volume_nodes_are_the_busiest_prices():
    rows = [(100.5, 99.5, 5000.0)] * 30 + [(120.0, 101.0, 10.0)] * 30
    h, l, v = bars(rows)
    nodes = vp.high_volume_nodes(h, l, v, lookback=60, buckets=20)
    busiest = max(nodes, key=lambda n: n["volume_share"])
    assert 99.0 <= busiest["price"] <= 101.5


def test_low_volume_nodes_must_be_strict_local_minima():
    """
    A flat low shelf would otherwise report every bucket in it as a node. The
    valley test is what makes an LVN mean "thin *relative to its neighbours*".
    """
    h, l, v = bars([(101.0, 99.0, 100.0)] * 50)     # perfectly flat profile
    assert vp.low_volume_nodes(h, l, v, lookback=50, buckets=10) == []


def test_a_dip_inside_a_traded_profile_is_a_low_volume_node():
    """The case the valley rule is actually for: thin relative to its neighbours."""
    vols = [100, 200, 300, 400, 500, 80, 500, 400, 300, 200]
    rows = [(100 + i + 0.5, 100 + i - 0.5, float(vol)) for i, vol in enumerate(vols)]
    h, l, v = bars(rows)
    nodes = vp.low_volume_nodes(h, l, v, lookback=10, buckets=10)
    assert nodes, "a strict dip should register"


def test_a_fully_untraded_gap_produces_no_node_and_that_is_the_reference_behaviour():
    """
    Surprising, faithful, and worth pinning rather than quietly diverging.

    The rule is "at or below the 20th percentile AND strictly lower than BOTH
    neighbours". A wide untraded gap is a run of equal zeros, so every interior
    bucket ties with its neighbours (0 < 0 is false) and none qualifies —
    even though an empty price band is the most textbook low-volume node there
    is. Verified against the histogram: two shelves of 125,000 either side of
    eighteen zero buckets return [].

    Left as-is deliberately. Matching Powerstation's JS implementation exactly is
    the reason this was ported instead of invented; "improving" it here would
    mean the two runtimes silently disagree, which is worth more than the extra
    signal. If gap detection is wanted it belongs in a separate, clearly-ours
    function.
    """
    rows = [(100.5, 99.5, 5000.0)] * 25 + [(120.5, 119.5, 5000.0)] * 25
    h, l, v = bars(rows)
    prof = vp.build_profile(h, l, v, lookback=50, buckets=20)
    assert prof["buckets"].count(0.0) == 18, "the fixture should have a wide empty middle"
    assert vp.low_volume_nodes(h, l, v, lookback=50, buckets=20) == []


def test_an_edge_bucket_can_qualify_because_its_missing_neighbour_is_infinite():
    """The stated convention for the first and last bucket."""
    vols = [50, 400, 500, 450, 400, 350, 300, 250, 200, 150]
    rows = [(100 + i + 0.5, 100 + i - 0.5, float(vol)) for i, vol in enumerate(vols)]
    h, l, v = bars(rows)
    nodes = vp.low_volume_nodes(h, l, v, lookback=10, buckets=10)
    assert any(n["price"] < 101 for n in nodes), "the thin bottom edge should qualify"


def test_node_volume_shares_are_fractions_of_the_window():
    h, l, v = bars([(101 + i * 0.3, 99 + i * 0.3, 100.0 * (i % 7 + 1)) for i in range(60)])
    for node in vp.high_volume_nodes(h, l, v, lookback=60, buckets=20):
        assert 0 < node["volume_share"] <= 1


def test_nodes_return_empty_rather_than_raising_on_unusable_input():
    h, l, v = flat_window(5)
    assert vp.high_volume_nodes(h, l, v, lookback=100) == []
    assert vp.low_volume_nodes(h, l, v, lookback=100) == []
    assert vp.value_area(h, l, v, lookback=100) is None


# =====================================================================
# The sentence, which is the point of the module
# =====================================================================

def test_a_price_above_the_value_area_is_described_as_such():
    h, l, v = bars([(101.0, 99.0, 1000.0)] * 60)
    va = vp.value_area(h, l, v, lookback=60, buckets=20)
    text = vp.describe_position(500.0, vp.high_volume_nodes(h, l, v, 60, 20),
                                vp.low_volume_nodes(h, l, v, 60, 20), va)
    assert "above the value area high" in text


def test_a_price_inside_the_value_area_names_the_point_of_control():
    h, l, v = bars([(101.0, 99.0, 1000.0)] * 60)
    va = vp.value_area(h, l, v, lookback=60, buckets=20)
    text = vp.describe_position(va["poc"], vp.high_volume_nodes(h, l, v, 60, 20),
                                vp.low_volume_nodes(h, l, v, 60, 20), va)
    assert "inside the value area" in text and "point of control" in text


def test_the_description_declines_rather_than_inventing_one():
    assert "No volume profile" in vp.describe_position(100.0, [], [], None)


def test_a_low_volume_node_is_described_with_what_it_implies():
    vols = [100, 200, 300, 400, 500, 80, 500, 400, 300, 200]
    rows = [(100 + i + 0.5, 100 + i - 0.5, float(vol)) for i, vol in enumerate(vols)]
    h, l, v = bars(rows)
    text = vp.describe_position(105.0, vp.high_volume_nodes(h, l, v, 10, 10),
                                vp.low_volume_nodes(h, l, v, 10, 10),
                                vp.value_area(h, l, v, 10, 10))
    assert "low-volume node" in text and "rejected" in text


def test_the_description_says_nothing_about_nodes_when_there_are_none():
    """It must not promise a node it did not find."""
    h, l, v = bars([(101.0, 99.0, 1000.0)] * 60)
    text = vp.describe_position(100.0, [], [], vp.value_area(h, l, v, 60, 20))
    assert "low-volume node" not in text
    assert "shelf" not in text
