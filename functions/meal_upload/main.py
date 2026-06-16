"""HTTP Cloud Function: meal photo -> GCS -> Gemini Vision -> BigQuery.

One call does the whole pipeline so it's trivial to invoke from an iPhone
Shortcut. Expects a multipart/form-data POST:

    image       the meal photo (required)
    user_id     which person this meal belongs to (required)
    capture_ts  ISO-8601 time the photo was taken = meal start (required)
    token       shared upload secret (required; checked against UPLOAD_TOKEN)

Returns the estimated macros as JSON so the caller can display them.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import uuid

import functions_framework
from google import genai
from google.genai import types
from google.cloud import bigquery, storage
from PIL import Image
from pydantic import BaseModel

PROJECT = os.environ["PROJECT"]
BUCKET = os.environ["BUCKET"]
BQ_DATASET = os.environ.get("BQ_DATASET", "health_twin")
BQ_TABLE = os.environ.get("BQ_TABLE", "meals")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_LOCATION = os.environ.get("GEMINI_LOCATION", "us-central1")
UPLOAD_TOKEN = os.environ.get("UPLOAD_TOKEN", "")
MAX_EDGE = 1024  # downscale long edge before storage + Gemini

# Clients are created once per instance (reused across invocations).
_storage = storage.Client(project=PROJECT)
_bq = bigquery.Client(project=PROJECT)
_genai = genai.Client(vertexai=True, project=PROJECT, location=GEMINI_LOCATION)


# --- Structured output schema Gemini must return ------------------------------
class FoodItem(BaseModel):
    food: str
    grams: float
    carbs_g: float
    protein_g: float
    fat_g: float
    fiber_g: float


class MealEstimate(BaseModel):
    is_food: bool  # false if the photo is not a meal (skip logging entirely)
    items: list[FoodItem]
    carbs_g: float
    protein_g: float
    fat_g: float
    fiber_g: float
    calories: float
    confidence: float  # 0-1, model's self-reported certainty
    notes: str


PROMPT = (
    "Look at this photo. First decide if it actually shows food/a meal a person "
    "is about to eat. If it is NOT food (a person, screenshot, scenery, object, "
    "abstract image, etc.), set is_food=false and leave all macros at 0. "
    "If it IS food: set is_food=true, identify each food, estimate its portion "
    "in grams, and give per-item and total macros (carbs, protein, fat, fiber in "
    "grams) plus total calories; if portions are ambiguous, estimate and lower "
    "your confidence. Respond only with the required JSON schema."
)


def _resize_jpeg(raw: bytes) -> bytes:
    """Downscale to MAX_EDGE long edge and re-encode as JPEG."""
    img = Image.open(io.BytesIO(raw))
    img = img.convert("RGB")
    img.thumbnail((MAX_EDGE, MAX_EDGE))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue()


@functions_framework.http
def meal_upload(request):
    # --- auth ---
    token = request.form.get("token") or request.headers.get("X-Upload-Token", "")
    if not UPLOAD_TOKEN or token != UPLOAD_TOKEN:
        return ("unauthorized", 401)

    # --- required fields ---
    user_id = (request.form.get("user_id") or "").strip().lower()
    capture_ts = (request.form.get("capture_ts") or "").strip()
    file = request.files.get("image")
    if not user_id or not capture_ts or file is None:
        return ("missing required field: user_id, capture_ts, and image", 400)
    try:
        # Normalise to a UTC ISO timestamp BigQuery accepts.
        capture_dt = dt.datetime.fromisoformat(capture_ts.replace("Z", "+00:00"))
        capture_iso = capture_dt.isoformat()
    except ValueError:
        return (f"capture_ts not valid ISO-8601: {capture_ts!r}", 400)

    img_bytes = _resize_jpeg(file.read())

    # --- Gemini Vision first: is this even a meal? ---
    resp = _genai.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
            PROMPT,
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=MealEstimate,
            temperature=0.2,
        ),
    )
    est: MealEstimate = resp.parsed

    # Non-food photo: skip storage AND BigQuery so stray shots can't pollute data.
    if not est.is_food or not est.items:
        return (
            json.dumps({"status": "skipped", "reason": "not a meal — nothing logged",
                        "detail": est.notes}),
            200,
            {"Content-Type": "application/json"},
        )

    # --- it's a meal: store photo in GCS (per-user prefix) ---
    blob_name = f"{user_id}/inbox/{capture_dt.strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}.jpg"
    bucket = _storage.bucket(BUCKET.replace("gs://", ""))
    bucket.blob(blob_name).upload_from_string(img_bytes, content_type="image/jpeg")
    gcs_uri = f"gs://{BUCKET.replace('gs://', '')}/{blob_name}"

    # --- write row to BigQuery (fully timestamped) ---
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    row = {
        "user_id": user_id,
        "meal_id": f"{user_id}-{capture_dt.strftime('%Y%m%dT%H%M%S')}",
        "capture_ts": capture_iso,
        "upload_ts": now_iso,
        "gcs_uri": gcs_uri,
        "carbs_g": est.carbs_g,
        "protein_g": est.protein_g,
        "fat_g": est.fat_g,
        "fiber_g": est.fiber_g,
        "calories": est.calories,
        "items": json.dumps([i.model_dump() for i in est.items]),
        "gemini_confidence": est.confidence,
        "gemini_model": GEMINI_MODEL,
        "user_corrected": False,
        "notes": est.notes,
    }
    errors = _bq.insert_rows_json(f"{PROJECT}.{BQ_DATASET}.{BQ_TABLE}", [row])
    if errors:
        return (json.dumps({"error": "bigquery insert failed", "details": errors}), 500)

    return (
        json.dumps({"status": "ok", "meal_id": row["meal_id"], "gcs_uri": gcs_uri,
                    "macros": est.model_dump()}),
        200,
        {"Content-Type": "application/json"},
    )
