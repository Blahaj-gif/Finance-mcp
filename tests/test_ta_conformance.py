"""
Golden-vector conformance for the vendored TA core.

The point of vendoring `dashboard/ta_core.py` rather than writing our own was
that a second, independent implementation — Powerstation's JS twin — is held to
this same fixture. That only stays true if we actually run it. A vendored copy
nobody checks is just a copy.

The fixture is the contract, not this file: `tests/fixtures/ta_golden_vectors.json`
carries 112 cases over 96 callables, with `null` meaning NaN and a per-case
tolerance. If a re-vendor drifts, these fail here before anything ships.
"""
import json
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import ta_core

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "ta_golden_vectors.json")

with open(FIXTURE, encoding="utf-8") as fh:
    _GOLDEN = json.load(fh)

TOL_DEFAULT = _GOLDEN["tol_default"]
CASES = _GOLDEN["cases"]
REGISTRY = ta_core.REGISTRY


def _matches(got, expected, tol):
    """NaN-aware comparison. `null` in the fixture means NaN, and NaN == NaN here."""
    a = float("nan") if got is None else float(got)
    b = float("nan") if expected is None else float(expected)
    if math.isnan(a) and math.isnan(b):
        return True
    if math.isnan(a) or math.isnan(b):
        return False
    return abs(a - b) <= tol


def _ids(cases):
    return [c["id"] for c in cases]


# =====================================================================
# The vectors themselves
# =====================================================================

@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
def test_golden_vector(case):
    fn = case["fn"]
    assert fn in REGISTRY, f"'{fn}' is not in the vendored REGISTRY"

    got = REGISTRY[fn](**case["input"], **case.get("params", {}))
    tol = case.get("tol", TOL_DEFAULT)

    if "expected_multi" in case:
        assert isinstance(got, dict), f"{fn} should return a dict of named series"
        for key, expected in case["expected_multi"].items():
            series = got.get(key)
            assert series is not None, f"{fn} did not return output '{key}'"
            assert len(series) == len(expected), (
                f"{fn}.{key}: length {len(series)} != {len(expected)}")
            for i, (g, e) in enumerate(zip(series, expected)):
                assert _matches(g, e, tol), f"{fn}.{key}[{i}]: got {g!r}, expected {e!r}"
    else:
        expected = case["expected"]
        # Index alignment is a contract every consumer relies on: an indicator
        # shorter than its input silently misaligns against the bar it belongs to.
        assert len(got) == len(expected), f"{fn}: length {len(got)} != {len(expected)}"
        for i, (g, e) in enumerate(zip(got, expected)):
            assert _matches(g, e, tol), f"{fn}[{i}]: got {g!r}, expected {e!r}"


# =====================================================================
# The fixture must keep covering the core
# =====================================================================

def test_every_callable_has_a_golden_vector():
    """
    Upstream's rule is "no indicator ships until its case passes in both
    runtimes". An unpinned callable is one that can drift on a re-vendor with
    nothing to catch it.
    """
    pinned = {c["fn"] for c in CASES}
    unpinned = sorted(set(REGISTRY) - pinned)
    assert not unpinned, f"{len(unpinned)} callables have no vector: {unpinned}"


def test_every_vector_names_a_real_callable():
    missing = sorted({c["fn"] for c in CASES} - set(REGISTRY))
    assert not missing, f"fixture references callables the core does not have: {missing}"


def test_the_core_is_the_size_upstream_shipped():
    """A partial re-vendor is a silent capability loss; pin the count."""
    assert len(REGISTRY) == 96
    assert len(CASES) == 112


def test_the_vendored_copy_is_not_edited_in_place():
    """
    Local edits break the parity that is the whole reason for vendoring. The
    header says so; this makes it enforceable.
    """
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "dashboard", "ta_core.py"), encoding="utf-8").read()
    assert "Do not edit in place" in src
    assert "Vendored verbatim" in src


# =====================================================================
# Contract properties the fixture does not state explicitly
# =====================================================================

# Which argument of each callable is a series and which is a scalar comes from
# the fixture, not from a guess at the name. Guessing got this wrong first time:
# `barssince(cond)` and `valuewhen(cond, src)` take *cond* as a series, a name
# that is not on any obvious list of series-ish words, so they were handed a
# scalar and raised — a harness bug that read exactly like a contract violation.
_SERIES_ARGS = {}
_SCALAR_ARGS = {}
for _case in CASES:
    _SERIES_ARGS.setdefault(_case["fn"], set()).update(_case["input"])
    _SCALAR_ARGS.setdefault(_case["fn"], {}).update(_case.get("params", {}))


@pytest.mark.parametrize("fn", sorted(REGISTRY))
def test_no_callable_raises_on_empty_input(fn):
    """
    "Never raises on short/empty input" is upstream's stated contract, and the
    MCP tools lean on it — a cold symbol with no bars must not take a tool down
    mid-answer.
    """
    kwargs = {name: [] for name in _SERIES_ARGS.get(fn, ())}
    kwargs.update(_SCALAR_ARGS.get(fn, {}))
    try:
        out = REGISTRY[fn](**kwargs)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"{fn} raised on empty input: {type(exc).__name__}: {exc}")
    assert len(out) == 0 if not isinstance(out, dict) else all(
        len(v) == 0 for v in out.values()), f"{fn} invented output from no input"


@pytest.mark.parametrize("fn", sorted(REGISTRY))
def test_no_callable_raises_on_a_series_shorter_than_its_period(fn):
    """
    The warm-up path. A two-bar series against a 14-period indicator is the
    normal state of a freshly-listed symbol, not an error.
    """
    kwargs = {name: [1.0, 2.0] for name in _SERIES_ARGS.get(fn, ())}
    kwargs.update(_SCALAR_ARGS.get(fn, {}))
    try:
        out = REGISTRY[fn](**kwargs)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"{fn} raised on a short series: {type(exc).__name__}: {exc}")
    series = list(out.values())[0] if isinstance(out, dict) else out
    assert len(series) == 2, f"{fn} broke index alignment on a short series"
