"""Biostream — post-prandial (N-of-1) experiment view for Kevin.

Streamlit on Cloud Run, private. Answers one question per meal: what did
this food do to glucose, and — when paired against a similar meal with or
without post-meal exercise — did the exercise change the response?

HRV coverage in this pipeline is currently limited to roughly 5am-3pm daily
(Garmin's overnight/wake-window algorithm), not the evening post-meal period
most experiments care about — so vagal-tone/parasympathetic statistics are
intentionally not shown yet; the CGM statistics below are what the data can
actually support today.

HOW TO READ THIS FILE — Streamlit works differently from Flask (meal_web):
there are no routes. The whole script re-runs top to bottom every time the
user changes any widget (picks a meal, moves a slider), and whatever the
script st.*-writes ends up on the page in order. So read this file like a
page rendering from top to bottom.

The module split:
    data.py       — every BigQuery/GCS read (all cached)
    transforms.py — small dataframe reshaping helpers
    experiment.py — the CGM statistics math (AUC, peak, velocity, ...)
    charts.py     — the Plotly figures
    app.py        — (this file) layout and glue only; no math, no queries
"""

import json
from pathlib import Path

import pandas as pd
import streamlit as st
from PIL import Image

import charts
import data
import experiment

# Path(__file__).parent = the directory this file lives in, so the logo is
# found no matter where the app is launched from.
LOGO_PATH = Path(__file__).parent / "assets" / "moai_logo.png"

# Must be the first Streamlit call in the script (Streamlit's rule).
st.set_page_config(page_title="Biostream — Post-Prandial", page_icon=Image.open(LOGO_PATH),
                   layout="wide", initial_sidebar_state="collapsed")

# Small CSS override — Streamlit has no theme knob for these two colors.
st.markdown("""
<style>
h3 { color: #35126A; }
[data-testid="stMetricValue"] { color: #151022; }
</style>
""", unsafe_allow_html=True)

DEFAULT_POST_MEAL_HOURS = 15   # default slider value in the paired view
BASELINE_WINDOW_MIN = 30       # minutes before the meal used as "baseline" glucose


def _meal_items(items_json) -> list[dict]:
    """The meals table stores the per-food breakdown as a JSON string
    ('[{"food": "eggs", ...}]'); decode it to a list, tolerating bad/empty."""
    if isinstance(items_json, str):
        try:
            return json.loads(items_json)
        except ValueError:
            return []
    return items_json or []


def _meal_label(row) -> str:
    """One-line description used in the meal dropdowns,
    e.g. 'Jul 27, 6:12 PM — eggs, toast + 1 more (450 kcal)'."""
    items = _meal_items(row["items"])
    foods = ", ".join(i.get("food", "?") for i in items[:2])   # first two foods only
    if len(items) > 2:
        foods += f" + {len(items) - 2} more"
    # pd.notna guards NULLs from BigQuery — formatting NaN would show "nan".
    kcal = f"{row['calories']:.0f} kcal" if pd.notna(row["calories"]) else "? kcal"
    when = row["capture_ts"].strftime("%b %d, %-I:%M %p")
    return f"{when} — {foods or 'meal'} ({kcal})"


def _meal_card(row):
    """The bordered card at the top: photo on the left, foods + macros right."""
    with st.container(border=True):
        img_col, macro_col = st.columns([1, 2])   # 1:2 width ratio
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


def _load_meal_window(meal_ts, pre_meal_min: int, post_meal_hours: int):
    """Single Meal view: window around the meal, user-adjustable via the
    sliders below the meal picker — a tight pre-meal span anchors the chart
    on the spike; widen it to see pre-meal context."""
    start = meal_ts - pd.Timedelta(minutes=pre_meal_min)
    end = meal_ts + pd.Timedelta(hours=post_meal_hours)
    return {
        "glucose": data.load_glucose_window(start, end),
        # Activities always look 2h back (regardless of the glucose slider) so
        # a pre-meal walk keeps its band on the chart and stays consistent
        # with the ±2h Post-Meal Activity card above.
        "activities": data.load_activities_window(meal_ts - pd.Timedelta(hours=2), end),
        # BP is sparse (a reading or two a day), so look much further ahead.
        "bp": data.load_bp_window(meal_ts, meal_ts + pd.Timedelta(hours=36)),
    }


def _activity_section(meal_ts):
    """Show the Garmin activity (if any) that started within +/- 2 hours of
    the meal — e.g. a post-meal walk — with its own metrics row."""
    with st.container(border=True):
        st.subheader("🏃 Post-Meal Activity")
        paired = data.load_activities_window(meal_ts - pd.Timedelta(hours=2), meal_ts + pd.Timedelta(hours=2))
        if paired.empty:
            st.caption("No activity logged in the 2 hours before and after this meal.")
            return

        a = paired.iloc[0]   # .iloc[0] = first row; earliest activity in the window
        mins_after = round((a["start_ts"] - meal_ts).total_seconds() / 60)
        min_before = round((meal_ts - a["start_ts"]).total_seconds() / 60)
        if min_before > 0: # means that the activity happened before the meal
            st.caption(f"**{a['activity_name']}** — started {min_before} min before this meal")
        else:
            st.caption(f"**{a['activity_name']}** — started {mins_after} min after this meal")

        cols = st.columns(4)
        cols[0].metric("Type", a["activity_type"].title() if pd.notna(a["activity_type"]) else "—")
        cols[1].metric("Duration", f"{a['duration_seconds'] / 60:.0f} min"
                       if pd.notna(a["duration_seconds"]) else "—")
        cols[2].metric("Calories", f"{a['calories']:.0f}" if pd.notna(a["calories"]) else "—")
        cols[3].metric("Avg HR", f"{a['avg_hr']:.0f} bpm" if pd.notna(a["avg_hr"]) else "—",
                       help=f"Max: {a['max_hr']:.0f} bpm" if pd.notna(a["max_hr"]) else None)
        if pd.notna(a["distance_m"]):
            st.caption(f"Distance: {a['distance_m'] / 1609.34:.2f} mi")   # meters -> miles


def _overnight_hrv_section(meal_ts):
    """HRV (paired with glucose) for the night following the meal — Garmin
    attributes a night's sleep to the following morning's calendar date."""
    with st.container(border=True):
        st.subheader("😴 Overnight HRV")
        # normalize() zeroes the time-of-day; +1 day = the morning after.
        sleep_date = (meal_ts.normalize() + pd.Timedelta(days=1)).date()
        hrv = data.load_hrv_for_sleep_date(sleep_date.isoformat())
        if hrv.empty:
            st.caption(f"No HRV data recorded for the night of {sleep_date.strftime('%b %d')}.")
            return

        # Fetch glucose covering the same span as the HRV readings, so the
        # two lines share one x-axis.
        glucose = data.load_glucose_window(hrv["ts"].min(), hrv["ts"].max())
        fig = charts.overnight_hrv_glucose_fig(hrv, glucose)
        st.plotly_chart(fig, use_container_width=True)


# ---- Page starts rendering here (top of the visible page) ------------------
logo_col, title_col = st.columns([1, 11], vertical_alignment="center")
with logo_col:
    st.image(str(LOGO_PATH), width=64)
with title_col:
    st.title("Kevin — Post-Prandial Experiment")
st.caption(
    "N-of-1 CGM analysis: how meals move glucose, and whether post-meal "
    "exercise changes the response. Fed live from the Biostream pipeline "
    "(Garmin, FreeStyle Libre CGM, Omron BP)."
)

# The view toggle. segmented_control returns None until first clicked,
# hence the `or "Single Meal"` fallback.
view = st.segmented_control("View", options=["Single Meal", "Paired Meal Experiment"],
                            default="Single Meal", label_visibility="collapsed") or "Single Meal"

meals = data.load_meals()
if meals.empty:
    st.info("No meals logged yet.")
    st.stop()   # halts the script — nothing below renders
# Add a _label column (the dropdown text) by applying _meal_label to each row.
meals = meals.assign(_label=meals.apply(_meal_label, axis=1))

if view == "Single Meal":
    selected_label = st.selectbox("Meal", meals["_label"])
    # Find the row whose label was picked (labels are unique per meal).
    meal = meals[meals["_label"] == selected_label].iloc[0]
    meal_ts = meal["capture_ts"]

    _meal_card(meal)
    _activity_section(meal_ts)

    # Chart-window knobs. Sliders return their current value on every rerun
    # (args: label, min, max, default); cache_data means each setting is
    # queried from BigQuery once, then served from cache.
    pre_col, post_col = st.columns(2)
    with pre_col:
        pre_meal_min = st.slider("Minutes shown before meal", 10, 120, 10, step=10)
    with post_col:
        post_meal_hours = st.slider("Hours shown after meal", 2, 8, 4)

    window = _load_meal_window(meal_ts, pre_meal_min, post_meal_hours)

    if window["glucose"].empty:
        st.warning("No CGM readings in this window — nothing to analyze for this meal.")
        st.stop()

    with st.container(border=True):
        st.subheader("🩸 Glucose Response")
        fig = charts.meal_timeline_fig(window["glucose"], window["activities"], window["bp"],
                                       meal_ts, baseline=None)
        st.plotly_chart(fig, use_container_width=True)

        if window["bp"].empty:
            st.caption("No blood-pressure reading in the following ~36 hours.")

    _overnight_hrv_section(meal_ts)

else:  # Paired Meal Experiment
    st.caption(
        "Pick any two meals to compare — e.g. the same dinner with and without "
        "a post-meal walk. Overlay is on 'minutes since meal' so the two "
        "excursions line up regardless of when each meal happened."
    )
    col_a, col_b = st.columns(2)
    with col_a:
        # Default A to the second-newest meal (index 1) so A and B start
        # different; min() guards the case of having only one meal.
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

    # Each meal's glucose window: 30 min before (to establish baseline)
    # through `hours_after` after.
    win_a = data.load_glucose_window(meal_a["capture_ts"] - pd.Timedelta(minutes=BASELINE_WINDOW_MIN),
                                     meal_a["capture_ts"] + pd.Timedelta(hours=hours_after))
    win_b = data.load_glucose_window(meal_b["capture_ts"] - pd.Timedelta(minutes=BASELINE_WINDOW_MIN),
                                     meal_b["capture_ts"] + pd.Timedelta(hours=hours_after))

    if win_a.empty or win_b.empty:
        st.warning("One or both meals have no CGM readings in this window.")
        st.stop()

    # The actual science: per-meal CGM statistics, computed in experiment.py.
    stats_a = experiment.cgm_meal_stats(win_a, meal_a["capture_ts"], BASELINE_WINDOW_MIN, hours_after)
    stats_b = experiment.cgm_meal_stats(win_b, meal_b["capture_ts"], BASELINE_WINDOW_MIN, hours_after)

    # Same data re-indexed to "minutes since meal" for the overlay chart.
    post_a = experiment.post_meal_window(win_a, meal_a["capture_ts"], hours_after)
    post_b = experiment.post_meal_window(win_b, meal_b["capture_ts"], hours_after)

    with st.container(border=True):
        st.subheader("🩸 Glucose Overlay")
        fig = charts.paired_cgm_overlay_fig(post_a, post_b, "Meal A", "Meal B")
        st.plotly_chart(fig, use_container_width=True)

    with st.container(border=True):
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
