"""Biostream — post-prandial (N-of-1) experiment view for Kevin.

Streamlit on Cloud Run, private. Answers one question per meal: what did
this food do to glucose, and — when paired against a similar meal with or
without post-meal exercise — did the exercise change the response?

HRV coverage in this pipeline is currently limited to roughly 5am-3pm daily
(Garmin's overnight/wake-window algorithm), not the evening post-meal period
most experiments care about — so vagal-tone/parasympathetic statistics are
intentionally not shown yet; the CGM statistics below are what the data can
actually support today.
"""

import json

import pandas as pd
import streamlit as st

import charts
import data
import experiment

st.set_page_config(page_title="Biostream — Post-Prandial", page_icon="🩸",
                   layout="wide", initial_sidebar_state="collapsed")

DEFAULT_POST_MEAL_HOURS = 15
BASELINE_WINDOW_MIN = 30


def _meal_items(items_json) -> list[dict]:
    if isinstance(items_json, str):
        try:
            return json.loads(items_json)
        except ValueError:
            return []
    return items_json or []


def _meal_label(row) -> str:
    items = _meal_items(row["items"])
    foods = ", ".join(i.get("food", "?") for i in items[:2])
    if len(items) > 2:
        foods += f" + {len(items) - 2} more"
    kcal = f"{row['calories']:.0f} kcal" if pd.notna(row["calories"]) else "? kcal"
    when = row["capture_ts"].strftime("%b %d, %-I:%M %p")
    return f"{when} — {foods or 'meal'} ({kcal})"


def _meal_card(row):
    img_col, macro_col = st.columns([1, 2])
    with img_col:
        img = data.load_meal_image_bytes(row["gcs_uri"])
        if img:
            st.image(img, use_container_width=True)
        else:
            st.caption("No photo for this meal.")
    with macro_col:
        st.caption(row["capture_ts"].strftime("%A, %b %d — %-I:%M %p"))
        items = _meal_items(row["items"])
        if items:
            st.markdown("\n".join(f"- {i.get('food', '?')} ({i.get('grams', '?')} g)"
                                  for i in items))
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Calories", f"{row['calories']:.0f}" if pd.notna(row["calories"]) else "—")
        m2.metric("Carbs (g)", f"{row['carbs_g']:.0f}" if pd.notna(row["carbs_g"]) else "—")
        m3.metric("Protein (g)", f"{row['protein_g']:.0f}" if pd.notna(row["protein_g"]) else "—")
        m4.metric("Fat (g)", f"{row['fat_g']:.0f}" if pd.notna(row["fat_g"]) else "—")


def _load_meal_window(meal_ts, hours_after: int):
    start = meal_ts - pd.Timedelta(minutes=BASELINE_WINDOW_MIN)
    end = meal_ts + pd.Timedelta(hours=hours_after)
    return {
        "glucose": data.load_glucose_window(start, end),
        "activities": data.load_activities_window(start, end),
        "bp": data.load_bp_window(meal_ts, meal_ts + pd.Timedelta(hours=36)),
    }


def _activity_section(meal_ts):
    """Show the Garmin activity (if any) that started 30-60 min after the
    meal — e.g. a post-meal walk — with its own metrics row."""
    st.subheader("🏃 Post-Meal Activity")
    paired = data.load_activities_window(meal_ts + pd.Timedelta(minutes=30),
                                         meal_ts + pd.Timedelta(hours=1))
    if paired.empty:
        st.caption("No activity logged 30–60 min after this meal.")
        return

    a = paired.iloc[0]
    mins_after = round((a["start_ts"] - meal_ts).total_seconds() / 60)
    st.caption(f"**{a['activity_name']}** — started {mins_after} min after this meal")

    cols = st.columns(4)
    cols[0].metric("Type", a["activity_type"].title() if pd.notna(a["activity_type"]) else "—")
    cols[1].metric("Duration", f"{a['duration_seconds'] / 60:.0f} min"
                   if pd.notna(a["duration_seconds"]) else "—")
    cols[2].metric("Calories", f"{a['calories']:.0f}" if pd.notna(a["calories"]) else "—")
    cols[3].metric("Avg HR", f"{a['avg_hr']:.0f} bpm" if pd.notna(a["avg_hr"]) else "—",
                   help=f"Max: {a['max_hr']:.0f} bpm" if pd.notna(a["max_hr"]) else None)
    if pd.notna(a["distance_m"]):
        st.caption(f"Distance: {a['distance_m'] / 1609.34:.2f} mi")


def _stat_metrics(stats: dict):
    cols = st.columns(4)
    cols[0].metric("Baseline", f"{stats['baseline_mg_dl']:.0f} mg/dL"
                   if stats["baseline_mg_dl"] is not None else "—")
    cols[1].metric("Peak", f"{stats['peak_mg_dl']:.0f} mg/dL"
                   if stats["peak_mg_dl"] is not None else "—",
                   help="Time to peak: "
                        f"{stats['time_to_peak_min']:.0f} min" if stats["time_to_peak_min"] is not None else None)
    cols[2].metric("AUC", f"{stats['auc_mg_dl_min']:,.0f}"
                   if stats["auc_mg_dl_min"] is not None else "—",
                   help="Incremental area under the curve above baseline (mg/dL·min)")
    cols[3].metric("Back to baseline", f"{stats['time_to_baseline_min']:.0f} min"
                   if stats["time_to_baseline_min"] is not None else "never (in window)")


st.title("Kevin — Post-Prandial Experiment")
st.caption(
    "N-of-1 CGM analysis: how meals move glucose, and whether post-meal "
    "exercise changes the response. Fed live from the Biostream pipeline "
    "(Garmin, FreeStyle Libre CGM, Omron BP)."
)

view = st.segmented_control("View", options=["Single Meal", "Paired Meal Experiment"],
                            default="Single Meal", label_visibility="collapsed") or "Single Meal"

meals = data.load_meals()
if meals.empty:
    st.info("No meals logged yet.")
    st.stop()
meals = meals.assign(_label=meals.apply(_meal_label, axis=1))

if view == "Single Meal":
    selected_label = st.selectbox("Meal", meals["_label"])
    meal = meals[meals["_label"] == selected_label].iloc[0]
    meal_ts = meal["capture_ts"]

    _meal_card(meal)
    _activity_section(meal_ts)
    st.divider()

    hours_after = st.slider("Hours to track after the meal", 4, 20, DEFAULT_POST_MEAL_HOURS)
    window = _load_meal_window(meal_ts, hours_after)

    if window["glucose"].empty:
        st.warning("No CGM readings in this window — nothing to analyze for this meal.")
        st.stop()

    stats = experiment.cgm_meal_stats(window["glucose"], meal_ts,
                                      BASELINE_WINDOW_MIN, hours_after)
    _stat_metrics(stats)

    fig = charts.meal_timeline_fig(window["glucose"], window["activities"], window["bp"],
                                   meal_ts, stats["baseline_mg_dl"])
    st.plotly_chart(fig, use_container_width=True)

    if window["activities"].empty:
        st.caption("No logged exercise in this window.")
    if window["bp"].empty:
        st.caption("No blood-pressure reading in the following ~36 hours.")

else:  # Paired Meal Experiment
    st.caption(
        "Pick any two meals to compare — e.g. the same dinner with and without "
        "a post-meal walk. Overlay is on 'minutes since meal' so the two "
        "excursions line up regardless of when each meal happened."
    )
    col_a, col_b = st.columns(2)
    with col_a:
        label_a = st.selectbox("Meal A", meals["_label"], index=min(1, len(meals) - 1))
    with col_b:
        label_b = st.selectbox("Meal B", meals["_label"], index=0)

    meal_a = meals[meals["_label"] == label_a].iloc[0]
    meal_b = meals[meals["_label"] == label_b].iloc[0]

    if meal_a["meal_id"] == meal_b["meal_id"]:
        st.info("Pick two different meals to compare.")
        st.stop()

    hours_after = st.slider("Hours to track after each meal", 4, 20, DEFAULT_POST_MEAL_HOURS)

    card_a, card_b = st.columns(2)
    with card_a:
        _meal_card(meal_a)
    with card_b:
        _meal_card(meal_b)
    st.divider()

    win_a = data.load_glucose_window(meal_a["capture_ts"] - pd.Timedelta(minutes=BASELINE_WINDOW_MIN),
                                     meal_a["capture_ts"] + pd.Timedelta(hours=hours_after))
    win_b = data.load_glucose_window(meal_b["capture_ts"] - pd.Timedelta(minutes=BASELINE_WINDOW_MIN),
                                     meal_b["capture_ts"] + pd.Timedelta(hours=hours_after))

    if win_a.empty or win_b.empty:
        st.warning("One or both meals have no CGM readings in this window.")
        st.stop()

    stats_a = experiment.cgm_meal_stats(win_a, meal_a["capture_ts"], BASELINE_WINDOW_MIN, hours_after)
    stats_b = experiment.cgm_meal_stats(win_b, meal_b["capture_ts"], BASELINE_WINDOW_MIN, hours_after)

    post_a = experiment.post_meal_window(win_a, meal_a["capture_ts"], hours_after)
    post_b = experiment.post_meal_window(win_b, meal_b["capture_ts"], hours_after)
    fig = charts.paired_cgm_overlay_fig(post_a, post_b, "Meal A", "Meal B")
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Statistics")
    st.caption("HRV/vagal-tone statistics aren't shown — this pipeline's HRV "
               "capture window doesn't reliably cover the evening post-meal "
               "period yet (see module docstring).")
    st.dataframe(experiment.compare_meal_stats(stats_a, stats_b, "Meal A", "Meal B"),
                use_container_width=True, hide_index=True)

st.divider()
st.caption(
    "**How it works** — Cloud Scheduler triggers Python Cloud Functions that "
    "poll Garmin Connect (wellness + activities), LibreLinkUp CGM, "
    "and Omron Connect into partitioned BigQuery tables. This page computes "
    "CGM statistics (incremental AUC, peak, time-to-peak, return-to-baseline, "
    "rise velocity/acceleration) live from that data, cached 30 minutes."
)
