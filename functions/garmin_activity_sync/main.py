"""garmin-activity-sync: poll Garmin workouts/activities -> BigQuery garmin_activities.

Same shape as garmin_sync: scheduled poll, auto-discovers users from
garmin-token-<user> secrets, idempotent by activity_id (delete-then-load).

This exists to place exercise start/end markers on the post-prandial
experiment timeline — garmin_daily has no activity data.

HOW TO READ THIS FILE: it's a slimmed-down sibling of garmin_sync/main.py,
which carries the detailed beginner annotations for the shared patterns
(env-var config, secret discovery, _safe/_int coercion, delete-then-load).
The one difference in the upsert: activities have a globally unique
activity_id from Garmin, so idempotency keys on that id alone — no
(user, date) pair needed.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys

import functions_framework
from garminconnect import Garmin
from google.cloud import bigquery, secretmanager

PROJECT = os.environ["PROJECT"]
DATASET = os.environ.get("BQ_DATASET", "health_twin")
DAYS_BACK = int(os.environ.get("DAYS_BACK", "2"))
TABLE = f"{PROJECT}.{DATASET}.garmin_activities"

_bq = bigquery.Client(project=PROJECT)
_sm = secretmanager.SecretManagerServiceClient()


def _token(user: str) -> str:
    """Cached Garmin login token from Secret Manager (see garmin_sync)."""
    name = f"projects/{PROJECT}/secrets/garmin-token-{user}/versions/latest"
    return _sm.access_secret_version(name=name).payload.data.decode()


def _users() -> list[str]:
    """garmin-token-* secrets = connected users; GARMIN_USERS env overrides."""
    env = [u.strip() for u in os.environ.get("GARMIN_USERS", "").split(",") if u.strip()]
    if env:
        return env
    out = []
    for s in _sm.list_secrets(parent=f"projects/{PROJECT}"):
        short = s.name.split("/")[-1]
        if short.startswith("garmin-token-"):
            out.append(short[len("garmin-token-"):])
    return out


def _int(x):
    """Coerce Garmin's float-ish numbers to int; anything else -> None (NULL)."""
    return int(round(x)) if isinstance(x, (int, float)) else None


def _ts(local: str | None) -> str | None:
    """Parse Garmin's 'startTimeLocal' ('2026-07-19 01:30:00', already local
    to the device's timezone) into a naive timestamp string."""
    if not local:
        return None
    try:
        return dt.datetime.strptime(local[:19], "%Y-%m-%d %H:%M:%S").isoformat()
    except Exception:
        return None


def _row(user: str, a: dict) -> dict | None:
    """One Garmin activity -> one garmin_activities row.
    Requires an id and a start time; everything else may be NULL."""
    activity_id = a.get("activityId")
    start = _ts(a.get("startTimeLocal"))
    if activity_id is None or start is None:
        return None
    duration = _int(a.get("duration"))
    # Garmin gives start + duration; derive the end timestamp ourselves so the
    # dashboard can draw an exercise window without doing date math.
    end = None
    if duration is not None:
        end = (dt.datetime.fromisoformat(start) + dt.timedelta(seconds=duration)).isoformat()
    return {
        "user_id": user,
        "activity_id": int(activity_id),
        "activity_type": (a.get("activityType") or {}).get("typeKey"),   # e.g. "running"
        "activity_name": a.get("activityName"),
        "start_ts": start,
        "end_ts": end,
        "duration_seconds": duration,
        "distance_m": a.get("distance"),
        "calories": _int(a.get("calories")),
        "avg_hr": _int(a.get("averageHR")),
        "max_hr": _int(a.get("maxHR")),
        "raw": json.dumps(a),
    }


def _upsert(rows: list[dict]) -> None:
    """Idempotent by activity_id: delete the ids we're about to write, then load."""
    ids = [r["activity_id"] for r in rows]
    _bq.query(
        f"DELETE FROM `{TABLE}` WHERE activity_id IN UNNEST(@ids)",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter("ids", "INT64", ids)]),
    ).result()
    _bq.load_table_from_json(
        rows, TABLE,
        job_config=bigquery.LoadJobConfig(write_disposition="WRITE_APPEND"),
    ).result()


@functions_framework.http
def garmin_activity_sync(request):
    days = int(request.args.get("days", DAYS_BACK))
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    out: dict[str, object] = {}
    for user in _users():
        try:
            g = Garmin()
            g.login(_token(user))
            activities = g.get_activities_by_date(start.isoformat(), end.isoformat())
            # `:=` (the "walrus" operator) assigns _row's result to r *inside*
            # the comprehension, so we can build the row and filter out the
            # Nones in a single pass.
            rows = [r for a in activities if (r := _row(user, a)) is not None]
            if rows:
                _upsert(rows)
            out[user] = {"activities": len(rows)}
        except Exception as exc:  # one user's failure must not abort the rest
            msg = f"error: {type(exc).__name__}: {exc}"
            print(f"[garmin-activity-sync] {user}: {msg}", file=sys.stderr)
            out[user] = msg
    status = "ok" if not any(isinstance(v, str) for v in out.values()) else "partial"
    return (json.dumps({"status": status, "start": start.isoformat(),
                        "end": end.isoformat(), "per_user": out}),
            200, {"Content-Type": "application/json"})
