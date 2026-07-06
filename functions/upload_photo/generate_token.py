"""
One-time local script: runs the OAuth consent flow and writes token.json.
Run this before deploying the cloud function.

Usage:
    python generate_token.py --secret client_secret_<...>.json
"""

import argparse
import json
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
]


def main():
    parser = argparse.ArgumentParser()
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

    flow = InstalledAppFlow.from_client_secrets_file(args.secret, SCOPES)
    creds = flow.run_local_server(port=0)

    with open(args.output, "w") as f:
        f.write(creds.to_json())

    print(f"Token saved to {args.output}")
    print("Next step: upload token.json to GCP Secret Manager, then deploy main.py.")


if __name__ == "__main__":
    main()
