import os
import re
import secrets
import sqlite3
import time

import docker
import psutil

from flask import (
    Flask,
    request,
    redirect,
    session,
    render_template_string,
    abort,
)

from werkzeug.security import generate_password_hash, check_password_hash


APP_DIR = "/opt/dockervps"
DB_FILE = os.path.join(APP_DIR, "panel.db")

ADMIN_USER = os.environ.get("DVP_ADMIN", "admin")
ADMIN_PASSWORD = os.environ.get("DVP_PASSWORD", "")
SECRET_KEY = os.environ.get("DVP_SECRET", secrets.token_hex(32))

app = Flask(__name__)
app.secret_key = SECRET_KEY

docker_client = docker.from_env()


# ---------------------------------------------------------
# Database
# ---------------------------------------------------------

def db():
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    try:
        with db() as con:
            con.execute("ALTER TABLE vps ADD COLUMN disk INTEGER DEFAULT 30")
    except Exception:
        pass

    os.makedirs(APP_DIR, exist_ok=True)

    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS admin (
                id INTEGER PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        con.execute("""
            INSERT OR IGNORE INTO settings(key,value)
            VALUES ('panel_name','King Cloud')
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS vps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                container_id TEXT NOT NULL,
                image TEXT NOT NULL,
                cpu REAL NOT NULL,
                ram INTEGER NOT NULL,
                pids INTEGER NOT NULL,
                created INTEGER NOT NULL
            )
        """)

        row = con.execute(
            "SELECT id FROM admin WHERE username=?",
            (ADMIN_USER,)
        ).fetchone()

        if not row:
            con.execute(
                "INSERT INTO admin(username,password_hash) VALUES (?,?)",
                (
                    ADMIN_USER,
                    generate_password_hash(ADMIN_PASSWORD)
                )
            )


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def logged_in():
    return bool(
        session.get("admin") == ADMIN_USER
        or session.get("user_id")
    )


def require_login():
    if not logged_in():
        return redirect("/login")
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

    # Docker accepts values from 0.01 to host CPU count.
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


def get_vps(vps_id):
    with db() as con:
        return con.execute(
            "SELECT * FROM vps WHERE id=?",
            (vps_id,)
        ).fetchone()


def get_container(vps):
    try:
        return docker_client.containers.get(vps["container_id"])
    except docker.errors.NotFound:
        abort(404, "Docker container not found")


def image_for_template(template):
    images = {
        "ubuntu": "ubuntu:24.04",
        "debian": "debian:12",
        "alpine": "alpine:3.20",
        "python": "python:3.12-slim",
        "node": "node:22-bookworm",
    }

    return images.get(template, "ubuntu:24.04")


# ---------------------------------------------------------
# UI
# ---------------------------------------------------------

CSS = """
* {
    box-sizing: border-box;
}

body {
    margin: 0;
    background: #07101f;
    color: #eef3ff;
    font-family: Arial, sans-serif;
}

nav {
    background: #0d172b;
    border-bottom: 1px solid #263653;
    padding: 16px 20px;
    position: sticky;
    top: 0;
    z-index: 10;
}

nav a {
    color: #fff;
    text-decoration: none;
    margin-right: 18px;
    font-weight: 700;
}

main {
    max-width: 1200px;
    margin: auto;
    padding: 22px;
}

.grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 14px;
}

.card {
    background: #101c32;
    border: 1px solid #263653;
    border-radius: 14px;
    padding: 18px;
    margin-bottom: 16px;
}

.stat {
    font-size: 28px;
    font-weight: 800;
}

.muted {
    color: #93a4c0;
}

input,
select {
    width: 100%;
    padding: 12px;
    margin: 7px 0;
    background: #07101f;
    color: white;
    border: 1px solid #344361;
    border-radius: 9px;
}

button,
.btn {
    display: inline-block;
    border: 0;
    border-radius: 9px;
    padding: 10px 13px;
    margin: 3px;
    color: white;
    background: #2563eb;
    text-decoration: none;
    cursor: pointer;
}

.green { background: #16a34a; }
.red { background: #dc2626; }
.orange { background: #ea580c; }
.gray { background: #334155; }

table {
    width: 100%;
    border-collapse: collapse;
}

th,
td {
    padding: 12px 8px;
    border-bottom: 1px solid #263653;
    text-align: left;
}

pre {
    background: #040914;
    padding: 14px;
    border-radius: 10px;
    overflow: auto;
    white-space: pre-wrap;
}

@media(max-width:800px) {
    .grid {
        grid-template-columns: repeat(2, 1fr);
    }
}

@media(max-width:500px) {
    main {
        padding: 10px;
    }

    .grid {
        grid-template-columns: 1fr;
    }
}
"""


def get_panel_name():
    try:
        with db() as con:
            row=con.execute(
                "SELECT value FROM settings WHERE key='panel_name'"
            ).fetchone()
            return row["value"] if row else "King Cloud"
    except Exception:
        return "King Cloud"

def page(body, title=None):
    if not title:
        title=get_panel_name()
    return render_template_string(
        """
        <!doctype html>
        <html>
        <head>
            <meta name="viewport"
                  content="width=device-width,initial-scale=1">
            <title>{{ title }}</title>
            <style>{{ css|safe }}
<style>
/* Mobile navbar fix */
@media (max-width:700px) {
    nav {
        display:flex !important;
        flex-wrap:nowrap !important;
        align-items:center !important;
        justify-content:flex-start !important;
        gap:0 !important;
        padding:5px 4px !important;
        width:100% !important;
        max-width:100% !important;
        height:auto !important;
        min-height:42px !important;
        box-sizing:border-box !important;
        overflow-x:auto !important;
        overflow-y:hidden !important;
        white-space:nowrap !important;
    }

    nav a {
        display:inline-flex !important;
        align-items:center !important;
        justify-content:center !important;
        flex:0 0 auto !important;
        white-space:nowrap !important;
        font-size:10px !important;
        line-height:1 !important;
        padding:5px 5px !important;
        margin:0 !important;
        box-sizing:border-box !important;
    }

    body {
        overflow-x:hidden !important;
    }
}




/* MOBILE NAV SINGLE ROW */
@media (max-width:700px){
    nav{
        display:flex !important;
        flex-direction:row !important;
        flex-wrap:nowrap !important;
        align-items:center !important;
        justify-content:flex-start !important;
        gap:4px !important;

        width:100% !important;
        max-width:100% !important;
        height:58px !important;
        min-height:58px !important;

        padding:6px 8px !important;
        box-sizing:border-box !important;

        overflow-x:auto !important;
        overflow-y:hidden !important;

        white-space:nowrap !important;
    }

    nav a{
        display:inline-flex !important;
        flex:none !important;
        width:auto !important;
        max-width:none !important;
        height:42px !important;
        min-height:42px !important;

        align-items:center !important;
        justify-content:center !important;

        padding:6px 9px !important;
        margin:0 !important;

        white-space:nowrap !important;
        word-break:keep-all !important;
        overflow-wrap:normal !important;

        font-size:13px !important;
        line-height:1 !important;
    }

    nav::-webkit-scrollbar{
        display:none !important;
        height:0 !important;
    }
}

</style>



<style>
/* FINAL MOBILE NAV FIX V2 */
@media (max-width:700px){
    html,body{
        width:100%;
        max-width:100%;
        overflow-x:hidden !important;
    }

    nav{
        width:100% !important;
        max-width:100vw !important;
        min-width:0 !important;
        box-sizing:border-box !important;

        display:flex !important;
        flex-direction:row !important;
        flex-wrap:nowrap !important;
        align-items:center !important;
        justify-content:flex-start !important;

        gap:2px !important;
        padding:7px 6px !important;
        margin:0 !important;

        overflow:hidden !important;
        white-space:nowrap !important;
    }

    nav a{
        display:block !important;
        flex:0 1 auto !important;
        width:auto !important;
        min-width:0 !important;
        margin:0 !important;
        padding:6px 6px !important;

        font-size:11px !important;
        line-height:1.15 !important;
        white-space:nowrap !important;
        text-align:center !important;
    }
}
</style>
</style>
        
<style>
/* ===== KINGCLOUD RESPONSIVE HAMBURGER ===== */

.kc-nav{
    position:relative !important;
    display:flex !important;
    align-items:center !important;
    width:100% !important;
    min-height:58px !important;
    box-sizing:border-box !important;
    overflow:visible !important;
}

.kc-menu-btn{
    display:none !important;
    border:0 !important;
    background:transparent !important;
    color:white !important;
    font-size:28px !important;
    line-height:1 !important;
    padding:8px 12px !important;
    cursor:pointer !important;
}

.kc-nav-links{
    display:flex !important;
    align-items:center !important;
    gap:8px !important;
    width:100% !important;
}

.kc-nav-links a{
    white-space:nowrap !important;
    flex:0 0 auto !important;
}

/* MOBILE */
@media (max-width:700px){

    .kc-nav{
        min-height:58px !important;
        padding:6px 8px !important;
        overflow:visible !important;
    }

    .kc-menu-btn{
        display:block !important;
        flex:0 0 auto !important;
        z-index:10001 !important;
    }

    .kc-nav-links{
        display:none !important;
        position:absolute !important;
        left:8px !important;
        top:58px !important;
        width:220px !important;
        max-width:calc(100vw - 16px) !important;
        padding:8px !important;
        flex-direction:column !important;
        align-items:stretch !important;
        gap:4px !important;
        background:#0d1b33 !important;
        border:1px solid #263b5c !important;
        border-radius:10px !important;
        box-sizing:border-box !important;
        z-index:10000 !important;
        box-shadow:0 10px 30px rgba(0,0,0,.45) !important;
    }

    .kc-open .kc-nav-links{
        display:flex !important;
    }

    .kc-nav-links a{
        display:block !important;
        width:100% !important;
        box-sizing:border-box !important;
        padding:12px 14px !important;
        margin:0 !important;
        font-size:15px !important;
        line-height:1.2 !important;
        white-space:nowrap !important;
        border-radius:7px !important;
    }

    .kc-nav-links a:active{
        opacity:.75 !important;
    }
}
</style>


<style>
/* KINGCLOUD NAV */
.kc-nav{
    position:relative !important;
    width:100% !important;
    min-height:64px !important;
    display:flex !important;
    align-items:center !important;
    box-sizing:border-box !important;
    padding:10px 18px !important;
    gap:20px !important;
    overflow:visible !important;
}

.kc-brand{
    font-size:24px !important;
    font-weight:800 !important;
    color:#fff !important;
    white-space:nowrap !important;
}

.kc-menu-btn{
    display:none !important;
}

.kc-menu{
    display:flex !important;
    align-items:center !important;
    gap:10px !important;
}

.kc-menu a{
    white-space:nowrap !important;
}

/* MOBILE */
@media(max-width:700px){

    .kc-nav{
        min-height:58px !important;
        padding:8px 14px !important;
        justify-content:space-between !important;
        gap:10px !important;
    }

    .kc-brand{
        font-size:21px !important;
        order:1 !important;
    }

    .kc-menu-btn{
        display:block !important;
        order:2 !important;
        background:transparent !important;
        border:0 !important;
        color:white !important;
        font-size:28px !important;
        line-height:1 !important;
        padding:5px 8px !important;
        cursor:pointer !important;
    }

    .kc-menu{
        display:none !important;
        position:absolute !important;
        top:58px !important;
        right:10px !important;
        width:230px !important;
        padding:8px !important;
        flex-direction:column !important;
        align-items:stretch !important;
        gap:3px !important;
        background:#0d1b33 !important;
        border:1px solid #263b5c !important;
        border-radius:10px !important;
        box-shadow:0 12px 30px rgba(0,0,0,.5) !important;
        z-index:99999 !important;
        box-sizing:border-box !important;
    }

    .kc-open .kc-menu{
        display:flex !important;
    }

    .kc-menu a{
        display:block !important;
        width:100% !important;
        padding:12px 14px !important;
        box-sizing:border-box !important;
        border-radius:7px !important;
        font-size:15px !important;
        text-decoration:none !important;
        white-space:nowrap !important;
    }

    .kc-menu a:hover{
        background:#172b4d !important;
    }
}
</style>

</head>
        <body>

        <nav class="kc-nav">
    <div class="kc-brand">🐳 KingCloud</div>

    <button type="button" class="kc-menu-btn"
            onclick="this.parentElement.classList.toggle('kc-open')">
        ☰
    </button>

    <div class="kc-menu">
        <a href="/dashboard">🏠 Dashboard</a>
        <a href="/create">➕ Create</a>
        <a href="/profile">👤 Profile</a>
        {% if session.get("admin") %}
        <a href="/settings">⚙ Settings</a>
        <a href="/users">👥 All Users</a>
        {% endif %}
        <a href="/logout">Logout</a>
    </div>
</nav>

        <main>
            {{ body|safe }}
        </main>

        </body>
        </html>
        """,
        title=title,
        css=CSS,
        body=body,
        logged=logged_in(),
        panel_name=get_panel_name()
    )


# ---------------------------------------------------------
# Login
# ---------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        with db() as con:
            admin = con.execute(
                "SELECT * FROM admin WHERE username=?",
                (username,)
            ).fetchone()

            user = con.execute(
                "SELECT * FROM users WHERE username=?",
                (username,)
            ).fetchone()

        if admin and check_password_hash(
            admin["password_hash"]
            if "password_hash" in admin.keys()
            else admin["password"],
            password
        ):
            session.clear()
            session["admin"] = username
            return redirect("/")

        if user and check_password_hash(
            user["password_hash"],
            password
        ):
            session.clear()
            session["user_id"] = user["id"]
            return redirect("/")

        message = "<p style='color:#fb7185'>Invalid login.</p>"
    else:
        message = ""

    return page(
        f"""
        <div class="card" style="max-width:430px;margin:70px auto">
            <h2>🔐 Login</h2>
            {message}
            <form method="post">
                <input name="username"
                       placeholder="Username"
                       required>
                <input name="password"
                       type="password"
                       placeholder="Password"
                       required>
                <button style="width:100%">Login</button>
            </form>
            <p style="margin-top:18px">
                New user?
                <a href="/register">Create account</a>
            </p>
        </div>
        """,
        "Login"
    )

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if len(username) < 3:
            message = "Username must be at least 3 characters."
        elif len(password) < 6:
            message = "Password must be at least 6 characters."
        else:
            try:
                with db() as con:
                    con.execute(
                        "INSERT INTO users (username,password_hash,created) VALUES (?,?,?)",
                        (username, generate_password_hash(password), int(time.time()))
                    )
                return redirect("/login")
            except sqlite3.IntegrityError:
                message = "Username already exists."

        return page(
            "<div class='card'><h2>Registration Error</h2><p>%s</p>"
            "<a href='/register'>Back</a></div>" % message,
            "Register"
        )

    return page(
        """
        <div class="card" style="max-width:430px;margin:70px auto">
            <h2>👤 Register</h2>
            <form method="post">
                <input name="username" placeholder="Username" required>
                <input name="password" type="password"
                       placeholder="Password" required>
                <button class="green" style="width:100%">Register</button>
            </form>
            <p><a href="/login">Already have an account? Login</a></p>
        </div>
        """,
        "Register"
    )

@app.route("/profile", methods=["GET","POST"])
def profile():
    r=require_login()
    if r:
        return r
    message=""
    if request.method=="POST":
        old=request.form.get("old_password","")
        new_password=request.form.get("new_password","")
        if len(new_password)<6:
            message="New password must be at least 6 characters."
        else:
            with db() as con:
                if session.get("admin")==ADMIN_USER:
                    row=con.execute(
                        "SELECT * FROM admin WHERE username=?",
                        (ADMIN_USER,)
                    ).fetchone()
                    if row:
                        current_hash = row["password_hash"] if "password_hash" in row.keys() else row["password"]
                        if check_password_hash(current_hash,old):
                            con.execute(
                                "UPDATE admin SET password=? WHERE username=?",
                                (generate_password_hash(new_password),ADMIN_USER)
                            )
                        else:
                            message="Current password is incorrect."
                        con.commit()
                        message="Password changed successfully."
                    else:
                        message="Current password is incorrect."
                else:
                    row=con.execute(
                        "SELECT * FROM users WHERE id=?",
                        (session.get("user_id"),)
                    ).fetchone()
                    if row and check_password_hash(row["password_hash"],old):
                        con.execute(
                            "UPDATE users SET password_hash=? WHERE id=?",
                            (generate_password_hash(new_password),row["id"])
                        )
                        con.commit()
                        message="Password changed successfully."
                    else:
                        message="Current password is incorrect."
    return page(
        """
        <div class="card" style="max-width:500px;margin:40px auto">
            <h2>👤 Profile</h2>
            <p class="muted">Change your account password.</p>
            <p style="color:#a78bfa">MESSAGE</p>
            <form method="post">
                <input name="old_password" type="password" placeholder="Current password" required>
                <input name="new_password" type="password" placeholder="New password" required>
                <button class="green" style="width:100%">Save Password</button>
            </form>
        </div>
        """.replace("MESSAGE", message),
        "Profile"
    )

@app.route("/settings", methods=["GET","POST"])
def settings():
    if not session.get("admin"):
        return redirect("/")
    message=""
    if request.method=="POST":
        name=request.form.get("panel_name","").strip()
        try:
            slots=max(1,int(request.form.get("user_vps_slots","10")))
        except:
            slots=10

        if not name:
            message="Panel name cannot be empty."
        else:
            with db() as con:
                con.execute(
                    "INSERT OR REPLACE INTO settings(key,value) VALUES ('panel_name',?)",
                    (name[:60],)
                )
                con.execute(
                    "INSERT OR REPLACE INTO settings(key,value) VALUES ('user_vps_slots',?)",
                    (str(slots),)
                )
                con.commit()
            message=f"Settings saved. User VPS slots: {slots}"

    with db() as con:
        row=con.execute(
            "SELECT value FROM settings WHERE key='user_vps_slots'"
        ).fetchone()
        slots_current=row["value"] if row else "10"

    current=get_panel_name().replace('"',"&quot;")
    return page(
        """
        <div class="card" style="max-width:600px;margin:40px auto">
            <h2>⚙ Panel Settings</h2>
            <p class="muted">Admin only</p>
            <p style="color:#a78bfa">MESSAGE</p>
            <form method="post">
                <label>Panel Name</label>
                <input name="panel_name" value="CURRENT" maxlength="60" required>

                <label>Total User VPS Slots</label>
                <input name="user_vps_slots" type="number"
                       min="1" value="SLOTS" required>
                <p class="muted">
                    Maximum total VPS allowed for all normal users.
                    Admin VPS are not counted.
                </p>

                <button class="green" style="width:100%">Save</button>
            </form>
        </div>
        """.replace("MESSAGE",message).replace("CURRENT",current).replace("SLOTS",str(slots_current)),
        "Settings"
    )

@app.route("/users", methods=["GET", "POST"])
def users_admin():
    if session.get("admin") != ADMIN_USER:
        return redirect("/")

    message = ""
    error = ""

    if request.method == "POST":
        user_id = request.form.get("user_id", "").strip()
        new_username = request.form.get("new_username", "").strip()
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")

        if not user_id.isdigit():
            error = "Invalid user."
        elif len(new_username) < 3:
            error = "Username must be at least 3 characters."
        elif new_password and len(new_password) < 6:
            error = "Password must be at least 6 characters."
        else:
            try:
                with db() as con:
                    user = con.execute(
                        "SELECT * FROM users WHERE id=?",
                        (int(user_id),)
                    ).fetchone()

                    if not user:
                        error = "User not found."
                    elif new_password and not current_password:
                        error = "Current password is required."
                    elif new_password and not check_password_hash(
                        user["password_hash"],
                        current_password
                    ):
                        error = "Current password is incorrect."
                    else:
                        con.execute(
                            "UPDATE users SET username=? WHERE id=?",
                            (new_username, int(user_id))
                        )

                        if new_password:
                            con.execute(
                                "UPDATE users SET password_hash=? WHERE id=?",
                                (
                                    generate_password_hash(new_password),
                                    int(user_id)
                                )
                            )

                        con.commit()
                        message = "✅ User updated successfully."

            except sqlite3.IntegrityError:
                error = "❌ Username already exists."
            except Exception as exc:
                error = "❌ Update failed: " + str(exc)

    with db() as con:
        users = con.execute(
            "SELECT id, username, created FROM users ORDER BY id DESC"
        ).fetchall()

    cards = ""

    for user in users:
        username = str(user["username"]).replace('"', "&quot;")

        cards += f"""
        <div style="clear:both;
            background:#0b1930;
            border:1px solid #1d3557;
            border-radius:10px;
            padding:12px;
            margin-bottom:8px;
        ">

            <div style="clear:both;
                display:flex;
                align-items:center;
                justify-content:space-between;
                gap:10px;
            ">
                <div style="clear:both;
                    display:flex;
                    align-items:center;
                    gap:8px;
                    min-width:0;
                ">
                    <span>👤</span>
                    <b style="
                        overflow:hidden;
                        text-overflow:ellipsis;
                        white-space:nowrap;
                    ">{username}</b>
                </div>

                <details style="flex:1;min-width:0;">
                    <summary class="btn" style="float:right;cursor:pointer;list-style:none;display:inline-block;" style="
                        cursor:pointer;
                        padding:6px 12px;
                        list-style:none;
                        display:inline-block;
                    ">✏️ EDIT</summary>

                    <div style="clear:both;
                        margin-top:12px;
                        width:100%;
                        box-sizing:border-box;
                        background:#101f38;
                        border:1px solid #263e60;
                        border-radius:8px;
                        padding:12px;
                    ">
                        <form method="post">

                            <input type="hidden"
                                   name="user_id"
                                   value="{user["id"]}">

                            <label>Current Username</label>
                            <input value="{username}" disabled
                                   style="width:100%;box-sizing:border-box;">

                            <label>New Username</label>
                            <input
                                name="new_username"
                                value="{username}"
                                required
                                autocomplete="off"
                                style="width:100%;box-sizing:border-box;">

                            <label>Current Password</label>
                            <input
                                name="current_password"
                                type="password"
                                placeholder="Current password"
                                autocomplete="current-password"
                                style="width:100%;box-sizing:border-box;">

                            <label>New Password</label>
                            <input
                                name="new_password"
                                type="password"
                                placeholder="New password"
                                autocomplete="new-password"
                                style="width:100%;box-sizing:border-box;">

                            <button class="green"
                                    type="submit"
                                    style="width:100%;margin-top:8px;">
                                💾 Save Changes
                            </button>

                        </form>
                    </div>
                </details>
            </div>

        </div>
        """

    if not cards:
        cards = """
        <div class="card" style="text-align:center">
            <p class="muted">No registered users yet.</p>
        </div>
        """

    notice = ""

    if message:
        notice = f"""
        <div style="clear:both;
            background:#12351f;
            border:1px solid #1f8f4d;
            color:#86efac;
            padding:12px 14px;
            border-radius:10px;
            margin-bottom:16px;
        ">
            {message}
        </div>
        """

    if error:
        notice = f"""
        <div style="clear:both;
            background:#35151b;
            border:1px solid #8f2738;
            color:#fda4af;
            padding:12px 14px;
            border-radius:10px;
            margin-bottom:16px;
        ">
            {error}
        </div>
        """

    return page(
        f"""
        <div class="card" style="
            max-width:700px;
            margin:25px auto;
        ">

            <div style="clear:both;
                display:flex;
                justify-content:space-between;
                align-items:center;
                gap:10px;
                margin-bottom:6px;
            ">
                <h2 style="margin:0">
                    👥 All Users
                </h2>

                <a class="btn gray" href="/users">
                    🔄 Refresh
                </a>
            </div>

            <p class="muted" style="margin-top:8px">
                Admin only · {len(users)} registered user(s)
            </p>

            {notice}

            <div style="clear:both;margin-top:18px">
                {cards}
            </div>

            <a class="btn gray" href="/dashboard" style="
                display:block;
                text-align:center;
                margin-top:18px;
            ">
                ← Dashboard
            </a>

        </div>
        """,
        "All Users"
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ---------------------------------------------------------
# Dashboard
# ---------------------------------------------------------

@app.route("/")
@app.route("/dashboard")
def home():
    panel_name=get_panel_name()
    r = require_login()
    if r:
        return r

    with db() as con:
        if session.get("admin") == ADMIN_USER:
            rows = con.execute(
                "SELECT * FROM vps ORDER BY id DESC"
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM vps WHERE owner_id=? ORDER BY id DESC",
                (session.get("user_id"),)
            ).fetchall()

    running = 0

    for row in rows:
        try:
            c = docker_client.containers.get(row["container_id"])
            if c.status == "running":
                running += 1
        except Exception:
            pass

    host_cpu = psutil.cpu_percent(interval=0.15)
    host_ram = psutil.virtual_memory().percent
    host_disk = psutil.disk_usage("/").percent

    trs = ""

    for row in rows:
        try:
            c = docker_client.containers.get(row["container_id"])
            status = c.status
        except Exception:
            status = "missing"

        cls = "green" if status == "running" else "red"

        trs += f"""
        <tr>
            <td><b>{row["name"]}</b></td>
            <td>{status}</td>
            <td>Ubuntu</td>
            <td>
                <a class="btn" href="/vps/{row["id"]}">
                    Manage
                </a>
            </td>
        </tr>
        """

    if not trs:
        trs = """
        <tr>
            <td colspan="4" class="muted">
                No Docker VPS created yet.
            </td>
        </tr>
        """

    return page(
        f"""
        <h1>🐳 {panel_name}</h1>

        <div class="grid">
            <div class="card">
                <div class="muted">VPS</div>
                <div class="stat">{len(rows)}</div>
            </div>

            <div class="card">
                <div class="muted">Running</div>
                <div class="stat">{running}</div>
            </div>

            <div class="card">
                <div class="muted">Host CPU</div>
                <div class="stat">{host_cpu}%</div>
            </div>

            <div class="card">
                <div class="muted">Host RAM</div>
                <div class="stat">{host_ram}%</div>
            </div>
        </div>

        <div class="card">
            <b>Disk:</b> {host_disk}% used
            &nbsp; | &nbsp;
            <b>Host CPUs:</b> {get_host_cpus()}
        </div>

        <div class="card">
            <a class="btn green" href="/create-kingcloud">
                ➕ Create {panel_name} VPS
            </a>
        </div>

        <div class="card">
            <h2>My {panel_name} VPS</h2>

            <table>
                <tr>
                    <th>Name</th>
                    <th>Status</th>
                    <th>OS</th>
                    <th>Action</th>
                </tr>
                {trs}
            </table>
        </div>
        """
    )


# ---------------------------------------------------------
# Create
# ---------------------------------------------------------

@app.route("/create", methods=["GET", "POST"])
def create():
    panel_name=get_panel_name()
    r = require_login()
    if r:
        return r

    error = ""

    if request.method == "POST":
        name = safe_name(request.form.get("name", ""))
        template = request.form.get("template", "ubuntu")

        cpu = clamp_cpu(request.form.get("cpu", "1"))
        ram = clamp_ram(request.form.get("ram", "1024"))
        pids = clamp_pids(request.form.get("pids", "256"))
        owner_id = request.form.get("owner_id", "").strip()

        if not name:
            error = "Invalid VPS name."
        else:
            container_name = "dvp-" + name

            try:
                existing = docker_client.containers.list(
                    all=True,
                    filters={"name": "^/" + container_name + "$"}
                )

                if existing:
                    raise RuntimeError(
                        "A VPS with this name already exists."
                    )

                image = image_for_template(template)

                docker_client.images.pull(image)

                volume_name = container_name + "-data"

                docker_client.volumes.create(
                    name=volume_name,
                    labels={
                        "dockervps.managed": "true",
                        "dockervps.v2": "true"
                    }
                )

                container = docker_client.containers.create(
                    image=image,
                    name=container_name,
                    command=["sleep", "infinity"],
                    mem_limit=f"{ram}m",
                    nano_cpus=int(cpu * 1_000_000_000),
                    pids_limit=pids,
                    labels={
                        "dockervps.managed": "true",
                        "dockervps.v2": "true",
                        "dockervps.template": template
                    },
                    volumes={
                        volume_name: {
                            "bind": "/data",
                            "mode": "rw"
                        }
                    },
                    restart_policy={
                        "Name": "unless-stopped"
                    },
                    tty=True,
                    stdin_open=True
                )

                container.start()

                with db() as con:
                    con.execute(
                        """
                        INSERT INTO vps
                        (name,container_id,image,cpu,ram,pids,created,owner_id)
                        VALUES (?,?,?,?,?,?,?,?)
                        """,
                        (
                            name,
                            container.id,
                            image,
                            cpu,
                            ram,
                            pids,
                            int(time.time()),
                            int(owner_id) if owner_id.isdigit() else None
                        )
                    )

                return redirect("/")

            except Exception as exc:
                error = str(exc)

    with db() as con:
        user_rows = con.execute(
            "SELECT id, username FROM users ORDER BY username"
        ).fetchall()

    user_options = "".join(
        "<option value=\"%s\">%s</option>" % (u["id"], u["username"])
        for u in user_rows
    )

    return page(
        f"""
        <div class="card">
            <h2>➕ Create {panel_name} VPS</h2>

            <p class="muted">
                Host CPUs: {get_host_cpus()} |
                Maximum CPU: {get_host_cpus()}
            </p>

            {f"<p style='color:#fb7185'>{error}</p>" if error else ""}

            <form method="post">

                <label>VPS Name</label>
                <input name="name"
                       placeholder="my-vps"
                       required>

                <label>Assign VPS To User</label>
                <select name="owner_id">
                    <option value="">No User / Admin</option>
                    {user_options}
                </select>
                <label>Template</label>
                <select name="template">
                    <option value="ubuntu">
                        Ubuntu 24.04
                    </option>
                    <option value="debian">
                        Debian 12
                    </option>
                    <option value="alpine">
                        Alpine 3.20
                    </option>
                    <option value="python">
                        Python 3.12
                    </option>
                    <option value="node">
                        Node.js 22
                    </option>
                </select>

                <label>CPU</label>
                <input name="cpu"
                       type="number"
                       min="0.01"
                       max="{get_host_cpus()}"
                       step="0.01"
                       value="2">

                <label>RAM (MB)</label>
                <input name="ram"
                       type="number"
                       min="128"
                       value="1024">

                <label>PID Limit</label>
                <input name="pids"
                       type="number"
                       min="32"
                       value="256">

                <button class="green">
                    🚀 Deploy {panel_name} VPS
                </button>

                <a class="btn gray" href="/dashboard">
                    Back
                </a>
            </form>
        </div>
        """
    )


# ---------------------------------------------------------
# User Create KingCloud VPS
@app.route("/create-kingcloud", methods=["GET","POST"])
def create_kingcloud():
    if not session.get("user_id") or session.get("admin") == ADMIN_USER:
        return redirect("/")
    error=""
    os_map={
        "ubuntu24":"ubuntu:24.04",
        "ubuntu22":"ubuntu:22.04",
        "ubuntu20":"ubuntu:20.04",
        "debian13":"debian:13",
        "debian12":"debian:12",
        "debian11":"debian:11"
    }
    if request.method=="POST":
        with db() as con:
            row=con.execute(
                "SELECT value FROM settings WHERE key='user_vps_slots'"
            ).fetchone()
            total_slots=int(row["value"]) if row and str(row["value"]).isdigit() else 10
            total_user_vps=con.execute(
                "SELECT COUNT(*) FROM vps WHERE owner_id IS NOT NULL"
            ).fetchone()[0]
            user_vps_count=con.execute(
                "SELECT COUNT(*) FROM vps WHERE owner_id=?",
                (int(session["user_id"]),)
            ).fetchone()[0]

        if total_user_vps >= total_slots:
            return page(f"""
            <div class="card">
                <h2>⚠️ Free VPS slots full</h2>
                <p>All {total_slots} free VPS slots are currently occupied.</p>
                <a class="btn gray" href="/dashboard">← Back</a>
            </div>
            """)

        if user_vps_count >= 1:
            return page("""
            <div class="card">
                <h2>⚠️ VPS Limit Reached</h2>
                <p>You already have 1 KingCloud VPS.</p>
                <a class="btn gray" href="/dashboard">← Back</a>
            </div>
            """)

        name=safe_name(request.form.get("name",""))
        os_type=request.form.get("os","")
        if not name:
            error="Invalid VPS name."
        elif os_type not in os_map:
            error="Invalid OS."
        else:
            try:
                image=os_map[os_type]
                cname="dvp-"+name
                if docker_client.containers.list(all=True,filters={"name":"^/"+cname+"$"}):
                    raise RuntimeError("A VPS with this name already exists.")
                docker_client.images.pull(image)
                volume=cname+"-data"
                docker_client.volumes.create(name=volume)
                c=docker_client.containers.create(
                    image=image,name=cname,command=["sleep","infinity"],
                    mem_limit="8192m",nano_cpus=2000000000,pids_limit=256,
                    volumes={volume:{"bind":"/data","mode":"rw"}},
                    restart_policy={"Name":"unless-stopped"},
                    tty=True,stdin_open=True
                )
                c.start()
                with db() as con:
                    con.execute(
                        "INSERT INTO vps (name,container_id,image,cpu,ram,pids,created,owner_id) VALUES (?,?,?,?,?,?,?,?)",
                        (name,c.id,image,2,8192,256,int(time.time()),int(session["user_id"]))
                    )
                return redirect("/")
            except Exception as e:
                error=str(e)
    return page(f"""
    <div class="card">
    <h2>👑 Create KingCloud VPS</h2>
    {f'<p style="color:#fb7185">{error}</p>' if error else ""}
    <p class="muted">2 Core • 8 GB RAM • 30 GB Disk</p>
    <form method="post">
    <label>Operating System</label>
    <select name="os" required>
    <option value="ubuntu24">Ubuntu 24.04</option>
    <option value="ubuntu22">Ubuntu 22.04</option>
    <option value="ubuntu20">Ubuntu 20.04</option>
    <option value="debian13">Debian 13</option>
    <option value="debian12">Debian 12</option>
    <option value="debian11">Debian 11</option>
    </select>
    <label>VPS Name</label>
    <input name="name" placeholder="my-vps" maxlength="40" required>
    <button class="green">🚀 Create KingCloud VPS</button>
    <a class="btn gray" href="/dashboard">Back</a>
    </form></div>
    """)

# VPS Management
# ---------------------------------------------------------

@app.route("/vps/<int:vps_id>")
def manage(vps_id):
    r = require_login()
    if r:
        return r

    vps = get_vps(vps_id)

    if not vps:
        abort(404)

    c = get_container(vps)

    return page(
        f"""
        <div class="card">
            <a class="btn gray" href="/dashboard">← Dashboard</a>

            <h1>🐳 {vps["name"]}</h1>

            <p>
                Status:
                <b>{c.status.upper()}</b>
            </p>

            <p class="muted">
                Image: {vps["image"]}<br>
                CPU: {vps["cpu"]}<br>
                RAM: {round(int(vps["ram"])/1024,2):g} GB<br>
                Disk: 30 GB
            </p>

            <a class="btn green"
               href="/action/{vps_id}/start">
               ▶ Start
            </a>

            <a class="btn gray"
               href="/action/{vps_id}/stop">
               ⏹ Stop
            </a>

            <a class="btn orange"
               href="/action/{vps_id}/restart">
               🔄 Restart
            </a>

            <a class="btn"
               href="/logs/{vps_id}">
               📜 Logs
            </a> <form method="POST" action="/tmate/{vps_id}" style="display:inline"><button class="btn" type="submit">🔐 Tmate</button></form>

            <form method="post"
                  action="/reinstall/{vps_id}"
                  style="display:inline"
                  onsubmit="return confirm('Reinstall this VPS?')">
                <button class="orange">
                    ♻ Reinstall
                </button>
            </form>

            <form method="post"
                  action="/delete/{vps_id}"
                  style="display:inline"
                  onsubmit="return confirm('Delete this VPS permanently?')">
                <button class="red">
                    🗑 Delete
                </button>
            </form>
        </div>
        """
    )


# ---------------------------------------------------------
# Actions
# ---------------------------------------------------------

@app.route("/action/<int:vps_id>/<action>")
def action(vps_id, action):
    r = require_login()
    if r:
        return r

    vps = get_vps(vps_id)
    if not vps:
        abort(404)

    c = get_container(vps)

    if action == "start":
        c.start()

    elif action == "stop":
        c.stop(timeout=10)

    elif action == "restart":
        c.restart(timeout=10)

    else:
        abort(400)

    return redirect(f"/vps/{vps_id}")


# ---------------------------------------------------------
# Logs
# ---------------------------------------------------------


@app.route("/tmate/<int:vps_id>", methods=["GET", "POST"])
def tmate_session(vps_id):
    import time

    con = db()
    vps = con.execute(
        "SELECT * FROM vps WHERE id=?",
        (vps_id,)
    ).fetchone()
    con.close()

    if not vps:
        abort(404)

    c = get_container(vps)

    if c.status != "running":
        return page(
            "<h2>VPS is not running</h2>"
            "<p>Start the VPS first.</p>"
            '<p><a class="btn" href="/vps/%s">Back</a></p>' % vps_id,
            "Tmate"
        )

    # Install Tmate automatically without restarting the VPS.
    check = c.exec_run(
        ["bash", "-lc", "command -v tmate >/dev/null 2>&1"]
    )

    if check.exit_code != 0:
        install = (
            "export DEBIAN_FRONTEND=noninteractive; "
            "apt-get update -qq && "
            "apt-get install -y -qq tmate"
        )

        result = c.exec_run(
            ["bash", "-lc", install],
            stdout=True,
            stderr=True
        )

        if result.exit_code != 0:
            return page(
                "<h2>Tmate installation failed</h2>"
                "<p>The VPS was not stopped or restarted.</p>"
                '<p><a class="btn" href="/vps/%s">Back</a></p>' % vps_id,
                "Tmate Error"
            )

    # Start Tmate only if no usable session already exists.
    start_cmd = r"""
SSH="$(tmate display -p '#{tmate_ssh}' 2>/dev/null || true)"
if [ -z "$SSH" ] || [ "$SSH" = "no sessions" ]; then
    nohup tmate -F >/tmp/dockervps-tmate.log 2>&1 &
fi
"""
    c.exec_run(["bash", "-lc", start_cmd])

    ssh_url = ""
    for _ in range(30):
        time.sleep(1)

        result = c.exec_run(
            ["bash", "-lc",
             "grep -E 'ssh session: ssh .*@.*tmate\\.io' "
             "/tmp/dockervps-tmate.log 2>/dev/null | tail -1 || true"]
        )

        value = (result.output or b"").decode(
            "utf-8",
            "ignore"
        ).strip()

        if "ssh session: " in value:
            ssh_url = value.split("ssh session: ", 1)[1].strip()
            break

    if not ssh_url:
        return page(
            "<h2>Tmate is still starting</h2>"
            "<p>Please wait a few seconds and try again.</p>"
            '<p><a class="btn" href="/tmate/%s">Retry</a></p>'
            % vps_id,
            "Tmate"
        )

    safe = (
        ssh_url
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

    body = (
        "<h2>🟢 Tmate Session Ready</h2>"
        "<p>Tmate is running inside this Docker VPS.</p>"
        "<div style='background:#111;padding:16px;border-radius:10px;"
        "word-break:break-all;margin:15px 0'>"
        "<code>%s</code>"
        "</div>"
        "<p>Copy the SSH command into Termux/JuiceSSH.</p>"
        "<p>"
        "<a class='btn green' href='/tmate/%s'>Generate New Session</a> "
        "<a class='btn' href='/vps/%s'>Back</a>"
        "</p>"
    ) % (safe, vps_id, vps_id)

    return page(body, "Tmate Session")

@app.route("/logs/<int:vps_id>")
def logs(vps_id):
    r = require_login()
    if r:
        return r

    vps = get_vps(vps_id)

    if not vps:
        abort(404)

    c = get_container(vps)

    try:
        output = c.logs(tail=500).decode(
            "utf-8",
            errors="replace"
        )
    except Exception as exc:
        output = str(exc)

    return page(
        f"""
        <div class="card">
            <a class="btn gray"
               href="/vps/{vps_id}">
               ← Back
            </a>

            <h2>📜 {vps["name"]} Logs</h2>

            <pre>{output}</pre>
        </div>
        """
    )


# ---------------------------------------------------------
# Reinstall
# ---------------------------------------------------------

@app.route("/reinstall/<int:vps_id>", methods=["POST"])
def reinstall(vps_id):
    r = require_login()
    if r:
        return r

    vps = get_vps(vps_id)

    if not vps:
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
        image=image,
        name=name,
        command=["sleep", "infinity"],
        mem_limit=f'{vps["ram"]}m',
        nano_cpus=int(vps["cpu"] * 1_000_000_000),
        pids_limit=vps["pids"],
        labels={
            "dockervps.managed": "true",
            "dockervps.v2": "true",
        },
        restart_policy={
            "Name": "unless-stopped"
        },
        tty=True,
        stdin_open=True
    )

    container.start()

    with db() as con:
        con.execute(
            "UPDATE vps SET container_id=? WHERE id=?",
            (container.id, vps_id)
        )

    return redirect(f"/vps/{vps_id}")


# ---------------------------------------------------------
# Delete
# ---------------------------------------------------------

@app.route("/delete/<int:vps_id>", methods=["POST"])
def delete(vps_id):
    r = require_login()
    if r:
        return r

    vps = get_vps(vps_id)

    if not vps:
        abort(404)

    try:
        c = docker_client.containers.get(vps["container_id"])
        volume_name = "dvp-" + vps["name"] + "-data"

        c.remove(force=True)

        try:
            docker_client.volumes.get(volume_name).remove()
        except Exception:
            pass

    except Exception:
        pass

    with db() as con:
        con.execute(
            "DELETE FROM vps WHERE id=?",
            (vps_id,)
        )

    return redirect("/")


# ---------------------------------------------------------
# Health
# ---------------------------------------------------------

@app.route("/health")
def health():
    return {
        "status": "online",
        "panel": "King Cloud",
        "backend": "Docker-only"
    }


# ---------------------------------------------------------
# Start
# ---------------------------------------------------------

init_db()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8080,
        debug=False
    )
