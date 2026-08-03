import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import auth  # noqa: E402
import data  # noqa: E402


def test_kevin_is_open_and_others_are_protected():
    """The whole point of the module: Kevin's data was already published on
    purpose, everyone else needs a password."""
    assert not auth.is_protected("kevin")
    assert auth.is_protected("christian")
    assert auth.is_protected("vincent")


def test_every_subject_except_kevin_is_protected():
    """Fail-safe default: a subject added to data.SUBJECTS later must be
    gated automatically. If someone adds a fourth subject and this test goes
    red, the fix is a password for them — not an entry in OPEN_SUBJECTS."""
    for subject in data.SUBJECTS:
        assert auth.is_protected(subject) == (subject != "kevin")


def test_env_var_name_per_subject():
    assert auth.env_var_for("christian") == "DASHBOARD_PASSWORD_CHRISTIAN"
    assert auth.env_var_for("vincent") == "DASHBOARD_PASSWORD_VINCENT"


def test_expected_password_none_when_unset(monkeypatch):
    """Fail-closed contract: no env var -> None -> gate() refuses to render.
    monkeypatch.delenv undoes itself after the test, so this can't leak into
    other tests' environments."""
    monkeypatch.delenv(auth.env_var_for("christian"), raising=False)
    assert auth.expected_password("christian") is None


def test_expected_password_none_when_empty(monkeypatch):
    """An empty string must count as 'not configured' — otherwise a blank
    password box would unlock the page."""
    monkeypatch.setenv(auth.env_var_for("christian"), "")
    assert auth.expected_password("christian") is None


def test_expected_password_returns_value(monkeypatch):
    monkeypatch.setenv(auth.env_var_for("vincent"), "hunter2")
    assert auth.expected_password("vincent") == "hunter2"


def test_subjects_do_not_share_a_password(monkeypatch):
    """Each subject reads its own env var — Christian's password must not
    unlock Vincent, and an unset Vincent must not inherit Christian's."""
    monkeypatch.setenv(auth.env_var_for("christian"), "christian-pw")
    monkeypatch.delenv(auth.env_var_for("vincent"), raising=False)
    assert auth.expected_password("christian") == "christian-pw"
    assert auth.expected_password("vincent") is None


def test_session_keys_are_per_subject():
    """Unlocking one subject must not mark the others as unlocked."""
    assert auth.session_key("christian") != auth.session_key("vincent")


def test_password_matches_exact_only():
    assert auth.password_matches("hunter2", "hunter2")
    # Near misses: prefix, wrong case, empty. All must fail — the comparison
    # is exact equality, just performed in constant time.
    assert not auth.password_matches("hunter", "hunter2")
    assert not auth.password_matches("HUNTER2", "hunter2")
    assert not auth.password_matches("", "hunter2")


def test_password_matches_handles_unicode():
    """compare_digest raises on non-ASCII str input; the encode() inside
    password_matches is what keeps emoji/accented passwords from crashing."""
    assert auth.password_matches("pässwörd🔒", "pässwörd🔒")
    assert not auth.password_matches("pässwörd", "pässwörd🔒")
