"""Plotly figure builders. Pure: dataframe in, go.Figure out.

Palette follows the validated dataviz reference instance, retuned to the
MOAI brand surface (#f2f1ef, matching the logo's own background) with a
purple-leaning neutral ramp. One entity, one hue, everywhere it appears:
glucose=blue, HRV=violet, heart rate=red, steps=orange, stress=yellow,
body battery / recovery=green. Sleep depth is an ordinal green ramp.

HOW TO READ THIS FILE: each *_fig function assembles one chart the same way —
    fig = go.Figure()          start empty
    fig.add_trace(...)         add each line/bar series
    fig.add_hrect/vline/...    add reference bands and event markers
    fig = _layout(fig)         apply the shared house style
Nothing here touches Streamlit or BigQuery; app.py passes dataframes in and
renders the returned figure.
"""

import pandas as pd
import plotly.graph_objects as go

from transforms import GLUCOSE_RANGE_MG_DL, break_time_gaps, fill_date_gaps

# --- House style constants (used by every chart) ----------------------------
SURFACE = "#f2f1ef"   # chart background
GRID = "#e3e0e6"
AXIS = "#c7c2cc"
INK = "#151022"       # darkest text
INK_2 = "#4a4356"
MUTED = "#8b8593"     # secondary text (axis labels, annotations)
BAND = "#eae8ec"  # neutral wash for reference ranges

# One entity = one hue, consistently across every chart on the page.
BLUE = "#2a78d6"     # glucose
VIOLET = "#4a3aa7"   # HRV
RED = "#e34948"      # heart rate (resting + intraday)
ORANGE = "#eb6834"   # steps
YELLOW = "#eda100"   # stress
GREEN = "#008300"    # body battery / diastolic
SLEEP_RAMP = {"Deep": "#0b5d0b", "REM": "#2f9e2f", "Light": "#7cc47c"}

FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'


def _layout(fig: go.Figure, height: int = 320, top: int = 8) -> go.Figure:
    """Shared house style applied to every figure: background, font, margins,
    unified hover (one tooltip for all series at a given x), quiet axes.
    Legends default off; charts that need one re-enable it after calling this."""
    fig.update_layout(
        height=height,
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        font=dict(family=FONT, color=INK_2, size=13),
        margin=dict(l=8, r=8, t=top, b=8),
        hovermode="x unified",
        hoverlabel=dict(bgcolor="#ffffff", font=dict(family=FONT, color=INK)),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0,
                    font=dict(color=INK_2)),
        showlegend=False,
    )
    fig.update_xaxes(showgrid=False, linecolor=AXIS, tickcolor=AXIS,
                     tickfont=dict(color=MUTED), zeroline=False)
    fig.update_yaxes(gridcolor=GRID, gridwidth=1, linecolor=SURFACE,
                     tickfont=dict(color=MUTED), zeroline=False)
    # Spikeline: hovering drops a thin dotted vertical line onto the time axis.
    # This is the chart's core reading gesture — line a glucose peak up against
    # an exercise band without eyeballing across empty space.
    #   spikemode="across" — draw the line across the whole plot area, not just
    #                        the stub between the point and the axis.
    #   spikedistance=-1   — no proximity cutoff: the spike follows the cursor
    #                        anywhere on the chart, not only near a data point.
    # Applied here in the shared helper, so every chart on the page gets it.
    fig.update_xaxes(showspikes=True, spikemode="across", spikethickness=1,
                     spikedash="dot", spikecolor=MUTED)
    fig.update_layout(spikedistance=-1)
    return fig


def glucose_fig(df: pd.DataFrame) -> go.Figure:
    """Plain CGM trace with the 70–180 target band behind it."""
    # break_time_gaps: no line drawn across sensor gaps > 30 min (see transforms.py).
    df = break_time_gaps(df, "ts", pd.Timedelta(minutes=30))
    lo, hi = GLUCOSE_RANGE_MG_DL
    fig = go.Figure()
    # hrect = horizontal band across the whole x-range; layer="below" keeps it
    # behind the data line.
    fig.add_hrect(y0=lo, y1=hi, fillcolor=BAND, line_width=0, layer="below")
    fig.add_trace(go.Scatter(
        x=df["ts"], y=df["glucose_mg_dl"], mode="lines",
        line=dict(color=BLUE, width=2, shape="spline", smoothing=0.6),
        # hovertemplate: tooltip text; <extra></extra> suppresses the default
        # trace-name box Plotly would otherwise append.
        name="Glucose", hovertemplate="%{y:.0f} mg/dL<extra></extra>",
    ))
    fig.add_annotation(x=0, xref="paper", y=hi, yanchor="bottom",
                       text=f"target {lo}–{hi}", showarrow=False,
                       font=dict(color=MUTED, size=11), xanchor="left")
    fig = _layout(fig, height=340)
    fig.update_yaxes(title_text="mg/dL", title_font=dict(color=MUTED))
    return fig


def sleep_fig(stages: pd.DataFrame) -> go.Figure:
    """Stacked nightly bars: deep at the bottom, then REM, then light."""
    fig = go.Figure()
    for stage in ["Deep", "REM", "Light"]:  # deep anchored at the baseline
        fig.add_trace(go.Bar(
            x=stages["date"], y=stages[f"{stage.lower()}_h"], name=stage,
            marker=dict(color=SLEEP_RAMP[stage],
                        line=dict(color=SURFACE, width=2)),
            hovertemplate="%{y:.1f} h<extra>" + stage + "</extra>",
        ))
    fig = _layout(fig)
    fig.update_layout(barmode="stack", showlegend=True, bargap=0.45)
    fig.update_yaxes(title_text="hours", title_font=dict(color=MUTED))
    return fig


def hrv_fig(daily: pd.DataFrame) -> go.Figure:
    """Daily average HRV. fill_date_gaps makes missing days break the line."""
    df = fill_date_gaps(daily.dropna(subset=["hrv_avg"]))
    fig = go.Figure(go.Scatter(
        x=df["date"], y=df["hrv_avg"], mode="lines+markers",
        line=dict(color=VIOLET, width=2),
        marker=dict(size=8, color=VIOLET, line=dict(color=SURFACE, width=2)),
        hovertemplate="%{y:.0f} ms<extra></extra>",
    ))
    fig = _layout(fig)
    fig.update_yaxes(title_text="ms", title_font=dict(color=MUTED))
    return fig


def resting_hr_fig(daily: pd.DataFrame) -> go.Figure:
    df = fill_date_gaps(daily.dropna(subset=["resting_hr"]))
    fig = go.Figure(go.Scatter(
        x=df["date"], y=df["resting_hr"], mode="lines+markers",
        line=dict(color=RED, width=2),
        marker=dict(size=8, color=RED, line=dict(color=SURFACE, width=2)),
        hovertemplate="%{y:.0f} bpm<extra></extra>",
    ))
    fig = _layout(fig)
    fig.update_yaxes(title_text="bpm", title_font=dict(color=MUTED))
    return fig


def steps_fig(daily: pd.DataFrame) -> go.Figure:
    df = daily.dropna(subset=["total_steps"])
    fig = go.Figure(go.Bar(
        x=df["date"], y=df["total_steps"],
        marker=dict(color=ORANGE, line=dict(color=SURFACE, width=2)),
        hovertemplate="%{y:,.0f} steps<extra></extra>",
    ))
    fig = _layout(fig)
    fig.update_layout(bargap=0.45, barcornerradius=4)
    fig.update_yaxes(title_text="steps", title_font=dict(color=MUTED))
    return fig


EXERCISE_BAND = "rgba(235, 104, 52, 0.12)"  # translucent orange wash

# --- Single Meal zoom controls ----------------------------------------------
# app.py fetches one generous window per meal (2 h before -> 8 h after) and the
# reader zooms inside it here, in the browser. Nothing below changes the data,
# only which slice of the x-axis is visible — so a zoom is a Plotly relayout:
# no Streamlit rerun, no BigQuery query, no spinner.
ZOOM_PRESET_HOURS = [1, 2, 4]   # "1h" = the first hour after the meal, etc.
DEFAULT_VIEW_PRE_MIN = 10       # how much pre-meal context the first render shows
DEFAULT_VIEW_POST_HOURS = 4     # ...and how far past the meal it reaches


def _meal_zoom_buttons(meal_ts) -> list[dict]:
    """Build the zoom preset buttons, each carrying an explicit x-range
    anchored on the MEAL — "2h" means the two hours after eating.

    Why not Plotly's built-in `rangeselector`: its buttons step backward from
    the RIGHT EDGE of the current view, so its "2h" means "the last two hours
    of whatever is on screen" — it has no way to anchor on a point inside the
    data. `updatemenus` buttons carry a literal range, which is exactly the
    meal-anchored behaviour this chart needs.

    method="relayout" = change layout only (here, the axis range); the traces
    are untouched, which is why the browser can do this without a round trip.
    """
    meal_ts = pd.Timestamp(meal_ts)
    start = meal_ts - pd.Timedelta(minutes=DEFAULT_VIEW_PRE_MIN)
    # .isoformat() matches how Plotly encodes the datetime x-values themselves
    # (same tz offset, or none for naive timestamps), so the two line up.
    buttons = [
        dict(label=f"{h}h", method="relayout",
             args=[{"xaxis.range": [start.isoformat(),
                                    (meal_ts + pd.Timedelta(hours=h)).isoformat()]}])
        for h in ZOOM_PRESET_HOURS
    ]
    # "All" hands the axis back to Plotly's autoscale — i.e. the entire window
    # app.py fetched, however wide that happens to be.
    buttons.append(dict(label="All", method="relayout", args=[{"xaxis.autorange": True}]))
    return buttons


def meal_timeline_fig(glucose: pd.DataFrame, activities: pd.DataFrame,
                      bp: pd.DataFrame, meal_ts, baseline: float | None) -> go.Figure:
    """Single Meal view: CGM anchored on the meal, with exercise windows and
    next-morning BP drawn as overlays.

    Layered from back to front: target band -> baseline line -> glucose trace
    -> meal marker -> exercise bands -> BP markers.
    """
    fig = go.Figure()

    lo, hi = GLUCOSE_RANGE_MG_DL
    fig.add_hrect(y0=lo, y1=hi, fillcolor=BAND, line_width=0, layer="below")
    if baseline is not None:
        fig.add_hline(y=baseline, line=dict(color=MUTED, width=1, dash="dot"))
    g = break_time_gaps(glucose, "ts", pd.Timedelta(minutes=30))
    fig.add_trace(go.Scatter(x=g["ts"], y=g["glucose_mg_dl"], mode="lines",
                             line=dict(color=BLUE, width=2),
                             hovertemplate="%{y:.0f} mg/dL<extra></extra>"))

    # vline = vertical marker at one instant (the meal).
    # yshift pulls the label down INSIDE the plot area: a vline's annotation
    # normally floats just above it, which is exactly where the zoom buttons
    # sit — and in the default view the meal is near the left edge, right under
    # them. This also lines "Meal" up with the exercise band labels.
    fig.add_vline(x=meal_ts, line=dict(color=INK_2, width=2, dash="dash"),
                 annotation_text="Meal", annotation_position="top",
                 annotation_yshift=-14,
                 annotation_font=dict(color=INK_2, size=11))

    # vrect = shaded span (start -> end of each workout).
    # iterrows() yields (index, row) pairs; `_` discards the index.
    for _, a in activities.iterrows():
        end = a["end_ts"] if pd.notna(a["end_ts"]) else a["start_ts"]
        fig.add_vrect(x0=a["start_ts"], x1=end, fillcolor=EXERCISE_BAND, line_width=0,
                     # pd.notna, not `or`: a missing type is NaN/NA, not falsy.
                     annotation_text=(a["activity_type"]
                                      if pd.notna(a["activity_type"]) else "Exercise"),
                     annotation_position="top left",
                     annotation_font=dict(color=ORANGE, size=10))

    for _, r in bp.iterrows():
        fig.add_vline(x=r["measurement_ts_utc"], line=dict(color=GREEN, width=1, dash="dot"),
                     annotation_text=f"BP {r['systolic']:.0f}/{r['diastolic']:.0f}",
                     annotation_position="bottom right",
                     annotation_font=dict(color=GREEN, size=10))

    # Taller than the other charts (and a roomier top margin) to pay for the
    # two extra strips this one carries: the zoom buttons above the plot and
    # the range slider below it.
    fig = _layout(fig, height=470, top=46)
    fig.update_yaxes(title_text="mg/dL", title_font=dict(color=MUTED))

    # Open on the same span the old "minutes before / hours after" sliders
    # defaulted to. The data underneath extends further; this is just the
    # starting view, and every zoom control below moves it client-side.
    meal_ts = pd.Timestamp(meal_ts)
    fig.update_xaxes(
        range=[(meal_ts - pd.Timedelta(minutes=DEFAULT_VIEW_PRE_MIN)).isoformat(),
               (meal_ts + pd.Timedelta(hours=DEFAULT_VIEW_POST_HOURS)).isoformat()],
        # The strip under the chart: a mini overview of the WHOLE fetched
        # window with the visible span as a draggable handle. thickness is a
        # fraction of plot height, so 0.08 keeps it slim.
        rangeslider=dict(visible=True, thickness=0.08, bgcolor=SURFACE,
                         bordercolor=AXIS, borderwidth=1),
    )
    fig.update_layout(updatemenus=[dict(
        type="buttons", direction="right", buttons=_meal_zoom_buttons(meal_ts),
        # x/y are in paper coordinates: pinned to the left edge, sitting just
        # above the plot area (y=1 anchored by its bottom).
        x=0, xanchor="left", y=1, yanchor="bottom", pad=dict(b=6),
        bgcolor=SURFACE, bordercolor=AXIS, borderwidth=1,
        font=dict(family=FONT, color=INK_2, size=11),
        # The opening range above is the "4h" preset, so highlight that button
        # (index 2) to start — otherwise the controls disagree with the view.
        showactive=True, active=ZOOM_PRESET_HOURS.index(DEFAULT_VIEW_POST_HOURS),
    )])
    return fig


# Per-meal exercise washes for the paired overlay: same 12% alpha as
# EXERCISE_BAND, but tinted with each curve's hue so a band is visually
# tied to the meal it belongs to (blue wash = Meal A, orange wash = Meal B).
EXERCISE_BAND_BLUE = "rgba(42, 120, 214, 0.12)"    # translucent BLUE
EXERCISE_BAND_ORANGE = "rgba(235, 104, 52, 0.12)"  # translucent ORANGE


def paired_cgm_overlay_fig(window_a: pd.DataFrame, window_b: pd.DataFrame,
                           label_a: str, label_b: str,
                           activities_a: pd.DataFrame | None = None,
                           activities_b: pd.DataFrame | None = None,
                           meal_ts_a=None, meal_ts_b=None) -> go.Figure:
    """Paired Meal Experiment overlay: both meals' CGM excursions on a shared
    'minutes since meal' axis so the two curves are directly comparable.
    (Minute 0 is the true meal timestamp when meal_ts_a/b are passed; older
    callers that omit them fall back to each window's first reading, which
    post_meal_window guarantees is at/just after the meal.)

    Optionally also marks WHEN things happened: a dashed line at minute 0
    (the meal itself — shared by both curves, since 0 is each meal by
    construction) and a shaded band per Garmin activity, tinted with its
    meal's curve color. The activity args default to None so older callers
    (and tests) that pass only the two windows keep working unchanged.
    """
    fig = go.Figure()
    lo, hi = GLUCOSE_RANGE_MG_DL
    fig.add_hrect(y0=lo, y1=hi, fillcolor=BAND, line_width=0, layer="below")

    # One pass per meal: draw its CGM curve, then its exercise bands.
    # The tuple also carries a short tag ("A"/"B") for band labels, the wash
    # color matching the curve, and where to pin the band annotation —
    # A's labels sit at the top, B's at the bottom, so they never collide.
    for window, label, color, activities, meal_ts, tag, wash, ann_pos in [
        (window_a, label_a, BLUE, activities_a, meal_ts_a, "A",
         EXERCISE_BAND_BLUE, "top left"),
        (window_b, label_b, ORANGE, activities_b, meal_ts_b, "B",
         EXERCISE_BAND_ORANGE, "bottom left"),
    ]:
        # Anchor: the instant that counts as minute 0 for this meal — shared
        # by the curve AND its activity bands, so the two can never drift
        # apart. Prefer the true meal timestamp; fall back to the window's
        # first reading (which post_meal_window guarantees is at/just after
        # the meal — "just after" is why the fallback alone isn't enough: a
        # sensor gap at mealtime would shift curve and bands out of sync).
        anchor = meal_ts if meal_ts is not None else (
            window["ts"].iloc[0] if not window.empty else None)

        if not window.empty:
            minutes = (window["ts"] - anchor).dt.total_seconds() / 60
            fig.add_trace(go.Scatter(x=minutes, y=window["glucose_mg_dl"], mode="lines",
                                     name=label, line=dict(color=color, width=2),
                                     hovertemplate="%{y:.0f} mg/dL<extra>" + label + "</extra>"))
        if activities is None or anchor is None:
            continue    # no activities passed, or nothing to anchor them to
        for _, a in activities.iterrows():
            # Some activities lack an end_ts; treat them as instantaneous.
            end_ts = a["end_ts"] if pd.notna(a["end_ts"]) else a["start_ts"]
            # Wall-clock -> minutes since THIS meal. Pre-meal activities land
            # at negative minutes on purpose (a pre-meal walk should show).
            x0 = (a["start_ts"] - anchor).total_seconds() / 60
            x1 = (end_ts - anchor).total_seconds() / 60
            # pd.notna, not `or`: a missing activity_type arrives as NaN/NA
            # (truthy or even un-boolable in pandas), which `or` mishandles.
            kind = a["activity_type"] if pd.notna(a["activity_type"]) else "Exercise"
            fig.add_vrect(x0=x0, x1=x1, fillcolor=wash, line_width=0,
                          # e.g. "walking (A)" — activity type + which meal,
                          # colored to match that meal's curve.
                          annotation_text=f"{kind} ({tag})",
                          annotation_position=ann_pos,
                          annotation_font=dict(color=color, size=10))

    # Minute 0 IS the meal for both curves, so one shared dashed marker.
    fig.add_vline(x=0, line=dict(color=INK_2, width=2, dash="dash"),
                  annotation_text="Meal", annotation_position="top",
                  annotation_font=dict(color=INK_2, size=11))

    fig = _layout(fig, height=380)
    fig.update_layout(showlegend=True)
    fig.update_xaxes(title_text="minutes since meal", title_font=dict(color=MUTED))
    fig.update_yaxes(title_text="mg/dL", title_font=dict(color=MUTED))
    return fig


def overnight_hrv_glucose_fig(hrv: pd.DataFrame, glucose: pd.DataFrame) -> go.Figure:
    """HRV during the night's sleep (violet, left axis) paired with glucose
    (blue, right axis) over the same overnight window.

    Dual-axis mechanics: the glucose trace declares yaxis="y2", and the
    layout's yaxis2 has overlaying="y" (drawn on the same plot area) and
    side="right" — two different scales sharing one time axis.
    """
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=hrv["ts"], y=hrv["hrv_value"], mode="lines+markers", name="HRV",
        line=dict(color=VIOLET, width=2), marker=dict(size=5, color=VIOLET),
        hovertemplate="%{y:.0f} ms<extra>HRV</extra>",
    ))
    g = break_time_gaps(glucose, "ts", pd.Timedelta(minutes=30))
    fig.add_trace(go.Scatter(
        x=g["ts"], y=g["glucose_mg_dl"], mode="lines", name="Glucose",
        line=dict(color=BLUE, width=2), yaxis="y2",
        hovertemplate="%{y:.0f} mg/dL<extra>Glucose</extra>",
    ))
    fig = _layout(fig, height=380)
    fig.update_layout(
        showlegend=True,
        yaxis=dict(title=dict(text="HRV (ms)", font=dict(color=VIOLET)),
                  gridcolor=GRID, tickfont=dict(color=MUTED)),
        yaxis2=dict(title=dict(text="Glucose (mg/dL)", font=dict(color=BLUE)),
                   overlaying="y", side="right", showgrid=False,
                   tickfont=dict(color=MUTED)),
    )
    return fig


def bp_fig(df: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    for name, col, color in [("Systolic", "systolic", BLUE),
                             ("Diastolic", "diastolic", GREEN)]:
        fig.add_trace(go.Scatter(
            x=df["measurement_ts_utc"], y=df[col], mode="lines+markers",
            name=name, line=dict(color=color, width=2),
            marker=dict(size=8, color=color, line=dict(color=SURFACE, width=2)),
            hovertemplate="%{y:.0f} mmHg<extra>" + name + "</extra>",
        ))
    fig = _layout(fig)
    fig.update_layout(showlegend=True)
    fig.update_yaxes(title_text="mmHg", title_font=dict(color=MUTED))
    return fig
