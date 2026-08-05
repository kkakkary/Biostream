"""Integration tests for WHERE the auth gate sits, not just what it does.

test_auth.py proves the helpers are correct in isolation; nothing there would
notice if app.py called auth.gate() *below* a data load — every unit test
would stay green while a locked subject's rows were being fetched. Since the
deploy workflow's test job is what gates production, that hole is
load-bearing. These tests close it by driving the real app.py headlessly
with streamlit.testing.v1.AppTest and asserting on actual loader calls.

The tripwire pattern: every data.load_* is replaced with a stub that records
the user_id it was called with and returns an empty frame. The question each
test asks is behavioral, not structural — "while subject X is locked, was any
loader EVER invoked for X?" — so the tests survive refactors of app.py's
layout and only fail if the gate stops doing its job.

app.py and these tests share one process, so monkeypatching the `data`
module here patches the same object app.py imported — that's why the
tripwires take effect inside AppTest.
"""

import os
import sys
from pathlib import Path

import pandas as pd
import pytest

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import data  # noqa: E402

from streamlit.testing.v1 import AppTest  # noqa: E402

APP_FILE = str(APP_DIR / "app.py")

# Every loader app.py can reach. If a new load_* is added later, add it here —
# an unlisted loader would be a hole in the tripwire net.
LOADERS = ["load_meals", "load_glucose_window", "load_activities_window",
           "load_bp_window", "load_hrv_for_sleep_date", "load_daily",
           "load_glucose", "load_blood_pressure", "load_meal_image_bytes"]


@pytest.fixture
def calls(monkeypatch):
    """Replace every loader with a recording stub. Returns the shared call
    log; monkeypatch restores the real functions after each test."""
    log = []

    def make(name):
        def stub(*args, **kwargs):
            # First positional arg is user_id for every loader except the
            # image fetch (whose arg is a gcs_uri, logged then ignored).
            log.append((name, args[0] if args else None))
            return None if name == "load_meal_image_bytes" else pd.DataFrame()
        return stub

    for fn in LOADERS:
        monkeypatch.setattr(data, fn, make(fn))
    return log


@pytest.fixture
def passwords(monkeypatch):
    """Known passwords for both protected subjects, and nothing else — any
    stray DASHBOARD_PASSWORD_* from the outer environment is removed so tests
    can't pass or fail because of the machine they run on."""
    for k in list(os.environ):
        if k.startswith("DASHBOARD_PASSWORD_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("DASHBOARD_PASSWORD_CHRISTIAN", "chris-pw")
    monkeypatch.setenv("DASHBOARD_PASSWORD_VINCENT", "vince-pw")


def _fresh_app(calls, session=None):
    """Run app.py once (default subject: kevin, open — those queries are
    legitimate), then clear the log so a test only counts what happens after
    its own actions."""
    at = AppTest.from_file(APP_FILE, default_timeout=30)
    for key, value in (session or {}).items():
        at.session_state[key] = value
    at.run()
    calls.clear()
    return at


def _queried_users(calls):
    """The set of user_ids that actually reached a loader."""
    return {user for _, user in calls if user is not None}


def _pick_subject(at, subject):
    """The header's subject picker is the first segmented_control on the
    page (the view toggle is the second — it only exists once the gate has
    opened, which is itself part of what these tests verify)."""
    at.segmented_control[0].set_value(subject).run()
    return at


def test_locked_subject_reaches_no_loader(calls, passwords):
    at = _pick_subject(_fresh_app(calls), "christian")
    assert _queried_users(calls) == set(), "locked subject reached a query"
    assert at.text_input, "locked subject should be shown a password box"


def test_wrong_password_stays_locked(calls, passwords):
    at = _pick_subject(_fresh_app(calls), "christian")
    at.text_input[0].set_value("nope").run()
    assert _queried_users(calls) == set()
    assert any("Wrong password" in e.value for e in at.error)


def test_unset_env_var_fails_closed(calls, monkeypatch):
    """No password configured must mean locked-with-a-message, not open."""
    for k in list(os.environ):
        if k.startswith("DASHBOARD_PASSWORD_"):
            monkeypatch.delenv(k)
    at = _pick_subject(_fresh_app(calls), "christian")
    assert _queried_users(calls) == set()
    assert at.error, "fail-closed state should explain itself"


def test_correct_password_opens_the_gate(calls, passwords):
    """The positive control: without this, a gate that returns False forever
    would pass every locked-subject test above."""
    at = _pick_subject(_fresh_app(calls), "christian")
    at.text_input[0].set_value("chris-pw").run()
    assert _queried_users(calls) == {"christian"}


def test_unlocking_one_subject_does_not_unlock_another(calls, passwords):
    """Session-forgery probe: a session that already unlocked christian must
    still be locked for vincent (and vice versa — symmetric by construction,
    so one direction suffices alongside the mid-session test below)."""
    at = _pick_subject(_fresh_app(calls, session={"authed_christian": True}),
                       "vincent")
    assert _queried_users(calls) == set()
    assert at.text_input, "vincent should get his own password prompt"


def test_mid_session_switching_relocks(calls, passwords):
    """One continuous session: unlock christian, move to vincent (must
    re-lock, with an empty box — not christian's password pre-filled), come
    back to christian (stays unlocked)."""
    at = _pick_subject(_fresh_app(calls), "christian")
    at.text_input[0].set_value("chris-pw").run()
    assert _queried_users(calls) == {"christian"}

    calls.clear()
    _pick_subject(at, "vincent")
    assert _queried_users(calls) == set(), "cross-subject leak on switch"
    assert at.text_input and at.text_input[0].value != "chris-pw"

    calls.clear()
    _pick_subject(at, "christian")
    assert _queried_users(calls) == {"christian"}, \
        "christian's unlock should survive the round trip"


def test_forged_picker_values_never_reach_protected_data(calls, passwords):
    """A hostile websocket client isn't limited to the offered options. A
    forged subject id must either be rejected by the widget layer or degrade
    to the open default — never reach a protected subject's rows."""
    for forged in ["vince", "admin", "kevin ", "KEVIN", "'; DROP TABLE--"]:
        at = _fresh_app(calls)
        try:
            _pick_subject(at, forged)
        except Exception:
            continue  # widget layer refused the value outright — also safe
        assert _queried_users(calls) <= {"kevin"}, \
            f"forged value {forged!r} reached a protected subject"
