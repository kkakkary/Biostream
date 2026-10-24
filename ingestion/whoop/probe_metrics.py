"""One-off probe: check the real granularity of WHOOP's unofficial
metrics-service heart-rate endpoint before building anything on it.

This hits an UNDOCUMENTED, reverse-engineered endpoint (not the public
developer.whoop.com API) that WHOOP's own web app calls internally. It may
change or break without notice, and WHOOP could rate-limit or flag the
account — this script exists to answer one question (what resolution does
step=6 actually return, and does it cover a full day or only
activity/sleep windows?), not to become a permanent ingestion path.

Nothing here is stored: the access token is entered via a no-echo prompt,
held only in memory for this run's HTTP requests, and never written to
disk, logged, or committed. Run this in your OWN terminal — don't paste the
token into a chat, since a getpass prompt can't be driven that way anyway.

One-time setup, in your own browser:
  1. Log into https://app.whoop.com
  2. DevTools -> Network tab, reload the page, find a request to
     api.prod.whoop.com whose JSON response has a top-level "user" object;
     copy its "id" (a plain number — not secret, but personal).
  3. DevTools -> Application -> Local Storage -> https://app.whoop.com ->
     copy the value of the key "security.accessToken" (this IS secret).

Usage:
    python ingestion/whoop/probe_metrics.py [--date YYYY-MM-DD] [--step 6]
                                            [--metric heart_rate] [--save]

--date defaults to today (UTC calendar day), capped at the current moment
rather than end-of-day, since today isn't over yet. Pass an earlier date to
probe a full finished day instead.
--save writes the raw response to ingestion/whoop/data/ (gitignored) so you
can look at it more closely later without re-querying.
"""

import argparse
import datetime as dt
import getpass
import json
import statistics
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_HOST = "https://api.prod.whoop.com"
OUT_DIR = Path(__file__).parent / "data"


def _iso_ms(epoch_ms: int) -> str:
    """Epoch milliseconds -> "2026-10-21T00:00:00.000Z". WHOOP's documented
    public endpoints all take start/end as ISO-8601, not raw epoch integers
    — a bare integer here is the likely cause of "Start or end invalid",
    even though this endpoint's response timestamps come back as epoch-ms."""
    return (dt.datetime.fromtimestamp(epoch_ms / 1000, tz=dt.timezone.utc)
           .strftime("%Y-%m-%dT%H:%M:%S.") + f"{epoch_ms % 1000:03d}Z")


def fetch_metric(token: str, user_id: str, metric: str,
                 start_ms: int, end_ms: int, step: int) -> dict:
    params = {
        "name": metric,
        "start": _iso_ms(start_ms),
        "end": _iso_ms(end_ms),
        "step": step,
        "order": "t",
        "apiVersion": 7,
    }
    url = (f"{API_HOST}/metrics-service/v1/metrics/user/{user_id}"
          f"?{urllib.parse.urlencode(params)}")
    # api.prod.whoop.com sits behind Cloudflare's Browser Integrity Check,
    # which 403s (error 1010) a bare urllib/requests call on header and JS
    # fingerprint alone — nothing to do with the token. These headers mimic
    # what app.whoop.com's own fetch() call sends; if Cloudflare is instead
    # fingerprinting the TLS handshake (JA3), no header set fixes that and
    # a TLS-impersonating client (e.g. curl_cffi) would be the next step.
    headers = {
        # Capital "Bearer" — confirmed directly off a real authenticated
        # request in this account's own devtools, overriding the
        # lowercase "bearer" the third-party reverse-engineering notes claimed.
        "authorization": f"Bearer {token}",
        "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/129.0.0.0 Safari/537.36"),
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "origin": "https://app.whoop.com",
        "referer": "https://app.whoop.com/",
        "sec-fetch-site": "same-site",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise SystemExit(f"HTTP {e.code} from WHOOP: {body[:500]}")


def summarize(payload: dict) -> None:
    values = payload.get("values", [])
    print(f"\nmetric: {payload.get('name')}")
    print(f"points returned: {len(values)}")
    if len(values) < 2:
        print("Not enough points to measure spacing — empty or near-empty response.")
        return

    times = [v["time"] for v in values]
    deltas_s = [(b - a) / 1000 for a, b in zip(times, times[1:])]
    span_start = dt.datetime.fromtimestamp(times[0] / 1000, tz=dt.timezone.utc)
    span_end = dt.datetime.fromtimestamp(times[-1] / 1000, tz=dt.timezone.utc)
    median = statistics.median(deltas_s)

    print(f"span: {span_start} -> {span_end} UTC")
    print(f"delta seconds -- min: {min(deltas_s):.1f}  "
          f"median: {median:.1f}  max: {max(deltas_s):.1f}")

    # Gaps much wider than the typical spacing are the interesting case:
    # either the strap was off-wrist, or (if this turns out to be true)
    # fine resolution only exists during workouts/sleep, not all day.
    gaps = [(a, b) for a, b in zip(times, times[1:]) if (b - a) / 1000 > median * 5]
    print(f"gaps > 5x median spacing: {len(gaps)}")
    for a, b in gaps[:5]:
        ga = dt.datetime.fromtimestamp(a / 1000, tz=dt.timezone.utc)
        gb = dt.datetime.fromtimestamp(b / 1000, tz=dt.timezone.utc)
        print(f"  gap: {ga} -> {gb} UTC  ({(b - a) / 1000:.0f}s)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="YYYY-MM-DD (UTC calendar day); default yesterday")
    parser.add_argument("--step", type=int, default=6)
    parser.add_argument("--metric", default="heart_rate")
    parser.add_argument("--save", action="store_true",
                        help="save the raw response under ingestion/whoop/data/ (gitignored)")
    args = parser.parse_args()

    if args.date:
        day = dt.datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    else:
        day = dt.datetime.now(dt.timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0)
    # Cap the end at "now" rather than end-of-day — a day that hasn't
    # finished yet (today, by default) shouldn't be queried past the
    # current moment, and this is harmless for a fully past day too (the
    # min() just falls back to the normal end-of-day in that case).
    now = dt.datetime.now(dt.timezone.utc)
    start_ms = int(day.timestamp() * 1000)
    end_ms = int(min(day + dt.timedelta(days=1), now).timestamp() * 1000)

    user_id = input("WHOOP user id (from the login response's \"user.id\"): ").strip()
    token = getpass.getpass(
        "WHOOP access token (security.accessToken — input hidden): ").strip()

    print(f"\nRequesting {args.metric} for {day.date()} UTC, step={args.step}...")
    payload = fetch_metric(token, user_id, args.metric, start_ms, end_ms, args.step)
    summarize(payload)

    if args.save:
        OUT_DIR.mkdir(exist_ok=True)
        out_path = OUT_DIR / f"{args.metric}_{day.date()}_step{args.step}.json"
        out_path.write_text(json.dumps(payload, indent=2))
        print(f"\nSaved raw response to {out_path}")


if __name__ == "__main__":
    main()
