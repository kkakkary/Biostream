"""One-time-per-login WHOOP sync: paste in a freshly captured token, this
backfills the last N days of heart rate immediately, in-process.

WHOOP has no usable token-refresh flow (see functions/whoop_sync/sync.py's
docstring and ingestion/whoop/probe_metrics.py, which both document the
exhaustive live testing that confirmed this) — a token is only good for
about a day after logging into app.whoop.com by hand. So unlike
ingestion/garmin/bootstrap_token.py (run once, ever, per person), this
script is meant to be rerun every time the subject logs back in: it saves
the fresh token AND immediately triggers a backfill in the same run, rather
than waiting on a Cloud Scheduler timer that would spend most of its
invocations failing against an already-dead token.

The default 7-day lookback is generous on purpose: sync_user()'s
delete-then-load is idempotent, so re-fetching already-synced days is
harmless — if a login gets skipped for a day or two, the next run's backfill
just self-heals the gap instead of permanently losing it.

One-time setup, in your own browser (same steps as probe_metrics.py):
  1. Log into https://app.whoop.com
  2. DevTools -> Network tab, find a request to api.prod.whoop.com whose
     JSON response has a top-level "user" object; copy its "id".
  3. DevTools -> Application -> Cookies -> https://app.whoop.com -> copy
     the value of the cookie "whoop-auth-token".

Usage:
    python ingestion/whoop/login_and_sync.py <user_slug> [--days 7]
    # e.g. python ingestion/whoop/login_and_sync.py christian
"""

import argparse
import getpass
import json
import subprocess
import sys
from pathlib import Path

# Import the shared sync logic directly — no HTTP round-trip to the deployed
# function, since this script already holds the fresh token in memory and
# already has BigQuery access via the operator's own gcloud ADC.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "functions" / "whoop_sync"))
import sync  # noqa: E402


def _save_token(user: str, whoop_user_id: str, access_token: str) -> None:
    """Write {"user_id", "access_token"} to Secret Manager as
    whoop-token-<user>. Same create-or-add-version subprocess pattern as
    ingestion/garmin/bootstrap_token.py — piped via stdin so the token is
    never printed to the terminal or left in shell history."""
    secret = f"whoop-token-{user}"
    payload = json.dumps({"user_id": whoop_user_id, "access_token": access_token}).encode()
    created = subprocess.run(
        ["gcloud", "secrets", "create", secret,
         "--data-file=-", "--replication-policy=automatic"],
        input=payload, capture_output=True)
    if created.returncode != 0:   # non-zero = it already exists
        subprocess.run(
            ["gcloud", "secrets", "versions", "add", secret, "--data-file=-"],
            input=payload, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("user", help="subject slug, e.g. christian")
    parser.add_argument("--days", type=int, default=7,
                        help="how many days back to backfill (default 7)")
    args = parser.parse_args()
    user = args.user.strip().lower()

    whoop_user_id = input(f"[{user}] WHOOP user id (from the login response's \"user.id\"): ").strip()
    access_token = getpass.getpass(f"[{user}] WHOOP access token (input hidden): ").strip()

    print(f"\nSaving token for {user}...")
    _save_token(user, whoop_user_id, access_token)

    print(f"Backfilling last {args.days} days of heart rate for {user}...")
    try:
        rows = sync.sync_user(user, whoop_user_id, access_token, args.days)
        print(f"\n✅ Synced {rows} heart-rate readings for {user}.")
    except Exception as exc:
        print(f"\n❌ Sync failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("Token is saved regardless — if this was a bad/expired token, "
              "recapture it and rerun this script.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
