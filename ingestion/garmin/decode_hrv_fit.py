"""Decode raw beat-to-beat RR intervals (IBI) out of a Garmin "Export Your
Data" zip, for training the sleep-stage HMM on real inter-beat data instead
of the 5-min-aggregated hrv_value we already have in BigQuery (see
garmin_sync/main.py's _hrv_readings).

WHERE THE ZIP COMES FROM (manual, one per person, not automatable):
    Garmin Connect website -> Account Settings -> Data Management ->
    Export Your Data. Garmin emails a download link after some hours; the
    zip contains that person's whole account history, including raw device
    FIT files buried under DI_CONNECT/... subfolders.

WHY THIS IS A SEPARATE SCRIPT FROM garmin_sync: garmin_sync talks to the
*Garmin Connect Cloud API*, which only exposes activity FIT files (by
activity_id) and the pre-aggregated wellness endpoints. Overnight raw RR
intervals are a Firstbeat-engine feature ("All-day HRV") that, if a watch
logs it at all, only shows up in the FIT files inside this manual export —
there is no API endpoint for it. This script never touches the network; it
only reads a zip already sitting on disk.

USAGE:
    # First run: see what's actually in the export before assuming anything.
    # We don't know this Garmin data structure firsthand yet, so `scan`
    # exists specifically to answer "does this watch even record RR
    # intervals, and where do the files live" before writing extraction
    # logic that guesses wrong.
    python ingestion/garmin/decode_hrv_fit.py scan <export.zip>

    # Once scan confirms 'hrv' messages exist, pull them into a CSV:
    python ingestion/garmin/decode_hrv_fit.py extract <export.zip> <user_id> <out.csv>

FIT FORMAT BACKGROUND (why the code below looks the way it does):
    A FIT file is a binary stream of typed "messages" (file_id, hrv,
    monitoring, event, ...). fitparse turns each into a Python object whose
    .get_value("field_name") reads one field. The message we care about is
    "hrv" (Garmin's Firstbeat RR-interval log): each one carries a `time`
    field that is a LIST of up to 5 RR intervals in seconds, not one value.
    There's no per-sample timestamp inside an hrv message — only the
    file's own start time (from the file_id message) tells you when
    recording began. So sample N's clock time is reconstructed by summing
    every RR interval before it and adding that offset to the file start.
    This reconstructed timeline drifts by whatever the RR intervals
    themselves are off by, which is negligible over one night for our
    purposes (we need sleep-stage-scale resolution, not medical-grade sync).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import sys
import zipfile
from pathlib import Path

import fitparse

# Garmin's export nests FIT files arbitrarily deep, sometimes inside a
# second zip-within-the-zip. Case-insensitive because zip tooling on
# different OSes has stored ".FIT" and ".fit" inconsistently in the wild.
_FIT_SUFFIX = ".fit"


def _iter_fit_blobs(zip_path: Path):
    """Yield (path_label, raw_bytes) for every .fit file found anywhere in
    the export zip, recursing into nested zips.

    A generator, not a list: exports can be large (years of daily FIT
    files), so we hand each blob to the caller one at a time instead of
    holding everything in memory at once.
    """
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename
            if name.lower().endswith(_FIT_SUFFIX):
                yield name, zf.read(info)
            elif name.lower().endswith(".zip"):
                # Nested export zip (Garmin does this for some data
                # categories) — recurse using the same in-memory bytes,
                # no temp file needed.
                nested = io.BytesIO(zf.read(info))
                with zipfile.ZipFile(nested) as inner:
                    for inner_info in inner.infolist():
                        if inner_info.filename.lower().endswith(_FIT_SUFFIX):
                            yield (f"{name}!{inner_info.filename}",
                                   inner.read(inner_info))


def _file_start(fit: fitparse.FitFile) -> dt.datetime | None:
    """The FIT file's own recorded start time, from its mandatory file_id
    message. Returns None if the message or field is missing rather than
    raising — a file we can't timestamp is a file we skip, not a crash."""
    for msg in fit.get_messages("file_id"):
        ts = msg.get_value("time_created")
        if isinstance(ts, dt.datetime):
            return ts
    return None


def scan(zip_path: Path) -> None:
    """Report what's actually inside the export: how many FIT files, what
    message types each contains, and whether any carry raw 'hrv' (RR
    interval) data. Run this FIRST on any new export — we're inferring the
    file layout and message names from FIT-format docs and community
    reverse-engineering, not from a Garmin spec we've verified against this
    account's actual watch, so trust this output over the assumptions in
    the module docstring above."""
    total_files = 0
    hrv_files = 0
    total_hrv_samples = 0
    message_type_counts: dict[str, int] = {}

    for label, blob in _iter_fit_blobs(zip_path):
        total_files += 1
        try:
            fit = fitparse.FitFile(io.BytesIO(blob))
            # Force full parse now (fitparse is lazy) so a corrupt file
            # raises here, inside the try, instead of mid-iteration below.
            fit.parse()
        except Exception as exc:
            print(f"  [unparseable] {label}: {exc}")
            continue

        types_here = set()
        for msg in fit.messages:
            message_type_counts[msg.name] = message_type_counts.get(msg.name, 0) + 1
            types_here.add(msg.name)

        if "hrv" in types_here:
            hrv_files += 1
            n = sum(len(m.get_value("time") or [])
                     for m in fit.get_messages("hrv"))
            total_hrv_samples += n
            start = _file_start(fit)
            print(f"  [hrv] {label}: {n} RR samples, "
                  f"starts {start.isoformat() if start else 'unknown'}")

    print(f"\n{total_files} FIT files found, {hrv_files} contain 'hrv' "
          f"messages, {total_hrv_samples} total RR samples.")
    print("\nAll message types seen across the export (helps spot where "
          "sleep-adjacent data actually lives if 'hrv' turns out empty):")
    for name, count in sorted(message_type_counts.items(),
                               key=lambda kv: -kv[1]):
        print(f"  {name}: {count}")


def extract(zip_path: Path, user_id: str, out_path: Path) -> None:
    """Pull every RR interval out of every 'hrv' FIT message in the export
    into one CSV: user_id, source_file, ts (reconstructed wall-clock time
    of that beat), rr_ms. This is the file the HMM training script reads —
    one row per heartbeat interval, not per 5-min aggregate."""
    rows_written = 0
    with out_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["user_id", "source_file", "ts", "rr_ms"])

        for label, blob in _iter_fit_blobs(zip_path):
            try:
                fit = fitparse.FitFile(io.BytesIO(blob))
                fit.parse()
            except Exception as exc:
                print(f"  [skip, unparseable] {label}: {exc}", file=sys.stderr)
                continue

            start = _file_start(fit)
            if start is None:
                continue  # can't place these beats on a timeline, so skip

            # Running clock, advanced by each RR interval as we consume it —
            # this is the "reconstruct the timeline" step from the module
            # docstring. elapsed_s accumulates in seconds (FIT's native
            # unit for this field); rr_ms is what we actually store, since
            # HRV work conventionally reports RR/IBI in milliseconds.
            elapsed_s = 0.0
            for msg in fit.get_messages("hrv"):
                for rr_s in (msg.get_value("time") or []):
                    if rr_s is None:
                        continue  # Garmin pads short batches with None
                    ts = start + dt.timedelta(seconds=elapsed_s)
                    writer.writerow([user_id, label, ts.isoformat(),
                                      round(rr_s * 1000, 1)])
                    elapsed_s += rr_s
                    rows_written += 1

    print(f"Wrote {rows_written} RR-interval rows to {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)

    p_scan = sub.add_parser("scan", help="inspect an export zip's contents")
    p_scan.add_argument("zip_path", type=Path)

    p_extract = sub.add_parser("extract", help="pull RR intervals into a CSV")
    p_extract.add_argument("zip_path", type=Path)
    p_extract.add_argument("user_id")
    p_extract.add_argument("out_path", type=Path)

    args = parser.parse_args()
    if args.mode == "scan":
        scan(args.zip_path)
    else:
        extract(args.zip_path, args.user_id, args.out_path)


if __name__ == "__main__":
    main()
