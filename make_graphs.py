"""Generate graphs from the pulled Garmin data.

Reads the CSV/JSON written by garmin_pull.py and produces PNG charts under
data/graphs/:

  * daily_trends.png  — day-over-day summary metrics (steps, resting HR, sleep
                        stages, stress, body battery, calories). Sparse until a
                        few days of data accumulate; fills in automatically.
  * intraday.png      — minute-level curves (heart rate, stress, body battery)
                        for the most recent day that has data.

Run after garmin_pull.py:
    python make_graphs.py
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: write files, no display needed
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).resolve().parent
CSV_DIR = ROOT / "data" / "csv"
JSON_DIR = ROOT / "data" / "json"
GRAPH_DIR = ROOT / "data" / "graphs"


def load_csv(name: str) -> pd.DataFrame | None:
    path = CSV_DIR / f"{name}.csv"
    if not path.exists():
        return None
    return pd.read_csv(path)


def daily_trends() -> Path | None:
    """Multi-panel day-over-day summary chart from user_summary + sleep."""
    us = load_csv("user_summary")
    if us is None or us.empty:
        print("daily_trends: no user_summary data")
        return None

    us["date"] = pd.to_datetime(us["calendarDate"])
    us = us.sort_values("date")

    sleep = load_csv("sleep")
    if sleep is not None and not sleep.empty:
        sleep["date"] = pd.to_datetime(sleep["dailySleepDTO.calendarDate"])
        sleep = sleep.sort_values("date")

    fig, axes = plt.subplots(3, 2, figsize=(14, 12))
    fig.suptitle("Garmin daily trends", fontsize=16, fontweight="bold")

    # Steps vs goal
    ax = axes[0, 0]
    ax.bar(us["date"], us["totalSteps"], color="#1f77b4", label="Steps")
    if us["dailyStepGoal"].notna().any():
        ax.plot(us["date"], us["dailyStepGoal"], "r--", label="Goal")
    ax.set_title("Steps")
    ax.legend(loc="upper left")

    # Resting / min / max heart rate
    ax = axes[0, 1]
    ax.plot(us["date"], us["restingHeartRate"], "o-", color="#2ca02c", label="Resting")
    if us["minHeartRate"].notna().any():
        ax.plot(us["date"], us["minHeartRate"], ".--", color="#7f7f7f", label="Min")
    if us["maxHeartRate"].notna().any():
        ax.plot(us["date"], us["maxHeartRate"], ".--", color="#d62728", label="Max")
    ax.set_title("Heart rate (bpm)")
    ax.legend(loc="upper left")

    # Sleep stages (stacked hours)
    ax = axes[1, 0]
    if sleep is not None and not sleep.empty:
        stages = {
            "Deep": ("dailySleepDTO.deepSleepSeconds", "#08306b"),
            "Light": ("dailySleepDTO.lightSleepSeconds", "#4292c6"),
            "REM": ("dailySleepDTO.remSleepSeconds", "#9e9ac8"),
            "Awake": ("dailySleepDTO.awakeSleepSeconds", "#fdae6b"),
        }
        bottom = pd.Series(0.0, index=sleep.index)
        plotted = False
        for label, (col, color) in stages.items():
            if col in sleep and sleep[col].notna().any():
                hours = sleep[col].fillna(0) / 3600.0
                ax.bar(sleep["date"], hours, bottom=bottom, label=label, color=color)
                bottom = bottom + hours
                plotted = True
        if plotted:
            ax.legend(loc="upper left")
    ax.set_title("Sleep stages (hours)")

    # Average stress
    ax = axes[1, 1]
    ax.bar(us["date"], us["averageStressLevel"], color="#ff7f0e")
    ax.set_title("Average stress level")
    ax.set_ylim(0, 100)

    # Body battery range
    ax = axes[2, 0]
    if us["bodyBatteryHighestValue"].notna().any():
        ax.plot(us["date"], us["bodyBatteryHighestValue"], "o-", color="#2ca02c",
                label="High")
        ax.plot(us["date"], us["bodyBatteryLowestValue"], "o-", color="#d62728",
                label="Low")
        ax.legend(loc="upper left")
    ax.set_title("Body battery")
    ax.set_ylim(0, 100)

    # Calories
    ax = axes[2, 1]
    if us["totalKilocalories"].notna().any():
        ax.bar(us["date"], us["totalKilocalories"], color="#c7c7c7", label="Total")
    if us["activeKilocalories"].notna().any():
        ax.bar(us["date"], us["activeKilocalories"], color="#ff9896", label="Active")
    ax.set_title("Calories (kcal)")
    ax.legend(loc="upper left")

    for ax in axes.flat:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
        for tick in ax.get_xticklabels():
            tick.set_rotation(45)

    GRAPH_DIR.mkdir(parents=True, exist_ok=True)
    out = GRAPH_DIR / "daily_trends.png"
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(out, dpi=120)
    plt.close(fig)
    return out


def _to_local_series(values, value_index, ts_index=0, offset_ms=0, skip=None):
    """Turn a Garmin [[ts, v, ...], ...] array into (times, values)."""
    times, vals = [], []
    for row in values or []:
        v = row[value_index]
        if v is None or (skip is not None and v == skip):
            continue
        times.append(pd.to_datetime(row[ts_index] + offset_ms, unit="ms"))
        vals.append(v)
    return times, vals


def _most_recent_with_data(metric: str, key: str):
    """Return (date_str, json) for the newest day whose `key` array is non-empty."""
    folder = JSON_DIR / metric
    if not folder.exists():
        return None, None
    for path in sorted(folder.glob("*.json"), reverse=True):
        data = json.loads(path.read_text())
        if isinstance(data, dict) and data.get(key):
            return path.stem, data
    return None, None


def intraday() -> Path | None:
    """Minute-level HR, stress, and body battery for the most recent day."""
    date_str, hr = _most_recent_with_data("heart_rates", "heartRateValues")
    if hr is None:
        print("intraday: no intraday heart-rate data yet")
        return None

    # GMT->local offset from this day's timestamps (ISO strings or epoch ms).
    local = pd.to_datetime(hr["startTimestampLocal"])
    gmt = pd.to_datetime(hr["startTimestampGMT"])
    offset_ms = int((local - gmt).total_seconds() * 1000)

    stress_path = JSON_DIR / "stress" / f"{date_str}.json"
    stress = json.loads(stress_path.read_text()) if stress_path.exists() else {}

    fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True)
    fig.suptitle(f"Intraday detail — {date_str}", fontsize=16, fontweight="bold")

    # Heart rate
    t, v = _to_local_series(hr.get("heartRateValues"), value_index=1, offset_ms=offset_ms)
    axes[0].plot(t, v, color="#d62728")
    axes[0].set_ylabel("Heart rate (bpm)")
    axes[0].set_title("Heart rate")

    # Stress (skip -1 = no reading)
    t, v = _to_local_series(stress.get("stressValuesArray"), value_index=1,
                            offset_ms=offset_ms, skip=-1)
    axes[1].plot(t, v, color="#ff7f0e")
    axes[1].set_ylabel("Stress (0-100)")
    axes[1].set_ylim(0, 100)
    axes[1].set_title("Stress")

    # Body battery: rows are [ts, status, value, version]
    t, v = _to_local_series(stress.get("bodyBatteryValuesArray"), value_index=2,
                            offset_ms=offset_ms)
    axes[2].plot(t, v, color="#2ca02c")
    axes[2].set_ylabel("Body battery")
    axes[2].set_ylim(0, 100)
    axes[2].set_title("Body battery")

    # Label the time axis on every panel (sharex hides upper ticks by default).
    for ax in axes:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax.tick_params(axis="x", labelbottom=True)
    axes[2].set_xlabel("Time of day (local)")

    GRAPH_DIR.mkdir(parents=True, exist_ok=True)
    out = GRAPH_DIR / "intraday.png"
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(out, dpi=120)
    plt.close(fig)
    return out


def main() -> None:
    made = []
    for fn in (daily_trends, intraday):
        path = fn()
        if path is not None:
            made.append(path)
            print(f"wrote {path}")
    if not made:
        print("No graphs generated — pull some data first with garmin_pull.py")


if __name__ == "__main__":
    main()
