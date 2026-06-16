"""meal-web: a phone-friendly web page for logging meals.

Each person opens an unguessable personal link (/m/<link_token>). The link
token maps to their user_id (server-side, from the MEAL_WEB_LINKS secret), so
nothing secret is ever exposed in the browser. Submitting a photo forwards it
to the meal-upload function with the real upload token attached server-side.

Deployed as a Cloud Run service (buildpacks: see Procfile).
"""

from __future__ import annotations

import datetime as dt
import json
import os

import requests
from flask import Flask, Response, abort, request

app = Flask(__name__)

MEAL_UPLOAD_URL = os.environ["MEAL_UPLOAD_URL"]
UPLOAD_TOKEN = os.environ["UPLOAD_TOKEN"]
# {"<link_token>": "<user_id>"} — injected from Secret Manager.
LINKS: dict[str, str] = json.loads(os.environ.get("MEAL_WEB_LINKS", "{}"))

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Log Meal">
<meta name="theme-color" content="#0b7">
<link rel="manifest" href="/manifest.webmanifest">
<title>Log a meal</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         background:#f4f6f5; color:#15211c; -webkit-tap-highlight-color:transparent; }
  .wrap { max-width:520px; margin:0 auto; padding:24px 18px 48px; }
  h1 { font-size:1.4rem; margin:8px 0 2px; }
  .who { color:#5a6b63; margin:0 0 22px; font-size:.95rem; }
  label.cam { display:block; background:#0b7; color:#fff; text-align:center;
              padding:22px; border-radius:16px; font-size:1.15rem; font-weight:600;
              cursor:pointer; box-shadow:0 2px 8px rgba(0,0,0,.12); }
  label.cam:active { transform:scale(.99); }
  input[type=file] { display:none; }
  #preview { width:100%; margin:18px 0 0; border-radius:14px; display:none; }
  button#send { width:100%; margin-top:18px; padding:18px; font-size:1.1rem; font-weight:600;
                border:0; border-radius:14px; background:#15211c; color:#fff; }
  button#send:disabled { opacity:.4; }
  #status { margin-top:22px; font-size:1rem; }
  .card { background:#fff; border-radius:14px; padding:16px 18px; margin-top:14px;
          box-shadow:0 1px 4px rgba(0,0,0,.06); }
  .big { font-size:1.5rem; font-weight:700; }
  .muted { color:#5a6b63; font-size:.9rem; }
  .err { color:#b00020; }
  .spinner { display:inline-block; width:18px; height:18px; border:3px solid #ccc;
             border-top-color:#0b7; border-radius:50%; animation:spin .8s linear infinite;
             vertical-align:-3px; margin-right:8px; }
  @keyframes spin { to { transform:rotate(360deg); } }
</style>
</head>
<body>
<div class="wrap">
  <h1>📸 Log a meal</h1>
  <p class="who">Logging as <strong>__USER__</strong></p>

  <label class="cam" for="photo">Take a photo of your meal</label>
  <input id="photo" type="file" accept="image/*" capture="environment">
  <img id="preview" alt="preview">
  <button id="send" disabled>Send</button>
  <div id="status"></div>
</div>

<script>
  const photo = document.getElementById('photo');
  const preview = document.getElementById('preview');
  const send = document.getElementById('send');
  const status = document.getElementById('status');

  photo.addEventListener('change', () => {
    if (!photo.files.length) return;
    preview.src = URL.createObjectURL(photo.files[0]);
    preview.style.display = 'block';
    send.disabled = false;
    status.innerHTML = '';
  });

  send.addEventListener('click', async () => {
    if (!photo.files.length) return;
    send.disabled = true;
    status.innerHTML = '<span class="spinner"></span>Analyzing your meal…';
    const fd = new FormData();
    fd.append('image', photo.files[0]);
    fd.append('capture_ts', new Date().toISOString());
    try {
      const r = await fetch(window.location.pathname + '/submit', { method:'POST', body:fd });
      const d = await r.json();
      if (d.status === 'ok') {
        const m = d.macros;
        status.innerHTML = `<div class="card"><div class="big">✅ Logged</div>
          <div style="margin-top:8px">≈ <strong>${Math.round(m.calories)}</strong> kcal</div>
          <div class="muted">${Math.round(m.carbs_g)}g carbs · ${Math.round(m.protein_g)}g protein · ${Math.round(m.fat_g)}g fat · ${Math.round(m.fiber_g)}g fiber</div>
          </div>`;
      } else if (d.status === 'skipped') {
        status.innerHTML = `<div class="card"><div class="big">🤔 Not a meal</div>
          <div class="muted">${d.detail || "That didn't look like food, so nothing was logged."}</div></div>`;
      } else {
        status.innerHTML = `<div class="card err">Something went wrong. Please try again.</div>`;
      }
    } catch (e) {
      status.innerHTML = `<div class="card err">Network error. Please try again.</div>`;
    }
    // reset for the next meal
    photo.value = '';
    preview.style.display = 'none';
  });
</script>
</body>
</html>"""

MANIFEST = {
    "name": "Log a meal",
    "short_name": "Log Meal",
    "display": "standalone",
    "background_color": "#f4f6f5",
    "theme_color": "#0b7",
    "start_url": ".",
    "icons": [],
}


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/manifest.webmanifest")
def manifest():
    return Response(json.dumps(MANIFEST), mimetype="application/manifest+json")


@app.get("/m/<link_token>")
def page(link_token: str):
    user = LINKS.get(link_token)
    if not user:
        abort(404)
    return Response(PAGE.replace("__USER__", user), mimetype="text/html")


@app.post("/m/<link_token>/submit")
def submit(link_token: str):
    user = LINKS.get(link_token)
    if not user:
        abort(404)
    file = request.files.get("image")
    if file is None:
        return Response(json.dumps({"status": "error"}), 400, mimetype="application/json")
    capture_ts = request.form.get("capture_ts") or dt.datetime.now(dt.timezone.utc).isoformat()

    # Forward to meal-upload with the real token attached server-side.
    resp = requests.post(
        MEAL_UPLOAD_URL,
        files={"image": (file.filename or "meal.jpg", file.read(),
                         file.mimetype or "image/jpeg")},
        data={"user_id": user, "capture_ts": capture_ts, "token": UPLOAD_TOKEN},
        timeout=120,
    )
    return Response(resp.text, status=resp.status_code, mimetype="application/json")
