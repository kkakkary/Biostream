"""
One-time local script: runs the OAuth consent flow and writes token.json.
Run this before deploying the cloud function.

Usage:
    python generate_token.py --secret client_secret_<...>.json

What it does: opens a browser window where you approve the "add photos to my
Google Photos library" permission; Google hands back a token (including a
refresh_token), which is saved to token.json for upload to Secret Manager.
"""

import argparse   # standard-library command-line argument parsing
import json
from google_auth_oauthlib.flow import InstalledAppFlow

# Must match the scope upload_photo/main.py requests — append-only access.
SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
]


def main():
    parser = argparse.ArgumentParser()
    # NOTE: the `help` strings below are leftover Windows paths from the
    # original machine, not real help text — only the `default` values matter.
    parser.add_argument(
        "--secret",
        default="client_secret_663692868459-kpqf7ui5tetit2uutglm9vfnava58qag.apps.googleusercontent.com.json",
        help="D:\Repositories\garmin-vivoactive-data\client_secret_663692868459-kpqf7ui5tetit2uutglm9vfnava58qag.apps.googleusercontent.com.json",
    )
    parser.add_argument(
        "--output",
        default="token.json",
        help="D:\Repositories\garmin-vivoactive-data",
    )
    args = parser.parse_args()

    # Opens the browser, waits for you to approve, and receives the tokens on
    # a temporary local web server (port=0 = pick any free port).
    flow = InstalledAppFlow.from_client_secrets_file(args.secret, SCOPES)
    creds = flow.run_local_server(port=0)

    with open(args.output, "w") as f:
        f.write(creds.to_json())

    print(f"Token saved to {args.output}")
    print("Next step: upload token.json to GCP Secret Manager, then deploy main.py.")


if __name__ == "__main__":
    main()
