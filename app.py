#!/usr/bin/env python3
"""
Sophie Unified — Landing + Dashboard + Crea in one Flask app.
"""
import io
import json
import os
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
    ALL_PAGES = ["pages","domaines","acq","flotte","emails","ads","proxy","mailsva","comptesva","phantom","music"]
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
                auth_err = auth_r.json().get("msg") or auth_r.json().get("message") or str(auth_r.status_code)
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
# Proxy rotation API
# =============================================================================

@app.route("/api/proxy/rotate", methods=["POST"])
@require_auth
def proxy_rotate():
    """Rotate proxy IP and return the new IP."""
    try:
        # Fetch proxy info from Supabase
        headers = {"apikey": SUPABASE_ANON_KEY}
        key = SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY
        headers["Authorization"] = f"Bearer {key}"
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/proxies",
            params={"select": "*", "order": "position", "limit": "1"},
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
        import time as _t; _t.sleep(2)  # Wait for IP to change

        # Get new IP through the proxy
        new_ip = None
        host = proxy.get("proxy_host")
        port = proxy.get("proxy_port")
        user = proxy.get("proxy_user")
        pwd = proxy.get("proxy_pass")
        if host and port:
            try:
                proxies = {"https": f"socks5://{user}:{pwd}@{host}:{port}",
                           "http": f"socks5://{user}:{pwd}@{host}:{port}"}
                ip_r = requests.get("https://api.ipify.org", proxies=proxies, timeout=10)
                new_ip = ip_r.text.strip()
            except Exception:
                pass

        # Update last_rotated_at and last_ip in Supabase
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/proxies",
            params={"id": f"eq.{proxy['id']}"},
            json={"last_rotated_at": "now()", "last_ip": new_ip},
            headers={**headers, "Content-Type": "application/json", "Prefer": "return=minimal"},
            timeout=5,
        )

        return jsonify(ok=True, ip=new_ip)
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.route("/api/proxy/ip")
@require_auth
def proxy_ip():
    """Get current proxy IP."""
    try:
        headers = {"apikey": SUPABASE_ANON_KEY}
        key = SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY
        headers["Authorization"] = f"Bearer {key}"
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/proxies",
            params={"select": "*", "order": "position", "limit": "1"},
            headers=headers, timeout=5,
        )
        if r.status_code != 200 or not r.json():
            return jsonify(error="Proxy non trouvé"), 404
        proxy = r.json()[0]
        host = proxy.get("proxy_host")
        port = proxy.get("proxy_port")
        user = proxy.get("proxy_user")
        pwd = proxy.get("proxy_pass")
        if not host:
            return jsonify(ip=None)
        proxies = {"https": f"socks5://{user}:{pwd}@{host}:{port}",
                   "http": f"socks5://{user}:{pwd}@{host}:{port}"}
        ip_r = requests.get("https://api.ipify.org", proxies=proxies, timeout=10)
        return jsonify(ip=ip_r.text.strip())
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


if __name__ == "__main__":
    print("Sophie Unified → http://localhost:5050")
    print("  /           → Landing page")
    print("  /dashboard  → Dashboard")
    print("  /crea       → Crea Studio")
    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
