"""meal-web: a phone-friendly web page for logging meals.

Each person opens an unguessable personal link (/m/<link_token>). The link
token maps to their user_id (server-side, from MEAL_WEB_LINKS), so nothing
secret reaches the browser. Features:

  * snap a photo (+ optional text description) -> forwarded to meal-upload
  * ⭐ save a logged meal as a reusable template (saved_meals table)
  * re-log a saved meal in one tap (no photo / no Gemini, fresh timestamp)

Deployed as a Cloud Run service (buildpacks: see Procfile).
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import uuid

import httpx
import requests
from flask import Flask, Response, abort, request
from garminconnect import Garmin
from google.cloud import bigquery, secretmanager

app = Flask(__name__)

PROJECT = os.environ["PROJECT"]
BQ_DATASET = os.environ.get("BQ_DATASET", "health_twin")
MEAL_UPLOAD_URL = os.environ["MEAL_UPLOAD_URL"]
UPLOAD_TOKEN = os.environ["UPLOAD_TOKEN"]
LINKS: dict[str, str] = json.loads(os.environ.get("MEAL_WEB_LINKS", "{}"))

_bq = bigquery.Client(project=PROJECT)
_sm = secretmanager.SecretManagerServiceClient()
MEALS = f"{PROJECT}.{BQ_DATASET}.meals"
SAVED = f"{PROJECT}.{BQ_DATASET}.saved_meals"
GARMIN_DAILY = f"{PROJECT}.{BQ_DATASET}.garmin_daily"


_OMRON_SERVER = "https://vlt-mobile-api.prd.us.ohiomron.com/prd"
_OMRON_USER_AGENT = "OmronConnect/3 CFNetwork/1410.0.3 Darwin/22.6.0"


def _store_garmin_token(user: str, token: str) -> None:
    """Save (or update) the user's Garmin token as garmin-token-<user>."""
    secret_id = f"garmin-token-{user}"
    parent = f"projects/{PROJECT}/secrets/{secret_id}"
    try:
        _sm.get_secret(name=parent)
    except Exception:
        _sm.create_secret(parent=f"projects/{PROJECT}", secret_id=secret_id,
                           secret={"replication": {"automatic": {}}})
    _sm.add_secret_version(parent=parent, payload={"data": token.encode()})


def _store_omron_token(user: str, tokens: dict) -> None:
    """Save (or update) the user's Omron tokens as omron-token-<user>."""
    secret_id = f"omron-token-{user}"
    parent = f"projects/{PROJECT}/secrets/{secret_id}"
    try:
        _sm.get_secret(name=parent)
    except Exception:
        _sm.create_secret(parent=f"projects/{PROJECT}", secret_id=secret_id,
                           secret={"replication": {"automatic": {}}})
    _sm.add_secret_version(parent=parent, payload={"data": json.dumps(tokens).encode()})


def _omron_checksum_hook(req: httpx.Request) -> None:
    if req.method in ("POST", "DELETE") and req.content:
        req.headers["Checksum"] = hashlib.sha256(req.content).hexdigest()


def _omron_login(email: str, password: str, country: str = "US") -> dict:
    """Authenticate to Omron Connect, return {email, accessToken, refreshToken}."""
    with httpx.Client(
        event_hooks={"request": [_omron_checksum_hook]},
        headers={"user-agent": _OMRON_USER_AGENT},
    ) as client:
        r = client.post(
            f"{_OMRON_SERVER}/login",
            json={"emailAddress": email, "password": password, "country": country, "app": "OCM"},
        )
        r.raise_for_status()
    resp = r.json()
    if "accessToken" not in resp:
        print(f"[omron-login] Unexpected response (no accessToken): {resp}")
    return {"email": email, "accessToken": resp["accessToken"], "refreshToken": resp["refreshToken"]}


def _user_for(link_token: str) -> str:
    user = LINKS.get(link_token)
    if not user:
        abort(404)
    return user


def fetch_saved(user: str) -> list[dict]:
    """Return the user's saved-meal templates, newest first."""
    q = f"""SELECT saved_meal_id, name, calories, carbs_g, protein_g, fat_g, fiber_g
            FROM `{SAVED}` WHERE user_id=@u ORDER BY created_ts DESC LIMIT 50"""
    job = _bq.query(q, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("u", "STRING", user)]))
    return [dict(r) for r in job.result()]


# --------------------------------------------------------------------------- #
# Medications — logged manually into garmin_daily (Garmin has no med data).
# Keyed by (user_id, date); the daily garmin-sync preserves these across
# re-syncs (see garmin_sync._existing_meds), so logging here is durable.
# --------------------------------------------------------------------------- #
def _valid_date(s: str) -> str:
    """Accept only a YYYY-MM-DD calendar date (guards the DML below)."""
    return dt.date.fromisoformat((s or "").strip()).isoformat()


def fetch_meds(user: str, date: str) -> list[dict]:
    """Return the medications logged for this (user, date), in log order."""
    q = f"""SELECT m.name AS name, m.dose AS dose
            FROM `{GARMIN_DAILY}`, UNNEST(medications) AS m
            WHERE user_id=@u AND date=@d"""
    job = _bq.query(q, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("u", "STRING", user),
        bigquery.ScalarQueryParameter("d", "DATE", date)]))
    return [dict(r) for r in job.result()]


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
  h2 { font-size:1.05rem; margin:30px 0 10px; color:#2a3a33; }
  .who { color:#5a6b63; margin:0 0 22px; font-size:.95rem; }
  label.cam { display:block; background:#0b7; color:#fff; text-align:center;
              padding:22px; border-radius:16px; font-size:1.15rem; font-weight:600;
              cursor:pointer; box-shadow:0 2px 8px rgba(0,0,0,.12); }
  label.cam:active { transform:scale(.99); }
  input[type=file] { display:none; }
  textarea { width:100%; margin-top:12px; padding:12px; border:1px solid #cdd6d2;
             border-radius:12px; font-size:1rem; font-family:inherit; resize:vertical; min-height:52px; }
  #preview { width:100%; margin:14px 0 0; border-radius:14px; display:none; }
  button { font-family:inherit; }
  button.primary { width:100%; margin-top:14px; padding:18px; font-size:1.1rem; font-weight:600;
                   border:0; border-radius:14px; background:#15211c; color:#fff; }
  button.primary:disabled { opacity:.4; }
  button.ghost { border:1px solid #0b7; background:#fff; color:#0b7; padding:10px 14px;
                 border-radius:10px; font-weight:600; }
  #status { margin-top:20px; font-size:1rem; }
  .card { background:#fff; border-radius:14px; padding:16px 18px; margin-top:12px;
          box-shadow:0 1px 4px rgba(0,0,0,.06); }
  .big { font-size:1.5rem; font-weight:700; }
  .muted { color:#5a6b63; font-size:.9rem; }
  .err { color:#b00020; }
  .saved-row { display:flex; align-items:center; justify-content:space-between; gap:12px; }
  .saved-row .name { font-weight:600; }
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

  <label class="cam" for="photo">📷 Take a photo <span style="font-weight:400;font-size:.9rem">(optional)</span></label>
  <input id="photo" type="file" accept="image/*" capture="environment">
  <img id="preview" alt="preview">
  <textarea id="notes" placeholder="…or just describe the meal (e.g. '2 eggs, 2 slices toast with butter'). A photo, a description, or both — either works."></textarea>
  <label for="when" style="display:block;margin-top:14px;font-size:.9rem;color:#5a6b63">When did you eat this? <span style="font-weight:400">(leave blank for “now”)</span></label>
  <input id="when" type="datetime-local" style="width:100%;margin-top:6px;padding:12px;border:1px solid #cdd6d2;border-radius:12px;font-size:1rem;font-family:inherit">
  <button id="send" class="primary" disabled>Send</button>
  <div id="status"></div>

  <h2>💊 Medications</h2>
  <label for="medDate" style="display:block;font-size:.9rem;color:#5a6b63">Day</label>
  <input id="medDate" type="date" style="width:100%;margin-top:6px;padding:12px;border:1px solid #cdd6d2;border-radius:12px;font-size:1rem;font-family:inherit">
  <input id="medName" type="text" placeholder="Medication (e.g. Metformin)" style="width:100%;margin-top:10px;padding:12px;border:1px solid #cdd6d2;border-radius:12px;font-size:1rem;font-family:inherit">
  <input id="medDose" type="text" placeholder="Dose (e.g. 500mg) — optional" style="width:100%;margin-top:10px;padding:12px;border:1px solid #cdd6d2;border-radius:12px;font-size:1rem;font-family:inherit">
  <button id="medBtn" class="primary" style="background:#0b7">💊 Log medication</button>
  <div id="medList" style="margin-top:12px"></div>

  <h2>⭐ Your saved meals</h2>
  <div id="saved"></div>
</div>

<script>
  const SAVED = __SAVED_JSON__;
  const base = window.location.pathname;             // /m/<token>
  const photo = document.getElementById('photo');
  const preview = document.getElementById('preview');
  const notes = document.getElementById('notes');
  const when = document.getElementById('when');
  const send = document.getElementById('send');
  // chosen meal time, or now if left blank (datetime-local is local time)
  function captureTs() { return when.value ? new Date(when.value).toISOString() : new Date().toISOString(); }
  const status = document.getElementById('status');
  const savedBox = document.getElementById('saved');
  let lastMeal = null;

  function refreshSend() { send.disabled = !(photo.files.length || notes.value.trim()); }
  photo.addEventListener('change', () => {
    if (photo.files.length) {
      preview.src = URL.createObjectURL(photo.files[0]);
      preview.style.display = 'block';
    }
    status.innerHTML = '';
    refreshSend();
  });
  notes.addEventListener('input', refreshSend);

  send.addEventListener('click', async () => {
    if (!photo.files.length && !notes.value.trim()) return;
    send.disabled = true;
    status.innerHTML = '<span class="spinner"></span>Analyzing your meal…';
    const fd = new FormData();
    if (photo.files.length) fd.append('image', photo.files[0]);
    fd.append('notes', notes.value || '');
    fd.append('capture_ts', captureTs());
    try {
      const r = await fetch(base + '/submit', { method:'POST', body:fd });
      const d = await r.json();
      if (d.status === 'ok') {
        const m = d.macros;
        lastMeal = { ...m, gcs_uri: d.gcs_uri, user_notes: notes.value || '' };
        status.innerHTML = macroCard(m, true);
      } else if (d.status === 'skipped') {
        status.innerHTML = `<div class="card"><div class="big">🤔 Not a meal</div>
          <div class="muted">${d.detail || "That didn't look like food, so nothing was logged."}</div></div>`;
      } else {
        status.innerHTML = `<div class="card err">Something went wrong. Please try again.</div>`;
      }
    } catch (e) {
      status.innerHTML = `<div class="card err">Network error. Please try again.</div>`;
    }
    photo.value = ''; preview.style.display='none'; notes.value=''; when.value='';
    refreshSend();
  });

  function macroCard(m, withSave) {
    return `<div class="card"><div class="big">✅ Logged</div>
      <div style="margin-top:8px">≈ <strong>${Math.round(m.calories)}</strong> kcal</div>
      <div class="muted">${Math.round(m.carbs_g)}g carbs · ${Math.round(m.protein_g)}g protein · ${Math.round(m.fat_g)}g fat · ${Math.round(m.fiber_g)}g fiber</div>
      ${withSave ? '<button class="ghost" style="margin-top:14px" onclick="saveMeal()">⭐ Save this meal</button>' : ''}
      </div>`;
  }

  async function saveMeal() {
    if (!lastMeal) return;
    const name = prompt("Name this meal (e.g. 'morning protein shake'):");
    if (!name) return;
    const r = await fetch(base + '/save', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ name, ...lastMeal })
    });
    const d = await r.json();
    if (d.status === 'ok') {
      SAVED.unshift({ saved_meal_id:d.saved_meal_id, name, calories:lastMeal.calories,
        carbs_g:lastMeal.carbs_g, protein_g:lastMeal.protein_g, fat_g:lastMeal.fat_g, fiber_g:lastMeal.fiber_g });
      renderSaved();
      status.innerHTML = `<div class="card"><div class="big">⭐ Saved</div>
        <div class="muted">"${name}" is now in your saved meals.</div></div>`;
    }
  }

  async function logSaved(id, btn) {
    btn.disabled = true; btn.textContent = 'Logging…';
    const r = await fetch(base + '/log-saved', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ saved_meal_id:id, capture_ts:captureTs() })
    });
    const d = await r.json();
    btn.disabled = false; btn.textContent = 'Log again';
    status.scrollIntoView({behavior:'smooth'});
    status.innerHTML = (d.status === 'ok')
      ? `<div class="card"><div class="big">✅ Logged</div><div class="muted">Re-logged "${d.name}" just now.</div></div>`
      : `<div class="card err">Couldn't log that. Try again.</div>`;
  }

  function renderSaved() {
    if (!SAVED.length) { savedBox.innerHTML = '<p class="muted">No saved meals yet — log one, then tap “Save this meal”.</p>'; return; }
    savedBox.innerHTML = SAVED.map(s => `<div class="card saved-row">
        <div><div class="name">${s.name}</div>
          <div class="muted">≈ ${Math.round(s.calories)} kcal · ${Math.round(s.carbs_g)}c / ${Math.round(s.protein_g)}p / ${Math.round(s.fat_g)}f</div></div>
        <button class="ghost" onclick="logSaved('${s.saved_meal_id}', this)">Log again</button>
      </div>`).join('');
  }
  renderSaved();

  // ----- Medications ------------------------------------------------------
  const medDate = document.getElementById('medDate');
  const medName = document.getElementById('medName');
  const medDose = document.getElementById('medDose');
  const medBtn = document.getElementById('medBtn');
  const medList = document.getElementById('medList');
  const esc = s => (s || '').replace(/[&<>"']/g,
      c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  medDate.value = new Date().toLocaleDateString('en-CA');  // YYYY-MM-DD, local

  function renderMeds(meds) {
    if (!meds.length) { medList.innerHTML = '<p class="muted">Nothing logged for this day.</p>'; return; }
    medList.innerHTML = meds.map((m, i) => `<div class="card saved-row">
        <div><span class="name">${esc(m.name)}</span>${m.dose ? ` <span class="muted">${esc(m.dose)}</span>` : ''}</div>
        <button class="ghost" onclick="removeMed(${i})" aria-label="Remove">✕</button>
      </div>`).join('');
  }
  async function loadMeds() {
    medList.innerHTML = '<p class="muted">Loading…</p>';
    try {
      const r = await fetch(base + '/meds?date=' + encodeURIComponent(medDate.value));
      const d = await r.json();
      renderMeds(d.medications || []);
    } catch (e) { medList.innerHTML = '<p class="muted err">Couldn’t load medications.</p>'; }
  }
  async function logMed() {
    const name = medName.value.trim();
    if (!name) return;
    medBtn.disabled = true; medBtn.textContent = 'Logging…';
    try {
      const r = await fetch(base + '/log-med', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ name, dose: medDose.value.trim(), date: medDate.value }) });
      const d = await r.json();
      if (d.status === 'ok') { medName.value = ''; medDose.value = ''; renderMeds(d.medications || []); }
    } catch (e) {}
    medBtn.disabled = false; medBtn.textContent = '💊 Log medication';
  }
  async function removeMed(i) {
    const r = await fetch(base + '/remove-med', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ date: medDate.value, index: i }) });
    const d = await r.json();
    if (d.status === 'ok') renderMeds(d.medications || []);
  }
  medBtn.addEventListener('click', logMed);
  medDate.addEventListener('change', loadMeds);
  loadMeds();
</script>
</body>
</html>"""

MANIFEST = {
    "name": "Log a meal", "short_name": "Log Meal", "display": "standalone",
    "background_color": "#f4f6f5", "theme_color": "#0b7", "start_url": ".", "icons": [],
}


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/manifest.webmanifest")
def manifest():
    return Response(json.dumps(MANIFEST), mimetype="application/manifest+json")


@app.get("/m/<link_token>")
def page(link_token: str):
    user = _user_for(link_token)
    saved_json = json.dumps(fetch_saved(user), default=str)
    html = PAGE.replace("__USER__", user).replace("__SAVED_JSON__", saved_json)
    return Response(html, mimetype="text/html")


@app.post("/m/<link_token>/submit")
def submit(link_token: str):
    user = _user_for(link_token)
    file = request.files.get("image")
    notes = request.form.get("notes", "")
    if file is None and not notes.strip():
        return Response(json.dumps({"status": "error", "reason": "photo or description required"}),
                        400, mimetype="application/json")
    capture_ts = request.form.get("capture_ts") or dt.datetime.now(dt.timezone.utc).isoformat()
    data = {"user_id": user, "capture_ts": capture_ts, "token": UPLOAD_TOKEN, "notes": notes}
    files = ({"image": (file.filename or "meal.jpg", file.read(), file.mimetype or "image/jpeg")}
             if file is not None else None)
    resp = requests.post(MEAL_UPLOAD_URL, files=files, data=data, timeout=120)
    return Response(resp.text, status=resp.status_code, mimetype="application/json")


@app.post("/m/<link_token>/save")
def save(link_token: str):
    user = _user_for(link_token)
    b = request.get_json(force=True, silent=True) or {}
    name = (b.get("name") or "").strip()
    if not name:
        return Response(json.dumps({"status": "error", "reason": "name required"}), 400,
                        mimetype="application/json")
    saved_meal_id = f"{user}-{uuid.uuid4().hex[:10]}"
    row = {
        "user_id": user, "saved_meal_id": saved_meal_id, "name": name,
        "carbs_g": b.get("carbs_g"), "protein_g": b.get("protein_g"),
        "fat_g": b.get("fat_g"), "fiber_g": b.get("fiber_g"), "calories": b.get("calories"),
        "items": json.dumps(b.get("items")) if b.get("items") is not None else None,
        "gcs_uri": b.get("gcs_uri"), "user_notes": b.get("user_notes"),
        "created_ts": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    errors = _bq.insert_rows_json(SAVED, [row])
    if errors:
        return Response(json.dumps({"status": "error", "details": errors}), 500,
                        mimetype="application/json")
    return Response(json.dumps({"status": "ok", "saved_meal_id": saved_meal_id}),
                    mimetype="application/json")


@app.post("/m/<link_token>/log-saved")
def log_saved(link_token: str):
    user = _user_for(link_token)
    b = request.get_json(force=True, silent=True) or {}
    sid = b.get("saved_meal_id")
    if not sid:
        return Response(json.dumps({"status": "error"}), 400, mimetype="application/json")
    capture_ts = b.get("capture_ts") or dt.datetime.now(dt.timezone.utc).isoformat()

    # Look up the saved template (must belong to this user).
    q = f"SELECT * FROM `{SAVED}` WHERE user_id=@u AND saved_meal_id=@s LIMIT 1"
    job = _bq.query(q, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("u", "STRING", user),
        bigquery.ScalarQueryParameter("s", "STRING", sid)]))
    rows = list(job.result())
    if not rows:
        return Response(json.dumps({"status": "error", "reason": "not found"}), 404,
                        mimetype="application/json")
    s = rows[0]
    cap_dt = dt.datetime.fromisoformat(capture_ts.replace("Z", "+00:00"))
    meal = {
        "user_id": user,
        "meal_id": f"{user}-{cap_dt.strftime('%Y%m%dT%H%M%S')}",
        "capture_ts": cap_dt.isoformat(),
        "upload_ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "gcs_uri": s.get("gcs_uri"),
        "carbs_g": s.get("carbs_g"), "protein_g": s.get("protein_g"),
        "fat_g": s.get("fat_g"), "fiber_g": s.get("fiber_g"), "calories": s.get("calories"),
        "items": json.dumps(s.get("items")) if s.get("items") is not None else None,
        "gemini_confidence": None, "gemini_model": None, "user_corrected": False,
        "notes": None, "user_notes": s.get("user_notes"), "source": "saved",
    }
    errors = _bq.insert_rows_json(MEALS, [meal])
    if errors:
        return Response(json.dumps({"status": "error", "details": errors}), 500,
                        mimetype="application/json")
    return Response(json.dumps({"status": "ok", "name": s.get("name")}),
                    mimetype="application/json")


@app.get("/m/<link_token>/meds")
def list_meds(link_token: str):
    user = _user_for(link_token)
    try:
        date = _valid_date(request.args.get("date", ""))
    except ValueError:
        return Response(json.dumps({"status": "error", "reason": "bad date"}), 400,
                        mimetype="application/json")
    return Response(json.dumps({"status": "ok", "medications": fetch_meds(user, date)}),
                    mimetype="application/json")


@app.post("/m/<link_token>/log-med")
def log_med(link_token: str):
    user = _user_for(link_token)
    b = request.get_json(force=True, silent=True) or {}
    name = (b.get("name") or "").strip()
    dose = (b.get("dose") or "").strip()
    if not name:
        return Response(json.dumps({"status": "error", "reason": "name required"}), 400,
                        mimetype="application/json")
    try:
        date = _valid_date(b.get("date", ""))
    except ValueError:
        return Response(json.dumps({"status": "error", "reason": "bad date"}), 400,
                        mimetype="application/json")
    # Append to the day's medications, creating the row if the day has none yet.
    # Garmin-derived columns on an existing row are left untouched.
    _bq.query(
        f"""MERGE `{GARMIN_DAILY}` T
            USING (SELECT @u AS user_id, @d AS date) S
            ON T.user_id = S.user_id AND T.date = S.date
            WHEN MATCHED THEN UPDATE SET medications =
                ARRAY_CONCAT(T.medications, [STRUCT(@name AS name, @dose AS dose)])
            WHEN NOT MATCHED THEN INSERT (user_id, date, medications)
                VALUES (S.user_id, S.date, [STRUCT(@name AS name, @dose AS dose)])""",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("u", "STRING", user),
            bigquery.ScalarQueryParameter("d", "DATE", date),
            bigquery.ScalarQueryParameter("name", "STRING", name),
            bigquery.ScalarQueryParameter("dose", "STRING", dose or None)]),
    ).result()
    return Response(json.dumps({"status": "ok", "medications": fetch_meds(user, date)}),
                    mimetype="application/json")


@app.post("/m/<link_token>/remove-med")
def remove_med(link_token: str):
    user = _user_for(link_token)
    b = request.get_json(force=True, silent=True) or {}
    try:
        date = _valid_date(b.get("date", ""))
        idx = int(b.get("index"))
    except (ValueError, TypeError):
        return Response(json.dumps({"status": "error", "reason": "bad input"}), 400,
                        mimetype="application/json")
    # Drop the medication at position `idx`, preserving the rest and the row.
    _bq.query(
        f"""UPDATE `{GARMIN_DAILY}`
            SET medications = ARRAY(
                SELECT m FROM UNNEST(medications) AS m WITH OFFSET off WHERE off != @i)
            WHERE user_id=@u AND date=@d""",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("u", "STRING", user),
            bigquery.ScalarQueryParameter("d", "DATE", date),
            bigquery.ScalarQueryParameter("i", "INT64", idx)]),
    ).result()
    return Response(json.dumps({"status": "ok", "medications": fetch_meds(user, date)}),
                    mimetype="application/json")


# --------------------------------------------------------------------------- #
# Connect Garmin — one-time, phone-friendly account link (same personal link).
# The password is used only for this login and never stored; only the
# resulting (auto-refreshing) token is saved to garmin-token-<user>.
# --------------------------------------------------------------------------- #
def _connect_shell(body: str, title: str = "Connect Garmin") -> str:
    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{title}</title>
<style>
  body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         background:#f4f6f5; color:#15211c; }}
  .wrap {{ max-width:520px; margin:0 auto; padding:28px 18px; }}
  h1 {{ font-size:1.35rem; }}
  input {{ width:100%; padding:13px; margin-top:10px; border:1px solid #cdd6d2;
          border-radius:12px; font-size:1rem; }}
  button {{ width:100%; margin-top:16px; padding:16px; font-size:1.1rem; font-weight:600;
           border:0; border-radius:14px; background:#0b7; color:#fff; }}
  .muted {{ color:#5a6b63; font-size:.9rem; }} .err {{ color:#b00020; }}
  .card {{ background:#fff; border-radius:14px; padding:18px; box-shadow:0 1px 4px rgba(0,0,0,.06); }}
</style></head><body><div class="wrap">{body}</div></body></html>"""


@app.get("/connect/<link_token>")
def connect_page(link_token: str):
    user = _user_for(link_token)
    body = f"""<h1>⌚ Connect Garmin</h1>
    <p class="muted">Hi <strong>{user}</strong> — sign in once to link your Garmin
    account. Your password is used only to connect and is never stored.</p>
    <form method="POST" action="/connect/{link_token}/start" class="card">
      <input name="email" type="email" placeholder="Garmin email" autocomplete="username" required>
      <input name="password" type="password" placeholder="Garmin password" autocomplete="current-password" required>
      <button type="submit">Connect</button>
    </form>"""
    return Response(_connect_shell(body), mimetype="text/html")


@app.post("/connect/<link_token>/start")
def connect_start(link_token: str):
    user = _user_for(link_token)
    email = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""
    if not email or not password:
        return Response(_connect_shell('<div class="card err">Email and password required.</div>'), 400,
                        mimetype="text/html")
    try:
        g = Garmin(email, password, return_on_mfa=True)
        r1, r2 = g.login()
        if r1 == "needs_mfa":
            # Carry the (serializable) login state to the MFA step via a hidden field.
            state = base64.b64encode(json.dumps(r2).encode()).decode()
            body = f"""<h1>⌚ Enter your code</h1>
            <p class="muted">Garmin sent a verification code to <strong>{user}</strong>'s
            email/phone. Enter it to finish connecting.</p>
            <form method="POST" action="/connect/{link_token}/mfa" class="card">
              <input type="hidden" name="state" value="{state}">
              <input name="code" inputmode="numeric" placeholder="6-digit code" required>
              <button type="submit">Verify &amp; connect</button>
            </form>"""
            return Response(_connect_shell(body), mimetype="text/html")
        _store_garmin_token(user, g.client.dumps())
        return Response(_connect_shell(_connect_success(user)), mimetype="text/html")
    except Exception:
        return Response(_connect_shell(
            '<div class="card err">Couldn\'t sign in — check the email/password and try '
            'again.</div>'), 400, mimetype="text/html")


@app.post("/connect/<link_token>/mfa")
def connect_mfa(link_token: str):
    user = _user_for(link_token)
    code = (request.form.get("code") or "").strip()
    try:
        state = json.loads(base64.b64decode(request.form.get("state", "")))
        g = Garmin(return_on_mfa=True)
        g.resume_login(state, code)
        _store_garmin_token(user, g.client.dumps())
        return Response(_connect_shell(_connect_success(user)), mimetype="text/html")
    except Exception:
        return Response(_connect_shell(
            '<div class="card err">That code didn\'t work. Go back and try connecting '
            'again.</div>'), 400, mimetype="text/html")


def _connect_success(user: str) -> str:
    return f"""<div class="card"><h1>✅ Garmin connected</h1>
    <p class="muted">Thanks {user} — your Garmin data will start syncing automatically.
    You can close this page.</p></div>"""


# --------------------------------------------------------------------------- #
# Connect Omron — one-time, phone-friendly account link (same personal link).
# The password is used only for this login and never stored; only the
# resulting {accessToken, refreshToken} JSON is saved to omron-token-<user>.
# No MFA step — Omron Connect returns tokens directly on credential login.
# --------------------------------------------------------------------------- #
@app.get("/connect-omron/<link_token>")
def connect_omron_page(link_token: str):
    user = _user_for(link_token)
    body = f"""<h1>🩺 Connect Omron</h1>
    <p class="muted">Hi <strong>{user}</strong> — sign in once to link your Omron Connect
    account. Your password is used only to connect and is never stored.</p>
    <form method="POST" action="/connect-omron/{link_token}/start" class="card">
      <input name="email" type="email" placeholder="Omron Connect email" autocomplete="username" required>
      <input name="password" type="password" placeholder="Omron Connect password" autocomplete="current-password" required>
      <button type="submit">Connect</button>
    </form>"""
    return Response(_connect_shell(body, title="Connect Omron"), mimetype="text/html")


@app.post("/connect-omron/<link_token>/start")
def connect_omron_start(link_token: str):
    user = _user_for(link_token)
    email = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""
    if not email or not password:
        return Response(
            _connect_shell('<div class="card err">Email and password required.</div>',
                           title="Connect Omron"), 400, mimetype="text/html")
    try:
        tokens = _omron_login(email, password)
        _store_omron_token(user, tokens)
        return Response(
            _connect_shell(_omron_connect_success(user), title="Connect Omron"),
            mimetype="text/html")
    except httpx.HTTPStatusError as exc:
        print(f"[connect-omron] HTTP error for user={user}: {exc.response.status_code} {exc.response.text[:200]}")
        msg = ("Couldn't sign in — check the email/password and try again."
               if exc.response.status_code in (401, 403)
               else "Omron Connect returned an error. Please try again.")
        return Response(
            _connect_shell(f'<div class="card err">{msg}</div>', title="Connect Omron"),
            400, mimetype="text/html")
    except Exception as exc:
        print(f"[connect-omron] Unexpected error for user={user}: {type(exc).__name__}: {exc}")
        return Response(
            _connect_shell('<div class="card err">Couldn\'t reach Omron Connect. Please try again.</div>',
                           title="Connect Omron"), 400, mimetype="text/html")


def _omron_connect_success(user: str) -> str:
    return f"""<div class="card"><h1>✅ Omron connected</h1>
    <p class="muted">Thanks {user} — your blood pressure data will start syncing automatically.
    You can close this page.</p></div>"""
