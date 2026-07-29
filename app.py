#!/usr/bin/env python3
"""
Sophie Unified — Landing + Dashboard + Crea in one Flask app.
"""
import io
import json
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from datetime import datetime
import zipfile
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from pathlib import Path

import requests
import jwt as pyjwt
from flask import (Flask, jsonify, render_template, request, send_file,
                   redirect, session, url_for, make_response)
from werkzeug.exceptions import NotFound, MethodNotAllowed

ROOT = Path(__file__).parent

env_path = ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()

import replicate
import metaghost as mg
import text_overlay as tx
import music as mu

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
app.secret_key = os.environ.get("CREA_SECRET", secrets.token_hex(32))

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://wmnirrzmmvleszmhodvr.supabase.co")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "sb_publishable_ZNP8wIwIcrCCbp-HM6JUDQ_53OR8roa")
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
ADMIN_EMAIL = "simcharbo6@gmail.com"

# Landing/analytics Supabase project (separate from the backend one above).
# Landing photos live in its public `landing_photos` bucket.
LANDING_SUPABASE_URL = os.environ.get("LANDING_SUPABASE_URL", "https://sfaxubipmidysfvtfvdx.supabase.co")


@app.after_request
def add_no_cache_headers(resp):
    if resp.mimetype in ("text/html", "application/json"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


JOBS_DIR = ROOT / "jobs"
JOBS_DIR.mkdir(exist_ok=True)
PHANTOM_DIR = ROOT / "phantom_jobs"
PHANTOM_DIR.mkdir(exist_ok=True)
TEMPLATES_DIR = ROOT / "templates_data"
TEMPLATES_DIR.mkdir(exist_ok=True)

JOBS = {}
PHANTOM_JOBS = {}
TEXT_JOBS = {}
LOCK = threading.Lock()

TEXT_DIR = ROOT / "text_jobs"
TEXT_DIR.mkdir(exist_ok=True)

MUSIC_JOBS = {}
MUSIC_DIR = ROOT / "music_jobs"
MUSIC_DIR.mkdir(exist_ok=True)
MUSIC_LIB = ROOT / "music_lib"
MUSIC_LIB.mkdir(exist_ok=True)


# =============================================================================
# Auth (Supabase JWT)
# =============================================================================

def get_current_user():
    """Validate Supabase access token via API. Cached in session for 5 min."""
    token = request.cookies.get("sb-access-token")
    if not token:
        return None

    # Check session cache (avoid API call on every request)
    cached = session.get("_auth_cache")
    if cached and cached.get("token") == token and (time.time() - cached.get("ts", 0)) < 300:
        return {"id": cached["id"], "email": cached["email"]}

    # Validate with Supabase Auth API
    try:
        r = requests.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={
                "apikey": SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {token}",
            },
            timeout=5,
        )
        if r.status_code == 200:
            data = r.json()
            user = {"id": data.get("id"), "email": (data.get("email") or "").lower()}
            session["_auth_cache"] = {"token": token, "ts": time.time(), **user}
            return user
        return None
    except Exception:
        return None


def _load_permissions(email):
    """Fetch services + role from members table (cached in session for 5 min)."""
    ALL_PAGES = ["pages","domaines","acq","flotte","emails","ads","proxy","mailsva","comptesva","warmup","logs","simulateur-insta","phantom","music"]
    if email == ADMIN_EMAIL:
        return {"services": ["landing", "dashboard", "crea"], "pages": ALL_PAGES, "is_admin": True, "name": "Admin"}
    cached = session.get("_perms")
    if cached and cached.get("email") == email and (time.time() - cached.get("ts", 0)) < 300:
        return cached
    try:
        headers = {"apikey": SUPABASE_ANON_KEY}
        key = SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY
        headers["Authorization"] = f"Bearer {key}"
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/members",
            params={"email": f"eq.{email}", "select": "services,role,name,pages"},
            headers=headers, timeout=5,
        )
        if r.status_code == 200 and r.json():
            m = r.json()[0]
            perms = {
                "services": m.get("services") or [],
                "pages": m.get("pages") or [],
                "is_admin": m.get("role") == "admin",
                "name": m.get("name", ""),
                "email": email,
                "ts": time.time(),
            }
            session["_perms"] = perms
            return perms
    except Exception:
        pass
    return {"services": [], "pages": [], "is_admin": False, "name": "", "email": email, "ts": time.time()}


def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if not user:
            if request.path.startswith("/api/"):
                return jsonify(error="unauthorized"), 401
            return redirect(f"/login?next={request.path}")
        perms = _load_permissions(user["email"])
        user.update(perms)
        request.user = user
        return f(*args, **kwargs)
    return wrapper


def require_service(service):
    """Use AFTER @require_auth to gate a route to a specific service."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            user = getattr(request, "user", None)
            if not user:
                return redirect(f"/login?next={request.path}")
            if user.get("is_admin") or service in user.get("services", []):
                return f(*args, **kwargs)
            if request.path.startswith("/api/"):
                return jsonify(error="forbidden"), 403
            return "<h2 style='font-family:sans-serif;padding:40px;color:#666'>Accès non autorisé à ce service</h2>", 403
        return wrapper
    return decorator


@app.route("/login")
def login():
    token = request.cookies.get("sb-access-token")
    if token and get_current_user():
        return redirect(request.args.get("next", "/"))
    return render_template("login.html",
                           supabase_url=SUPABASE_URL,
                           supabase_key=SUPABASE_ANON_KEY)

@app.route("/api/login", methods=["POST"])
def api_login():
    """Server-side login: the browser only talks to us; WE call Supabase.
    Fixes 'failed to fetch' on restricted networks (VA phones/proxies) that
    can't reach supabase.co or the CDN directly."""
    d = request.get_json(silent=True) or {}
    email = (d.get("email") or "").strip().lower()
    password = d.get("password") or ""
    if not email or not password:
        return jsonify(error="Email et mot de passe requis"), 400
    try:
        r = requests.post(
            f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
            headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
            json={"email": email, "password": password},
            timeout=10,
        )
    except Exception as e:
        return jsonify(ok=False, debug=repr(e)[:400], sburl=SUPABASE_URL[:60]), 200
    if r.status_code != 200:
        if r.status_code in (400, 401):
            return jsonify(error="Email ou mot de passe incorrect"), 400
        return jsonify(error="Erreur d'authentification"), r.status_code
    data = r.json()
    access = data.get("access_token")
    refresh = data.get("refresh_token")
    if not access:
        return jsonify(error="Réponse d'authentification invalide"), 502
    resp = make_response(jsonify(ok=True))
    week = 60 * 60 * 24 * 7
    resp.set_cookie("sb-access-token", access, max_age=week, path="/", samesite="Lax")
    resp.set_cookie("sb-refresh-token", refresh or "", max_age=week * 4, path="/", samesite="Lax")
    return resp


@app.route("/logout")
def logout():
    session.clear()
    resp = make_response(redirect("/login"))
    resp.delete_cookie("sb-access-token")
    resp.delete_cookie("sb-refresh-token")
    return resp


# =============================================================================
# Members management (admin only)
# =============================================================================

@app.route("/members")
@require_auth
def members_global():
    if not request.user.get("is_admin"):
        return "<h2 style='font-family:sans-serif;padding:40px;color:#666'>Réservé aux administrateurs</h2>", 403
    return render_template("members_global.html",
                           supabase_url=SUPABASE_URL,
                           supabase_key=SUPABASE_ANON_KEY)

@app.route("/api/members", methods=["GET"])
@require_auth
def api_members_list():
    if not request.user.get("is_admin"):
        return jsonify(error="admin only"), 403
    headers = {"apikey": SUPABASE_ANON_KEY}
    key = SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY
    headers["Authorization"] = f"Bearer {key}"
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/members",
        params={"select": "*", "order": "created_at"},
        headers=headers, timeout=10,
    )
    return jsonify(members=r.json() if r.status_code == 200 else [])

@app.route("/api/members", methods=["POST"])
@require_auth
def api_members_create():
    if not request.user.get("is_admin"):
        return jsonify(error="admin only"), 403
    data = request.json or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    services = data.get("services", [])
    role = data.get("role", "member")
    if not name or not email:
        return jsonify(error="Nom et email requis"), 400

    headers = {"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json",
               "Prefer": "return=representation"}
    key = SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY
    headers["Authorization"] = f"Bearer {key}"

    password = (data.get("password") or "").strip()
    if not password or len(password) < 6:
        return jsonify(error="Mot de passe requis (6 caractères min)"), 400

    # Create auth user via Supabase Admin API
    auth_ok = False
    auth_err = None
    if SUPABASE_SERVICE_KEY:
        try:
            auth_r = requests.post(
                f"{SUPABASE_URL}/auth/v1/admin/users",
                json={"email": email, "password": password, "email_confirm": True},
                headers={
                    "apikey": SUPABASE_ANON_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
            if auth_r.status_code < 300:
                auth_ok = True
            else:
                err_body = auth_r.json() if auth_r.headers.get("content-type","").startswith("application/json") else {}
                err_msg = err_body.get("msg") or err_body.get("message") or str(auth_r.status_code)
                # If user already exists in auth, update their password and continue
                if "already" in err_msg.lower() or auth_r.status_code == 422:
                    # Find existing user and update password
                    try:
                        list_r = requests.get(
                            f"{SUPABASE_URL}/auth/v1/admin/users",
                            headers={"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}"},
                            timeout=10,
                        )
                        if list_r.status_code == 200:
                            for u in list_r.json().get("users", []):
                                if u.get("email", "").lower() == email:
                                    requests.put(
                                        f"{SUPABASE_URL}/auth/v1/admin/users/{u['id']}",
                                        json={"password": password},
                                        headers={"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}", "Content-Type": "application/json"},
                                        timeout=10,
                                    )
                                    break
                    except Exception:
                        pass
                    auth_ok = True
                else:
                    auth_err = err_msg
        except Exception as e:
            auth_err = str(e)
    else:
        return jsonify(error="SUPABASE_SERVICE_KEY manquant — ajoute-le dans .env"), 400

    if not auth_ok:
        return jsonify(error=f"Erreur création compte: {auth_err}"), 400

    # Insert member in table
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/members",
        json={"name": name, "email": email, "services": services, "role": role, "pages": data.get("pages", [])},
        headers=headers, timeout=10,
    )
    member = r.json()[0] if r.status_code in (200, 201) and r.json() else None

    return jsonify(ok=True, member=member)

@app.route("/api/members/<member_id>", methods=["PATCH"])
@require_auth
def api_members_update(member_id):
    if not request.user.get("is_admin"):
        return jsonify(error="admin only"), 403
    data = request.json or {}
    update = {}
    if "name" in data: update["name"] = data["name"].strip()
    if "email" in data: update["email"] = data["email"].strip().lower()
    if "services" in data: update["services"] = data["services"]
    if "pages" in data: update["pages"] = data["pages"]
    if "role" in data: update["role"] = data["role"]
    if not update:
        return jsonify(error="nothing to update"), 400

    headers = {"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json",
               "Prefer": "return=representation"}
    key = SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY
    headers["Authorization"] = f"Bearer {key}"
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/members",
        params={"id": f"eq.{member_id}"},
        json=update, headers=headers, timeout=10,
    )
    return jsonify(ok=True, member=r.json()[0] if r.status_code == 200 and r.json() else None)

@app.route("/api/members/<member_id>", methods=["DELETE"])
@require_auth
def api_members_delete(member_id):
    if not request.user.get("is_admin"):
        return jsonify(error="admin only"), 403
    headers = {"apikey": SUPABASE_ANON_KEY}
    key = SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY
    headers["Authorization"] = f"Bearer {key}"
    requests.delete(
        f"{SUPABASE_URL}/rest/v1/members",
        params={"id": f"eq.{member_id}"},
        headers=headers, timeout=10,
    )
    return jsonify(ok=True)


# =============================================================================
# Landing page (protected)
# =============================================================================

@app.route("/")
@require_auth
def centrale():
    user = request.user
    return render_template("centrale.html",
                           user_services=json.dumps(user.get("services", [])),
                           user_pages=json.dumps(user.get("pages", [])),
                           user_email=user.get("email", ""),
                           is_admin=user.get("is_admin", False))

@app.route("/landing")
@require_auth
@require_service("landing")
def landing():
    return send_file(ROOT / "templates" / "dashboard" / "liens.html", mimetype="text/html")

@app.route("/landing/edit")
@require_auth
@require_service("landing")
def landing_edit():
    return render_template("landing.html")


# =============================================================================
# Dashboard pages
# =============================================================================

DASHBOARD_PAGES = {
    "acquisition": "dashboard/index.html",
    "flotte": "dashboard/flotte.html",
    "emails": "dashboard/emails.html",
    "tasks": "dashboard/tasks.html",
    "strategies": "dashboard/strategies.html",
    "comptes-va": "dashboard/comptes-va.html",
    "ads": "dashboard/ads.html",
    "proxy": "dashboard/proxy.html",
    "mails-va": "dashboard/mails-va.html",
    "members": "dashboard/members.html",
    "logs": "dashboard/logs.html",
    "simulateur-insta": "dashboard/simulateur-insta.html",
}

@app.route("/dashboard")
@app.route("/dashboard/")
@require_auth
@require_service("dashboard")
def dashboard_index():
    return render_template("dashboard/index.html",
                           supabase_url=SUPABASE_URL,
                           supabase_key=SUPABASE_ANON_KEY)

@app.route("/dashboard/<page>")
@require_auth
@require_service("dashboard")
def dashboard_page(page):
    template = DASHBOARD_PAGES.get(page)
    if not template:
        return "Page not found", 404
    # Pages with inline React/JSX must bypass Jinja ({{ }} conflict)
    if page in ("liens", "simulateur-insta"):
        return send_file(ROOT / "templates" / template, mimetype="text/html")
    return render_template(template,
                           supabase_url=SUPABASE_URL,
                           supabase_key=SUPABASE_ANON_KEY)


# =============================================================================
# Crea page
# =============================================================================

@app.route("/crea")
@require_auth
@require_service("crea")
def crea():
    return render_template("crea.html")


# =============================================================================
# Replicate models
# =============================================================================

FACE_SWAP_MODELS = {
    "seedream": {
        "id": "bytedance/seedream-4", "name": "Seedream 4.5",
        "duration": 33, "cost": 0.030,
        "params": {"aspect_ratio": "9:16", "size": "4K"},
    },
    "nanobanana": {
        "id": "google/nano-banana", "name": "Nano Banana",
        "duration": 13, "cost": 0.036,
        "params": {"output_format": "jpg"},
    },
}

I2V_MODELS = {
    "off": None,
    "kling-v3": {"id": "kwaivgi/kling-v3-video", "name": "Kling v3",
                 "duration": 130, "cost": 0.65,
                 "params": {"aspect_ratio": "9:16"},
                 "supports": {"quality": ["standard", "pro", "4k"],
                              "duration_range": (5, 15),
                              "audio": True}},
    "kling-v25-turbo": {"id": "kwaivgi/kling-v2.5-turbo-pro",
                        "name": "Kling 2.5 Turbo Pro",
                        "duration": 70, "cost": 0.40,
                        "params": {"aspect_ratio": "9:16"},
                        "supports": {"duration_range": (5, 10)}},
    "kling-v16-pro": {"id": "kwaivgi/kling-v1.6-pro", "name": "Kling 1.6 Pro",
                      "duration": 90, "cost": 0.30,
                      "params": {"aspect_ratio": "9:16"},
                      "supports": {"duration_range": (5, 10)}},
}

DEFAULT_FACE_PROMPT = ("l'item 2 doit avoir le visage et les cheveux de l'item 1. "
                      "pas de tatouage sur le resultat, pas de lunette sur le résultat")
DEFAULT_VIDEO_PROMPT = "subtle natural movement, soft lighting, cinematic, breathing"


# =============================================================================
# Generation core
# =============================================================================

def run_face_swap(sophie, swap, model_key, prompt):
    cfg = FACE_SWAP_MODELS[model_key]
    return replicate.run(cfg["id"], input={
        "prompt": prompt,
        "image_input": [open(sophie, "rb"), open(swap, "rb")],
        **cfg["params"],
    })

def run_i2v(start_url, model_key, prompt, duration_s, quality=None, audio=False):
    cfg = I2V_MODELS[model_key]
    inp = {
        "start_image": start_url,
        "prompt": prompt,
        "duration": duration_s,
        **cfg["params"],
    }
    supports = cfg.get("supports", {})
    if quality and "quality" in supports:
        inp["mode"] = quality
    if "audio" in supports:
        inp["generate_audio"] = bool(audio)
    return replicate.run(cfg["id"], input=inp)

def save_output(output, dst):
    import urllib.request
    if hasattr(output, "read"):
        with open(dst, "wb") as f: f.write(output.read())
    elif isinstance(output, list) and output:
        item = output[0]
        if hasattr(item, "read"):
            with open(dst, "wb") as f: f.write(item.read())
        else:
            urllib.request.urlretrieve(str(item), dst)
    elif isinstance(output, str):
        urllib.request.urlretrieve(output, dst)
    else:
        raise ValueError(f"Unknown output: {type(output)}")

def get_url(output):
    if hasattr(output, "url"): return str(output.url)
    if isinstance(output, list) and output:
        item = output[0]
        return str(item.url) if hasattr(item, "url") else str(item)
    return str(output) if isinstance(output, str) else str(output)


def run_one(job_id, sophie, swap_path, swap_idx, var_idx,
            face_model, face_prompt, i2v_model, video_prompts, duration_s,
            phantom_after=False, phantom_stealth=3, phantom_phantom=2,
            video_quality=None, video_audio=False):
    if isinstance(video_prompts, str):
        video_prompts = [video_prompts]
    video_prompt = video_prompts[(swap_idx - 1) % len(video_prompts)] if video_prompts else DEFAULT_VIDEO_PROMPT
    skip_face = face_model in (None, "off", "")
    job = JOBS[job_id]
    out_dir = JOBS_DIR / job_id / "out"
    base = f"sophie_{swap_idx:02d}_{var_idx:02d}"

    try:
        face_out = None
        jpg = out_dir / f"{base}.jpg"
        if skip_face:
            with LOCK: job["log"].append(f"[{base}] face swap skipped — using original")
            shutil.copy(swap_path, jpg)
        else:
            with LOCK: job["log"].append(f"[{base}] face swap...")
            face_out = run_face_swap(sophie, swap_path, face_model, face_prompt)
            save_output(face_out, jpg)
            with LOCK: job["log"].append(f"[{base}] ✓ image saved")
        with LOCK:
            job["files"].append({
                "name": jpg.name, "kind": "image",
                "size": jpg.stat().st_size,
                "swap_idx": swap_idx, "variant_idx": var_idx,
            })
            job["done"] += 1

        mp4 = None
        if i2v_model and i2v_model != "off":
            with LOCK: job["log"].append(f"[{base}] i2v ({i2v_model}, {duration_s}s)...")
            if face_out is not None:
                start = get_url(face_out)
            else:
                start = open(swap_path, "rb")
            v_out = run_i2v(start, i2v_model, video_prompt, duration_s,
                            quality=video_quality, audio=video_audio)
            mp4 = out_dir / f"{base}.mp4"
            save_output(v_out, mp4)
            with LOCK:
                job["log"].append(f"[{base}] ✓ video saved")
                job["files"].append({
                    "name": mp4.name, "kind": "video",
                    "size": mp4.stat().st_size,
                    "swap_idx": swap_idx, "variant_idx": var_idx,
                })
                job["done"] += 1

        if phantom_after and mp4:
            with LOCK: job["log"].append(f"[{base}] phantom uniqualisation...")
            phantom_dir = out_dir / "phantom"
            phantom_dir.mkdir(exist_ok=True)
            try:
                p = mg.make_params(stealth=phantom_stealth)
                p["deai_intensity"] = phantom_phantom
                info = mg.probe(str(mp4))
                phantom_out = phantom_dir / f"{base}_phantom.mp4"
                mg.gen_variant(str(mp4), str(phantom_out), p, info)
                with LOCK:
                    job["log"].append(f"[{base}] ✓ phantom done")
                    job["files"].append({
                        "name": f"phantom/{phantom_out.name}",
                        "kind": "video", "phantom": True,
                        "size": phantom_out.stat().st_size,
                        "swap_idx": swap_idx, "variant_idx": var_idx,
                    })
                    job["done"] += 1
            except Exception as e:
                with LOCK: job["log"].append(f"[{base}] phantom err: {e}")

    except Exception as e:
        with LOCK:
            job["errors"].append(f"[{base}] {e}")
            job["log"].append(f"[{base}] ✗ {e}")


def worker(job_id, sophie, swaps, variants, face_model, face_prompt,
           i2v_model, video_prompts, duration_s, parallelism,
           phantom_after, phantom_stealth, phantom_phantom,
           video_quality=None, video_audio=False):
    job = JOBS[job_id]
    (JOBS_DIR / job_id / "out").mkdir(parents=True, exist_ok=True)
    try:
        with LOCK:
            job["state"] = "running"
            np = len(video_prompts) if isinstance(video_prompts, list) else 1
            job["log"].append(f"start · {len(swaps)}×{variants} · face={face_model} · i2v={i2v_model or 'off'} · {np} video prompt(s) · phantom={phantom_after}")

        tasks = [(s, v, p) for s, p in enumerate(swaps, 1) for v in range(1, variants+1)]
        with ThreadPoolExecutor(max_workers=parallelism) as ex:
            futures = [ex.submit(run_one, job_id, sophie, p, s, v,
                                 face_model, face_prompt, i2v_model,
                                 video_prompts, duration_s,
                                 phantom_after, phantom_stealth, phantom_phantom,
                                 video_quality, video_audio)
                       for s, v, p in tasks]
            for f in futures: f.result()

        with LOCK:
            job["state"] = "done"
            job["log"].append(f"done · {job['done']} files")
    except Exception as e:
        with LOCK:
            job["state"] = "error"
            job["errors"].append(f"fatal: {e}")


# =============================================================================
# Phantom worker
# =============================================================================

def phantom_worker(job_id, src_paths, count, stealth, mirror, prefix, deai):
    job = PHANTOM_JOBS[job_id]
    out_dir = PHANTOM_DIR / job_id / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        with LOCK: job["state"] = "running"
        for s_idx, src in enumerate(src_paths):
            is_img = mg.is_image(src)
            stem = Path(src).stem
            ext = ".jpg" if is_img else ".mp4"
            try:
                info = None if is_img else mg.probe(src)
            except Exception as e:
                with LOCK: job["errors"].append(f"probe {Path(src).name}: {e}")
                continue
            for i in range(1, count+1):
                p = mg.make_params(stealth=stealth)
                if not mirror: p["mirror"] = False
                p["deai_intensity"] = deai
                out = out_dir / f"{prefix}_{s_idx+1:02d}_{i:02d}_{stem}{ext}"
                try:
                    if is_img: mg.gen_variant_image(src, out, p)
                    else: mg.gen_variant(src, out, p, info)
                    with LOCK:
                        job["done"] += 1
                        job["files"].append({
                            "name": out.name, "size": out.stat().st_size,
                            "kind": "image" if is_img else "video",
                        })
                except Exception as e:
                    with LOCK: job["errors"].append(f"{Path(src).name} #{i}: {e}")
        with LOCK: job["state"] = "done"
    except Exception as e:
        with LOCK:
            job["state"] = "error"
            job["errors"].append(f"fatal: {e}")


# =============================================================================
# Text overlay worker
# =============================================================================

def text_worker(job_id, src_paths, text, style, position, color, bg, size_pct,
                prefix, parallelism):
    job = TEXT_JOBS[job_id]
    out_dir = TEXT_DIR / job_id / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    def process(src_idx, src):
        is_img = mg.is_image(src)
        stem = Path(src).stem
        ext = ".jpg" if is_img else ".mp4"
        out = out_dir / f"{prefix}_{src_idx+1:02d}_{stem}{ext}"
        try:
            if is_img:
                tx.apply_to_image(src, out, text, style=style, position=position,
                                  color=color, bg=bg, size_pct=size_pct)
            else:
                tx.apply_to_video(src, out, text, style=style, position=position,
                                  color=color, bg=bg, size_pct=size_pct,
                                  workdir=str(out_dir))
            with LOCK:
                job["done"] += 1
                job["files"].append({
                    "name": out.name, "size": out.stat().st_size,
                    "kind": "image" if is_img else "video",
                })
        except Exception as e:
            with LOCK: job["errors"].append(f"{Path(src).name}: {e}")

    try:
        with LOCK: job["state"] = "running"
        with ThreadPoolExecutor(max_workers=parallelism) as ex:
            futures = [ex.submit(process, i, s) for i, s in enumerate(src_paths)]
            for f in futures:
                f.result()
        with LOCK: job["state"] = "done"
    except Exception as e:
        with LOCK:
            job["state"] = "error"
            job["errors"].append(f"fatal: {e}")


# =============================================================================
# Crea API routes
# =============================================================================

@app.route("/api/balance")
@require_auth
def api_balance():
    token = os.environ.get("REPLICATE_API_TOKEN")
    if not token: return jsonify(balance=None, error="no token")
    try:
        r = requests.get("https://api.replicate.com/v1/account",
                         headers={"Authorization": f"Bearer {token}"},
                         timeout=5)
        if r.status_code == 200:
            data = r.json()
            return jsonify(account=data.get("username", "?"))
        return jsonify(error=f"http {r.status_code}")
    except Exception as e:
        return jsonify(error=str(e))


@app.route("/api/job", methods=["POST"])
@require_auth
def job_create():
    job_id = uuid.uuid4().hex[:12]
    (JOBS_DIR / job_id).mkdir(parents=True, exist_ok=True)
    JOBS[job_id] = {
        "state": "uploading", "total": 0, "done": 0,
        "errors": [], "files": [], "log": [],
        "started": time.time(), "sophie": None, "swaps": [],
        "params": {},
    }
    return jsonify(job_id=job_id)

@app.route("/api/job/<job_id>/sophie", methods=["POST"])
@require_auth
def job_sophie(job_id):
    job = JOBS.get(job_id)
    if not job: return jsonify(error="unknown"), 404
    f = request.files.get("file")
    if not f: return jsonify(error="no file"), 400
    path = JOBS_DIR / job_id / f"sophie{Path(f.filename).suffix}"
    f.save(path)
    job["sophie"] = str(path)
    return jsonify(name=f.filename, size=path.stat().st_size)

@app.route("/api/job/<job_id>/swap", methods=["POST"])
@require_auth
def job_swap(job_id):
    job = JOBS.get(job_id)
    if not job: return jsonify(error="unknown"), 404
    f = request.files.get("file")
    if not f: return jsonify(error="no file"), 400
    swap_dir = JOBS_DIR / job_id / "swap"
    swap_dir.mkdir(exist_ok=True)
    name = Path(f.filename).name
    path = swap_dir / name
    suf = 1
    while path.exists():
        path = swap_dir / f"{Path(name).stem}_{suf}{Path(name).suffix}"; suf += 1
    f.save(path)
    job["swaps"].append(str(path))
    return jsonify(name=path.name, total=len(job["swaps"]))

@app.route("/api/job/<job_id>/start", methods=["POST"])
@require_auth
def job_start(job_id):
    job = JOBS.get(job_id)
    if not job: return jsonify(error="unknown"), 404
    if job["state"] != "uploading": return jsonify(error="started"), 400
    if not job["swaps"]: return jsonify(error="no photos"), 400

    fm = request.form.get("face_model", "seedream")
    iv = request.form.get("i2v_model", "off")
    if fm not in FACE_SWAP_MODELS and fm != "off": return jsonify(error="bad face"), 400
    if iv not in I2V_MODELS: return jsonify(error="bad i2v"), 400
    if fm == "off" and iv == "off":
        return jsonify(error="rien à générer (face swap et i2v désactivés)"), 400
    if fm != "off" and not job["sophie"]:
        return jsonify(error="sophie ref missing"), 400
    variants = max(1, min(4, int(request.form.get("variants", 1))))
    duration = max(5, min(15, int(request.form.get("duration", 5))))
    par = max(1, min(8, int(request.form.get("parallelism", 4))))
    fp = request.form.get("face_prompt") or DEFAULT_FACE_PROMPT
    vp_raw = request.form.get("video_prompts")
    if vp_raw:
        try: vp = json.loads(vp_raw)
        except: vp = [vp_raw]
    else:
        vp = [request.form.get("video_prompt") or DEFAULT_VIDEO_PROMPT]
    if not vp: vp = [DEFAULT_VIDEO_PROMPT]
    pa = request.form.get("phantom_after", "false") == "true"
    ps = max(1, min(5, int(request.form.get("phantom_stealth", 3))))
    pp = max(0, min(3, int(request.form.get("phantom_phantom", 2))))
    vq = request.form.get("video_quality") or None
    va = request.form.get("video_audio", "false") == "true"

    n = len(job["swaps"])
    files_per = 1 + (1 if iv != "off" else 0) + (1 if pa and iv != "off" else 0)
    job["total"] = n * variants * files_per
    job["params"] = {"face_model": fm, "i2v_model": iv,
                     "variants": variants, "phantom_after": pa, "n": n}
    job["state"] = "queued"

    threading.Thread(target=worker, args=(
        job_id, job["sophie"], list(job["swaps"]), variants,
        fm, fp, iv if iv != "off" else None, vp, duration, par,
        pa, ps, pp, vq, va,
    ), daemon=True).start()
    return jsonify(ok=True, total=job["total"])

@app.route("/api/status/<job_id>")
@require_auth
def job_status(job_id):
    job = JOBS.get(job_id)
    if not job: return jsonify(error="unknown"), 404
    return jsonify(
        state=job["state"], total=job["total"], done=job["done"],
        errors=job["errors"][-10:], files=job["files"],
        log=job["log"][-100:], params=job.get("params", {}),
        elapsed=time.time() - job["started"],
    )

@app.route("/api/jobs")
@require_auth
def jobs_list():
    out = []
    for jid, j in JOBS.items():
        out.append({
            "id": jid, "state": j["state"], "total": j["total"],
            "done": j["done"], "started": j["started"],
            "elapsed": time.time() - j["started"],
            "params": j.get("params", {}),
        })
    out.sort(key=lambda x: -x["started"])
    return jsonify(jobs=out)


# ---- Phantom-only jobs ----
@app.route("/api/phantom/job", methods=["POST"])
@require_auth
def phantom_create():
    job_id = uuid.uuid4().hex[:12]
    (PHANTOM_DIR / job_id).mkdir(parents=True, exist_ok=True)
    PHANTOM_JOBS[job_id] = {
        "state": "uploading", "total": 0, "done": 0,
        "errors": [], "files": [], "started": time.time(),
        "src_paths": [],
    }
    return jsonify(job_id=job_id)

@app.route("/api/phantom/job/<job_id>/file", methods=["POST"])
@require_auth
def phantom_file(job_id):
    job = PHANTOM_JOBS.get(job_id)
    if not job: return jsonify(error="unknown"), 404
    f = request.files.get("file")
    if not f: return jsonify(error="no file"), 400
    path = PHANTOM_DIR / job_id / Path(f.filename).name
    suf = 1
    while path.exists():
        path = PHANTOM_DIR / job_id / f"{Path(f.filename).stem}_{suf}{Path(f.filename).suffix}"; suf += 1
    f.save(path)
    job["src_paths"].append(str(path))
    return jsonify(name=path.name, total=len(job["src_paths"]))

@app.route("/api/phantom/job/<job_id>/start", methods=["POST"])
@require_auth
def phantom_start(job_id):
    job = PHANTOM_JOBS.get(job_id)
    if not job: return jsonify(error="unknown"), 404
    count = max(1, min(50, int(request.form.get("count", 5))))
    stealth = max(1, min(5, int(request.form.get("stealth", 3))))
    mirror = request.form.get("mirror", "true") == "true"
    deai = max(0, min(3, int(request.form.get("deai", 0))))
    prefix = (request.form.get("prefix", "acc") or "acc")[:20]
    job["total"] = count * len(job["src_paths"])
    job["state"] = "queued"
    threading.Thread(target=phantom_worker, args=(
        job_id, list(job["src_paths"]), count, stealth, mirror, prefix, deai,
    ), daemon=True).start()
    return jsonify(ok=True, total=job["total"])

@app.route("/api/phantom/status/<job_id>")
@require_auth
def phantom_status(job_id):
    job = PHANTOM_JOBS.get(job_id)
    if not job: return jsonify(error="unknown"), 404
    return jsonify(
        state=job["state"], total=job["total"], done=job["done"],
        errors=job["errors"][-10:], files=job["files"],
        elapsed=time.time() - job["started"],
    )


# ---- Text overlay ----
@app.route("/api/text/styles")
@require_auth
def text_styles():
    return jsonify(styles=tx.available_styles())

@app.route("/api/text/job", methods=["POST"])
@require_auth
def text_create():
    job_id = uuid.uuid4().hex[:12]
    (TEXT_DIR / job_id).mkdir(parents=True, exist_ok=True)
    TEXT_JOBS[job_id] = {
        "state": "uploading", "total": 0, "done": 0,
        "errors": [], "files": [], "started": time.time(),
        "src_paths": [],
    }
    return jsonify(job_id=job_id)

@app.route("/api/text/job/<job_id>/file", methods=["POST"])
@require_auth
def text_file(job_id):
    job = TEXT_JOBS.get(job_id)
    if not job: return jsonify(error="unknown"), 404
    f = request.files.get("file")
    if not f: return jsonify(error="no file"), 400
    path = TEXT_DIR / job_id / Path(f.filename).name
    suf = 1
    while path.exists():
        path = TEXT_DIR / job_id / f"{Path(f.filename).stem}_{suf}{Path(f.filename).suffix}"
        suf += 1
    f.save(path)
    job["src_paths"].append(str(path))
    return jsonify(name=path.name, total=len(job["src_paths"]))

@app.route("/api/text/job/<job_id>/start", methods=["POST"])
@require_auth
def text_start(job_id):
    job = TEXT_JOBS.get(job_id)
    if not job: return jsonify(error="unknown"), 404
    if not job["src_paths"]: return jsonify(error="no files"), 400

    text = (request.form.get("text") or "").strip()
    if not text: return jsonify(error="no text"), 400

    style = request.form.get("style", "modern")
    if style not in tx.STYLE_FONTS:
        return jsonify(error="bad style"), 400
    position = request.form.get("position", "top")
    if position not in ("top", "middle", "bottom"):
        position = "top"
    color = request.form.get("color", "white")
    bg = request.form.get("bg", "none")
    size_pct = float(request.form.get("size_pct", "0.052"))
    size_pct = max(0.02, min(0.12, size_pct))
    prefix = (request.form.get("prefix", "txt") or "txt")[:20]
    parallelism = max(1, min(8, int(request.form.get("parallelism", 4))))

    job["total"] = len(job["src_paths"])
    job["state"] = "queued"

    threading.Thread(target=text_worker, args=(
        job_id, list(job["src_paths"]), text, style, position, color, bg,
        size_pct, prefix, parallelism,
    ), daemon=True).start()
    return jsonify(ok=True, total=job["total"])

@app.route("/api/text/status/<job_id>")
@require_auth
def text_status(job_id):
    job = TEXT_JOBS.get(job_id)
    if not job: return jsonify(error="unknown"), 404
    return jsonify(
        state=job["state"], total=job["total"], done=job["done"],
        errors=job["errors"][-10:], files=job["files"],
        elapsed=time.time() - job["started"],
    )

@app.route("/text_download/<job_id>/<path:filename>")
@require_auth
def text_download(job_id, filename):
    f = TEXT_DIR / job_id / "out" / filename
    if not f.exists(): return "not found", 404
    return send_file(f, as_attachment=True)

@app.route("/text_download/<job_id>/all.zip")
@require_auth
def text_download_all(job_id):
    out_dir = TEXT_DIR / job_id / "out"
    if not out_dir.exists(): return "not found", 404
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for f in sorted(out_dir.iterdir()):
            if f.is_file() and not f.name.startswith("."):
                zf.write(f, arcname=f.name)
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"text_{job_id}.zip")


# =============================================================================
# Music overlay
# =============================================================================

def music_worker(job_id, src_paths, mp3_paths, mode, mapping, volume, prefix, parallelism):
    import random
    job = MUSIC_JOBS[job_id]
    out_dir = MUSIC_DIR / job_id / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    def pick_mp3(idx, video_name):
        if mode == "manual":
            return mapping.get(video_name) or mapping.get(str(idx)) or (mp3_paths[0] if mp3_paths else None)
        if mode == "same":
            return mp3_paths[0]
        if mode == "cycle":
            return mp3_paths[idx % len(mp3_paths)]
        return random.choice(mp3_paths)

    def process(src_idx, src):
        stem = Path(src).stem
        out = out_dir / f"{prefix}_{src_idx+1:02d}_{stem}.mp4"
        mp3 = pick_mp3(src_idx, Path(src).name)
        if not mp3 or not Path(mp3).exists():
            with LOCK: job["errors"].append(f"{Path(src).name}: no mp3")
            return
        try:
            mu.apply_music_to_video(src, mp3, out, volume=volume)
            with LOCK:
                job["done"] += 1
                job["files"].append({
                    "name": out.name, "size": out.stat().st_size,
                    "mp3": Path(mp3).name,
                })
        except Exception as e:
            with LOCK: job["errors"].append(f"{Path(src).name}: {e}")

    try:
        with LOCK: job["state"] = "running"
        with ThreadPoolExecutor(max_workers=parallelism) as ex:
            futures = [ex.submit(process, i, s) for i, s in enumerate(src_paths)]
            for f in futures:
                f.result()
        with LOCK: job["state"] = "done"
    except Exception as e:
        with LOCK:
            job["state"] = "error"
            job["errors"].append(f"fatal: {e}")


@app.route("/api/music/library")
@require_auth
def music_library():
    return jsonify(mu.list_library(MUSIC_LIB))

@app.route("/api/music/folder", methods=["POST"])
@require_auth
def music_folder_create():
    name = (request.json.get("name") or "").strip()
    if not name or "/" in name or name.startswith("."):
        return jsonify(error="invalid name"), 400
    (MUSIC_LIB / name).mkdir(exist_ok=True)
    return jsonify(ok=True, folders=mu.list_library(MUSIC_LIB))

@app.route("/api/music/upload", methods=["POST"])
@require_auth
def music_upload():
    folder = (request.form.get("folder") or "default").strip()
    fld = MUSIC_LIB / folder
    fld.mkdir(exist_ok=True)
    saved = []
    for f in request.files.getlist("file"):
        if not f.filename.lower().endswith(mu.AUDIO_EXTS):
            continue
        dest = fld / Path(f.filename).name
        if dest.exists():
            suf = secrets.token_hex(3)
            dest = fld / f"{Path(f.filename).stem}_{suf}{Path(f.filename).suffix}"
        f.save(dest)
        saved.append(dest.name)
    return jsonify(ok=True, saved=saved, folders=mu.list_library(MUSIC_LIB))

@app.route("/api/music/job", methods=["POST"])
@require_auth
def music_create():
    job_id = secrets.token_hex(6)
    (MUSIC_DIR / job_id).mkdir(parents=True, exist_ok=True)
    MUSIC_JOBS[job_id] = {
        "id": job_id, "state": "uploading", "done": 0, "total": 0,
        "files": [], "errors": [], "src_paths": [],
    }
    return jsonify(job_id=job_id)

@app.route("/api/music/job/<job_id>/file", methods=["POST"])
@require_auth
def music_file(job_id):
    job = MUSIC_JOBS.get(job_id)
    if not job: return jsonify(error="no job"), 404
    for f in request.files.getlist("file"):
        if not f.filename.lower().endswith((".mp4", ".mov", ".m4v", ".webm")):
            continue
        path = MUSIC_DIR / job_id / Path(f.filename).name
        if path.exists():
            suf = secrets.token_hex(3)
            path = MUSIC_DIR / job_id / f"{Path(f.filename).stem}_{suf}{Path(f.filename).suffix}"
        f.save(path)
        job["src_paths"].append(str(path))
    job["total"] = len(job["src_paths"])
    return jsonify(ok=True, count=job["total"],
                   files=[Path(p).name for p in job["src_paths"]])

@app.route("/api/music/job/<job_id>/start", methods=["POST"])
@require_auth
def music_start(job_id):
    job = MUSIC_JOBS.get(job_id)
    if not job: return jsonify(error="no job"), 404
    if not job["src_paths"]: return jsonify(error="no videos"), 400
    data = request.json or {}
    folder = data.get("folder", "default")
    mode = data.get("mode", "random")
    mapping = data.get("mapping", {})
    selected = data.get("selected", [])
    volume = float(data.get("volume", 1.0))
    prefix = (data.get("prefix") or "music").strip() or "music"
    parallelism = int(data.get("parallelism", 4))

    fld = MUSIC_LIB / folder
    if not fld.exists() and folder != "default":
        return jsonify(error="folder not found"), 400
    all_mp3s = sorted([f for f in fld.iterdir() if f.is_file() and f.suffix.lower() in mu.AUDIO_EXTS])
    if selected:
        all_mp3s = [f for f in all_mp3s if f.name in selected]
    if not all_mp3s and mode != "manual":
        return jsonify(error="no mp3 in folder"), 400

    abs_mapping = {}
    for k, v in mapping.items():
        p = fld / v
        if p.exists(): abs_mapping[k] = str(p)

    threading.Thread(target=music_worker, args=(
        job_id, list(job["src_paths"]),
        [str(p) for p in all_mp3s], mode, abs_mapping,
        volume, prefix, parallelism
    ), daemon=True).start()
    return jsonify(ok=True)

@app.route("/api/music/status/<job_id>")
@require_auth
def music_status(job_id):
    job = MUSIC_JOBS.get(job_id)
    if not job: return jsonify(error="no job"), 404
    return jsonify({k: v for k, v in job.items() if k != "src_paths"})

@app.route("/music_download/<job_id>/<path:filename>")
@require_auth
def music_download(job_id, filename):
    f = MUSIC_DIR / job_id / "out" / filename
    if not f.exists(): return "not found", 404
    return send_file(f, as_attachment=True)

@app.route("/music_download/<job_id>/all.zip")
@require_auth
def music_download_all(job_id):
    out_dir = MUSIC_DIR / job_id / "out"
    if not out_dir.exists(): return "not found", 404
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for f in sorted(out_dir.iterdir()):
            if f.is_file() and not f.name.startswith("."):
                zf.write(f, arcname=f.name)
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"music_{job_id}.zip")


# ---- Templates (save/load configs) ----
@app.route("/api/templates")
@require_auth
def templates_list():
    out = []
    for f in sorted(TEMPLATES_DIR.glob("*.json")):
        try: out.append(json.loads(f.read_text()))
        except: pass
    return jsonify(templates=out)

@app.route("/api/templates", methods=["POST"])
@require_auth
def templates_save():
    data = request.json
    name = data.get("name", "Untitled")
    tid = uuid.uuid4().hex[:8]
    obj = {"id": tid, "name": name, "config": data.get("config", {}),
           "created_at": time.time()}
    (TEMPLATES_DIR / f"{tid}.json").write_text(json.dumps(obj, indent=2))
    return jsonify(template=obj)

@app.route("/api/templates/<tid>", methods=["DELETE"])
@require_auth
def templates_delete(tid):
    f = TEMPLATES_DIR / f"{tid}.json"
    if f.exists(): f.unlink()
    return jsonify(ok=True)


# ---- CSV import ----
@app.route("/api/csv/parse", methods=["POST"])
@require_auth
def csv_parse():
    f = request.files.get("file")
    if not f: return jsonify(error="no file"), 400
    import csv as csvm
    text = f.read().decode("utf-8")
    reader = csvm.DictReader(io.StringIO(text))
    rows = []
    for row in reader:
        rows.append({"prompt": row.get("prompt", "").strip(),
                     "name": row.get("name", "").strip()})
    return jsonify(rows=rows[:100])


# ---- Downloads ----
@app.route("/download/<job_id>/<path:filename>")
@require_auth
def download(job_id, filename):
    f = JOBS_DIR / job_id / "out" / filename
    if not f.exists(): return "not found", 404
    return send_file(f, as_attachment=True)

@app.route("/download/<job_id>/all.zip")
@require_auth
def download_all(job_id):
    out_dir = JOBS_DIR / job_id / "out"
    if not out_dir.exists(): return "not found", 404
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for f in sorted(out_dir.rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                zf.write(f, arcname=str(f.relative_to(out_dir)))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"crea_{job_id}.zip")

@app.route("/phantom_download/<job_id>/<path:filename>")
@require_auth
def phantom_download(job_id, filename):
    f = PHANTOM_DIR / job_id / "out" / filename
    if not f.exists(): return "not found", 404
    return send_file(f, as_attachment=True)

@app.route("/phantom_download/<job_id>/all.zip")
@require_auth
def phantom_download_all(job_id):
    out_dir = PHANTOM_DIR / job_id / "out"
    if not out_dir.exists(): return "not found", 404
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for f in sorted(out_dir.iterdir()):
            if f.is_file() and not f.name.startswith("."):
                zf.write(f, arcname=f.name)
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"phantom_{job_id}.zip")

@app.route("/api/clear", methods=["POST"])
@require_auth
def api_clear():
    if JOBS_DIR.exists(): shutil.rmtree(JOBS_DIR)
    if PHANTOM_DIR.exists(): shutil.rmtree(PHANTOM_DIR)
    JOBS_DIR.mkdir(exist_ok=True)
    PHANTOM_DIR.mkdir(exist_ok=True)
    JOBS.clear(); PHANTOM_JOBS.clear()
    return jsonify(ok=True)


# =============================================================================
# Follower stats auto-refresh
# =============================================================================

STATS_SECRET = os.environ.get("STATS_SECRET", "conquerorz-stats-2026")

def _get_proxy():
    """Fetch first proxy from Supabase for scraping."""
    try:
        h = _sb_headers()
        r = requests.get(f"{SUPABASE_URL}/rest/v1/proxies",
                         params={"select": "*", "order": "position", "limit": "1"},
                         headers=h, timeout=5)
        if r.status_code == 200 and r.json():
            p = r.json()[0]
            proxy_url = f"socks5://{p['proxy_user']}:{p['proxy_pass']}@{p['proxy_host']}:{p['proxy_port']}"
            return {"http": proxy_url, "https": proxy_url}
    except Exception:
        pass
    return None

def _scrape_instagram(handle):
    try:
        px = _get_proxy()
        r = requests.get(
            f"https://www.instagram.com/api/v1/users/web_profile_info/?username={handle}",
            headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
                     "x-ig-app-id": "936619743392459"},
            proxies=px, timeout=15,
        )
        if r.status_code == 200:
            return r.json()["data"]["user"]["edge_followed_by"]["count"]
    except Exception:
        pass
    return None

def _scrape_tiktok(handle):
    import re
    try:
        r = requests.get(
            f"https://www.tiktok.com/@{handle}",
            headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"},
            timeout=10,
        )
        if r.status_code == 200:
            m = re.search(r'"followerCount":(\d+)', r.text)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None

@app.route("/api/stats/refresh", methods=["POST"])
def stats_refresh():
    """Scrape followers for all active accounts and save to acquisition table."""
    # Auth via secret key (for cron) or logged-in admin
    secret = request.args.get("secret") or (request.json or {}).get("secret")
    if secret != STATS_SECRET:
        user = get_current_user()
        if not user or not user.get("is_admin"):
            return jsonify(error="unauthorized"), 401

    # Fetch active accounts
    h = _sb_headers()
    r = requests.get(f"{SUPABASE_URL}/rest/v1/accounts",
                     params={"status": "eq.actif", "select": "id,handle,platform,tracking_key"},
                     headers=h, timeout=10)
    accounts = r.json() if r.status_code == 200 else []

    data = {}
    for acc in accounts:
        handle = acc.get("handle", "")
        platform = acc.get("platform", "")
        tracking_key = acc.get("tracking_key", "")
        if not handle:
            continue
        if platform.lower() == "instagram":
            count = _scrape_instagram(handle)
            key = tracking_key or f"insta_{handle}"
            if count is not None:
                data[key] = count
        elif platform.lower() == "tiktok":
            count = _scrape_tiktok(handle)
            key = tracking_key or f"tiktok_{handle}"
            if count is not None:
                data[key] = count
        time.sleep(1)  # Rate limit

    if not data:
        return jsonify(ok=False, error="no data scraped"), 500

    # Upsert into acquisition table
    today = datetime.utcnow().strftime("%Y-%m-%d")
    workspace_id = "00000000-0000-0000-0000-000000000001"

    # Check if today's row exists
    existing = requests.get(f"{SUPABASE_URL}/rest/v1/acquisition",
                           params={"date": f"eq.{today}", "workspace_id": f"eq.{workspace_id}"},
                           headers=h, timeout=10)
    if existing.status_code == 200 and existing.json():
        # Merge with existing data
        old_data = existing.json()[0].get("data", {})
        old_data.update(data)
        requests.patch(f"{SUPABASE_URL}/rest/v1/acquisition",
                      params={"date": f"eq.{today}", "workspace_id": f"eq.{workspace_id}"},
                      json={"data": old_data, "updated_at": datetime.utcnow().isoformat()},
                      headers={**h, "Prefer": "return=minimal"}, timeout=10)
    else:
        requests.post(f"{SUPABASE_URL}/rest/v1/acquisition",
                     json={"date": today, "data": data, "workspace_id": workspace_id},
                     headers={**h, "Prefer": "return=minimal"}, timeout=10)

    return jsonify(ok=True, date=today, accounts=len(data), data=data)


# =============================================================================
# Proxy rotation API
# =============================================================================

@app.route("/api/proxy/list")
@require_auth
def proxy_list():
    """List all proxies."""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/proxies",
            params={"select": "*", "order": "position"},
            headers=_sb_headers(), timeout=5,
        )
        return jsonify(proxies=r.json() if r.status_code == 200 else [])
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.route("/api/proxy/rotate", methods=["POST"])
@require_auth
def proxy_rotate():
    """Rotate proxy IP and return the new IP."""
    try:
        proxy_id = (request.json or {}).get("proxy_id") if request.is_json else request.args.get("proxy_id")
        # Fetch proxy info from Supabase
        headers = {"apikey": SUPABASE_ANON_KEY}
        key = SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY
        headers["Authorization"] = f"Bearer {key}"
        params = {"select": "*"}
        if proxy_id:
            params["id"] = f"eq.{proxy_id}"
        else:
            params["order"] = "position"
            params["limit"] = "1"
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/proxies",
            params=params,
            headers=headers, timeout=5,
        )
        if r.status_code != 200 or not r.json():
            return jsonify(error="Proxy non trouvé"), 404
        proxy = r.json()[0]

        change_url = proxy.get("change_url")
        if not change_url:
            return jsonify(error="Pas d'URL de rotation"), 400

        # Call rotation URL
        rot = requests.get(change_url, timeout=10)

        # Get new IP through the proxy. Mobile dongles reconnect after a
        # rotation (~15-35s), so retry a few times instead of a single shot.
        new_ip = None
        host = proxy.get("proxy_host")
        port = proxy.get("proxy_port")
        user = proxy.get("proxy_user")
        pwd = proxy.get("proxy_pass")
        if host and port:
            proxies = {"https": f"socks5://{user}:{pwd}@{host}:{port}",
                       "http": f"socks5://{user}:{pwd}@{host}:{port}"}
            for attempt in range(6):  # up to ~36s total
                try:
                    ip_r = requests.get("https://api.ipify.org", proxies=proxies, timeout=10)
                    candidate = ip_r.text.strip()
                    if candidate:
                        new_ip = candidate
                        break
                except Exception:
                    pass
                time.sleep(6)

        # Update Supabase. Never overwrite a known IP with None (e.g. if the
        # dongle is still reconnecting) — keep the previous value in that case.
        patch_body = {"last_rotated_at": "now()"}
        if new_ip:
            patch_body["last_ip"] = new_ip
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/proxies",
            params={"id": f"eq.{proxy['id']}"},
            json=patch_body,
            headers={**headers, "Content-Type": "application/json", "Prefer": "return=minimal"},
            timeout=5,
        )

        # ip=None tells the frontend "rotation done, IP still reconnecting"
        return jsonify(ok=True, ip=new_ip, reconnecting=(new_ip is None))
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.route("/api/proxy/ip")
@require_auth
def proxy_ip():
    """Get current proxy IP."""
    try:
        proxy_id = request.args.get("proxy_id")
        headers = {"apikey": SUPABASE_ANON_KEY}
        key = SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY
        headers["Authorization"] = f"Bearer {key}"
        params = {"select": "*"}
        if proxy_id:
            params["id"] = f"eq.{proxy_id}"
        else:
            params["order"] = "position"
            params["limit"] = "1"
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/proxies",
            params=params,
            headers=headers, timeout=5,
        )
        if r.status_code != 200 or not r.json():
            return jsonify(error="Proxy non trouvé"), 404
        proxy = r.json()[0]
        host = proxy.get("proxy_host")
        port = proxy.get("proxy_port")
        user = proxy.get("proxy_user")
        pwd = proxy.get("proxy_pass")
        last_ip = proxy.get("last_ip")
        if not host:
            return jsonify(ip=last_ip, stale=bool(last_ip))
        # Try a live check through the proxy (2 quick attempts).
        proxies = {"https": f"socks5://{user}:{pwd}@{host}:{port}",
                   "http": f"socks5://{user}:{pwd}@{host}:{port}"}
        live_ip = None
        for _ in range(2):
            try:
                ip_r = requests.get("https://api.ipify.org", proxies=proxies, timeout=8)
                candidate = ip_r.text.strip()
                if candidate:
                    live_ip = candidate
                    break
            except Exception:
                pass
        if live_ip:
            # Refresh stored IP if it changed
            if live_ip != last_ip:
                try:
                    requests.patch(
                        f"{SUPABASE_URL}/rest/v1/proxies",
                        params={"id": f"eq.{proxy['id']}"},
                        json={"last_ip": live_ip},
                        headers={**headers, "Content-Type": "application/json", "Prefer": "return=minimal"},
                        timeout=5,
                    )
                except Exception:
                    pass
            return jsonify(ip=live_ip, stale=False)
        # Live check failed (proxy reconnecting / slow): fall back to last known IP
        return jsonify(ip=last_ip, stale=bool(last_ip))
    except Exception as e:
        return jsonify(error=str(e)), 500


# =============================================================================
# VA Accounts API
# =============================================================================

def _sb_headers():
    key = SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY
    return {"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {key}",
            "Content-Type": "application/json", "Prefer": "return=representation"}

@app.route("/api/va-accounts")
@require_auth
@require_service("dashboard")
def va_accounts_list():
    r = requests.get(f"{SUPABASE_URL}/rest/v1/va_accounts",
                     params={"select": "*", "order": "created_at.desc"},
                     headers=_sb_headers(), timeout=10)
    return jsonify(accounts=r.json() if r.status_code == 200 else [])

@app.route("/api/va-accounts", methods=["POST"])
@require_auth
@require_service("dashboard")
def va_accounts_create():
    d = request.json or {}
    username = (d.get("username") or "").strip()
    if not username:
        return jsonify(error="Username requis"), 400
    row = {
        "username": username,
        "email": (d.get("email") or "").strip(),
        "password": (d.get("password") or "").strip(),
        "platform": d.get("platform", "instagram"),
        "status": d.get("status", "warming"),
        "warmup_day": d.get("warmup_day", 0),
        "warmup_max": d.get("warmup_max", 14),
        "warmup_started_at": datetime.utcnow().isoformat(),
    }
    r = requests.post(f"{SUPABASE_URL}/rest/v1/va_accounts",
                      json=row, headers=_sb_headers(), timeout=10)
    if r.status_code in (200, 201):
        return jsonify(ok=True, account=r.json()[0] if r.json() else {})
    return jsonify(error="Erreur création"), 500

@app.route("/api/va-accounts/<aid>", methods=["PATCH"])
@require_auth
@require_service("dashboard")
def va_accounts_update(aid):
    d = request.json or {}
    patch = {}
    for k in ("username", "email", "password", "platform", "status", "warmup_day", "warmup_max", "pages"):
        if k in d:
            patch[k] = d[k]
    patch["updated_at"] = datetime.utcnow().isoformat()
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/va_accounts",
                       params={"id": f"eq.{aid}"},
                       json=patch, headers=_sb_headers(), timeout=10)
    if r.status_code in (200, 201):
        return jsonify(ok=True)
    return jsonify(error="Erreur mise à jour"), 500

@app.route("/api/va-accounts/<aid>", methods=["DELETE"])
@require_auth
@require_service("dashboard")
def va_accounts_delete(aid):
    r = requests.delete(f"{SUPABASE_URL}/rest/v1/va_accounts",
                        params={"id": f"eq.{aid}"},
                        headers=_sb_headers(), timeout=10)
    if r.status_code in (200, 204):
        return jsonify(ok=True)
    return jsonify(error="Erreur suppression"), 500


# ----- Logs (nom / username / mot de passe) -----
@app.route("/api/logs")
@require_auth
@require_service("dashboard")
def logs_list():
    r = requests.get(f"{SUPABASE_URL}/rest/v1/logs",
                     params={"select": "*", "order": "position.asc,created_at.asc"},
                     headers=_sb_headers(), timeout=10)
    return jsonify(logs=r.json() if r.status_code == 200 else [])

@app.route("/api/logs", methods=["POST"])
@require_auth
@require_service("dashboard")
def logs_create():
    d = request.json or {}
    row = {
        "name": (d.get("name") or "").strip(),
        "username": (d.get("username") or "").strip(),
        "password": (d.get("password") or "").strip(),
        "va": d.get("va") or "VA1",
        "position": d.get("position", 0),
    }
    r = requests.post(f"{SUPABASE_URL}/rest/v1/logs",
                      json=row, headers=_sb_headers(), timeout=10)
    if r.status_code in (200, 201):
        return jsonify(ok=True, log=r.json()[0] if r.json() else {})
    return jsonify(error="Erreur création"), 500

@app.route("/api/logs/<lid>", methods=["PATCH"])
@require_auth
@require_service("dashboard")
def logs_update(lid):
    d = request.json or {}
    patch = {}
    for k in ("name", "username", "password", "va", "position"):
        if k in d:
            patch[k] = d[k]
    patch["updated_at"] = datetime.utcnow().isoformat()
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/logs",
                       params={"id": f"eq.{lid}"},
                       json=patch, headers=_sb_headers(), timeout=10)
    if r.status_code in (200, 201, 204):
        return jsonify(ok=True)
    return jsonify(error="Erreur mise à jour"), 500

@app.route("/api/logs/<lid>", methods=["DELETE"])
@require_auth
@require_service("dashboard")
def logs_delete(lid):
    r = requests.delete(f"{SUPABASE_URL}/rest/v1/logs",
                        params={"id": f"eq.{lid}"},
                        headers=_sb_headers(), timeout=10)
    if r.status_code in (200, 204):
        return jsonify(ok=True)
    return jsonify(error="Erreur suppression"), 500


# =============================================================================
# Background scheduler — refresh follower stats daily at 10:00 Paris time
# =============================================================================

def _run_stats_refresh():
    """Call the stats refresh logic directly (no HTTP needed)."""
    import re as _re
    try:
        h = _sb_headers()
        r = requests.get(f"{SUPABASE_URL}/rest/v1/accounts",
                         params={"status": "eq.actif", "select": "id,handle,platform,tracking_key"},
                         headers=h, timeout=10)
        accounts = r.json() if r.status_code == 200 else []
        data = {}
        for acc in accounts:
            handle = acc.get("handle", "")
            platform = acc.get("platform", "")
            tracking_key = acc.get("tracking_key", "")
            if not handle:
                continue
            if platform.lower() == "instagram":
                count = _scrape_instagram(handle)
                key = tracking_key or f"insta_{handle}"
                if count is not None:
                    data[key] = count
            elif platform.lower() == "tiktok":
                count = _scrape_tiktok(handle)
                key = tracking_key or f"tiktok_{handle}"
                if count is not None:
                    data[key] = count
            time.sleep(1)
        if not data:
            print(f"[STATS] No data scraped")
            return
        today = datetime.utcnow().strftime("%Y-%m-%d")
        workspace_id = "00000000-0000-0000-0000-000000000001"
        existing = requests.get(f"{SUPABASE_URL}/rest/v1/acquisition",
                               params={"date": f"eq.{today}", "workspace_id": f"eq.{workspace_id}"},
                               headers=h, timeout=10)
        if existing.status_code == 200 and existing.json():
            old_data = existing.json()[0].get("data", {})
            old_data.update(data)
            requests.patch(f"{SUPABASE_URL}/rest/v1/acquisition",
                          params={"date": f"eq.{today}", "workspace_id": f"eq.{workspace_id}"},
                          json={"data": old_data, "updated_at": datetime.utcnow().isoformat()},
                          headers={**h, "Prefer": "return=minimal"}, timeout=10)
        else:
            requests.post(f"{SUPABASE_URL}/rest/v1/acquisition",
                         json={"date": today, "data": data, "workspace_id": workspace_id},
                         headers={**h, "Prefer": "return=minimal"}, timeout=10)
        print(f"[STATS] Refreshed {len(data)} accounts for {today}")
    except Exception as e:
        print(f"[STATS] Error: {e}")


def _scheduler_loop():
    """Background thread: run stats refresh at 10:00 Paris time every day."""
    import zoneinfo
    tz = zoneinfo.ZoneInfo("Europe/Paris")
    last_run_date = None
    while True:
        now = datetime.now(tz)
        today = now.strftime("%Y-%m-%d")
        if now.hour == 10 and now.minute < 5 and last_run_date != today:
            last_run_date = today
            print(f"[SCHEDULER] Starting daily stats refresh at {now.strftime('%H:%M')}")
            _run_stats_refresh()
        time.sleep(60)


# Start scheduler in background thread
_sched_thread = threading.Thread(target=_scheduler_loop, daemon=True)
_sched_thread.start()
print("[SCHEDULER] Daily stats refresh scheduled at 10:00 Europe/Paris")


# =============================================================================
# Warm-up management API
# =============================================================================

@app.route("/api/warmup/plans")
@require_auth
def warmup_plans_list():
    r = requests.get(f"{SUPABASE_URL}/rest/v1/warmup_plans",
                     params={"select": "*", "order": "created_at"},
                     headers=_sb_headers(), timeout=10)
    return jsonify(plans=r.json() if r.status_code == 200 else [])

@app.route("/api/warmup/plans", methods=["POST"])
@require_auth
def warmup_plans_create():
    d = request.json or {}
    name = (d.get("name") or "").strip()
    days = d.get("days", [])
    if not name or not days:
        return jsonify(error="Nom et jours requis"), 400
    row = {"name": name, "total_days": len(days), "days": days}
    r = requests.post(f"{SUPABASE_URL}/rest/v1/warmup_plans",
                      json=row, headers={**_sb_headers(), "Prefer": "return=representation"}, timeout=10)
    return jsonify(ok=True, plan=r.json()[0] if r.status_code in (200, 201) and r.json() else {})

@app.route("/api/warmup/plans/<pid>", methods=["PATCH"])
@require_auth
def warmup_plans_update(pid):
    d = request.json or {}
    patch = {}
    if "name" in d: patch["name"] = d["name"]
    if "days" in d:
        patch["days"] = d["days"]
        patch["total_days"] = len(d["days"])
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/warmup_plans",
                       params={"id": f"eq.{pid}"},
                       json=patch, headers=_sb_headers(), timeout=10)
    return jsonify(ok=True)

@app.route("/api/warmup/plans/<pid>", methods=["DELETE"])
@require_auth
def warmup_plans_delete(pid):
    requests.delete(f"{SUPABASE_URL}/rest/v1/warmup_plans",
                    params={"id": f"eq.{pid}"}, headers=_sb_headers(), timeout=10)
    return jsonify(ok=True)

@app.route("/api/warmup/assignments")
@require_auth
def warmup_assignments_list():
    r = requests.get(f"{SUPABASE_URL}/rest/v1/warmup_assignments",
                     params={"select": "*,warmup_plans(name,total_days,days)", "order": "created_at"},
                     headers=_sb_headers(), timeout=10)
    return jsonify(assignments=r.json() if r.status_code == 200 else [])

@app.route("/api/warmup/assignments", methods=["POST"])
@require_auth
def warmup_assignments_create():
    d = request.json or {}
    account_name = (d.get("account_name") or "").strip()
    plan_id = d.get("plan_id")
    if not account_name or not plan_id:
        return jsonify(error="Compte et plan requis"), 400
    start_day = int(d.get("current_day", 1))
    row = {"account_name": account_name, "plan_id": plan_id, "current_day": start_day, "status": "active"}
    r = requests.post(f"{SUPABASE_URL}/rest/v1/warmup_assignments",
                      json=row, headers={**_sb_headers(), "Prefer": "return=representation"}, timeout=10)
    return jsonify(ok=True, assignment=r.json()[0] if r.status_code in (200, 201) and r.json() else {})

@app.route("/api/warmup/assignments/<aid>", methods=["PATCH"])
@require_auth
def warmup_assignments_update(aid):
    d = request.json or {}
    patch = {}
    if "current_day" in d: patch["current_day"] = int(d["current_day"])
    if "status" in d: patch["status"] = d["status"]
    if not patch:
        return jsonify(error="nothing to update"), 400
    requests.patch(f"{SUPABASE_URL}/rest/v1/warmup_assignments",
                   params={"id": f"eq.{aid}"},
                   json=patch, headers=_sb_headers(), timeout=10)
    return jsonify(ok=True)

@app.route("/api/warmup/assignments/<aid>", methods=["DELETE"])
@require_auth
def warmup_assignments_delete(aid):
    requests.delete(f"{SUPABASE_URL}/rest/v1/warmup_assignments",
                    params={"id": f"eq.{aid}"}, headers=_sb_headers(), timeout=10)
    return jsonify(ok=True)

@app.route("/api/warmup/complete", methods=["POST"])
@require_auth
def warmup_complete_day():
    d = request.json or {}
    assignment_id = d.get("assignment_id")
    if not assignment_id:
        return jsonify(error="assignment_id requis"), 400
    h = _sb_headers()
    # Get assignment
    r = requests.get(f"{SUPABASE_URL}/rest/v1/warmup_assignments",
                     params={"id": f"eq.{assignment_id}", "select": "*,warmup_plans(total_days)"},
                     headers=h, timeout=10)
    if not r.json():
        return jsonify(error="Assignment non trouvé"), 404
    assignment = r.json()[0]
    day = assignment["current_day"]
    total = assignment.get("warmup_plans", {}).get("total_days", 99)
    # Log completion
    user = get_current_user()
    completed_by = user.get("email", "") if user else ""
    requests.post(f"{SUPABASE_URL}/rest/v1/warmup_logs",
                  json={"assignment_id": assignment_id, "day_number": day, "completed_by": completed_by},
                  headers={**h, "Prefer": "return=minimal"}, timeout=10)
    # Advance day or mark done
    new_day = day + 1
    update = {"current_day": new_day, "last_completed_at": datetime.utcnow().isoformat()}
    if new_day > total:
        update["status"] = "done"
    requests.patch(f"{SUPABASE_URL}/rest/v1/warmup_assignments",
                   params={"id": f"eq.{assignment_id}"},
                   json=update, headers=h, timeout=10)
    return jsonify(ok=True, new_day=new_day, done=new_day > total)


# =============================================================================
# Warm-up page
# =============================================================================

@app.route("/dashboard/warmup")
@require_auth
@require_service("dashboard")
def dashboard_warmup():
    return render_template("dashboard/warmup.html",
                           supabase_url=SUPABASE_URL,
                           supabase_key=SUPABASE_ANON_KEY)


# =============================================================================
# Public landing pages (sophiemercier.fr)
# =============================================================================

LANDING_DOMAINS = {"sophiemercier.fr", "www.sophiemercier.fr"}


def _matches_app_route(path):
    """True if `path` is served by a registered Flask route (dashboard, api,
    static, img, /, login…). False means it's a public bio-link slug (/sophie…)."""
    try:
        adapter = app.url_map.bind(request.host)
        adapter.match(path, method="GET")
        return True
    except MethodNotAllowed:
        return True   # route exists, just a different HTTP method
    except NotFound:
        return False
    except Exception:
        return False


@app.before_request
def landing_domain_router():
    """On the landing domain (sophiemercier.fr) the whole app is available:
    real routes (dashboard, login, api, static, img, /) run normally, and only
    paths that match no route are treated as public bio-link slugs → landing SPA.
    (Client-side routes /go/… and /l/… also fall through to the SPA.)"""
    host = request.host.split(":")[0].lower()
    if host not in LANDING_DOMAINS:
        return None  # other hosts: normal flow
    if _matches_app_route(request.path):
        return None  # dashboard / api / static / img / login / …
    # Public bio-link slug or client-side route → serve the landing SPA.
    ua = request.headers.get("User-Agent", "")
    seg = request.path.strip("/")
    real_landing = seg and not seg.startswith("go/") and not seg.startswith("l/")
    if real_landing and _is_inapp_browser(ua):
        # In-app browsers (Instagram/FB/Snap…) get an instant lightweight escape
        # page that bounces to the real browser. Same links for everyone — no cloaking.
        target = f"https://{request.host}{request.full_path}".rstrip("?")
        return make_response(_render_escape_page(target))
    # Serve as raw HTML (not Jinja2) to avoid template parsing issues
    html_path = ROOT / "templates" / "landing_public.html"
    return send_file(html_path, mimetype="text/html")


# In-app browser detection (TikTok excluded — it uses the 18+ overlay instead,
# and the x-safari/extbrowser schemes don't work inside TikTok anyway).
_INAPP_UA_RE = re.compile(r"Instagram|FBAN|FBAV|Snapchat|Messenger|Line/", re.I)
_TIKTOK_UA_RE = re.compile(r"BytedanceWebview|TikTok|musical_ly", re.I)

def _is_inapp_browser(ua):
    if not ua or _TIKTOK_UA_RE.search(ua):
        return False
    return bool(_INAPP_UA_RE.search(ua))

def _render_escape_page(target):
    safe = (target or "").replace("\\", "").replace('"', "%22").replace("<", "").replace(">", "")
    return """<!doctype html>
<html lang="fr"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>Ouvre dans ton navigateur</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{min-height:100vh;min-height:100dvh}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
background:linear-gradient(160deg,#0f1830 0%,#11294a 55%,#0e3a63 100%);
color:#fff;display:flex;align-items:center;justify-content:center;padding:28px 22px}
.card{width:100%;max-width:360px;text-align:center;display:flex;flex-direction:column;align-items:center;gap:18px}
.avatar{width:84px;height:84px;border-radius:50%;background:linear-gradient(135deg,#3B9DF7,#1565B8);
display:flex;align-items:center;justify-content:center;font-size:34px;box-shadow:0 14px 36px -12px rgba(59,157,247,.6)}
h1{font-size:22px;font-weight:700;letter-spacing:-.3px}
.lead{font-size:15px;line-height:1.5;color:rgba(255,255,255,.78)}
.steps{text-align:left;list-style:none;display:flex;flex-direction:column;gap:12px;width:100%}
.steps li{background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.12);
border-radius:12px;padding:13px 15px;font-size:14px;line-height:1.4;color:rgba(255,255,255,.9)}
.menu{font-weight:800;letter-spacing:2px;padding:0 6px}
.foot{font-size:11px;color:rgba(255,255,255,.4);margin-top:6px}
b{color:#fff}
</style></head>
<body>
<main class="card">
<div class="avatar">✨</div>
<h1>Un instant…</h1>
<p class="lead">Cette page s'ouvre mieux dans ton navigateur. Redirection en cours…</p>
<ol class="steps">
<li>Touche le menu <span class="menu">•••</span> en haut de l'écran</li>
<li>Choisis <b>« Ouvrir dans le navigateur »</b></li>
</ol>
<div class="foot">Continue dans ton navigateur pour voir la page.</div>
</main>
<script>
(function(){
  var ua=navigator.userAgent||"";
  var target="__TARGET__";
  try{
    if(/Instagram/.test(ua)){
      location.href="instagram://extbrowser/?url="+encodeURIComponent(target);
    }else if(/Android/.test(ua)){
      var hp=target.replace(/^https?:\\/\\//,"");
      location.href="intent://"+hp+"#Intent;scheme=https;package=com.android.chrome;S.browser_fallback_url="+encodeURIComponent(target)+";end";
    }
  }catch(e){}
})();
</script>
</body></html>""".replace("__TARGET__", safe)


@app.route("/img/<path:photo_path>")
def landing_image(photo_path):
    """Proxy landing photos behind our own (Cloudflare-cached) domain.

    The browser/Cloudflare caches these for a year, so Supabase Storage only
    ever serves each image on a cache-miss instead of on every page view —
    which is what blew past the cached-egress quota.
    """
    try:
        url = f"{LANDING_SUPABASE_URL}/storage/v1/object/public/landing_photos/{photo_path}"
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return "", r.status_code
        resp = make_response(r.content)
        resp.headers["Content-Type"] = r.headers.get("Content-Type", "image/jpeg")
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp
    except Exception:
        return "", 502


@app.route("/api/dnsdebug")
def api_dnsdebug():
    import socket
    host = "wmnirrzmmvleszmhodvr.supabase.co"
    out = {}
    try: out["resolv_conf"] = open("/etc/resolv.conf").read()[:600]
    except Exception as e: out["resolv_conf_err"] = repr(e)[:200]
    for label, args in [("any", (host, 443)), ("v4", (host, 443, socket.AF_INET)),
                        ("ipapi_v4", ("ipapi.co", 443, socket.AF_INET))]:
        try: out["gai_" + label] = str(socket.getaddrinfo(*args))[:250]
        except Exception as e: out["gai_" + label + "_err"] = repr(e)[:200]
    try:
        r = requests.get("https://1.1.1.1/dns-query", params={"name": host, "type": "A"},
                         headers={"accept": "application/dns-json"}, timeout=8)
        out["doh_1111"] = r.json()
    except Exception as e:
        out["doh_1111_err"] = repr(e)[:200]
    return jsonify(out)


@app.route("/api/geo")
def api_geo():
    """Return the visitor's approximate city for the 'Proche de…' chip."""
    # Real client IP behind Cloudflare / Railway proxies
    ip = (request.headers.get("CF-Connecting-IP")
          or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr
          or "")
    if not ip or ip.startswith(("127.", "10.", "192.168.", "172.")):
        return jsonify({"nearby": None})
    try:
        r = requests.get(f"https://ipapi.co/{ip}/json/", timeout=3)
        if r.ok:
            d = r.json()
            city = d.get("city") or d.get("region")
            if city:
                return jsonify({"nearby": city})
    except Exception:
        pass
    return jsonify({"nearby": None})


if __name__ == "__main__":
    print("Sophie Unified → http://localhost:5050")
    print("  /           → Landing page")
    print("  /dashboard  → Dashboard")
    print("  /crea       → Crea Studio")
    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
