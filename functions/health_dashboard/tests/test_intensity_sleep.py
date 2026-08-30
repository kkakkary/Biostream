"""Tests for the Intensity × Sleep view's math (experiment.py) and chart
(charts.py): the day/night split by intensity minutes and Welch's t-test.
The BigQuery join that PAIRS day D with night D+1 lives in SQL
(data.load_intensity_sleep) and is exercised against real data, not here —
these tests cover everything downstream of that frame.
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import charts      # noqa: E402
import experiment  # noqa: E402


def _frame(rows):
    """rows = [(moderate, vigorous, latency_seconds), ...]"""
    return pd.DataFrame(rows, columns=["moderate_intensity_minutes",
                                       "vigorous_intensity_minutes",
                                       "deep_sleep_latency_seconds"])


def test_garmin_intensity_minutes_doubles_vigorous():
    """Garmin's convention: 10 moderate + 10 vigorous = 30 intensity minutes."""
    df = _frame([(10, 10, 600)])
    assert experiment.garmin_intensity_minutes(df).iloc[0] == 30


def test_garmin_intensity_minutes_treats_nan_as_zero():
    df = _frame([(None, None, 600), (15, None, 600)])
    assert list(experiment.garmin_intensity_minutes(df)) == [0, 15]


def test_split_threshold_is_strictly_greater_than_and_converts_to_minutes():
    """Strict >: exactly at the threshold stays a rest day (the user-facing
    rule is '> threshold intensity minutes'); latency comes back in minutes,
    not seconds. Threshold passed explicitly so this test doesn't drift
    every time the business default (INTENSITY_THRESHOLD_MIN) is retuned."""
    df = _frame([(41, 0, 600),    # just over threshold -> active, 10 min
                 (40, 0, 1200),   # exactly at threshold -> rest, 20 min
                 (0, 21, 300)])   # 2x21 vigorous = 42 -> active, 5 min
    active, rest = experiment.split_latency_by_intensity(df, threshold=40)
    assert sorted(active) == [5.0, 10.0]
    assert list(rest) == [20.0]


def test_split_threshold_custom_value_is_strict():
    """A caller-supplied threshold is also strict >, not >=."""
    df = _frame([(20, 0, 600), (19, 0, 1200)])
    active, rest = experiment.split_latency_by_intensity(df, threshold=20)
    assert active.empty
    assert sorted(rest) == [10.0, 20.0]


def test_welch_t_test_matches_hand_computation():
    """Fixed groups with known Welch result. For a=[10,12,14], b=[20,22,24]
    (equal variances n=3 each, mean diff -10, se = sqrt(4/3+4/3)):
    t = -10/sqrt(8/3) = -6.1237, Welch df = 4, two-sided p = 0.003602."""
    result = experiment.welch_t_test(pd.Series([10.0, 12.0, 14.0]),
                                     pd.Series([20.0, 22.0, 24.0]))
    assert result["n_a"] == result["n_b"] == 3
    assert result["mean_a"] == 12.0 and result["mean_b"] == 22.0
    assert result["sd_a"] == pytest.approx(2.0)
    assert result["t_stat"] == pytest.approx(-6.1237, abs=1e-4)
    assert result["p_value"] == pytest.approx(0.003602, abs=1e-5)


def test_welch_t_test_needs_two_per_group():
    """One night in a group -> descriptives only, no fabricated test."""
    result = experiment.welch_t_test(pd.Series([10.0]), pd.Series([20.0, 22.0]))
    assert result["t_stat"] is None and result["p_value"] is None
    assert result["n_a"] == 1 and result["mean_a"] == 10.0
    assert result["sd_a"] is None


def test_welch_t_test_drops_nans_before_counting():
    result = experiment.welch_t_test(pd.Series([10.0, None, 12.0]),
                                     pd.Series([20.0, 22.0]))
    assert result["n_a"] == 2 and result["p_value"] is not None


def test_spearman_perfect_negative_correlation():
    """More intensity, monotonically faster latency, every night -> rho = -1
    exactly, regardless of the actual minute gaps between nights."""
    df = _frame([(10, 0, 40 * 60), (20, 0, 30 * 60),
                (30, 0, 20 * 60), (40, 0, 10 * 60)])
    result = experiment.intensity_latency_spearman(df)
    assert result["n"] == 4
    assert result["rho"] == pytest.approx(-1.0)
    assert result["p_value"] == pytest.approx(0.0, abs=1e-9)


def test_spearman_partial_correlation_matches_scipy():
    """A non-monotonic relationship: known rho/p, hand-verified against
    scipy.stats.spearmanr directly (not just re-deriving the same call)."""
    df = _frame([(10, 0, 15 * 60), (20, 0, 25 * 60), (30, 0, 10 * 60),
                (40, 0, 30 * 60), (50, 0, 20 * 60), (60, 0, 5 * 60)])
    result = experiment.intensity_latency_spearman(df)
    assert result["n"] == 6
    assert result["rho"] == pytest.approx(-0.2571428571428572)
    assert result["p_value"] == pytest.approx(0.6227871720116619)


def test_spearman_constant_intensity_is_none_not_nan():
    """Regression test: when every night's intensity is identical, scipy's
    correlation is mathematically undefined and returns NaN — which must
    surface as None (rho/p unknown), not as a NaN that a naive `p < 0.05`
    check would silently read as False, i.e. "not statistically
    significant" — a wrong, definitive-looking claim to show a user."""
    df = _frame([(10, 0, 600), (10, 0, 1200), (10, 0, 300)])
    result = experiment.intensity_latency_spearman(df)
    assert result["n"] == 3
    assert result["rho"] is None
    assert result["p_value"] is None


def test_spearman_needs_at_least_three_nights():
    """Below scipy's minimum for a meaningful statistic -> n reported, no
    fabricated rho/p."""
    df = _frame([(10, 0, 600), (20, 0, 1200)])
    result = experiment.intensity_latency_spearman(df)
    assert result["n"] == 2
    assert result["rho"] is None and result["p_value"] is None


def test_intensity_latency_scatter_fig_structure():
    fig = charts.intensity_latency_scatter_fig(
        pd.Series([10, 20, 30]), pd.Series([40, 30, 10]),
        pd.Series(["2026-01-01", "2026-01-02", "2026-01-03"]))
    scatters = [t for t in fig.data if t.type == "scatter"]
    assert len(scatters) == 1
    assert scatters[0].mode == "markers"
    assert list(scatters[0].x) == [10, 20, 30]
    assert list(scatters[0].customdata) == ["2026-01-01", "2026-01-02", "2026-01-03"]
    # Single series -> no legend, per the dataviz rule (title names the series).
    assert fig.layout.showlegend in (None, False)


def test_latency_distribution_fig_structure():
    """Two overlaid histograms sharing one bin size, so bars are comparable."""
    fig = charts.latency_distribution_fig(pd.Series([5.0, 10.0]),
                                          pd.Series([15.0, 20.0]), threshold=20)
    hists = [t for t in fig.data if t.type == "histogram"]
    assert len(hists) == 2
    assert all(t.xbins.size == 5 for t in hists)
    assert fig.layout.barmode == "overlay"
    # One dashed mean line per group.
    mean_lines = [s for s in fig.layout.shapes if s.line.dash == "dash"]
    assert len(mean_lines) == 2


def test_latency_distribution_fig_empty_group():
    """A subject whose days are all one group still gets a chart, with the
    empty group simply absent (no zero-length trace, no crash)."""
    fig = charts.latency_distribution_fig(pd.Series([], dtype=float),
                                          pd.Series([15.0, 20.0]), threshold=20)
    assert len([t for t in fig.data if t.type == "histogram"]) == 1
