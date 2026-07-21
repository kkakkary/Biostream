"""Plotly figure builders. Pure: dataframe in, go.Figure out.

Palette follows the validated dataviz reference instance (light mode,
surface #fcfcfb). One entity, one hue, everywhere it appears:
glucose=blue, HRV=violet, heart rate=red, steps=orange, stress=yellow,
body battery / recovery=green. Sleep depth is an ordinal green ramp.
"""

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from transforms import GLUCOSE_RANGE_MG_DL, break_time_gaps, fill_date_gaps

SURFACE = "#fcfcfb"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
BAND = "#f0efec"  # neutral wash for reference ranges

BLUE = "#2a78d6"     # glucose
VIOLET = "#4a3aa7"   # HRV
RED = "#e34948"      # heart rate (resting + intraday)
ORANGE = "#eb6834"   # steps
YELLOW = "#eda100"   # stress
GREEN = "#008300"    # body battery / diastolic
SLEEP_RAMP = {"Deep": "#0b5d0b", "REM": "#2f9e2f", "Light": "#7cc47c"}

FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'


def _layout(fig: go.Figure, height: int = 320, top: int = 8) -> go.Figure:
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
    return fig


def glucose_fig(df: pd.DataFrame) -> go.Figure:
    df = break_time_gaps(df, "ts", pd.Timedelta(minutes=30))
    lo, hi = GLUCOSE_RANGE_MG_DL
    fig = go.Figure()
    fig.add_hrect(y0=lo, y1=hi, fillcolor=BAND, line_width=0, layer="below")
    fig.add_trace(go.Scatter(
        x=df["ts"], y=df["glucose_mg_dl"], mode="lines",
        line=dict(color=BLUE, width=2, shape="spline", smoothing=0.6),
        name="Glucose", hovertemplate="%{y:.0f} mg/dL<extra></extra>",
    ))
    fig.add_annotation(x=0, xref="paper", y=hi, yanchor="bottom",
                       text=f"target {lo}–{hi}", showarrow=False,
                       font=dict(color=MUTED, size=11), xanchor="left")
    fig = _layout(fig, height=340)
    fig.update_yaxes(title_text="mg/dL", title_font=dict(color=MUTED))
    return fig


def sleep_fig(stages: pd.DataFrame) -> go.Figure:
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


def intraday_fig(df: pd.DataFrame) -> go.Figure:
    """Three metrics on different scales → three stacked panels, one shared
    time axis. Never a dual-axis chart."""
    panels = [
        ("Heart rate", "heart_rate", RED, "bpm"),
        ("Stress", "stress", YELLOW, ""),
        ("Body battery", "body_battery", GREEN, ""),
    ]
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True,
                        vertical_spacing=0.10,
                        subplot_titles=[p[0] for p in panels])
    for i, (_, col, color, unit) in enumerate(panels, start=1):
        sub = break_time_gaps(df.dropna(subset=[col]), "ts",
                              pd.Timedelta(hours=1))
        suffix = f" {unit}".rstrip()
        fig.add_trace(go.Scatter(
            x=sub["ts"], y=sub[col], mode="lines",
            line=dict(color=color, width=2),
            hovertemplate="%{y:.0f}" + suffix + "<extra></extra>",
        ), row=i, col=1)
    fig = _layout(fig, height=520, top=28)
    fig.update_annotations(font=dict(color=INK_2, size=13), x=0, xanchor="left")
    return fig


EXERCISE_BAND = "rgba(235, 104, 52, 0.12)"  # translucent orange wash


def meal_timeline_fig(glucose: pd.DataFrame, intraday: pd.DataFrame,
                      activities: pd.DataFrame, bp: pd.DataFrame,
                      meal_ts, baseline: float | None) -> go.Figure:
    """Single Meal view: CGM + heart rate, one shared time axis, meal start +
    exercise windows + next-morning BP drawn as overlays."""
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        vertical_spacing=0.10,
                        row_heights=[0.55, 0.45],
                        subplot_titles=["Glucose (CGM)", "Heart rate"])

    lo, hi = GLUCOSE_RANGE_MG_DL
    fig.add_hrect(y0=lo, y1=hi, fillcolor=BAND, line_width=0, layer="below", row=1, col=1)
    if baseline is not None:
        fig.add_hline(y=baseline, line=dict(color=MUTED, width=1, dash="dot"), row=1, col=1)
    g = break_time_gaps(glucose, "ts", pd.Timedelta(minutes=30))
    fig.add_trace(go.Scatter(x=g["ts"], y=g["glucose_mg_dl"], mode="lines",
                             line=dict(color=BLUE, width=2),
                             hovertemplate="%{y:.0f} mg/dL<extra></extra>"), row=1, col=1)

    hr = break_time_gaps(intraday.dropna(subset=["heart_rate"]), "ts", pd.Timedelta(hours=1))
    fig.add_trace(go.Scatter(x=hr["ts"], y=hr["heart_rate"], mode="lines",
                             line=dict(color=RED, width=2),
                             hovertemplate="%{y:.0f} bpm<extra></extra>"), row=2, col=1)

    fig.add_vline(x=meal_ts, line=dict(color=INK_2, width=2, dash="dash"),
                 annotation_text="Meal", annotation_position="top",
                 annotation_font=dict(color=INK_2, size=11), row="all", col="all")

    for _, a in activities.iterrows():
        end = a["end_ts"] if pd.notna(a["end_ts"]) else a["start_ts"]
        fig.add_vrect(x0=a["start_ts"], x1=end, fillcolor=EXERCISE_BAND, line_width=0,
                     annotation_text=a["activity_type"] or "Exercise",
                     annotation_position="top left",
                     annotation_font=dict(color=ORANGE, size=10), row="all", col="all")

    for _, r in bp.iterrows():
        fig.add_vline(x=r["measurement_ts_utc"], line=dict(color=GREEN, width=1, dash="dot"),
                     annotation_text=f"BP {r['systolic']:.0f}/{r['diastolic']:.0f}",
                     annotation_position="bottom right",
                     annotation_font=dict(color=GREEN, size=10), row=1, col=1)

    fig = _layout(fig, height=520, top=28)
    fig.update_yaxes(title_text="mg/dL", title_font=dict(color=MUTED), row=1, col=1)
    fig.update_yaxes(title_text="bpm", title_font=dict(color=MUTED), row=2, col=1)
    fig.update_annotations(font=dict(color=INK_2, size=13))
    return fig


def paired_cgm_overlay_fig(window_a: pd.DataFrame, window_b: pd.DataFrame,
                           label_a: str, label_b: str) -> go.Figure:
    """Paired Meal Experiment overlay: both meals' CGM excursions on a shared
    'minutes since meal' axis so the two curves are directly comparable."""
    fig = go.Figure()
    lo, hi = GLUCOSE_RANGE_MG_DL
    fig.add_hrect(y0=lo, y1=hi, fillcolor=BAND, line_width=0, layer="below")
    for window, label, color in [(window_a, label_a, BLUE), (window_b, label_b, ORANGE)]:
        if window.empty:
            continue
        minutes = (window["ts"] - window["ts"].iloc[0]).dt.total_seconds() / 60
        fig.add_trace(go.Scatter(x=minutes, y=window["glucose_mg_dl"], mode="lines",
                                 name=label, line=dict(color=color, width=2),
                                 hovertemplate="%{y:.0f} mg/dL<extra>" + label + "</extra>"))
    fig = _layout(fig, height=380)
    fig.update_layout(showlegend=True)
    fig.update_xaxes(title_text="minutes since meal", title_font=dict(color=MUTED))
    fig.update_yaxes(title_text="mg/dL", title_font=dict(color=MUTED))
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
