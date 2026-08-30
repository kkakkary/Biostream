"""Biostream — post-prandial (N-of-1) experiment view.

Streamlit on Cloud Run, private. Answers one question per meal: what did
this food do to glucose, and — when paired against a similar meal with or
without post-meal exercise — did the exercise change the response?

One subject at a time, chosen with the picker in the page header; the id is
threaded down into every data.load_* call rather than read from a global, so
Streamlit's per-argument cache keeps the subjects' data separate.

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

Two escape hatches from that full rerun, both used below:
  @st.fragment  — a function whose widgets re-run only IT, not the page
                  (see _paired_experiment); use it when a widget's effect is
                  real but local.
  client-side   — a control Plotly handles in the browser, so there's no
                  rerun at all (the Single Meal chart's zoom buttons); use it
                  when a control changes only what's DRAWN, not what's
                  computed.

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

import auth
import charts
import data
import experiment

# Path(__file__).parent = the directory this file lives in, so the logo is
# found no matter where the app is launched from.
LOGO_PATH = Path(__file__).parent / "assets" / "moai_logo.png"

# Must be the first Streamlit call in the script (Streamlit's rule).
st.set_page_config(page_title="Biostream — Post-Prandial", page_icon=Image.open(LOGO_PATH),
                   layout="wide", initial_sidebar_state="collapsed")

# Small CSS override, layered on top of .streamlit/config.toml's theme keys
# (which handle font families, base colors, radius, and borders — see that
# file's comment for the type system this draws from). What's left here is
# what the theme API has no knob for:
#   - metric values in the monospace face, so glucose/latency/p-value
#     readouts look like instrument output, not prose
#   - a thin signal-colored cap on every metric tile, turning each into a
#     small readout module instead of a bare number
#   - dividers in the brand violet instead of Streamlit's default grey line
st.markdown("""
<style>
h3 { color: #35126A; }
[data-testid="stMetricValue"] {
    color: #151022;
    font-family: "IBM Plex Mono", monospace;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.01em;
}
[data-testid="stMetricLabel"] {
    letter-spacing: 0.02em;
}
[data-testid="stMetric"] {
    border-top: 3px solid #6b3fc9;
    padding-top: 0.6rem;
}
hr {
    background: linear-gradient(90deg, #35126A, transparent);
    height: 2px;
    border: none;
}
</style>
""", unsafe_allow_html=True)

DEFAULT_POST_MEAL_HOURS = 15   # default slider value in the paired view
BASELINE_WINDOW_MIN = 30       # minutes before the meal used as "baseline" glucose

# Single Meal fetches ONE generous window per meal and lets the reader zoom
# inside it in the browser (see charts.meal_timeline_fig). Generous on purpose:
# every zoom preset has to be already-fetched data, or zooming would mean a
# rerun and a query. Statistics are computed over this full window too, so they
# don't shift as the reader zooms.
SINGLE_MEAL_PRE_MIN = 120      # minutes of pre-meal context fetched
SINGLE_MEAL_POST_HOURS = 8     # hours of post-meal response fetched

# Shared Plotly toolbar settings for every chart on the page.
#   displaylogo            — off: it's an outbound link to plotly.com, and this
#                            is a private health page that shouldn't advertise.
#   modeBarButtonsToRemove — the selection tools do nothing useful on time
#                            series (there's nothing downstream of a selection).
#   scrollZoom             — on: wheel-zoom the time axis, no toolbar needed.
PLOTLY_CONFIG = {
    "displaylogo": False,
    "modeBarButtonsToRemove": ["lasso2d", "select2d", "autoScale2d"],
    "scrollZoom": True,
}


def _meal_items(items_json) -> list[dict]:
    """The meals table stores the per-food breakdown as a JSON string
    ('[{"food": "eggs", ...}]'); decode it to a list, tolerating bad/empty."""
    if isinstance(items_json, str):
        try:
            return json.loads(items_json)
        except ValueError:
            return []
    return items_json or []


def _fmt_time_12h(ts) -> str:
    """'6:12 PM' — 12-hour time with no leading zero on the hour.
    Cross-platform stand-in for strftime's %-I: Windows' C runtime doesn't
    support the '-' no-pad flag (glibc/BSD only), so %-I works when deployed
    to Cloud Run (Linux) but raises ValueError when run locally on Windows."""
    return f"{int(ts.strftime('%I'))}:{ts.strftime('%M %p')}"


def _meal_label(row) -> str:
    """One-line description used in the meal dropdowns,
    e.g. 'Jul 27, 6:12 PM — eggs, toast + 1 more (450 kcal)'."""
    items = _meal_items(row["items"])
    foods = ", ".join(i.get("food", "?") for i in items[:2])   # first two foods only
    if len(items) > 2:
        foods += f" + {len(items) - 2} more"
    # pd.notna guards NULLs from BigQuery — formatting NaN would show "nan".
    kcal = f"{row['calories']:.0f} kcal" if pd.notna(row["calories"]) else "? kcal"
    when = f"{row['capture_ts'].strftime('%b %d')}, {_fmt_time_12h(row['capture_ts'])}"
    return f"{when} — {foods or 'meal'} ({kcal})"


def _stat(value, fmt: str = "{:.0f}") -> str:
    """Format one experiment.cgm_meal_stats value for st.metric, turning a
    missing one into an em dash. Every field there can legitimately be None —
    no pre-meal readings to average, glucose that never returned to baseline —
    and a literal "None" on screen reads like a bug rather than a finding."""
    return "—" if pd.isna(value) else fmt.format(value)   # pd.isna(None) is True


def _meal_card(row):
    """The bordered card at the top: photo on the left, foods + macros right."""
    with st.container(border=True):
        img_col, macro_col = st.columns([1, 2])   # 1:2 width ratio
        with img_col:
            img = data.load_meal_image_bytes(row["gcs_uri"])
            if img:
                st.image(img, width="stretch")
            else:
                st.caption("No photo for this meal.")
        with macro_col:
            st.caption(f"{row['capture_ts'].strftime('%A, %b %d —')} {_fmt_time_12h(row['capture_ts'])}")
            items = _meal_items(row["items"])
            if items:
                st.markdown("\n".join(f"- {i.get('food', '?')} ({i.get('grams', '?')} g)"
                                      for i in items))
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Calories", f"{row['calories']:.0f}" if pd.notna(row["calories"]) else "—")
            m2.metric("Carbs (g)", f"{row['carbs_g']:.0f}" if pd.notna(row["carbs_g"]) else "—")
            m3.metric("Protein (g)", f"{row['protein_g']:.0f}" if pd.notna(row["protein_g"]) else "—")
            m4.metric("Fat (g)", f"{row['fat_g']:.0f}" if pd.notna(row["fat_g"]) else "—")


def _load_meal_window(user_id: str, meal_ts,
                      pre_meal_min: int = SINGLE_MEAL_PRE_MIN,
                      post_meal_hours: int = SINGLE_MEAL_POST_HOURS):
    """Single Meal view: the one fixed window fetched around the meal. Fixed,
    not user-adjustable — the chart's zoom buttons narrow the VIEW inside this
    window without refetching, so the same cached frame serves every zoom."""
    start = meal_ts - pd.Timedelta(minutes=pre_meal_min)
    end = meal_ts + pd.Timedelta(hours=post_meal_hours)
    protocol = pd.Timedelta(minutes=experiment.POST_MEAL_EXERCISE_MAX_MIN)
    return {
        "glucose": data.load_glucose_window(user_id, start, end),
        # Activity bands are bounded by the protocol window on BOTH sides, not
        # by the glucose window: forward, because a workout hours later isn't
        # this meal's exercise (see experiment.POST_MEAL_EXERCISE_MAX_MIN) and
        # shouldn't be shaded as if it were; backward, so a pre-meal walk still
        # shows as context. Same span as the Post-Meal Activity card above.
        "activities": data.load_activities_window(user_id, meal_ts - protocol,
                                                  meal_ts + protocol),
        # BP is sparse (a reading or two a day), so look much further ahead.
        "bp": data.load_bp_window(user_id, meal_ts, meal_ts + pd.Timedelta(hours=36)),
    }


def _activity_section(user_id: str, meal_ts):
    """Show the Garmin activity (if any) that started within the protocol
    window either side of the meal — e.g. a post-meal walk — with its own
    metrics row. An activity AFTER the meal is what makes this an exercise
    arm (see experiment.POST_MEAL_EXERCISE_MAX_MIN); one before is context."""
    with st.container(border=True):
        st.subheader("🏃 Post-Meal Activity")
        protocol = pd.Timedelta(minutes=experiment.POST_MEAL_EXERCISE_MAX_MIN)
        paired = data.load_activities_window(user_id, meal_ts - protocol, meal_ts + protocol)
        if paired.empty:
            st.caption(f"No activity logged within {experiment.POST_MEAL_EXERCISE_MAX_MIN} "
                       "minutes before or after this meal.")
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


def _overnight_hrv_section(user_id: str, meal_ts):
    """HRV (paired with glucose) for the night following the meal — Garmin
    attributes a night's sleep to the following morning's calendar date."""
    with st.container(border=True):
        st.subheader("😴 Overnight HRV")
        # normalize() zeroes the time-of-day; +1 day = the morning after.
        sleep_date = (meal_ts.normalize() + pd.Timedelta(days=1)).date()
        hrv = data.load_hrv_for_sleep_date(user_id, sleep_date.isoformat())
        if hrv.empty:
            st.caption(f"No HRV data recorded for the night of {sleep_date.strftime('%b %d')}.")
            return

        # Fetch glucose covering the same span as the HRV readings, so the
        # two lines share one x-axis.
        glucose = data.load_glucose_window(user_id, hrv["ts"].min(), hrv["ts"].max())
        fig = charts.overnight_hrv_glucose_fig(hrv, glucose)
        st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG)


def _intensity_sleep_view(user_id: str):
    """Intensity × Sleep view: split days by intensity minutes, compare the
    following nights' deep-sleep latency distributions, Welch's t-test.

    The pairing direction matters and is handled in data.load_intensity_sleep:
    Garmin books a night's sleep under the NEXT morning's date, so the night
    that follows activity day D is row D+1 — the join, not this function,
    encodes that.
    """
    st.caption(
        "Does an active day get you into deep sleep faster? Each night is "
        "paired with the day **before** it and grouped by that day's Garmin "
        "intensity minutes (moderate + 2×vigorous) against the threshold "
        "below. Latency is minutes from sleep onset to the first deep-sleep "
        "stage."
    )
    df = data.load_intensity_sleep(user_id)
    if df.empty:
        st.info("No paired day/night data yet for this subject — this view needs "
                "days with intensity minutes followed by nights with sleep-stage "
                "data (backfilled or synced after the stage pipeline was added).")
        return

    threshold = st.slider("Intensity threshold (minutes)", min_value=0, max_value=120,
                          value=experiment.INTENSITY_THRESHOLD_MIN, step=5,
                          help="A night counts as following an active day when that "
                               "day's Garmin intensity minutes exceed this value; "
                               "otherwise it's an inactive-day night.")
    st.caption(f"> {threshold} min = active day, ≤ {threshold} = inactive day.")

    active, rest = experiment.split_latency_by_intensity(df, threshold)
    result = experiment.welch_t_test(active, rest)

    o1, o2, o3 = st.columns(3)
    o1.metric("Total nights", len(df), border=True,
              help="Paired day/night rows this subject has: a day with intensity "
                   "minutes followed by a night with deep-sleep-latency data.")
    o2.metric("Active nights", result["n_a"], border=True,
              help=f"Nights following a day with > {threshold} intensity minutes.")
    o3.metric("Inactive nights", result["n_b"], border=True,
              help=f"Nights following a day with ≤ {threshold} intensity minutes.")

    with st.container(border=True):
        st.subheader("😴 Time to Deep Sleep — Distributions")
        fig = charts.latency_distribution_fig(active, rest, threshold)
        st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG)

    with st.container(border=True):
        st.subheader("Welch's t-test")
        # Counts already shown in the overview row above — this grid is just
        # the test's own numbers, so 4-across fits without clipping.
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Active mean (min)", _stat(result["mean_a"], "{:.1f}"), border=True,
                  help=None if result["sd_a"] is None else f"SD: {result['sd_a']:.1f} min")
        c2.metric("Inactive mean (min)", _stat(result["mean_b"], "{:.1f}"), border=True,
                  help=None if result["sd_b"] is None else f"SD: {result['sd_b']:.1f} min")
        c3.metric("t statistic", _stat(result["t_stat"], "{:.2f}"), border=True,
                  help="Welch's two-sample t (unequal variances). Negative = "
                       "active nights reached deep sleep faster on average.")
        c4.metric("p-value", _stat(result["p_value"], "{:.3f}"), border=True,
                  help="Probability of a mean difference at least this large "
                       "if activity made no difference at all.")

        if result["p_value"] is None:
            st.caption(f"Not enough nights on both sides to run the test (needs at "
                       f"least 2 each; have {result['n_a']} active, {result['n_b']} inactive).")
        else:
            delta = result["mean_a"] - result["mean_b"]
            direction = "faster" if delta < 0 else "slower"
            verdict = ("statistically significant" if result["p_value"] < 0.05
                       else "not statistically significant")
            st.caption(f"Nights after active days reached deep sleep "
                       f"{abs(delta):.1f} min {direction} on average — "
                       f"**{verdict}** at α = 0.05 (p = {result['p_value']:.3f}).")
        st.caption("Caveats: observational, not randomized — active days may "
                   "differ in more than exercise (caffeine, stress, timing). "
                   "Nights with no sleep-stage data, and nights that never "
                   "reached deep sleep, are excluded.")


@st.fragment
def _paired_experiment(user_id: str, meal_a, meal_b, acts_a, acts_b):
    """Paired Meal Experiment: the slider and everything it drives.

    WHY THIS IS A FRAGMENT: `hours_after` genuinely changes the math (both
    windows, both stat sets, the overlay), so it has to stay a real widget and
    a real rerun. @st.fragment narrows the blast radius of that rerun to this
    function — moving the slider redraws the chart and table, and leaves the
    header, the auth gate, the meal pickers and the two meal cards alone.

    Everything the fragment shows is created INSIDE it. A fragment may only
    write into containers made outside it if they were written during the
    initial full run; keeping every element self-created sidesteps that rule
    entirely. The meal pickers deliberately stay outside — changing a meal
    reruns the whole page, which is correct (the cards must change too) and
    rare compared to dragging the slider.

    acts_a/acts_b arrive as arguments rather than being loaded here: which
    activities belong to a meal is fixed by the protocol window, so the slider
    can't change them and re-deriving them on every drag would be waste.
    """
    hours_after = st.slider("Hours to track after each meal", 4, 20, DEFAULT_POST_MEAL_HOURS)

    # Each meal's glucose window: 30 min before (to establish baseline)
    # through `hours_after` after.
    win_a = data.load_glucose_window(user_id,
                                     meal_a["capture_ts"] - pd.Timedelta(minutes=BASELINE_WINDOW_MIN),
                                     meal_a["capture_ts"] + pd.Timedelta(hours=hours_after))
    win_b = data.load_glucose_window(user_id,
                                     meal_b["capture_ts"] - pd.Timedelta(minutes=BASELINE_WINDOW_MIN),
                                     meal_b["capture_ts"] + pd.Timedelta(hours=hours_after))

    if win_a.empty or win_b.empty:
        st.warning("One or both meals have no CGM readings in this window.")
        # `return`, not st.stop(): inside a fragment st.stop() would halt the
        # fragment anyway, but returning makes that intent explicit.
        return

    # The actual science: per-meal CGM statistics, computed in experiment.py.
    stats_a = experiment.cgm_meal_stats(win_a, meal_a["capture_ts"], BASELINE_WINDOW_MIN, hours_after)
    stats_b = experiment.cgm_meal_stats(win_b, meal_b["capture_ts"], BASELINE_WINDOW_MIN, hours_after)

    # Same data re-indexed to "minutes since meal" for the overlay chart.
    post_a = experiment.post_meal_window(win_a, meal_a["capture_ts"], hours_after)
    post_b = experiment.post_meal_window(win_b, meal_b["capture_ts"], hours_after)

    with st.container(border=True):
        st.subheader("🩸 Glucose Overlay")
        # Meal timestamps anchor each activity to minutes-since-ITS-meal.
        fig = charts.paired_cgm_overlay_fig(post_a, post_b, "Meal A", "Meal B",
                                            activities_a=acts_a, activities_b=acts_b,
                                            meal_ts_a=meal_a["capture_ts"],
                                            meal_ts_b=meal_b["capture_ts"])
        st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG)

    with st.container(border=True):
        st.subheader("Glucose Statistics")
        st.caption("HRV/vagal-tone statistics aren't shown — this pipeline's HRV "
                   "capture window doesn't reliably cover the evening post-meal "
                   "period yet (see module docstring).")
        st.dataframe(experiment.compare_meal_stats(stats_a, stats_b, "Meal A", "Meal B"),
                     width="stretch", hide_index=True)


# ---- Page starts rendering here (top of the visible page) ------------------
# Three columns across the header: logo, title, subject picker. Note the
# `with` blocks run out of visual order on purpose — the title text depends on
# which subject is picked, and a widget's value only exists after the widget is
# created. Columns place content by *which* column it went into, not by the
# order the blocks ran, so building the picker first costs nothing visually.
logo_col, title_col, user_col = st.columns([1, 8, 3], vertical_alignment="center")
with user_col:
    # Options come straight from data.SUBJECTS so the picker can't offer a
    # subject the loaders would reject. format_func only changes the *label*
    # ("kevin" -> "Kevin"); the value handed back is still the raw id the
    # BigQuery rows use. Like the View control below, segmented_control returns
    # None when nothing is selected, hence the `or` fallback.
    user_id = st.segmented_control("Subject", options=data.SUBJECTS,
                                   default=data.DEFAULT_SUBJECT,
                                   format_func=str.title) or data.DEFAULT_SUBJECT
with logo_col:
    st.image(str(LOGO_PATH), width=64)
with title_col:
    st.title(f"{user_id.title()} — Post-Prandial Experiment")
st.caption(
    "N-of-1 CGM analysis: how meals move glucose, and whether post-meal "
    "exercise changes the response. Fed live from the Biostream pipeline "
    "(Garmin, FreeStyle Libre CGM, Omron BP)."
)

# ---- Password gate ---------------------------------------------------------
# Kevin's view is open; the other subjects need a password (see auth.py).
# This sits ABOVE every data.load_* call on purpose: st.stop() halts the
# script right here, so a viewer who hasn't unlocked a subject never reaches
# a query and never sees a row of that subject's data.
if not auth.gate(user_id):
    st.stop()

# The view toggle. segmented_control returns None until first clicked,
# hence the `or "Single Meal"` fallback.
view = st.segmented_control("View",
                            options=["Single Meal", "Paired Meal Experiment",
                                     "Intensity × Sleep"],
                            default="Single Meal", label_visibility="collapsed") or "Single Meal"

# Meals are only loaded for the meal-anchored views — Intensity × Sleep runs
# on Garmin daily data alone, so a subject with no logged meals still gets it.
if view != "Intensity × Sleep":
    # Every load_* call below takes user_id, so switching subjects re-keys the
    # cache and re-queries rather than reusing the previous subject's frames.
    meals = data.load_meals(user_id)
    if meals.empty:
        st.info(f"No meals logged yet for {user_id.title()}.")
        st.stop()   # halts the script — nothing below renders
    # Add a _label column (the dropdown text) by applying _meal_label to each row.
    meals = meals.assign(_label=meals.apply(_meal_label, axis=1))

if view == "Intensity × Sleep":
    _intensity_sleep_view(user_id)

elif view == "Single Meal":
    selected_label = st.selectbox("Meal", meals["_label"])
    # Find the row whose label was picked (labels are unique per meal).
    meal = meals[meals["_label"] == selected_label].iloc[0]
    meal_ts = meal["capture_ts"]

    _meal_card(meal)
    _activity_section(user_id, meal_ts)

    # One fixed window, fetched once. There are no window sliders here anymore:
    # they only changed what was DRAWN, and every one of them cost a rerun (and
    # a BigQuery miss on first use) to redraw the same underlying readings. The
    # chart's own zoom buttons do that job in the browser instead.
    window = _load_meal_window(user_id, meal_ts)

    if window["glucose"].empty:
        st.warning("No CGM readings in this window — nothing to analyze for this meal.")
        st.stop()

    # The statistics behind the curve (same math the Paired view tabulates),
    # computed over the FULL fetched window — zooming the chart can't move
    # them, which is the point: the numbers describe the meal, not the view.
    stats = experiment.cgm_meal_stats(window["glucose"], meal_ts,
                                      BASELINE_WINDOW_MIN, SINGLE_MEAL_POST_HOURS)
    baseline = experiment.baseline_glucose(window["glucose"], meal_ts, BASELINE_WINDOW_MIN)

    with st.container(border=True):
        st.subheader("🩸 Glucose Response")
        # baseline= draws the dotted pre-meal reference line, so the excursion
        # is readable as a height above it rather than an absolute number.
        fig = charts.meal_timeline_fig(window["glucose"], window["activities"], window["bp"],
                                       meal_ts, baseline=baseline)
        st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG)

        # border=True boxes each metric so the row reads as a set of readings.
        # Every value can legitimately be None (no pre-meal readings, no peak,
        # never came back down), hence the "—" fallback on each.
        # Units live in the labels (matching the macro row on the meal card),
        # so the values stay bare numbers and the row scans as one set.
        # Two rows of 3/2, not one of 5 — five-across left "Return to
        # baseline (min)" and "Incremental AUC (mg/dL·min)" clipped mid-word
        # at normal window widths.
        s1, s2, s3 = st.columns(3)
        s1.metric("Baseline (mg/dL)", _stat(stats["baseline_mg_dl"]), border=True,
                  help=f"Mean glucose in the {BASELINE_WINDOW_MIN} minutes before the meal.")
        s2.metric("Peak (mg/dL)", _stat(stats["peak_mg_dl"]), border=True,
                  help="Highest reading after the meal.")
        s3.metric("Time to peak (min)", _stat(stats["time_to_peak_min"]), border=True,
                  help="Minutes from the meal to that peak.")
        s4, s5 = st.columns(2)
        s4.metric("Return to baseline (min)", _stat(stats["time_to_baseline_min"]), border=True,
                  help="Minutes until glucose fell back to baseline. “—” means it "
                       f"hadn't within {SINGLE_MEAL_POST_HOURS} hours of the meal.")
        s5.metric("Incremental AUC (mg/dL·min)", _stat(stats["auc_mg_dl_min"], "{:,.0f}"),
                  border=True,
                  help="Area between the curve and baseline — the single best summary "
                       "of how much this meal moved glucose overall.")

        if window["bp"].empty:
            st.caption("No blood-pressure reading in the following ~36 hours.")

    _overnight_hrv_section(user_id, meal_ts)

elif view == "Paired Meal Experiment":
    st.caption(
        "Pick any two meals to compare — e.g. the same dinner with and without "
        "a post-meal walk. **Meal B is the exercise arm**, Meal A the control. "
        "Overlay is on 'minutes since meal' so the two excursions line up "
        "regardless of when each meal happened."
    )

    # One activities query covering every meal in the picker, so labelling all
    # of them as exercise/control costs a single (cached) round trip instead of
    # one per meal. The bounds are padded by the protocol window on each side.
    pad = pd.Timedelta(minutes=experiment.POST_MEAL_EXERCISE_MAX_MIN)
    all_acts = data.load_activities_window(user_id,
                                           meals["capture_ts"].min() - pad,
                                           meals["capture_ts"].max() + pad)
    # Which meals are exercise arms — an activity started within the protocol
    # window after eating (experiment.POST_MEAL_EXERCISE_MAX_MIN minutes).
    is_arm = {row["_label"]: experiment.has_post_meal_exercise(all_acts, row["capture_ts"])
              for _, row in meals.iterrows()}

    def _arm_label(label: str) -> str:
        """Dropdown text, flagged so the exercise arms are pickable at a glance
        rather than by remembering which night had the walk."""
        return f"🏃 {label}" if is_arm[label] else label

    # Defaults now follow the protocol instead of "the two newest meals":
    # B = the most recent exercise arm, A = the most recent control. next()
    # walks the labels newest-first and takes the first match; the default
    # argument covers "no meal qualifies", where we fall back to the old
    # newest/second-newest behaviour rather than showing nothing.
    labels = list(meals["_label"])
    default_b = next((i for i, lbl in enumerate(labels) if is_arm[lbl]), 0)
    default_a = next((i for i, lbl in enumerate(labels)
                      if not is_arm[lbl] and i != default_b), min(1, len(labels) - 1))

    col_a, col_b = st.columns(2)
    with col_a:
        label_a = st.selectbox("Meal A — control", labels, index=default_a,
                               format_func=_arm_label)
    with col_b:
        label_b = st.selectbox("Meal B — exercise arm", labels, index=default_b,
                               format_func=_arm_label)

    meal_a = meals[meals["_label"] == label_a].iloc[0]
    meal_b = meals[meals["_label"] == label_b].iloc[0]

    if meal_a["meal_id"] == meal_b["meal_id"]:
        st.info("Pick two different meals to compare.")
        st.stop()

    # The pickers stay free — you may want to compare two controls — but say so
    # when the pair doesn't match the protocol, rather than silently reporting a
    # "with vs without exercise" delta that isn't one.
    if not is_arm[label_b]:
        st.warning(f"Meal B has no activity logged within "
                   f"{experiment.POST_MEAL_EXERCISE_MAX_MIN} minutes of the meal, so this "
                   "isn't an exercise-vs-control pair. Meals marked 🏃 are the exercise arms.")
    if is_arm[label_a]:
        st.warning("Meal A is also an exercise arm, so the Δ below compares two "
                   "exercised meals rather than exercise against control.")

    # What each meal's overlay shades. after_min caps at the protocol window —
    # a walk hours later isn't this meal's intervention and shouldn't be drawn
    # as though it were. before_min keeps pre-meal activity visible as context
    # (it lands at negative minutes on the overlay, which is the point).
    acts_a = experiment.activities_around_meal(
        all_acts, meal_a["capture_ts"],
        before_min=experiment.POST_MEAL_EXERCISE_MAX_MIN,
        after_min=experiment.POST_MEAL_EXERCISE_MAX_MIN)
    acts_b = experiment.activities_around_meal(
        all_acts, meal_b["capture_ts"],
        before_min=experiment.POST_MEAL_EXERCISE_MAX_MIN,
        after_min=experiment.POST_MEAL_EXERCISE_MAX_MIN)

    # The cards sit OUTSIDE the fragment: they describe the meals themselves,
    # so nothing the slider does can change them — no reason to redraw them
    # (and refetch two meal photos) every time it moves.
    card_a, card_b = st.columns(2)
    with card_a:
        _meal_card(meal_a)
    with card_b:
        _meal_card(meal_b)

    # Slider + everything downstream of it, isolated in its own rerun scope.
    _paired_experiment(user_id, meal_a, meal_b, acts_a, acts_b)

    # HRV Overlay: outside the fragment on purpose, like the meal cards above —
    # it doesn't depend on hours_after, so there's no reason to redraw it on
    # every slider drag. Sleep date = the morning after each meal, same
    # derivation _overnight_hrv_section uses for the Single Meal tab (Garmin
    # attributes a night's sleep to the following morning's calendar date).
    sleep_date_a = (meal_a["capture_ts"].normalize() + pd.Timedelta(days=1)).date()
    sleep_date_b = (meal_b["capture_ts"].normalize() + pd.Timedelta(days=1)).date()
    hrv_a = data.load_hrv_for_sleep_date(user_id, sleep_date_a.isoformat())
    hrv_b = data.load_hrv_for_sleep_date(user_id, sleep_date_b.isoformat())

    with st.container(border=True):
        st.subheader("😴 Sleep HRV Overlay")
        if hrv_a.empty and hrv_b.empty:
            st.caption("No HRV data recorded for either night.")
        else:
            fig = charts.paired_hrv_overlay_fig(hrv_a, hrv_b, "Meal A", "Meal B")
            st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG)

    with st.container(border=True):
        st.subheader("Sleep HRV Statistics")
        if hrv_a.empty and hrv_b.empty:
            st.caption("No HRV data recorded for either night.")
        else:
            hrv_stats_a = experiment.hrv_sleep_stats(hrv_a)
            hrv_stats_b = experiment.hrv_sleep_stats(hrv_b)
            st.dataframe(experiment.compare_meal_stats(hrv_stats_a, hrv_stats_b, "Meal A", "Meal B",
                                                        stat_labels=experiment.HRV_STAT_LABELS),
                        width="stretch", hide_index=True)

st.divider()
st.caption(
    "**How it works** — Cloud Scheduler triggers Python Cloud Functions that "
    "poll Garmin Connect (wellness + activities), LibreLinkUp CGM, "
    "and Omron Connect into partitioned BigQuery tables. This page computes "
    "CGM statistics (incremental AUC, peak, time-to-peak, return-to-baseline, "
    "rise velocity/acceleration) live from that data, cached 30 minutes."
)
