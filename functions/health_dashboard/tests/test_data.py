import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import data  # noqa: E402


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
