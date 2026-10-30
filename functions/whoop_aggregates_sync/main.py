"""whoop-aggregates-sync: thin HTTP wrapper around sync.sync_user.

Unlike functions/whoop_sync/ (no Cloud Scheduler job — see that function's
docstring for why), THIS one is meant to run on a normal Scheduler
cadence, because WHOOP's official OAuth2 app flow gives it a refresh token
that actually works. Set up once, manually (no script here provisions
Scheduler jobs — confirmed nothing in this repo's infra/ does that for any
function; it's always a one-off `gcloud scheduler jobs create`, same as
every other sync function's schedule):

    gcloud scheduler jobs create http whoop-aggregates-sync \\
      --schedule="0 */6 * * *" --uri="<function-url>?days=2" \\
      --http-method=GET \\
      --oidc-service-account-email=twin-pipeline@digitaltwin-499202.iam.gserviceaccount.com

?days=N overrides the default 7-day lookback (generous on purpose — the
idempotent delete-then-load in sync.py makes re-fetching already-synced
days harmless).
?user=<slug> syncs just that one person; omitted syncs everyone with a
whoop-oauth-<user> secret.
"""

from __future__ import annotations

import json
import sys

import functions_framework
import requests
from google.cloud import secretmanager

import aggregates_sync as sync

DAYS_BACK_DEFAULT = 7

_sm = secretmanager.SecretManagerServiceClient()


def _client_credentials() -> tuple[str, str]:
    """The WHOOP app's own client_id/client_secret (one pair, shared by
    every user) — stored as whoop-oauth-client, {"client_id", "client_secret"}."""
    name = f"projects/{sync.PROJECT}/secrets/whoop-oauth-client/versions/latest"
    body = json.loads(_sm.access_secret_version(name=name).payload.data.decode())
    return body["client_id"], body["client_secret"]


def _load_tokens(user: str) -> dict:
    name = f"projects/{sync.PROJECT}/secrets/whoop-oauth-{user}/versions/latest"
    return json.loads(_sm.access_secret_version(name=name).payload.data.decode())


def _save_tokens(user: str, tokens: dict) -> None:
    """Persist the (possibly refreshed) tokens. Called immediately after
    sync_user returns — even on a downstream failure — because refreshing
    invalidates the previous refresh token; losing a freshly-rotated one
    would lock the user out until they reconnect via meal-web."""
    parent = f"projects/{sync.PROJECT}/secrets/whoop-oauth-{user}"
    _sm.add_secret_version(parent=parent, payload={"data": json.dumps(tokens).encode()})


def _users() -> list[str]:
    """Auto-discover users with a connected WHOOP OAuth account via
    whoop-oauth-* secrets (same pattern as omron_sync._users, and distinct
    from whoop_sync's whoop-token-* — these two pipelines never overlap)."""
    out = []
    for s in _sm.list_secrets(parent=f"projects/{sync.PROJECT}"):
        short = s.name.split("/")[-1]
        if short.startswith("whoop-oauth-") and short != "whoop-oauth-client":
            out.append(short[len("whoop-oauth-"):])
    return out


@functions_framework.http
def whoop_aggregates_sync(request):
    days = int(request.args.get("days", DAYS_BACK_DEFAULT))
    only_user = request.args.get("user")
    users = [only_user] if only_user else _users()
    client_id, client_secret = _client_credentials()

    out: dict[str, object] = {}
    for user in users:
        try:
            tokens = _load_tokens(user)
            refreshed = sync.refresh_if_needed(tokens, client_id, client_secret)
            if refreshed is not tokens:   # refresh_if_needed returns the same
                _save_tokens(user, refreshed)   # object untouched when no refresh
                tokens = refreshed              # happened — skip writing a
                                                 # no-op secret version every run
            result = sync.sync_user_data(user, tokens["access_token"], days)
            out[user] = result
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            msg = ("token expired or revoked — reconnect via meal-web's /connect-whoop/<token>"
                  if status == 401 else f"error: HTTP {status}")
            print(f"[whoop-aggregates-sync] {user}: {msg}", file=sys.stderr)
            out[user] = msg
        except Exception as exc:   # one user's failure must not abort the rest
            msg = f"error: {type(exc).__name__}: {exc}"
            print(f"[whoop-aggregates-sync] {user}: {msg}", file=sys.stderr)
            out[user] = msg

    status = "ok" if not any(isinstance(v, str) for v in out.values()) else "partial"
    return (json.dumps({"status": status, "days": days, "per_user": out}),
            200, {"Content-Type": "application/json"})
