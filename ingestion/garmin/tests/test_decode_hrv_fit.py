"""Tests for decode_hrv_fit.py's zip-walking logic.

We can't easily construct a real FIT binary in a test (fitparse is
decode-only, no encoder), so these tests stick to what's testable without
one: finding .fit files inside a zip, including one nested inside another
zip, using dummy byte content that doesn't need to be valid FIT — the
function under test never looks inside the bytes, only at file names.
Actual FIT parsing (scan/extract) only gets exercised by hand against a
real Garmin export zip, once someone has one.
"""

import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decode_hrv_fit import _iter_fit_blobs  # noqa: E402


def _make_zip(path: Path, entries: dict[str, bytes]) -> None:
    """Write a zip at `path` containing each name -> bytes pair in `entries`."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


def test_finds_fit_files_top_level(tmp_path):
    zip_path = tmp_path / "export.zip"
    _make_zip(zip_path, {
        "DI_CONNECT/wellness/2026-08-09.fit": b"fit-bytes-1",
        "DI_CONNECT/wellness/notes.txt": b"not a fit file",
    })
    found = dict(_iter_fit_blobs(zip_path))
    assert found == {"DI_CONNECT/wellness/2026-08-09.fit": b"fit-bytes-1"}


def test_case_insensitive_extension(tmp_path):
    zip_path = tmp_path / "export.zip"
    _make_zip(zip_path, {"DEVICE/UPPER.FIT": b"fit-bytes-2"})
    found = dict(_iter_fit_blobs(zip_path))
    assert found == {"DEVICE/UPPER.FIT": b"fit-bytes-2"}


def test_recurses_into_nested_zip(tmp_path):
    # Build the inner zip in memory first, then embed its bytes as one
    # entry of the outer zip — mirrors how Garmin nests a sub-export.
    inner_buf = io.BytesIO()
    with zipfile.ZipFile(inner_buf, "w") as inner_zf:
        inner_zf.writestr("nested/night.fit", b"fit-bytes-nested")

    zip_path = tmp_path / "export.zip"
    with zipfile.ZipFile(zip_path, "w") as outer_zf:
        outer_zf.writestr("DI_CONNECT/sub_export.zip", inner_buf.getvalue())

    found = dict(_iter_fit_blobs(zip_path))
    assert found == {"DI_CONNECT/sub_export.zip!nested/night.fit": b"fit-bytes-nested"}


def test_empty_zip_yields_nothing(tmp_path):
    zip_path = tmp_path / "export.zip"
    _make_zip(zip_path, {"readme.txt": b"hello"})
    assert list(_iter_fit_blobs(zip_path)) == []
