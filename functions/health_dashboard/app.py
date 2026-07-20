"""Biostream — public N-of-1 health dashboard (subject: kevin).

Streamlit on Cloud Run, reading the health_twin BigQuery dataset that the
Biostream ingestion pipeline fills continuously from Garmin, a FreeStyle
Libre CGM, and an Omron blood-pressure cuff.
"""

import streamlit as st

import charts
import data
import transforms

st.set_page_config(page_title="Biostream — Kevin", page_icon="🫀",
                   layout="wide", initial_sidebar_state="collapsed")

RANGES = {"Last 7 days": 7, "Last 14 days": 14, "Last 30 days": 30,
          "Last 90 days": 90}


def _table(df, label="View as table"):
    with st.expander(label):
        st.dataframe(df, use_container_width=True, hide_index=True)


st.title("Kevin — Biostream health dashboard")
st.caption(
    "A live feed: Garmin wearable + FreeStyle Libre CGM + Omron "
    "Wearable/Sensor/Device → scheduled Cloud Functions → BigQuery → this page. "
    "Data refreshes automatically; every chart below is the subject(Me)'s own data"
)

choice = st.segmented_control("Date range", options=list(RANGES),
                              default="Last 30 days",
                              label_visibility="collapsed")
days = RANGES[choice or "Last 30 days"]

daily = data.load_daily(days)
glucose = data.load_glucose(days)

if daily.empty:
    st.info("No data in this range yet.")
    st.stop()

kpi = transforms.kpi_row(daily, glucose)

cols = st.columns(5)
with cols[0]:
    st.metric("Resting heart rate", f"{kpi['resting_hr']:.0f} bpm"
              if kpi["resting_hr"] is not None else "—",
              delta=f"{kpi['resting_hr_delta']:+.0f} vs 7-day avg"
              if kpi["resting_hr_delta"] is not None else None,
              delta_color="inverse")
with cols[1]:
    st.metric("Overnight HRV", f"{kpi['hrv']:.0f} ms"
              if kpi["hrv"] is not None else "—",
              delta=f"{kpi['hrv_delta']:+.0f} vs 7-day avg"
              if kpi["hrv_delta"] is not None else None)
with cols[2]:
    st.metric("Sleep last night", f"{kpi['sleep_h']:.1f} h"
              if kpi["sleep_h"] is not None else "—")
with cols[3]:
    st.metric("Steps yesterday", f"{kpi['steps_yday']:,.0f}"
              if kpi["steps_yday"] is not None else "—")
with cols[4]:
    st.metric("Avg glucose", f"{kpi['glucose_avg']:.0f} mg/dL"
              if kpi["glucose_avg"] is not None else "—",
              help="Mean of all CGM readings in the selected range")

st.divider()

# --- Glucose (CGM) ----------------------------------------------------------
st.subheader("Continuous glucose")
if glucose.empty:
    st.caption("No CGM readings in this range.")
else:
    tir = kpi["time_in_range"]
    st.caption(
        f"FreeStyle Libre, one reading ≈ every 5 min · "
        f"**{tir:.0f}% time in range** (70–180 mg/dL) over the selected period"
        if tir is not None else "FreeStyle Libre readings")
    st.plotly_chart(charts.glucose_fig(glucose), use_container_width=True)
    _table(glucose)

# --- Sleep & HRV -------------------------------------------------------------
left, right = st.columns(2)
with left:
    st.subheader("Sleep stages")
    st.caption("Nightly deep / REM / light split from Garmin")
    stages = transforms.sleep_stages(daily)
    st.plotly_chart(charts.sleep_fig(stages), use_container_width=True)
    _table(stages)
with right:
    st.subheader("Overnight HRV")
    st.caption("Nightly average heart-rate variability — recovery signal")
    st.plotly_chart(charts.hrv_fig(daily), use_container_width=True)
    _table(daily[["date", "hrv_avg"]].dropna())

# --- Resting HR & steps -------------------------------------------------------
left, right = st.columns(2)
with left:
    st.subheader("Resting heart rate")
    st.caption("Daily minimum from the wearable")
    st.plotly_chart(charts.resting_hr_fig(daily), use_container_width=True)
with right:
    st.subheader("Steps")
    st.caption("Daily totals (today is still accumulating)")
    st.plotly_chart(charts.steps_fig(daily), use_container_width=True)

# --- Intraday -----------------------------------------------------------------
st.subheader("Last 48 hours, up close")
st.caption("Intraday stream polled every 15 minutes: heart rate, "
           "Garmin stress score, and body battery")
intraday = data.load_intraday(48)
if intraday.empty:
    st.caption("No intraday data yet.")
else:
    st.plotly_chart(charts.intraday_fig(intraday), use_container_width=True)

# --- Blood pressure -----------------------------------------------------------
bp = data.load_blood_pressure(days)
if not bp.empty:
    st.subheader("Blood pressure")
    st.caption("Omron cuff readings, synced daily")
    st.plotly_chart(charts.bp_fig(bp), use_container_width=True)
    _table(bp)

st.divider()
st.caption(
    "**How it works** — Cloud Scheduler triggers Python Cloud Functions that "
    "poll each device API (Garmin Connect daily + every 15 min, LibreLinkUp, "
    "Omron Connect) into date-partitioned BigQuery tables clustered by "
    "subject. This dashboard is Streamlit + Plotly on Cloud Run, querying "
    "BigQuery live with a 30-minute cache. Built as an N-of-1 experimentation "
    "platform: one subject, dense time series, every intervention measurable."
)
