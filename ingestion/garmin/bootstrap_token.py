"""One-time Garmin login -> store the auth token in Secret Manager.

Run once per person to connect their Garmin account. Their password is only
used for this single login and is never stored; only the resulting token
(which auto-refreshes for ~1 year) is saved, as garmin-token-<user_id>.

Usage:
    python ingestion/garmin/bootstrap_token.py <user_id>
    # e.g.  python ingestion/garmin/bootstrap_token.py christian

You'll be prompted for the Garmin email + password, and an MFA code if that
account has two-factor enabled. Requires gcloud to be authenticated (it is).
"""

import getpass
import subprocess
import sys

from garminconnect import Garmin


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python bootstrap_token.py <user_id>   (e.g. christian)")
    user = sys.argv[1].strip().lower()

    email = input(f"[{user}] Garmin email: ").strip()
    password = getpass.getpass(f"[{user}] Garmin password: ")

    g = Garmin(email, password,
               prompt_mfa=lambda: input("MFA code (press Enter if none): ").strip())
    print("Logging in to Garmin…")
    g.login()                          # fresh login (handles MFA)
    token = g.client.dumps()           # serialized session (the thing we store)

    secret = f"garmin-token-{user}"
    # Create the secret (or add a new version if it already exists). The token
    # is piped via stdin so it is never printed to the terminal.
    created = subprocess.run(
        ["gcloud", "secrets", "create", secret,
         "--data-file=-", "--replication-policy=automatic"],
        input=token.encode(), capture_output=True)
    if created.returncode != 0:
        subprocess.run(
            ["gcloud", "secrets", "versions", "add", secret, "--data-file=-"],
            input=token.encode(), check=True)

    print(f"\n✅ Stored token as Secret Manager secret: {secret}")
    print(f"   Next: add '{user}' to GARMIN_USERS on the sync functions "
          f"(your operator can do this), then backfill.")


if __name__ == "__main__":
    main()
