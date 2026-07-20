import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import charts  # noqa: E402


def test_glucose_fig_breaks_line_at_data_gaps():
    # Arrange: two readings 5 min apart, then a 6-hour hole, then one more.
    df = pd.DataFrame({
        "ts": pd.to_datetime(["2026-07-18 00:00", "2026-07-18 00:05",
                              "2026-07-18 06:00"]),
        "glucose_mg_dl": [80.0, 82.0, 90.0],
    })

    # Act
    fig = charts.glucose_fig(df)

    # Assert: the plotted y-values contain a None/NaN where the hole is,
    # which is what makes Plotly lift the pen instead of bridging the gap.
    y = fig.data[0].y
    assert pd.isna(y).sum() == 1
    assert len(y) == 4  # 3 real points + 1 inserted break


def test_bp_fig_has_one_trace_per_series():
    df = pd.DataFrame({
        "measurement_ts_utc": pd.to_datetime(["2026-07-02", "2026-07-03"]),
        "systolic": [130, 118],
        "diastolic": [79, 75],
        "pulse": [60, 62],
    })

    fig = charts.bp_fig(df)

    assert [t.name for t in fig.data] == ["Systolic", "Diastolic"]
    assert fig.layout.showlegend is True


def test_sleep_fig_stacks_three_stages():
    df = pd.DataFrame({
        "date": pd.to_datetime(["2026-07-18"]),
        "deep_h": [2.0],
        "rem_h": [1.0],
        "light_h": [4.0],
    })

    fig = charts.sleep_fig(df)

    assert [t.name for t in fig.data] == ["Deep", "REM", "Light"]
    assert fig.data[0].y[0] == 2.0  # Deep trace carries deep_h, not a swap
    assert fig.layout.barmode == "stack"