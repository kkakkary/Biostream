"""whoop-sync: thin HTTP wrapper around sync.sync_user for Cloud Functions.

Deliberately NOT on a Cloud Scheduler timer, unlike every other *_sync
function in this repo. WHOOP has no usable token-refresh flow (see
sync.py's docstring) — a token is only ever good for about a day after a
human logs into app.whoop.com by hand, so a timer firing every few hours
would spend most of its invocations failing against a token that's already
dead. Instead, ingestion/whoop/login_and_sync.py calls sync.sync_user()
in-process right after each manual login, and THIS endpoint exists only for
an ad-hoc manual resync (gcloud functions call) and to be ready for a
Scheduler later if WHOOP's mobile-app refresh flow is ever separately
reverse-engineered.

?days=N overrides the default 7-day lookback. It's generous on purpose: the
idempotent delete-then-load in sync.py makes re-fetching already-synced
days harmless, so a skipped login day (gap > 1 day) self-heals on the next
one instead of permanently losing data.
?user=<slug> syncs just that one person; omitted syncs everyone with a
whoop-token-<user> secret.
"""

from __future__ import annotations

import json
import sys

import functions_framework
import httpx
from google.cloud import secretmanager

import sync

DAYS_BACK_DEFAULT = 7

_sm = secretmanager.SecretManagerServiceClient()


def _load_token(user: str) -> dict:
    """Read {"user_id": <whoop numeric id>, "access_token": ...} for `user`."""
    name = f"projects/{sync.PROJECT}/secrets/whoop-token-{user}/versions/latest"
    return json.loads(_sm.access_secret_version(name=name).payload.data.decode())


def _users() -> list[str]:
    """Auto-discover users with a connected WHOOP account via whoop-token-*
    secrets (same discovery pattern as omron_sync._users/garmin_sync._users)."""
    out = []
    for s in _sm.list_secrets(parent=f"projects/{sync.PROJECT}"):
        short = s.name.split("/")[-1]
        if short.startswith("whoop-token-"):
            out.append(short[len("whoop-token-"):])
    return out


@functions_framework.http
def whoop_sync(request):
    days = int(request.args.get("days", DAYS_BACK_DEFAULT))
    only_user = request.args.get("user")
    users = [only_user] if only_user else _users()

    out: dict[str, object] = {}
    for user in users:
        try:
            tokens = _load_token(user)
            rows = sync.sync_user(user, tokens["user_id"], tokens["access_token"], days)
            out[user] = {"rows": rows}
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                msg = "token expired or invalid — rerun ingestion/whoop/login_and_sync.py"
            else:
                msg = f"error: HTTP {exc.response.status_code}"
            print(f"[whoop-sync] {user}: {msg}", file=sys.stderr)
            out[user] = msg
        except Exception as exc:   # one user's failure must not abort the rest
            msg = f"error: {type(exc).__name__}: {exc}"
            print(f"[whoop-sync] {user}: {msg}", file=sys.stderr)
            out[user] = msg

    status = "ok" if not any(isinstance(v, str) for v in out.values()) else "partial"
    return (json.dumps({"status": status, "days": days, "per_user": out}),
            200, {"Content-Type": "application/json"})
