import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import data  # noqa: E402


def test_user_param_accepts_every_known_subject():
    """_user_param is the single choke point between the UI's subject picker
    and BigQuery — every loader routes its @user_id through it. All three
    real subjects must pass, and the bind parameter must carry the id
    verbatim (BigQuery matches it against the user_id column exactly)."""
    for subject in data.SUBJECTS:
        param = data._user_param(subject)
        assert param.value == subject


def test_user_param_rejects_anything_not_in_allowlist():
    """Security regression test: the picker is untrusted input on a public
    page, so anything outside SUBJECTS must raise BEFORE a query parameter is
    built — including the classic typo ("vince"), empty string, None, and an
    injection-shaped string (harmless as a bind parameter, but it must still
    never reach one)."""
    for bad in ["vince", "", None, "kevin' OR 1=1--", "KEVIN"]:
        with pytest.raises(ValueError):
            data._user_param(bad)


def test_load_meal_image_bytes_none_for_nan_gcs_uri():
    """Regression test: meals logged by text (not photo) store gcs_uri as SQL
    NULL, which pandas surfaces as float('nan') — not None or ''. A bare
    `not gcs_uri` check missed this (nan is truthy) and crashed on
    .startswith(). Exercises only the pre-network guard clause, so it needs
    no BigQuery/GCS credentials."""
    assert data.load_meal_image_bytes(float("nan")) is None


def test_load_meal_image_bytes_none_for_none():
    assert data.load_meal_image_bytes(None) is None


def test_load_meal_image_bytes_none_for_non_gs_string():
    assert data.load_meal_image_bytes("https://example.com/photo.jpg") is None
