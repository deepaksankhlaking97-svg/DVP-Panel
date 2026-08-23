# /opt/dockervps/app.py
# KingCloud VPS Panel — Premium Edition
# ============================================================

import os
import re
import secrets
import sqlite3
import time
import json
import threading
from datetime import datetime

import docker
import psutil

from flask import (
    Flask, request, redirect, session, render_template_string,
    abort, jsonify, flash, url_for, Response, stream_with_context,
)
from werkzeug.security import generate_password_hash, check_password_hash

# ============================================================
# Config
# ============================================================
APP_DIR = "/opt/dockervps"
DB_FILE = os.path.join(APP_DIR, "panel.db")
LOG_FILE = os.path.join(APP_DIR, "panel.log")

os.makedirs(APP_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("DVP_SECRET", secrets.token_hex(32))
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 7  # 7 days

docker_client = docker.from_env()

# ============================================================
# Database
# ============================================================
def db():
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    return con


def log_event(user, action, detail=""):
    try:
        with db() as con:
            con.execute(
                "INSERT INTO activity(user,action,detail,created) VALUES (?,?,?,?)",
                (user, action, detail, int(time.time()))
            )
    except Exception:
        pass


def init_db():
    with db() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS admin (
                id INTEGER PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created INTEGER NOT NULL,
                last_login INTEGER DEFAULT 0,
                role TEXT DEFAULT 'user'
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS vps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                container_id TEXT NOT NULL,
                image TEXT NOT NULL,
                cpu REAL NOT NULL,
                ram INTEGER NOT NULL,
                pids INTEGER NOT NULL,
                disk INTEGER DEFAULT 30,
                created INTEGER NOT NULL,
                owner_id INTEGER,
                status TEXT DEFAULT 'running',
                notes TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user TEXT,
                action TEXT,
                detail TEXT,
                created INTEGER
            );
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                key TEXT UNIQUE,
                name TEXT,
                created INTEGER
            );
        """)
        # Defaults
        defaults = {
            "panel_name": "KingCloud",
            "panel_tagline": "Premium Docker VPS Hosting",
            "user_vps_slots": "50",
            "user_vps_limit": "1",
            "default_cpu": "2",
            "default_ram": "8192",
            "default_pids": "256",
            "default_disk": "30",
            "brand_color": "#6366f1",
            "accent_color": "#ec4899",
            "maintenance_mode": "0",
            "allow_registration": "1",
        }
        for k, v in defaults.items():
            con.execute(
                "INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)",
                (k, v),
            )


def get_setting(key, default=""):
    try:
        with db() as con:
            row = con.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
            return row["value"] if row else default
    except Exception:
        return default


def set_setting(key, value):
    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)",
            (key, str(value)),
        )


def has_admin():
    with db() as con:
        row = con.execute("SELECT COUNT(*) c FROM admin").fetchone()
        return row["c"] > 0


# ============================================================
# Helpers
# ============================================================
def logged_in():
    return bool(session.get("admin") or session.get("user_id"))


def is_admin():
    return bool(session.get("admin"))


def current_user():
    if session.get("admin"):
        return {"id": 0, "username": session["admin"], "role": "admin"}
    if session.get("user_id"):
        with db() as con:
            r = con.execute(
                "SELECT * FROM users WHERE id=?", (session["user_id"],)
            ).fetchone()
            if r:
                return dict(r)
    return None


def require_login():
    if not logged_in():
        flash("Please log in to continue.", "warn")
        return redirect("/login")
    return None


def require_admin():
    if not is_admin():
        flash("Admin access required.", "error")
        return redirect("/")
    return None


def safe_name(value):
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9_-]+", "-", value)
    value = value.strip("-_")
    return value[:40]


def get_host_cpus():
    return max(1, psutil.cpu_count(logical=True) or 1)


def clamp_cpu(value):
    try:
        value = float(value)
    except Exception:
        value = 1.0
    host = float(get_host_cpus())
    return max(0.01, min(value, host))


def clamp_ram(value):
    try:
        value = int(value)
    except Exception:
        value = 1024
    return max(128, min(value, 1024 * 1024))


def clamp_pids(value):
    try:
        value = int(value)
    except Exception:
        value = 256
    return max(32, min(value, 100000))


def clamp_disk(value):
    try:
        value = int(value)
    except Exception:
        value = 30
    return max(5, min(value, 10000))


def get_vps(vps_id):
    with db() as con:
        return con.execute(
            "SELECT * FROM vps WHERE id=?", (vps_id,)
        ).fetchone()


def user_can_access_vps(vps):
    u = current_user()
    if not u:
        return False
    if u["role"] == "admin":
        return True
    return vps["owner_id"] == u["id"]


def get_container(vps):
    try:
        return docker_client.containers.get(vps["container_id"])
    except docker.errors.NotFound:
        abort(404, "Docker container not found")


def image_for_template(template):
    images = {
        "ubuntu24": "ubuntu:24.04",
        "ubuntu22": "ubuntu:22.04",
        "ubuntu20": "ubuntu:20.04",
        "debian13": "debian:13",
        "debian12": "debian:12",
        "debian11": "debian:11",
        "alpine320": "alpine:3.20",
        "python312": "python:3.12-slim",
        "node22": "node:22-bookworm",
        "nginx": "nginx:alpine",
        "redis": "redis:7-alpine",
    }
    return images.get(template, "ubuntu:24.04")


def human_time(ts):
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


def format_bytes(b):
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"


# ============================================================
# UI Templates
# ============================================================
CSS = """
:root{
    --bg-0:#050816;
    --bg-1:#0a0f24;
    --bg-2:#111836;
    --bg-3:#1a2347;
    --line:rgba(255,255,255,.08);
    --line-2:rgba(255,255,255,.14);
    --text:#e6ebff;
    --muted:#8892b8;
    --brand:#6366f1;
    --brand-2:#8b5cf6;
    --accent:#ec4899;
    --ok:#10b981;
    --warn:#f59e0b;
    --err:#ef4444;
    --info:#3b82f6;
    --grad:linear-gradient(135deg,#6366f1 0%,#8b5cf6 50%,#ec4899 100%);
    --grad-2:linear-gradient(135deg,#0ea5e9 0%,#6366f1 100%);
    --grad-3:linear-gradient(135deg,#10b981 0%,#0ea5e9 100%);
    --shadow:0 20px 60px -20px rgba(99,102,241,.35);
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
    background:var(--bg-0);
    color:var(--text);
    font-family:'Inter','Segoe UI',system-ui,-apple-system,sans-serif;
    min-height:100vh;
    overflow-x:hidden;
    background-image:
        radial-gradient(1200px 600px at 10% -10%,rgba(99,102,241,.18),transparent 60%),
        radial-gradient(900px 500px at 110% 10%,rgba(236,72,153,.14),transparent 60%),
        radial-gradient(700px 400px at 50% 120%,rgba(14,165,233,.12),transparent 60%);
}
a{color:var(--brand);text-decoration:none}
a:hover{color:var(--accent)}

/* Top bar */
.topbar{
    position:sticky;top:0;z-index:50;
    backdrop-filter:blur(18px);
    background:rgba(10,15,36,.72);
    border-bottom:1px solid var(--line);
}
.topbar-inner{
    max-width:1400px;margin:0 auto;
    display:flex;align-items:center;gap:18px;
    padding:14px 22px;
}
.brand{
    display:flex;align-items:center;gap:10px;
    font-weight:800;font-size:20px;letter-spacing:.3px;
}
.brand-logo{
    width:36px;height:36px;border-radius:10px;
    background:var(--grad);
    display:grid;place-items:center;
    box-shadow:0 8px 24px -6px rgba(99,102,241,.6);
    font-size:20px;
}
.brand-text{
    background:var(--grad);
    -webkit-background-clip:text;background-clip:text;
    -webkit-text-fill-color:transparent;
}
.nav{display:flex;gap:4px;flex:1;flex-wrap:wrap}
.nav a{
    color:var(--text);
    padding:8px 14px;border-radius:10px;
    font-weight:600;font-size:14px;
    display:inline-flex;align-items:center;gap:6px;
    transition:.2s;
}
.nav a:hover{background:rgba(255,255,255,.06)}
.nav a.active{background:var(--grad);color:#fff}
.topbar-right{display:flex;align-items:center;gap:10px}
.avatar{
    width:36px;height:36px;border-radius:50%;
    background:var(--grad);display:grid;place-items:center;
    font-weight:800;color:#fff;font-size:14px;
}
.menu-btn{
    display:none;background:transparent;border:0;color:#fff;
    font-size:26px;cursor:pointer;padding:4px 8px;
}

/* Layout */
.container{max-width:1400px;margin:0 auto;padding:26px 22px 80px}
.page-head{
    display:flex;align-items:center;justify-content:space-between;
    gap:16px;margin-bottom:22px;flex-wrap:wrap;
}
.page-title{
    font-size:28px;font-weight:800;margin:0;
    display:flex;align-items:center;gap:10px;
}
.page-sub{color:var(--muted);margin-top:4px;font-size:14px}

/* Cards */
.card{
    background:linear-gradient(180deg,rgba(26,35,71,.7),rgba(17,24,54,.7));
    border:1px solid var(--line);
    border-radius:18px;
    padding:22px;
    backdrop-filter:blur(10px);
    box-shadow:var(--shadow);
    margin-bottom:18px;
    position:relative;
    overflow:hidden;
}
.card::before{
    content:"";position:absolute;inset:0;
    background:linear-gradient(135deg,rgba(99,102,241,.05),transparent 60%);
    pointer-events:none;
}
.card h2{margin:0 0 14px;font-size:18px;font-weight:700}
.card h3{margin:0 0 10px;font-size:15px;font-weight:700;color:var(--muted)}

/* Stats grid */
.stats{
    display:grid;grid-template-columns:repeat(4,1fr);gap:14px;
    margin-bottom:22px;
}
.stat{
    position:relative;
    background:linear-gradient(180deg,rgba(26,35,71,.8),rgba(17,24,54,.8));
    border:1px solid var(--line);
    border-radius:18px;padding:18px;
    overflow:hidden;
}
.stat-icon{
    width:42px;height:42px;border-radius:12px;
    display:grid;place-items:center;font-size:20px;
    margin-bottom:10px;
}
.stat-icon.blue{background:rgba(59,130,246,.15);color:#60a5fa}
.stat-icon.green{background:rgba(16,185,129,.15);color:#34d399}
.stat-icon.purple{background:rgba(139,92,246,.15);color:#a78bfa}
.stat-icon.pink{background:rgba(236,72,153,.15);color:#f472b6}
.stat-label{color:var(--muted);font-size:13px;font-weight:500}
.stat-value{font-size:28px;font-weight:800;margin-top:4px;letter-spacing:-.5px}
.stat-delta{font-size:12px;color:var(--muted);margin-top:4px}

/* Buttons */
.btn{
    display:inline-flex;align-items:center;gap:8px;
    padding:10px 16px;border-radius:10px;border:0;
    font-weight:600;font-size:14px;cursor:pointer;
    transition:.2s;text-decoration:none;color:#fff;
    background:rgba(255,255,255,.06);
    border:1px solid var(--line-2);
}
.btn:hover{transform:translateY(-1px);background:rgba(255,255,255,.1)}
.btn-primary{background:var(--grad);border:0;box-shadow:0 8px 24px -8px rgba(99,102,241,.6)}
.btn-primary:hover{box-shadow:0 12px 30px -8px rgba(99,102,241,.8)}
.btn-success{background:linear-gradient(135deg,#10b981,#059669);border:0}
.btn-danger{background:linear-gradient(135deg,#ef4444,#dc2626);border:0}
.btn-warn{background:linear-gradient(135deg,#f59e0b,#d97706);border:0}
.btn-info{background:linear-gradient(135deg,#3b82f6,#2563eb);border:0}
.btn-ghost{background:transparent}
.btn-sm{padding:6px 10px;font-size:12px;border-radius:8px}
.btn-lg{padding:14px 22px;font-size:15px}
.btn-block{display:flex;width:100%;justify-content:center}

/* Forms */
.field{margin-bottom:14px}
.field label{
    display:block;font-size:13px;font-weight:600;
    color:var(--muted);margin-bottom:6px;
}
.input,.select,textarea{
    width:100%;padding:11px 14px;
    background:rgba(5,8,22,.6);
    border:1px solid var(--line-2);
    border-radius:10px;color:var(--text);
    font-size:14px;font-family:inherit;
    transition:.2s;
}
.input:focus,.select:focus,textarea:focus{
    outline:0;border-color:var(--brand);
    box-shadow:0 0 0 3px rgba(99,102,241,.18);
}
.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:700px){.row{grid-template-columns:1fr}}

/* Tables */
.table{width:100%;border-collapse:collapse}
.table th,.table td{
    padding:12px 14px;text-align:left;
    border-bottom:1px solid var(--line);
    font-size:14px;
}
.table th{
    color:var(--muted);font-weight:600;font-size:12px;
    text-transform:uppercase;letter-spacing:.5px;
}
.table tr:hover td{background:rgba(255,255,255,.02)}

/* Badges */
.badge{
    display:inline-flex;align-items:center;gap:6px;
    padding:4px 10px;border-radius:20px;
    font-size:12px;font-weight:600;
}
.badge-ok{background:rgba(16,185,129,.15);color:#34d399}
.badge-err{background:rgba(239,68,68,.15);color:#f87171}
.badge-warn{background:rgba(245,158,11,.15);color:#fbbf24}
.badge-info{background:rgba(59,130,246,.15);color:#60a5fa}
.badge-neutral{background:rgba(255,255,255,.08);color:var(--muted)}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.dot-ok{background:#10b981;box-shadow:0 0 10px #10b981}
.dot-err{background:#ef4444;box-shadow:0 0 10px #ef4444}
.dot-warn{background:#f59e0b;box-shadow:0 0 10px #f59e0b}

/* Flash */
.flash{
    padding:12px 16px;border-radius:12px;margin-bottom:16px;
    display:flex;align-items:center;gap:10px;font-size:14px;
    border:1px solid var(--line);
}
.flash.ok{background:rgba(16,185,129,.1);border-color:rgba(16,185,129,.3);color:#6ee7b7}
.flash.warn{background:rgba(245,158,11,.1);border-color:rgba(245,158,11,.3);color:#fcd34d}
.flash.error{background:rgba(239,68,68,.1);border-color:rgba(239,68,68,.3);color:#fca5a5}
.flash.info{background:rgba(59,130,246,.1);border-color:rgba(59,130,246,.3);color:#93c5fd}

/* Pre */
pre{
    background:rgba(5,8,22,.8);
    padding:16px;border-radius:12px;
    border:1px solid var(--line);
    overflow:auto;white-space:pre-wrap;
    font-family:'JetBrains Mono','Fira Code',monospace;
    font-size:13px;line-height:1.6;
    max-height:500px;
}

/* Terminal */
.term-wrap{
    background:#000;border-radius:12px;
    border:1px solid var(--line);
    overflow:hidden;
}
.term-head{
    background:rgba(255,255,255,.04);
    padding:10px 14px;display:flex;align-items:center;gap:10px;
    border-bottom:1px solid var(--line);
}
.term-dots{display:flex;gap:6px}
.term-dots span{width:12px;height:12px;border-radius:50%}
.term-dots span:nth-child(1){background:#ef4444}
.term-dots span:nth-child(2){background:#f59e0b}
.term-dots span:nth-child(3){background:#10b981}
.term-title{color:var(--muted);font-size:13px;font-family:monospace}
#terminal{height:420px;padding:10px;font-family:monospace;font-size:13px}

/* VPS grid */
.vps-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px}
.vps-card{
    background:linear-gradient(180deg,rgba(26,35,71,.75),rgba(17,24,54,.75));
    border:1px solid var(--line);
    border-radius:16px;padding:18px;
    transition:.25s;cursor:pointer;
    position:relative;overflow:hidden;
}
.vps-card:hover{
    transform:translateY(-3px);
    border-color:var(--brand);
    box-shadow:0 20px 40px -20px rgba(99,102,241,.5);
}
.vps-card::after{
    content:"";position:absolute;top:0;right:0;
    width:100px;height:100px;
    background:radial-gradient(circle,rgba(99,102,241,.15),transparent 70%);
    pointer-events:none;
}
.vps-head{display:flex;align-items:center;gap:12px;margin-bottom:14px}
.vps-icon{
    width:44px;height:44px;border-radius:12px;
    background:var(--grad);display:grid;place-items:center;
    font-size:22px;flex-shrink:0;
}
.vps-name{font-weight:700;font-size:16px;word-break:break-all}
.vps-meta{color:var(--muted);font-size:12px;margin-top:2px}
.vps-specs{
    display:grid;grid-template-columns:repeat(3,1fr);gap:8px;
    padding:12px 0;border-top:1px solid var(--line);
    border-bottom:1px solid var(--line);
    margin:12px 0;
}
.spec{text-align:center}
.spec-val{font-weight:700;font-size:14px}
.spec-lbl{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}

/* Progress */
.progress{
    height:8px;background:rgba(255,255,255,.06);
    border-radius:20px;overflow:hidden;
}
.progress-bar{
    height:100%;border-radius:20px;
    background:var(--grad);
    transition:width .6s;
}

/* Login */
.auth-wrap{
    min-height:100vh;display:grid;place-items:center;
    padding:20px;
}
.auth-card{
    width:100%;max-width:440px;
    background:linear-gradient(180deg,rgba(26,35,71,.85),rgba(17,24,54,.85));
    border:1px solid var(--line-2);
    border-radius:22px;padding:36px 30px;
    backdrop-filter:blur(20px);
    box-shadow:0 30px 80px -20px rgba(99,102,241,.4);
}
.auth-logo{
    width:64px;height:64px;border-radius:18px;
    background:var(--grad);display:grid;place-items:center;
    font-size:32px;margin:0 auto 18px;
    box-shadow:0 12px 30px -8px rgba(99,102,241,.6);
}
.auth-title{text-align:center;font-size:24px;font-weight:800;margin:0 0 6px}
.auth-sub{text-align:center;color:var(--muted);font-size:14px;margin-bottom:24px}

/* Mobile */
@media(max-width:900px){
    .stats{grid-template-columns:repeat(2,1fr)}
}
@media(max-width:700px){
    .nav{display:none;position:absolute;top:64px;left:10px;right:10px;
        background:rgba(17,24,54,.98);border:1px solid var(--line);
        border-radius:14px;padding:10px;flex-direction:column;
        box-shadow:0 20px 40px -10px rgba(0,0,0,.6);
    }
    .nav.open{display:flex}
    .nav a{padding:12px 14px}
    .menu-btn{display:block}
    .container{padding:18px 14px 60px}
    .page-title{font-size:22px}
    .stat-value{font-size:22px}
}

/* Animations */
@keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.fade-in{animation:fadeIn .4s ease both}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
.pulse{animation:pulse 2s infinite}

/* Scrollbar */
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,.1);border-radius:10px}
::-webkit-scrollbar-thumb:hover{background:rgba(255,255,255,.2)}

/* Toast */
.toast-wrap{
    position:fixed;bottom:20px;right:20px;z-index:9999;
    display:flex;flex-direction:column;gap:10px;
}
.toast{
    background:rgba(17,24,54,.95);
    border:1px solid var(--line-2);
    border-radius:12px;padding:12px 16px;
    min-width:260px;backdrop-filter:blur(10px);
    box-shadow:0 10px 30px -10px rgba(0,0,0,.5);
    animation:fadeIn .3s;
    display:flex;align-items:center;gap:10px;
}
"""

BASE_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }} · {{ panel_name }}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>{{ css|safe }}</style>
</head>
<body>
{% if show_nav %}
<div class="topbar">
    <div class="topbar-inner">
        <div class="brand">
            <div class="brand-logo">👑</div>
            <div class="brand-text">{{ panel_name }}</div>
        </div>
        <button class="menu-btn" onclick="document.getElementById('nav').classList.toggle('open')">☰</button>
        <nav class="nav" id="nav">
            <a href="/dashboard" class="{{ 'active' if active=='dashboard' else '' }}">🏠 Dashboard</a>
            <a href="/create" class="{{ 'active' if active=='create' else '' }}">➕ Create VPS</a>
            <a href="/profile" class="{{ 'active' if active=='profile' else '' }}">👤 Profile</a>
            {% if is_admin %}
            <a href="/admin" class="{{ 'active' if active=='admin' else '' }}">⚙️ Admin</a>
            <a href="/users" class="{{ 'active' if active=='users' else '' }}">👥 Users</a>
            <a href="/activity" class="{{ 'active' if active=='activity' else '' }}">📊 Activity</a>
            {% endif %}
        </nav>
        <div class="topbar-right">
            <div class="avatar" title="{{ current_user.username }}">{{ current_user.username[0]|upper }}</div>
            <a href="/logout" class="btn btn-sm btn-ghost">Logout</a>
        </div>
    </div>
</div>
{% endif %}

<div class="container">
    {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
        {% for cat,msg in messages %}
        <div class="flash {{ cat }}">{{ msg }}</div>
        {% endfor %}
    {% endif %}
    {% endwith %}
    {{ body|safe }}
</div>

<div class="toast-wrap" id="toasts"></div>
<script>
function toast(msg,type='info'){
    const el=document.createElement('div');
    el.className='toast';
    el.innerHTML='<span>'+(type==='ok'?'✅':type==='error'?'❌':type==='warn'?'⚠️':'ℹ️')+'</span><span>'+msg+'</span>';
    document.getElementById('toasts').appendChild(el);
    setTimeout(()=>el.remove(),4000);
}
</script>
{{ extra_js|safe }}
</body>
</html>
"""


def page(body, title=None, active="dashboard", extra_js=""):
    u = current_user()
    return render_template_string(
        BASE_TEMPLATE,
        title=title or get_setting("panel_name", "KingCloud"),
        panel_name=get_setting("panel_name", "KingCloud"),
        css=CSS,
        body=body,
        show_nav=logged_in(),
        is_admin=is_admin(),
        current_user=u or {"username": "Guest"},
        active=active,
        extra_js=extra_js,
    )


# ============================================================
# Setup Wizard (first-run admin)
# ============================================================
@app.route("/setup", methods=["GET", "POST"])
def setup():
    if has_admin():
        return redirect("/login")
    err = ""
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        p2 = request.form.get("password2", "")
        if len(u) < 3:
            err = "Username must be at least 3 characters."
        elif len(p) < 6:
            err = "Password must be at least 6 characters."
        elif p != p2:
            err = "Passwords do not match."
        else:
            with db() as con:
                con.execute(
                    "INSERT INTO admin(username,password_hash,created) VALUES (?,?,?)",
                    (u, generate_password_hash(p), int(time.time())),
                )
            session["admin"] = u
            log_event(u, "setup", "Initial admin account created")
            flash("Welcome to KingCloud! Your admin account is ready.", "ok")
            return redirect("/dashboard")

    return render_template_string(
        """
        <!doctype html>
        <html><head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>Setup · KingCloud</title>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet">
        <style>{{ css|safe }}</style>
        </head><body>
        <div class="auth-wrap">
            <div class="auth-card fade-in">
                <div class="auth-logo">👑</div>
                <h1 class="auth-title">Welcome to KingCloud</h1>
                <p class="auth-sub">Create your administrator account to begin.</p>
                {% if err %}<div class="flash error">{{ err }}</div>{% endif %}
                <form method="post">
                    <div class="field">
                        <label>Admin Username</label>
                        <input class="input" name="username" required autofocus>
                    </div>
                    <div class="field">
                        <label>Password</label>
                        <input class="input" name="password" type="password" required>
                    </div>
                    <div class="field">
                        <label>Confirm Password</label>
                        <input class="input" name="password2" type="password" required>
                    </div>
                    <button class="btn btn-primary btn-block btn-lg" type="submit">
                        🚀 Create Admin Account
                    </button>
                </form>
            </div>
        </div>
        </body></html>
        """,
        css=CSS,
        err=err,
    )


# ============================================================
# Auth
# ============================================================
@app.route("/login", methods=["GET", "POST"])
def login():
    if not has_admin():
        return redirect("/setup")
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        with db() as con:
            admin = con.execute(
                "SELECT * FROM admin WHERE username=?", (u,)
            ).fetchone()
            user = con.execute(
                "SELECT * FROM users WHERE username=?", (u,)
            ).fetchone()
        if admin and check_password_hash(admin["password_hash"], p):
            session.clear()
            session["admin"] = u
            session.permanent = True
            log_event(u, "login", "Admin login")
            return redirect("/dashboard")
        if user and check_password_hash(user["password_hash"], p):
            session.clear()
            session["user_id"] = user["id"]
            session.permanent = True
            with db() as con:
                con.execute(
                    "UPDATE users SET last_login=? WHERE id=?",
                    (int(time.time()), user["id"]),
                )
            log_event(u, "login", "User login")
            return redirect("/dashboard")
        flash("Invalid username or password.", "error")

    return render_template_string(
        """
        <!doctype html>
        <html><head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>Login · {{ panel_name }}</title>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet">
        <style>{{ css|safe }}</style>
        </head><body>
        <div class="auth-wrap">
            <div class="auth-card fade-in">
                <div class="auth-logo">👑</div>
                <h1 class="auth-title">{{ panel_name }}</h1>
                <p class="auth-sub">{{ tagline }}</p>
                {% with messages = get_flashed_messages(with_categories=true) %}
                {% for cat,msg in messages %}
                <div class="flash {{ cat }}">{{ msg }}</div>
                {% endfor %}{% endwith %}
                <form method="post">
                    <div class="field">
                        <label>Username</label>
                        <input class="input" name="username" required autofocus>
                    </div>
                    <div class="field">
                        <label>Password</label>
                        <input class="input" name="password" type="password" required>
                    </div>
                    <button class="btn btn-primary btn-block btn-lg" type="submit">
                        🔐 Sign In
                    </button>
                </form>
                {% if allow_reg %}
                <p style="text-align:center;margin-top:18px;color:var(--muted);font-size:14px">
                    No account? <a href="/register">Create one</a>
                </p>
                {% endif %}
            </div>
        </div>
        </body></html>
        """,
        css=CSS,
        panel_name=get_setting("panel_name", "KingCloud"),
        tagline=get_setting("panel_tagline", "Premium Docker VPS Hosting"),
        allow_reg=get_setting("allow_registration", "1") == "1",
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    if get_setting("allow_registration", "1") != "1":
        flash("Registration is disabled.", "warn")
        return redirect("/login")
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        if len(u) < 3:
            flash("Username must be at least 3 characters.", "error")
        elif len(p) < 6:
            flash("Password must be at least 6 characters.", "error")
        else:
            try:
                with db() as con:
                    con.execute(
                        "INSERT INTO users(username,password_hash,created) VALUES (?,?,?)",
                        (u, generate_password_hash(p), int(time.time())),
                    )
                log_event(u, "register", "New user registered")
                flash("Account created! Please log in.", "ok")
                return redirect("/login")
            except sqlite3.IntegrityError:
                flash("Username already taken.", "error")

    return render_template_string(
        """
        <!doctype html>
        <html><head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>Register · {{ panel_name }}</title>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet">
        <style>{{ css|safe }}</style>
        </head><body>
        <div class="auth-wrap">
            <div class="auth-card fade-in">
                <div class="auth-logo">✨</div>
                <h1 class="auth-title">Create Account</h1>
                <p class="auth-sub">Join {{ panel_name }} today.</p>
                {% with messages = get_flashed_messages(with_categories=true) %}
                {% for cat,msg in messages %}
                <div class="flash {{ cat }}">{{ msg }}</div>
                {% endfor %}{% endwith %}
                <form method="post">
                    <div class="field">
                        <label>Username</label>
                        <input class="input" name="username" required autofocus>
                    </div>
                    <div class="field">
                        <label>Password</label>
                        <input class="input" name="password" type="password" required>
                    </div>
                    <button class="btn btn-primary btn-block btn-lg" type="submit">
                        🚀 Create Account
                    </button>
                </form>
                <p style="text-align:center;margin-top:18px;color:var(--muted);font-size:14px">
                    Already have an account? <a href="/login">Sign in</a>
                </p>
            </div>
        </div>
        </body></html>
        """,
        css=CSS,
        panel_name=get_setting("panel_name", "KingCloud"),
    )


@app.route("/logout")
def logout():
    u = current_user()
    if u:
        log_event(u["username"], "logout", "")
    session.clear()
    return redirect("/login")


# ============================================================
# Dashboard
# ============================================================
@app.route("/")
@app.route("/dashboard")
def dashboard():
    r = require_login()
    if r:
        return r

    u = current_user()
    with db() as con:
        if is_admin():
            rows = con.execute("SELECT * FROM vps ORDER BY id DESC").fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM vps WHERE owner_id=? ORDER BY id DESC",
                (u["id"],),
            ).fetchall()

    running = stopped = total_cpu = total_ram = 0
    vps_list = []
    for row in rows:
        try:
            c = docker_client.containers.get(row["container_id"])
            status = c.status
        except Exception:
            status = "missing"
        if status == "running":
            running += 1
        else:
            stopped += 1
        total_cpu += row["cpu"]
        total_ram += row["ram"]
        vps_list.append({**dict(row), "status": status})

    host_cpu = psutil.cpu_percent(interval=0.2)
    host_ram = psutil.virtual_memory().percent
    host_disk = psutil.disk_usage("/").percent

    cards = ""
    for v in vps_list:
        dot = "dot-ok" if v["status"] == "running" else "dot-err"
        if v["status"] not in ("running", "exited"):
            dot = "dot-warn"
        icon = {
            "ubuntu": "🐧",
            "debian": "🌀",
            "alpine": "⛰️",
            "python": "🐍",
            "node": "🟢",
            "nginx": "🌐",
            "redis": "🔴",
        }.get(v["image"].split(":")[0], "📦")
        cards += f"""
        <a href="/vps/{v['id']}" class="vps-card fade-in" style="text-decoration:none;color:inherit">
            <div class="vps-head">
                <div class="vps-icon">{icon}</div>
                <div style="flex:1;min-width:0">
                    <div class="vps-name">{v['name']}</div>
                    <div class="vps-meta">
                        <span class="dot {dot}"></span>
                        {v['status'].upper()} · {v['image']}
                    </div>
                </div>
            </div>
            <div class="vps-specs">
                <div class="spec"><div class="spec-val">{v['cpu']}</div><div class="spec-lbl">CPU</div></div>
                <div class="spec"><div class="spec-val">{round(v['ram']/1024,1)}G</div><div class="spec-lbl">RAM</div></div>
                <div class="spec"><div class="spec-val">{v['disk']}G</div><div class="spec-lbl">Disk</div></div>
            </div>
            <div style="font-size:12px;color:var(--muted)">Created {human_time(v['created'])}</div>
        </a>
        """
    if not cards:
        cards = """
        <div class="card" style="text-align:center;grid-column:1/-1">
            <div style="font-size:48px;margin-bottom:10px">📦</div>
            <h2>No VPS yet</h2>
            <p style="color:var(--muted)">Create your first Docker VPS to get started.</p>
            <a href="/create" class="btn btn-primary">➕ Create VPS</a>
        </div>
        """

    body = f"""
    <div class="page-head">
        <div>
            <h1 class="page-title">👋 Welcome, {u['username']}</h1>
            <div class="page-sub">Here's what's happening with your cloud.</div>
        </div>
        <a href="/create" class="btn btn-primary btn-lg">➕ Deploy New VPS</a>
    </div>

    <div class="stats">
        <div class="stat fade-in">
            <div class="stat-icon blue">📦</div>
            <div class="stat-label">Total VPS</div>
            <div class="stat-value">{len(rows)}</div>
        </div>
        <div class="stat fade-in">
            <div class="stat-icon green">✅</div>
            <div class="stat-label">Running</div>
            <div class="stat-value">{running}</div>
            <div class="stat-delta">{stopped} stopped</div>
        </div>
        <div class="stat fade-in">
            <div class="stat-icon purple">⚡</div>
            <div class="stat-label">Host CPU</div>
            <div class="stat-value">{host_cpu:.1f}%</div>
            <div class="progress" style="margin-top:8px">
                <div class="progress-bar" style="width:{host_cpu}%"></div>
            </div>
        </div>
        <div class="stat fade-in">
            <div class="stat-icon pink">💾</div>
            <div class="stat-label">Host RAM</div>
            <div class="stat-value">{host_ram:.1f}%</div>
            <div class="progress" style="margin-top:8px">
                <div class="progress-bar" style="width:{host_ram}%"></div>
            </div>
        </div>
    </div>

    <div class="card">
        <h2>💻 Host Overview</h2>
        <div class="row">
            <div>
                <div style="color:var(--muted);font-size:13px;margin-bottom:6px">Disk Usage</div>
                <div style="font-size:20px;font-weight:700">{host_disk:.1f}%</div>
                <div class="progress" style="margin-top:8px">
                    <div class="progress-bar" style="width:{host_disk}%"></div>
                </div>
            </div>
            <div>
                <div style="color:var(--muted);font-size:13px;margin-bottom:6px">Allocated Resources</div>
                <div style="font-size:14px">CPU: <b>{total_cpu}</b> cores · RAM: <b>{round(total_ram/1024,1)} GB</b></div>
                <div style="font-size:14px;margin-top:4px">Host CPUs: <b>{get_host_cpus()}</b></div>
            </div>
        </div>
    </div>

    <div class="page-head" style="margin-top:20px">
        <h2 class="page-title" style="font-size:20px">🚀 My VPS Instances</h2>
    </div>
    <div class="vps-grid">{cards}</div>
    """
    return page(body, "Dashboard", active="dashboard")


# ============================================================
# Create VPS
# ============================================================
@app.route("/create", methods=["GET", "POST"])
def create():
    r = require_login()
    if r:
        return r

    u = current_user()
    error = ""

    if request.method == "POST":
        name = safe_name(request.form.get("name", ""))
        template = request.form.get("template", "ubuntu24")
        cpu = clamp_cpu(request.form.get("cpu", get_setting("default_cpu", "2")))
        ram = clamp_ram(request.form.get("ram", get_setting("default_ram", "8192")))
        pids = clamp_pids(request.form.get("pids", get_setting("default_pids", "256")))
        disk = clamp_disk(request.form.get("disk", get_setting("default_disk", "30")))

        if not name:
            error = "Invalid VPS name."
        else:
            # Check user limits
            if not is_admin():
                with db() as con:
                    total_slots = int(get_setting("user_vps_slots", "50"))
                    user_limit = int(get_setting("user_vps_limit", "1"))
                    total_user_vps = con.execute(
                        "SELECT COUNT(*) FROM vps WHERE owner_id IS NOT NULL"
                    ).fetchone()[0]
                    user_vps_count = con.execute(
                        "SELECT COUNT(*) FROM vps WHERE owner_id=?", (u["id"],)
                    ).fetchone()[0]
                if total_user_vps >= total_slots:
                    error = "Platform VPS slots are full. Contact admin."
                elif user_vps_count >= user_limit:
                    error = f"You've reached your limit of {user_limit} VPS."

        if not error:
            container_name = "kc-" + name
            try:
                existing = docker_client.containers.list(
                    all=True, filters={"name": "^/" + container_name + "$"}
                )
                if existing:
                    raise RuntimeError("A VPS with this name already exists.")

                image = image_for_template(template)
                flash("Pulling image " + image + "...", "info")
                docker_client.images.pull(image)

                volume_name = container_name + "-data"
                docker_client.volumes.create(
                    name=volume_name,
                    labels={"kingcloud.managed": "true", "kingcloud.vps": name},
                )

                container = docker_client.containers.create(
                    image=image,
                    name=container_name,
                    command=["sleep", "infinity"],
                    mem_limit=f"{ram}m",
                    nano_cpus=int(cpu * 1_000_000_000),
                    pids_limit=pids,
                    labels={
                        "kingcloud.managed": "true",
                        "kingcloud.template": template,
                        "kingcloud.vps": name,
                    },
                    volumes={volume_name: {"bind": "/data", "mode": "rw"}},
                    restart_policy={"Name": "unless-stopped"},
                    tty=True,
                    stdin_open=True,
                    hostname=name,
                )
                container.start()

                with db() as con:
                    con.execute(
                        """INSERT INTO vps
                           (name,container_id,image,cpu,ram,pids,disk,created,owner_id)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (
                            name, container.id, image, cpu, ram, pids, disk,
                            int(time.time()),
                            None if is_admin() else u["id"],
                        ),
                    )
                log_event(u["username"], "create_vps", f"{name} ({image})")
                flash(f"VPS '{name}' deployed successfully!", "ok")
                return redirect("/dashboard")
            except Exception as exc:
                error = str(exc)

    templates = [
        ("ubuntu24", "🐧 Ubuntu 24.04 LTS", "Recommended"),
        ("ubuntu22", "🐧 Ubuntu 22.04 LTS", "Stable"),
        ("ubuntu20", "🐧 Ubuntu 20.04 LTS", "Legacy"),
        ("debian13", "🌀 Debian 13", "Latest"),
        ("debian12", "🌀 Debian 12", "Stable"),
        ("debian11", "🌀 Debian 11", "Oldstable"),
        ("alpine320", "⛰️ Alpine 3.20", "Minimal"),
        ("python312", "🐍 Python 3.12", "Dev"),
        ("node22", "🟢 Node.js 22", "Dev"),
        ("nginx", "🌐 Nginx", "Web"),
        ("redis", "🔴 Redis 7", "Cache"),
    ]
    template_options = "".join(
        f'<option value="{k}">{n} — <small style="color:var(--muted)">{d}</small></option>'
        for k, n, d in templates
    )

    body = f"""
    <div class="page-head">
        <div>
            <h1 class="page-title">➕ Deploy New VPS</h1>
            <div class="page-sub">Launch a Docker-powered virtual server in seconds.</div>
        </div>
        <a href="/dashboard" class="btn btn-ghost">← Back</a>
    </div>

    <div class="card">
        {% if error %}<div class="flash error">{{ error }}</div>{% endif %}
        <form method="post">
            <div class="row">
                <div class="field">
                    <label>VPS Name</label>
                    <input class="input" name="name" placeholder="my-awesome-vps"
                           pattern="[a-z0-9-]+" required maxlength="40">
                    <small style="color:var(--muted)">Lowercase, numbers, hyphens only.</small>
                </div>
                <div class="field">
                    <label>Operating System / Template</label>
                    <select class="select" name="template" required>
                        {template_options}
                    </select>
                </div>
            </div>

            <div class="row">
                <div class="field">
                    <label>CPU Cores (max {get_host_cpus()})</label>
                    <input class="input" name="cpu" type="number"
                           min="0.01" max="{get_host_cpus()}" step="0.01"
                           value="{get_setting('default_cpu','2')}">
                </div>
                <div class="field">
                    <label>RAM (MB)</label>
                    <input class="input" name="ram" type="number"
                           min="128" value="{get_setting('default_ram','8192')}">
                </div>
            </div>

            <div class="row">
                <div class="field">
                    <label>Disk Size (GB)</label>
                    <input class="input" name="disk" type="number"
                           min="5" value="{get_setting('default_disk','30')}">
                </div>
                <div class="field">
                    <label>PID Limit</label>
                    <input class="input" name="pids" type="number"
                           min="32" value="{get_setting('default_pids','256')}">
                </div>
            </div>

            <div style="display:flex;gap:10px;margin-top:10px">
                <button class="btn btn-primary btn-lg" type="submit">
                    🚀 Deploy VPS
                </button>
                <a href="/dashboard" class="btn btn-ghost btn-lg">Cancel</a>
            </div>
        </form>
    </div>
    """.replace("{% if error %}<div class=\"flash error\">{{ error }}</div>{% endif %}",
                f'<div class="flash error">{error}</div>' if error else "")

    return page(body, "Create VPS", active="create")


# ============================================================
# VPS Management
# ============================================================
@app.route("/vps/<int:vps_id>")
def manage(vps_id):
    r = require_login()
    if r:
        return r
    vps = get_vps(vps_id)
    if not vps:
        abort(404)
    if not user_can_access_vps(vps):
        abort(403)

    try:
        c = docker_client.containers.get(vps["container_id"])
        status = c.status
        stats = c.stats(stream=False)
        cpu_pct = 0.0
        mem_use = 0
        mem_limit = 1
        if "cpu_stats" in stats and "precpu_stats" in stats:
            cpu_delta = (
                stats["cpu_stats"]["cpu_usage"]["total_usage"]
                - stats["precpu_stats"]["cpu_usage"]["total_usage"]
            )
            sys_delta = (
                stats["cpu_stats"]["system_cpu_usage"]
                - stats["precpu_stats"]["system_cpu_usage"]
            )
            ncpu = stats["cpu_stats"].get("online_cpus", 1)
            if sys_delta > 0:
                cpu_pct = (cpu_delta / sys_delta) * ncpu * 100
        if "memory_stats" in stats:
            mem_use = stats["memory_stats"].get("usage", 0)
            mem_limit = stats["memory_stats"].get("limit", 1)
        mem_pct = (mem_use / mem_limit) * 100 if mem_limit else 0
    except Exception as e:
        status = "missing"
        cpu_pct = mem_pct = 0
        mem_use = 0

    dot = "dot-ok" if status == "running" else "dot-err"
    badge = "badge-ok" if status == "running" else "badge-err"

    body = f"""
    <div class="page-head">
        <div>
            <h1 class="page-title">🐳 {vps['name']}</h1>
            <div class="page-sub">
                <span class="badge {badge}">
                    <span class="dot {dot}"></span> {status.upper()}
                </span>
                · {vps['image']} · Created {human_time(vps['created'])}
            </div>
        </div>
        <a href="/dashboard" class="btn btn-ghost">← Dashboard</a>
    </div>

    <div class="stats">
        <div class="stat">
            <div class="stat-icon purple">⚡</div>
            <div class="stat-label">CPU Usage</div>
            <div class="stat-value">{cpu_pct:.1f}%</div>
            <div class="progress" style="margin-top:8px">
                <div class="progress-bar" style="width:{min(cpu_pct,100)}%"></div>
            </div>
            <div class="stat-delta">Allocated: {vps['cpu']} cores</div>
        </div>
        <div class="stat">
            <div class="stat-icon pink">💾</div>
            <div class="stat-label">Memory Usage</div>
            <div class="stat-value">{mem_pct:.1f}%</div>
            <div class="progress" style="margin-top:8px">
                <div class="progress-bar" style="width:{min(mem_pct,100)}%"></div>
            </div>
            <div class="stat-delta">{format_bytes(mem_use)} / {format_bytes(mem_limit)}</div>
        </div>
        <div class="stat">
            <div class="stat-icon blue">💿</div>
            <div class="stat-label">Disk</div>
            <div class="stat-value">{vps['disk']} GB</div>
            <div class="stat-delta">Allocated volume</div>
        </div>
        <div class="stat">
            <div class="stat-icon green">🔧</div>
            <div class="stat-label">PID Limit</div>
            <div class="stat-value">{vps['pids']}</div>
            <div class="stat-delta">RAM: {round(vps['ram']/1024,1)} GB</div>
        </div>
    </div>

    <div class="card">
        <h2>⚙️ Quick Actions</h2>
        <div style="display:flex;gap:10px;flex-wrap:wrap">
            <a href="/action/{vps_id}/start" class="btn btn-success">▶ Start</a>
            <a href="/action/{vps_id}/stop" class="btn btn-warn">⏹ Stop</a>
            <a href="/action/{vps_id}/restart" class="btn btn-info">🔄 Restart</a>
            <a href="/terminal/{vps_id}" class="btn btn-primary">💻 Web Terminal</a>
            <a href="/tmate/{vps_id}" class="btn btn-ghost">🔐 Tmate SSH</a>
            <a href="/files/{vps_id}" class="btn btn-ghost">📁 Files</a>
            <a href="/logs/{vps_id}" class="btn btn-ghost">📜 Logs</a>
            <a href="/console/{vps_id}" class="btn btn-ghost">⌨️ Exec</a>
            <form method="post" action="/reinstall/{vps_id}" style="display:inline"
                  onsubmit="return confirm('Reinstall this VPS? All data will be lost!')">
                <button class="btn btn-warn">♻️ Reinstall</button>
            </form>
            <form method="post" action="/delete/{vps_id}" style="display:inline"
                  onsubmit="return confirm('Delete this VPS permanently?')">
                <button class="btn btn-danger">🗑️ Delete</button>
            </form>
        </div>
    </div>

    <div class="card">
        <h2>📋 Details</h2>
        <table class="table">
            <tr><td style="color:var(--muted)">Container ID</td><td><code>{vps['container_id'][:12]}</code></td></tr>
            <tr><td style="color:var(--muted)">Image</td><td>{vps['image']}</td></tr>
            <tr><td style="color:var(--muted)">Hostname</td><td>{vps['name']}</td></tr>
            <tr><td style="color:var(--muted)">Resources</td><td>{vps['cpu']} CPU · {round(vps['ram']/1024,1)} GB RAM · {vps['disk']} GB Disk</td></tr>
            <tr><td style="color:var(--muted)">Created</td><td>{human_time(vps['created'])}</td></tr>
            <tr><td style="color:var(--muted)">Owner</td><td>{'Admin' if not vps['owner_id'] else 'User #'+str(vps['owner_id'])}</td></tr>
        </table>
    </div>

    <div class="card">
        <h2>📝 Notes</h2>
        <form method="post" action="/vps/{vps_id}/notes">
            <textarea class="input" name="notes" rows="3"
                      placeholder="Add notes about this VPS...">{vps['notes'] or ''}</textarea>
            <button class="btn btn-primary" style="margin-top:10px" type="submit">💾 Save Notes</button>
        </form>
    </div>
    """
    return page(body, vps["name"], active="dashboard")


@app.route("/vps/<int:vps_id>/notes", methods=["POST"])
def vps_notes(vps_id):
    r = require_login()
    if r:
        return r
    vps = get_vps(vps_id)
    if not vps or not user_can_access_vps(vps):
        abort(404)
    notes = request.form.get("notes", "")[:1000]
    with db() as con:
        con.execute("UPDATE vps SET notes=? WHERE id=?", (notes, vps_id))
    flash("Notes saved.", "ok")
    return redirect(f"/vps/{vps_id}")


# ============================================================
# Actions
# ============================================================
@app.route("/action/<int:vps_id>/<action>")
def action(vps_id, action):
    r = require_login()
    if r:
        return r
    vps = get_vps(vps_id)
    if not vps or not user_can_access_vps(vps):
        abort(404)
    c = get_container(vps)
    try:
        if action == "start":
            c.start()
        elif action == "stop":
            c.stop(timeout=10)
        elif action == "restart":
            c.restart(timeout=10)
        else:
            abort(400)
        log_event(current_user()["username"], f"vps_{action}", vps["name"])
        flash(f"VPS {action} successful.", "ok")
    except Exception as e:
        flash(f"Action failed: {e}", "error")
    return redirect(f"/vps/{vps_id}")


# ============================================================
# Web Terminal (exec-based)
# ============================================================
@app.route("/terminal/<int:vps_id>")
def terminal(vps_id):
    r = require_login()
    if r:
        return r
    vps = get_vps(vps_id)
    if not vps or not user_can_access_vps(vps):
        abort(404)

    body = f"""
    <div class="page-head">
        <div>
            <h1 class="page-title">💻 Web Terminal · {vps['name']}</h1>
            <div class="page-sub">Interactive shell inside your VPS.</div>
        </div>
        <a href="/vps/{vps_id}" class="btn btn-ghost">← Back</a>
    </div>
    <div class="card">
        <div class="term-wrap">
            <div class="term-head">
                <div class="term-dots"><span></span><span></span><span></span></div>
                <div class="term-title">root@{vps['name']}:~</div>
            </div>
            <div id="terminal"></div>
        </div>
        <div style="margin-top:14px;display:flex;gap:10px;flex-wrap:wrap">
            <input class="input" id="cmd" placeholder="Type a command..." style="flex:1"
                   onkeydown="if(event.key==='Enter')runCmd()">
            <button class="btn btn-primary" onclick="runCmd()">▶ Run</button>
            <button class="btn btn-ghost" onclick="document.getElementById('terminal').innerHTML=''">🧹 Clear</button>
        </div>
        <p style="color:var(--muted);font-size:13px;margin-top:10px">
            💡 Each command runs independently. For persistent sessions use
            <a href="/tmate/{vps_id}">Tmate SSH</a>.
        </p>
    </div>
    """
    js = f"""
    <script>
    const term=document.getElementById('terminal');
    const vpsId={vps_id};
    function appendOut(t,cls=''){
        const d=document.createElement('div');
        d.style.whiteSpace='pre-wrap';
        d.style.fontFamily='monospace';
        d.style.fontSize='13px';
        d.style.lineHeight='1.5';
        d.className=cls;
        d.textContent=t;
        term.appendChild(d);
        term.scrollTop=term.scrollHeight;
    }
    async function runCmd(){
        const inp=document.getElementById('cmd');
        const cmd=inp.value.trim();
        if(!cmd)return;
        appendOut('$ '+cmd,'');
        inp.value='';
        try{
            const r=await fetch('/api/exec/'+vpsId,{
                method:'POST',
                headers:{{'Content-Type':'application/json'}},
                body:JSON.stringify({{cmd:cmd}})
            });
            const j=await r.json();
            if(j.stdout)appendOut(j.stdout);
            if(j.stderr)appendOut(j.stderr,'');
            if(j.error)appendOut('Error: '+j.error,'');
        }catch(e){
            appendOut('Request failed: '+e.message,'');
        }
    }
    appendOut('Welcome to '+{json.dumps(vps["name"])+' shell'});
    appendOut('Type commands below and press Enter.');
    appendOut('');
    </script>
    """
    return page(body, "Terminal · " + vps["name"], active="dashboard", extra_js=js)


@app.route("/api/exec/<int:vps_id>", methods=["POST"])
def api_exec(vps_id):
    r = require_login()
    if r:
        return r
    vps = get_vps(vps_id)
    if not vps or not user_can_access_vps(vps):
        return jsonify({"error": "not found"}), 404
    try:
        c = get_container(vps)
        data = request.get_json(force=True) or {}
        cmd = (data.get("cmd") or "").strip()
        if not cmd:
            return jsonify({"error": "empty command"})
        # Wrap in bash -lc for proper shell behavior
        res = c.exec_run(["bash", "-lc", cmd], demux=True)
        out, err = res.output or (b"", b"")
        return jsonify({
            "stdout": (out or b"").decode("utf-8", "replace"),
            "stderr": (err or b"").decode("utf-8", "replace"),
            "exit": res.exit_code,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# Tmate SSH
# ============================================================
@app.route("/tmate/<int:vps_id>", methods=["GET", "POST"])
def tmate_session(vps_id):
    r = require_login()
    if r:
        return r
    vps = get_vps(vps_id)
    if not vps or not user_can_access_vps(vps):
        abort(404)
    c = get_container(vps)
    if c.status != "running":
        flash("VPS is not running. Start it first.", "warn")
        return redirect(f"/vps/{vps_id}")

    # Install tmate if missing
    check = c.exec_run(["bash", "-lc", "command -v tmate >/dev/null 2>&1"])
    if check.exit_code != 0:
        install = (
            "export DEBIAN_FRONTEND=noninteractive; "
            "apt-get update -qq && apt-get install -y -qq tmate"
        )
        res = c.exec_run(["bash", "-lc", install])
        if res.exit_code != 0:
            flash("Tmate installation failed.", "error")
            return redirect(f"/vps/{vps_id}")

    # Start tmate
    start_cmd = r"""
SSH="$(tmate display -p '#{tmate_ssh}' 2>/dev/null || true)"
if [ -z "$SSH" ] || [ "$SSH" = "no sessions" ]; then
    nohup tmate -F >/tmp/kc-tmate.log 2>&1 &
fi
"""
    c.exec_run(["bash", "-lc", start_cmd])

    ssh_url = ""
    for _ in range(30):
        time.sleep(1)
        res = c.exec_run(
            ["bash", "-lc",
             "grep -E 'ssh session: ssh .*@.*tmate\\.io' /tmp/kc-tmate.log 2>/dev/null | tail -1 || true"]
        )
        value = (res.output or b"").decode("utf-8", "ignore").strip()
        if "ssh session: " in value:
            ssh_url = value.split("ssh session: ", 1)[1].strip()
            break

    if not ssh_url:
        flash("Tmate is still starting. Please retry in a few seconds.", "warn")
        return redirect(f"/vps/{vps_id}")

    safe = ssh_url.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    log_event(current_user()["username"], "tmate", vps["name"])

    body = f"""
    <div class="page-head">
        <div>
            <h1 class="page-title">🔐 Tmate Session · {vps['name']}</h1>
            <div class="page-sub">SSH into your VPS from anywhere.</div>
        </div>
        <a href="/vps/{vps_id}" class="btn btn-ghost">← Back</a>
    </div>
    <div class="card">
        <h2>🟢 SSH Session Ready</h2>
        <p style="color:var(--muted)">Copy this command into your terminal, Termux, or JuiceSSH:</p>
        <pre style="word-break:break-all;cursor:pointer" onclick="navigator.clipboard.writeText(this.textContent);toast('Copied!','ok')">{safe}</pre>
        <div style="display:flex;gap:10px;margin-top:14px;flex-wrap:wrap">
            <button class="btn btn-primary" onclick="navigator.clipboard.writeText(document.querySelector('pre').textContent);toast('Copied!','ok')">📋 Copy Command</button>
            <form method="post" style="display:inline">
                <button class="btn btn-warn" type="submit">🔄 Generate New Session</button>
            </form>
            <a href="/vps/{vps_id}" class="btn btn-ghost">Back to VPS</a>
        </div>
    </div>
    """
    return page(body, "Tmate · " + vps["name"], active="dashboard")


# ============================================================
# File Manager (basic)
# ============================================================
@app.route("/files/<int:vps_id>")
def files(vps_id):
    r = require_login()
    if r:
        return r
    vps = get_vps(vps_id)
    if not vps or not user_can_access_vps(vps):
        abort(404)
    c = get_container(vps)
    path = request.args.get("path", "/")
    if c.status != "running":
        flash("VPS must be running to browse files.", "warn")
        return redirect(f"/vps/{vps_id}")
    try:
        res = c.exec_run(
            ["bash", "-lc",
             f"ls -la --time-style=long-iso {json.dumps(path)} 2>&1 | head -200"]
        )
        listing = (res.output or b"").decode("utf-8", "replace")
    except Exception as e:
        listing = "Error: " + str(e)

    rows = ""
    for line in listing.splitlines()[1:]:
        parts = line.split(None, 8)
        if len(parts) < 8:
            continue
        perms, _, owner, group, size, date, time_, name = parts
        is_dir = perms.startswith("d")
        icon = "📁" if is_dir else "📄"
        if name in (".", ".."):
            continue
        next_path = path.rstrip("/") + "/" + name if is_dir else path
        link = f"/files/{vps_id}?path={next_path}" if is_dir else f"/files/{vps_id}/view?path={next_path}"
        rows += f"""
        <tr>
            <td>{icon} <a href="{link}">{name}</a></td>
            <td style="color:var(--muted)">{perms}</td>
            <td>{owner}:{group}</td>
            <td>{size}</td>
            <td style="color:var(--muted)">{date} {time_}</td>
        </tr>
        """

    body = f"""
    <div class="page-head">
        <div>
            <h1 class="page-title">📁 File Manager · {vps['name']}</h1>
            <div class="page-sub"><code>{path}</code></div>
        </div>
        <a href="/vps/{vps_id}" class="btn btn-ghost">← Back</a>
    </div>
    <div class="card">
        <form method="get" style="display:flex;gap:10px;margin-bottom:14px">
            <input class="input" name="path" value="{path}" placeholder="/path/to/dir">
            <button class="btn btn-primary" type="submit">Go</button>
        </form>
        <table class="table">
            <thead>
                <tr><th>Name</th><th>Perms</th><th>Owner</th><th>Size</th><th>Modified</th></tr>
            </thead>
            <tbody>{rows or '<tr><td colspan="5" style="color:var(--muted)">Empty or inaccessible.</td></tr>'}</tbody>
        </table>
    </div>
    """
    return page(body, "Files · " + vps["name"], active="dashboard")


@app.route("/files/<int:vps_id>/view")
def file_view(vps_id):
    r = require_login()
    if r:
        return r
    vps = get_vps(vps_id)
    if not vps or not user_can_access_vps(vps):
        abort(404)
    c = get_container(vps)
    path = request.args.get("path", "")
    try:
        res = c.exec_run(["bash", "-lc", f"head -c 100000 {json.dumps(path)}"])
        content = (res.output or b"").decode("utf-8", "replace")
    except Exception as e:
        content = "Error: " + str(e)
    body = f"""
    <div class="page-head">
        <div>
            <h1 class="page-title">📄 {path}</h1>
        </div>
        <a href="/files/{vps_id}?path={'/'.join(path.split('/')[:-1]) or '/'}" class="btn btn-ghost">← Back</a>
    </div>
    <div class="card"><pre>{content}</pre></div>
    """
    return page(body, path, active="dashboard")


# ============================================================
# Logs
# ============================================================
@app.route("/logs/<int:vps_id>")
def logs(vps_id):
    r = require_login()
    if r:
        return r
    vps = get_vps(vps_id)
    if not vps or not user_can_access_vps(vps):
        abort(404)
    c = get_container(vps)
    try:
        output = c.logs(tail=500).decode("utf-8", errors="replace")
    except Exception as e:
        output = str(e)
    body = f"""
    <div class="page-head">
        <div>
            <h1 class="page-title">📜 Logs · {vps['name']}</h1>
            <div class="page-sub">Last 500 lines</div>
        </div>
        <a href="/vps/{vps_id}" class="btn btn-ghost">← Back</a>
    </div>
    <div class="card"><pre>{output or '(no logs)'}</pre></div>
    """
    return page(body, "Logs · " + vps["name"], active="dashboard")


# ============================================================
# Console (simple exec form)
# ============================================================
@app.route("/console/<int:vps_id>", methods=["GET", "POST"])
def console(vps_id):
    r = require_login()
    if r:
        return r
    vps = get_vps(vps_id)
    if not vps or not user_can_access_vps(vps):
        abort(404)
    c = get_container(vps)
    out = ""
    cmd = ""
    if request.method == "POST":
        cmd = request.form.get("cmd", "")
        if cmd:
            try:
                res = c.exec_run(["bash", "-lc", cmd])
                out = (res.output or b"").decode("utf-8", "replace")
            except Exception as e:
                out = "Error: " + str(e)
    body = f"""
    <div class="page-head">
        <div>
            <h1 class="page-title">⌨️ Command Console · {vps['name']}</h1>
        </div>
        <a href="/vps/{vps_id}" class="btn btn-ghost">← Back</a>
    </div>
    <div class="card">
        <form method="post">
            <div class="field">
                <label>Command</label>
                <input class="input" name="cmd" value="{cmd}" placeholder="ls -la /" autofocus>
            </div>
            <button class="btn btn-primary" type="submit">▶ Run</button>
        </form>
        {% if out %}<pre style="margin-top:14px">{out}</pre>{% endif %}
    </div>
    """
    body = body.replace("{% if out %}<pre style=\"margin-top:14px\">{out}</pre>{% endif %}",
                        f'<pre style="margin-top:14px">{out}</pre>' if out else "")
    return page(body, "Console · " + vps["name"], active="dashboard")


# ============================================================
# Reinstall / Delete
# ============================================================
@app.route("/reinstall/<int:vps_id>", methods=["POST"])
def reinstall(vps_id):
    r = require_login()
    if r:
        return r
    vps = get_vps(vps_id)
    if not vps or not user_can_access_vps(vps):
        abort(404)
    old = get_container(vps)
    image = vps["image"]
    name = old.name
    try:
        old.remove(force=True)
    except Exception:
        pass
    try:
        docker_client.images.pull(image)
    except Exception:
        pass
    container = docker_client.containers.create(
        image=image, name=name,
        command=["sleep", "infinity"],
        mem_limit=f'{vps["ram"]}m',
        nano_cpus=int(vps["cpu"] * 1_000_000_000),
        pids_limit=vps["pids"],
        labels={"kingcloud.managed": "true"},
        restart_policy={"Name": "unless-stopped"},
        tty=True, stdin_open=True, hostname=vps["name"],
    )
    container.start()
    with db() as con:
        con.execute("UPDATE vps SET container_id=? WHERE id=?", (container.id, vps_id))
    log_event(current_user()["username"], "reinstall", vps["name"])
    flash("VPS reinstalled with fresh image.", "ok")
    return redirect(f"/vps/{vps_id}")


@app.route("/delete/<int:vps_id>", methods=["POST"])
def delete(vps_id):
    r = require_login()
    if r:
        return r
    vps = get_vps(vps_id)
    if not vps or not user_can_access_vps(vps):
        abort(404)
    try:
        c = docker_client.containers.get(vps["container_id"])
        c.remove(force=True)
        try:
            docker_client.volumes.get("kc-" + vps["name"] + "-data").remove()
        except Exception:
            pass
    except Exception:
        pass
    with db() as con:
        con.execute("DELETE FROM vps WHERE id=?", (vps_id,))
    log_event(current_user()["username"], "delete_vps", vps["name"])
    flash("VPS deleted.", "ok")
    return redirect("/dashboard")


# ============================================================
# Profile
# ============================================================
@app.route("/profile", methods=["GET", "POST"])
def profile():
    r = require_login()
    if r:
        return r
    u = current_user()
    if request.method == "POST":
        old = request.form.get("old_password", "")
        new = request.form.get("new_password", "")
        if len(new) < 6:
            flash("New password must be at least 6 characters.", "error")
        else:
            with db() as con:
                if is_admin():
                    row = con.execute(
                        "SELECT * FROM admin WHERE username=?", (u["username"],)
                    ).fetchone()
                    if row and check_password_hash(row["password_hash"], old):
                        con.execute(
                            "UPDATE admin SET password_hash=? WHERE username=?",
                            (generate_password_hash(new), u["username"]),
                        )
                        flash("Password updated.", "ok")
                    else:
                        flash("Current password is incorrect.", "error")
                else:
                    row = con.execute(
                        "SELECT * FROM users WHERE id=?", (u["id"],)
                    ).fetchone()
                    if row and check_password_hash(row["password_hash"], old):
                        con.execute(
                            "UPDATE users SET password_hash=? WHERE id=?",
                            (generate_password_hash(new), u["id"]),
                        )
                        flash("Password updated.", "ok")
                    else:
                        flash("Current password is incorrect.", "error")
    body = f"""
    <div class="page-head">
        <h1 class="page-title">👤 My Profile</h1>
    </div>
    <div class="card" style="max-width:600px">
        <h2>Account Info</h2>
        <table class="table">
            <tr><td style="color:var(--muted)">Username</td><td><b>{u['username']}</b></td></tr>
            <tr><td style="color:var(--muted)">Role</td><td>{'Administrator' if is_admin() else 'User'}</td></tr>
            <tr><td style="color:var(--muted)">Member since</td><td>{human_time(u.get('created',0))}</td></tr>
        </table>
    </div>
    <div class="card" style="max-width:600px">
        <h2>🔐 Change Password</h2>
        <form method="post">
            <div class="field">
                <label>Current Password</label>
                <input class="input" name="old_password" type="password" required>
            </div>
            <div class="field">
                <label>New Password</label>
                <input class="input" name="new_password" type="password" required minlength="6">
            </div>
            <button class="btn btn-primary" type="submit">💾 Update Password</button>
        </form>
    </div>
    """
    return page(body, "Profile", active="profile")


# ============================================================
# Admin Panel
# ============================================================
@app.route("/admin", methods=["GET", "POST"])
def admin_panel():
    r = require_admin()
    if r:
        return r

    if request.method == "POST":
        for key in ["panel_name", "panel_tagline", "user_vps_slots", "user_vps_limit",
                    "default_cpu", "default_ram", "default_pids", "default_disk",
                    "allow_registration", "maintenance_mode"]:
            val = request.form.get(key, "")
            if val != "":
                set_setting(key, val)
        flash("Settings saved.", "ok")
        log_event(current_user()["username"], "settings_update", "")
        return redirect("/admin")

    keys = [
        ("panel_name", "Panel Name", "input"),
        ("panel_tagline", "Tagline", "input"),
        ("user_vps_slots", "Max Total User VPS", "input"),
        ("user_vps_limit", "VPS per User", "input"),
        ("default_cpu", "Default CPU", "input"),
        ("default_ram", "Default RAM (MB)", "input"),
        ("default_pids", "Default PID Limit", "input"),
        ("default_disk", "Default Disk (GB)", "input"),
        ("allow_registration", "Allow Registration (1/0)", "input"),
        ("maintenance_mode", "Maintenance Mode (1/0)", "input"),
    ]
    fields = ""
    for k, lbl, typ in keys:
        v = get_setting(k, "")
        fields += f"""
        <div class="field">
            <label>{lbl}</label>
            <input class="input" name="{k}" value="{v}">
        </div>
        """

    # Host stats
    host_cpu = psutil.cpu_percent(interval=0.2)
    host_ram = psutil.virtual_memory()
    host_disk = psutil.disk_usage("/")

    body = f"""
    <div class="page-head">
        <h1 class="page-title">⚙️ Admin Panel</h1>
    </div>

    <div class="stats">
        <div class="stat">
            <div class="stat-icon blue">🖥️</div>
            <div class="stat-label">Host CPU</div>
            <div class="stat-value">{host_cpu:.1f}%</div>
        </div>
        <div class="stat">
            <div class="stat-icon green">💾</div>
            <div class="stat-label">Host RAM</div>
            <div class="stat-value">{host_ram.percent:.1f}%</div>
            <div class="stat-delta">{format_bytes(host_ram.used)} / {format_bytes(host_ram.total)}</div>
        </div>
        <div class="stat">
            <div class="stat-icon purple">💿</div>
            <div class="stat-label">Host Disk</div>
            <div class="stat-value">{host_disk.percent:.1f}%</div>
            <div class="stat-delta">{format_bytes(host_disk.used)} / {format_bytes(host_disk.total)}</div>
        </div>
        <div class="stat">
            <div class="stat-icon pink">⚡</div>
            <div class="stat-label">CPUs</div>
            <div class="stat-value">{get_host_cpus()}</div>
        </div>
    </div>

    <div class="card">
        <h2>🎨 Panel Configuration</h2>
        <form method="post">
            {fields}
            <button class="btn btn-primary" type="submit">💾 Save Settings</button>
        </form>
    </div>

    <div class="card">
        <h2>🔗 Quick Links</h2>
        <div style="display:flex;gap:10px;flex-wrap:wrap">
            <a href="/users" class="btn btn-info">👥 Manage Users</a>
            <a href="/activity" class="btn btn-primary">📊 Activity Log</a>
            <a href="/health" class="btn btn-ghost" target="_blank">🏥 Health Check</a>
        </div>
    </div>
    """
    return page(body, "Admin", active="admin")


# ============================================================
# Users Admin
# ============================================================
@app.route("/users", methods=["GET", "POST"])
def users_admin():
    r = require_admin()
    if r:
        return r

    if request.method == "POST":
        action = request.form.get("action", "")
        uid = request.form.get("user_id", "")
        try:
            uid = int(uid)
        except Exception:
            uid = 0
        if action == "delete" and uid:
            with db() as con:
                con.execute("DELETE FROM users WHERE id=?", (uid,))
                con.execute("UPDATE vps SET owner_id=NULL WHERE owner_id=?", (uid,))
            flash("User deleted.", "ok")
            log_event(current_user()["username"], "delete_user", f"id={uid}")
        elif action == "reset" and uid:
            new_pass = request.form.get("new_password", "")
            if len(new_pass) >= 6:
                with db() as con:
                    con.execute(
                        "UPDATE users SET password_hash=? WHERE id=?",
                        (generate_password_hash(new_pass), uid),
                    )
                flash("Password reset.", "ok")
            else:
                flash("Password must be at least 6 characters.", "error")
        return redirect("/users")

    with db() as con:
        users = con.execute(
            "SELECT u.*, (SELECT COUNT(*) FROM vps WHERE owner_id=u.id) as vps_count "
            "FROM users u ORDER BY id DESC"
        ).fetchall()

    rows = ""
    for u in users:
        rows += f"""
        <tr>
            <td><b>{u['username']}</b></td>
            <td>{u['vps_count']}</td>
            <td>{human_time(u['created'])}</td>
            <td>{human_time(u['last_login']) if u['last_login'] else '—'}</td>
            <td>
                <form method="post" style="display:inline" onsubmit="return confirm('Delete user?')">
                    <input type="hidden" name="action" value="delete">
                    <input type="hidden" name="user_id" value="{u['id']}">
                    <button class="btn btn-sm btn-danger">🗑️ Delete</button>
                </form>
                <details style="display:inline">
                    <summary class="btn btn-sm btn-warn">🔑 Reset Pass</summary>
                    <form method="post" style="margin-top:8px;display:flex;gap:6px">
                        <input type="hidden" name="action" value="reset">
                        <input type="hidden" name="user_id" value="{u['id']}">
                        <input class="input" name="new_password" type="password" placeholder="New password" minlength="6" required>
                        <button class="btn btn-sm btn-primary">Save</button>
                    </form>
                </details>
            </td>
        </tr>
        """

    body = f"""
    <div class="page-head">
        <h1 class="page-title">👥 User Management</h1>
    </div>
    <div class="card">
        <p style="color:var(--muted)">Total: <b>{len(users)}</b> users</p>
        <table class="table">
            <thead>
                <tr><th>Username</th><th>VPS</th><th>Joined</th><th>Last Login</th><th>Actions</th></tr>
            </thead>
            <tbody>{rows or '<tr><td colspan="5" style="color:var(--muted)">No users yet.</td></tr>'}</tbody>
        </table>
    </div>
    """
    return page(body, "Users", active="users")


# ============================================================
# Activity Log
# ============================================================
@app.route("/activity")
def activity():
    r = require_admin()
    if r:
        return r
    with db() as con:
        rows = con.execute(
            "SELECT * FROM activity ORDER BY id DESC LIMIT 200"
        ).fetchall()
    items = ""
    for r in rows:
        items += f"""
        <tr>
            <td>{human_time(r['created'])}</td>
            <td><b>{r['user']}</b></td>
            <td><span class="badge badge-info">{r['action']}</span></td>
            <td style="color:var(--muted)">{r['detail']}</td>
        </tr>
        """
    body = f"""
    <div class="page-head">
        <h1 class="page-title">📊 Activity Log</h1>
    </div>
    <div class="card">
        <table class="table">
            <thead>
                <tr><th>Time</th><th>User</th><th>Action</th><th>Detail</th></tr>
            </thead>
            <tbody>{items or '<tr><td colspan="4" style="color:var(--muted)">No activity yet.</td></tr>'}</tbody>
        </table>
    </div>
    """
    return page(body, "Activity", active="activity")


# ============================================================
# Health
# ============================================================
@app.route("/health")
def health():
    with db() as con:
        users = con.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        vps = con.execute("SELECT COUNT(*) c FROM vps").fetchone()["c"]
    return jsonify({
        "status": "online",
        "panel": get_setting("panel_name", "KingCloud"),
        "version": "2.0.0",
        "users": users,
        "vps": vps,
        "host": {
            "cpu": psutil.cpu_percent(interval=0.1),
            "ram_percent": psutil.virtual_memory().percent,
            "disk_percent": psutil.disk_usage("/").percent,
            "cpus": get_host_cpus(),
        },
    })


# ============================================================
# Root
# ============================================================
@app.route("/")
def root():
    if logged_in():
        return redirect("/dashboard")
    return redirect("/login")


# ============================================================
# Boot
# ============================================================
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
