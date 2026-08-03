"""Per-subject password gate for the dashboard.

Kevin's view is open — this page has always been his own data, published on
purpose. Every other subject is gated behind a password, because the Cloud Run
service is deployed with --allow-unauthenticated: without this module, picking
"christian" in the header would hand a stranger someone else's CGM traces,
meal photos, and blood pressure.

Each protected subject gets its own password, carried in its own env var
(DASHBOARD_PASSWORD_CHRISTIAN, DASHBOARD_PASSWORD_VINCENT). Set them on the
Cloud Run service, never in code or git. deploy.yml passes no --set-env-vars,
so a code push preserves whatever is already configured.

Design choices worth knowing:
  open by exception — OPEN_SUBJECTS lists who needs NO password, so a subject
      added to data.SUBJECTS later is protected by default. The reverse
      (listing who IS protected) would silently publish anyone forgotten.
  fail closed — a protected subject whose env var is missing stays locked and
      says so, rather than falling open. A forgotten variable on a fresh
      service must break the page, not republish the data.
  constant-time compare — hmac.compare_digest instead of ==, so a wrong guess
      takes the same time no matter how many leading characters were right
      (== short-circuits, which leaks timing information).
  per-subject session state — Streamlit gives each browser tab its own
      session_state dict that survives reruns. Unlocking is stored under a
      per-subject key, so the password for one subject does not also open
      the others.
"""

import hmac
import os

import streamlit as st

# Subjects that render without a password. Everyone else is gated — see
# "open by exception" in the module docstring.
OPEN_SUBJECTS = {"kevin"}


def is_protected(subject: str) -> bool:
    """Whether this subject needs a password before anything renders."""
    return subject not in OPEN_SUBJECTS


def env_var_for(subject: str) -> str:
    """Env var holding one subject's password, e.g. christian ->
    DASHBOARD_PASSWORD_CHRISTIAN. Upper-cased because env var names are
    conventionally shouted, and the subject ids are already lowercase."""
    return f"DASHBOARD_PASSWORD_{subject.upper()}"


def expected_password(subject: str) -> str | None:
    """That subject's configured password, or None when absent/empty (both
    mean 'not configured' — an empty password would let anyone in with a
    blank box)."""
    value = os.environ.get(env_var_for(subject))
    return value if value else None


def password_matches(entered: str, expected: str) -> bool:
    """Constant-time equality check (see module docstring for why not ==).
    compare_digest wants same-type arguments; encode() normalizes both to
    bytes so unicode input can't raise."""
    return hmac.compare_digest(entered.encode(), expected.encode())


def session_key(subject: str) -> str:
    """Where a successful unlock is remembered for this browser session.
    Keyed per subject so unlocking Christian doesn't also unlock Vincent."""
    return f"authed_{subject}"


def gate(subject: str) -> bool:
    """Render the password prompt until the viewer unlocks this subject.

    Returns True when the page may render. app.py calls st.stop() on False,
    which halts the script at that line, so nothing below the gate ever runs
    — queries included. A viewer who hasn't entered the password triggers
    zero BigQuery reads for that subject.
    """
    if not is_protected(subject):
        return True

    expected = expected_password(subject)
    if expected is None:
        # Fail closed: unconfigured means locked, not open.
        st.error(f"{env_var_for(subject)} is not set on this service — "
                 f"{subject.title()}'s data is locked until an operator "
                 "configures it.")
        return False

    if st.session_state.get(session_key(subject)):
        return True

    st.info(f"{subject.title()}'s data is password-protected.")
    # A per-subject widget key keeps each subject's box independent; without
    # it Streamlit would reuse one widget's typed value across subjects.
    entered = st.text_input("Password", type="password", key=f"pw_{subject}")
    if entered and password_matches(entered, expected):
        st.session_state[session_key(subject)] = True
        # Rerun so the page redraws without the password box at the top.
        st.rerun()
    elif entered:
        # Only complain once something was actually typed — a fresh visit
        # shows a clean prompt, not an error.
        st.error("Wrong password.")
    return False
