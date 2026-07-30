"""
GCP Cloud Function: upload a photo to Google Photos.

(Standalone utility — not part of the health-data pipeline. Kept for
uploading arbitrary images into Google Photos via its REST API.)

Deploy:
    gcloud functions deploy upload_photo \
        --runtime python311 \
        --trigger-http \
        --allow-unauthenticated \
        --set-env-vars SECRET_NAME=projects/<PROJECT_ID>/secrets/google-photos-token/versions/latest

Expected request body (JSON):
    { "image_path": "gs://your-bucket/photo.jpg" }   # GCS URI
    or
    { "image_url": "https://..." }                    # public URL

Store token.json in Secret Manager:
    gcloud secrets create google-photos-token --data-file=token.json

FLOW: Google Photos uploads are a two-step dance —
  1. POST the raw bytes to the uploads endpoint -> get back an "upload token"
  2. POST that token to mediaItems:batchCreate -> the photo appears in the library
"""

import json
import os

import functions_framework
import google.auth.transport.requests
import requests
from google.oauth2.credentials import Credentials
from google.cloud import secretmanager


# OAuth scope: append-only — this token can add photos but never read or
# delete the library (least privilege).
SCOPES = ["https://www.googleapis.com/auth/photoslibrary.appendonly"]
UPLOAD_URL = "https://photoslibrary.googleapis.com/v1/uploads"
BATCH_CREATE_URL = "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate"


def _load_credentials() -> Credentials:
    """OAuth credentials from Secret Manager, silently refreshed if expired.
    (The stored token.json contains a refresh_token, so no human re-login.)"""
    secret_name = os.environ["SECRET_NAME"]
    client = secretmanager.SecretManagerServiceClient()
    response = client.access_secret_version(name=secret_name)
    token_data = json.loads(response.payload.data.decode("utf-8"))
    creds = Credentials.from_authorized_user_info(token_data, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(google.auth.transport.requests.Request())
    return creds


def _upload_bytes(creds: Credentials, data: bytes, filename: str) -> str:
    """Step 1: push the raw image bytes; Google replies with an upload token
    (a receipt proving the bytes arrived, used in step 2)."""
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/octet-stream",
        "X-Goog-Upload-File-Name": filename,
        "X-Goog-Upload-Protocol": "raw",
    }
    resp = requests.post(UPLOAD_URL, headers=headers, data=data)
    resp.raise_for_status()
    return resp.text  # upload token


def _create_media_item(creds: Credentials, upload_token: str, description: str = "") -> dict:
    """Step 2: turn the uploaded bytes into an actual library item."""
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }
    body = {
        "newMediaItems": [
            {
                "description": description,
                "simpleMediaItem": {"uploadToken": upload_token},
            }
        ]
    }
    resp = requests.post(BATCH_CREATE_URL, headers=headers, json=body)
    resp.raise_for_status()
    return resp.json()


@functions_framework.http
def upload_photo(request):
    # silent=True: return None on malformed JSON instead of raising; `or {}`
    # then lets the .get() calls below work either way.
    payload = request.get_json(silent=True) or {}

    image_url = payload.get("image_url")
    if not image_url:
        return {"error": "Missing image_url in request body"}, 400

    creds = _load_credentials()

    # Download the source image first — the browser-ish User-Agent avoids
    # 403s from servers that block "bot" requests.
    image_resp = requests.get(image_url, headers={"User-Agent": "Mozilla/5.0"})
    image_resp.raise_for_status()
    filename = image_url.split("/")[-1] or "photo.jpg"

    upload_token = _upload_bytes(creds, image_resp.content, filename)
    result = _create_media_item(creds, upload_token, description=payload.get("description", ""))

    return result, 200
