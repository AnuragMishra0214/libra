"""Libra Smart Library - Flask backend (sessions, hashed passwords, RBAC, JSON storage)."""
import copy, glob, json, os, re, secrets, shutil, socket, tempfile, threading, time
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
SEED_DIR = os.path.join(BASE_DIR, "data")
if not os.path.isdir(FRONTEND_DIR) and os.path.exists(os.path.join(HERE, "index.html")):
    # "flat" layout: app.py, index.html and library_data.json all sit in one folder
    FRONTEND_DIR = HERE
    SEED_DIR = os.path.join(HERE, "data")  # data shipped with the repo (used to seed an empty disk)
DATA_DIR = os.environ.get("LIBRA_DATA_DIR") or SEED_DIR  # on Render: point this at the persistent disk
os.makedirs(DATA_DIR, exist_ok=True)
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
DATA_FILE = os.path.join(DATA_DIR, "library_data.json")
COMPLAINTS_FILE = os.path.join(DATA_DIR, "complaints.json")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
SECRET_FILE = os.path.join(DATA_DIR, ".secret_key")
os.makedirs(BACKUP_DIR, exist_ok=True)
_seed = os.path.join(SEED_DIR, "library_data.json")
if not os.path.exists(_seed) and os.path.exists(os.path.join(HERE, "library_data.json")):
    _seed = os.path.join(HERE, "library_data.json")
if not os.path.exists(DATA_FILE) and os.path.exists(_seed) and os.path.abspath(_seed) != os.path.abspath(DATA_FILE):
    shutil.copy2(_seed, DATA_FILE)  # first run on an empty persistent disk

ADMIN_EMAIL = os.environ.get("LIBRA_ADMIN_EMAIL", "admin@gmail.com").lower()
ADMIN_DEFAULT_PASSWORD = os.environ.get("LIBRA_ADMIN_PASSWORD", "admin123")
REACTION_TTL = 15  # seconds a complaint reaction stays visible
COLLECTIONS = ["booksData", "studentsData", "recordsData", "reservationsData",
               "suggestionsData", "seatsData", "categoriesData", "activityData"]
LOCK = threading.RLock()

app = Flask(__name__, static_folder=None)
app.config.update(MAX_CONTENT_LENGTH=5 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=os.environ.get("LIBRA_SECURE_COOKIES") == "1",  # set to 1 behind HTTPS (Render)
                  PERMANENT_SESSION_LIFETIME=timedelta(hours=8))


# ------------------------------------------------------------------ storage
def _secret():
    if os.path.exists(SECRET_FILE):
        return open(SECRET_FILE).read().strip()
    k = secrets.token_hex(32)
    with open(SECRET_FILE, "w") as f:
        f.write(k)
    return k


app.secret_key = os.environ.get("LIBRA_SECRET_KEY") or _secret()


def atomic_write(path, obj):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def backup(path, tag="auto", keep=30):
    if not os.path.exists(path):
        return
    stem = os.path.splitext(os.path.basename(path))[0]
    shutil.copy2(path, os.path.join(BACKUP_DIR, f"{stem}_{tag}_{datetime.now():%Y%m%d_%H%M%S}.json"))
    old = sorted(glob.glob(os.path.join(BACKUP_DIR, f"{stem}_*.json")))
    for p in old[:-keep]:
        os.remove(p)


def read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        stem = os.path.splitext(os.path.basename(path))[0]
        for p in sorted(glob.glob(os.path.join(BACKUP_DIR, f"{stem}_*.json")), reverse=True):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except (ValueError, OSError):
                continue
        raise


_last_backup = 0


def save_library(data):
    global _last_backup
    if time.time() - _last_backup > 600:  # rolling backup at most every 10 minutes
        backup(DATA_FILE, "auto")
        _last_backup = time.time()
    atomic_write(DATA_FILE, data)


# --------------------------------------------------------------- validation
def s(v, n=300):
    return "" if v is None else str(v).strip()[:n]


def to_int(v, d=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def normalize_book(b):
    b = dict(b)
    b["id"] = s(b.get("id"), 40).upper()
    for k in ("title", "author", "category", "branch", "subject"):
        b[k] = s(b.get(k))
    b["isbn"] = re.sub(r"[\s-]", "", s(b.get("isbn"), 20))
    for k in ("block", "floor", "rack", "shelf"):  # physical location (existing fields)
        b[k] = s(b.get(k), 20)
    b["qty"] = max(0, to_int(b.get("qty")))
    if "total" in b:
        b["total"] = max(0, to_int(b["total"]))
    b["pages"] = max(0, to_int(b.get("pages")))
    b["archived"] = bool(b.get("archived", False))
    return b


def validate_library(payload):
    out = {}
    for k in COLLECTIONS:
        if k in payload:
            v = payload[k]
            if not isinstance(v, list) or not all(isinstance(x, dict) for x in v):
                raise ValueError(f"{k} must be a list of objects")
            out[k] = v
    if "booksData" in out:
        seen_i, seen_id, books = set(), set(), []
        for b in out["booksData"]:
            b = normalize_book(b)
            if not b["id"] or not b["title"]:
                raise ValueError("Every book needs an id and title")
            if b["id"] in seen_id:
                raise ValueError("Duplicate book ID: " + b["id"])
            seen_id.add(b["id"])
            if b["isbn"]:
                if b["isbn"] in seen_i:
                    raise ValueError("Duplicate ISBN: " + b["isbn"])
                seen_i.add(b["isbn"])
            books.append(b)
        out["booksData"] = books
    if "studentsData" in out:
        ids = [s(x.get("id")).upper() for x in out["studentsData"]]
        if "" in ids or len(ids) != len(set(ids)):
            raise ValueError("Student IDs must be unique and non-empty")
    return out


# ------------------------------------------------------------------- users
def load_users():
    d = read_json(USERS_FILE, None)
    if d is None:
        d = {"users": [{"id": 1, "name": "Admin", "email": ADMIN_EMAIL, "role": "admin",
                        "password_hash": generate_password_hash(ADMIN_DEFAULT_PASSWORD),
                        "default_password": ADMIN_DEFAULT_PASSWORD == "admin123",
                        "created_at": datetime.now().isoformat()}]}
        atomic_write(USERS_FILE, d)
    return d


def public_user(u):
    return {"id": u["id"], "name": u["name"], "email": u["email"], "role": u["role"],
            "must_change_password": bool(u.get("default_password"))}


FAILS = {}


def too_many(key):
    t = [x for x in FAILS.get(key, []) if time.time() - x < 900]
    FAILS[key] = t
    return len(t) >= 8


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    with LOCK:
        for u in load_users()["users"]:
            if u["id"] == uid and u["role"] == session.get("role"):
                return u
    return None


def require(role=None):
    def deco(fn):
        @wraps(fn)
        def w(*a, **k):
            u = current_user()
            if not u:
                return jsonify({"error": "Authentication required"}), 401
            if role and u["role"] != role:
                return jsonify({"error": "Admin access required"}), 403
            return fn(u, *a, **k)
        return w
    return deco


@app.before_request
def csrf_guard():
    # state-changing API calls must be JSON (blocks cross-site form posts) and same-origin
    if request.method in ("POST", "PUT", "DELETE", "PATCH") and request.path.startswith("/api/"):
        if not request.is_json:
            return jsonify({"error": "JSON body required"}), 415
        o = request.headers.get("Origin")
        if o and o.split("://", 1)[-1] != request.host:
            return jsonify({"error": "Cross-origin request blocked"}), 403


@app.after_request
def headers(r):
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["Cache-Control"] = "no-store"
    return r


@app.errorhandler(413)
def too_big(e):
    return jsonify({"error": "Payload too large"}), 413


def body():
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


@app.route("/")
def serve_frontend():
    for d in (FRONTEND_DIR, HERE, os.getcwd(), os.path.join(os.getcwd(), "frontend"), os.path.join(HERE, "frontend")):
        if os.path.exists(os.path.join(d, "index.html")):
            return send_from_directory(d, "index.html")
    return ("index.html was not found. Upload index.html to the same GitHub folder as app.py "
            "(or into a 'frontend' folder). Looked in: " + ", ".join([FRONTEND_DIR, HERE, os.getcwd()]), 500)


# --------------------------------------------------------------------- auth
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.route("/api/auth/register", methods=["POST"])
def register():
    d = body()
    name, email, pw = s(d.get("name"), 80), s(d.get("email"), 120).lower(), str(d.get("password") or "")
    if not name or not EMAIL_RE.match(email):
        return jsonify({"error": "Valid name and email are required"}), 400
    if len(pw) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    if email == ADMIN_EMAIL:
        return jsonify({"error": "This email is reserved for Admin"}), 409
    with LOCK:
        db = load_users()
        if any(u["email"] == email for u in db["users"]):
            return jsonify({"error": "An account with this email already exists"}), 409
        u = {"id": max([x["id"] for x in db["users"]] + [0]) + 1, "name": name, "email": email, "role": "student",
             "password_hash": generate_password_hash(pw), "created_at": datetime.now().isoformat(),
             "migrated_from_local": bool(d.get("legacy"))}
        db["users"].append(u)
        atomic_write(USERS_FILE, db)
    return jsonify({"status": "registered", "user": public_user(u)}), 201


@app.route("/api/auth/login", methods=["POST"])
def login():
    d = body()
    email, pw = s(d.get("email"), 120).lower(), str(d.get("password") or "")
    key = email + "|" + (request.remote_addr or "")
    if too_many(key):
        return jsonify({"error": "Too many failed attempts. Try again in 15 minutes."}), 429
    with LOCK:
        u = next((x for x in load_users()["users"] if x["email"] == email), None)
    if not u or not check_password_hash(u["password_hash"], pw):
        FAILS.setdefault(key, []).append(time.time())
        return jsonify({"error": "Invalid email or password", "known": bool(u)}), 401
    FAILS.pop(key, None)
    session.clear()
    session.update(uid=u["id"], role=u["role"])
    session.permanent = True
    return jsonify({"user": public_user(u)})


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"status": "logged out"})


@app.route("/api/auth/me")
def me():
    u = current_user()
    return jsonify({"user": public_user(u) if u else None})


@app.route("/api/auth/change-password", methods=["POST"])
@require()
def change_password(user):
    d = body()
    new = str(d.get("new_password") or "")
    if len(new) < 6:
        return jsonify({"error": "New password must be at least 6 characters"}), 400
    with LOCK:
        db = load_users()
        u = next(x for x in db["users"] if x["id"] == user["id"])
        if not check_password_hash(u["password_hash"], str(d.get("old_password") or "")):
            return jsonify({"error": "Current password is incorrect"}), 403
        u["password_hash"] = generate_password_hash(new)
        u["default_password"] = False
        atomic_write(USERS_FILE, db)
    return jsonify({"status": "password changed"})


# ------------------------------------------------------------ library data
def own(name):
    return s(name).lower()


def merge_student(cur, inc, user):
    """Students may only: self-issue, request a return, add reservations/suggestions/students,
    book seats and log activity. Everything else in their payload is ignored."""
    me_ = own(user["name"])
    out = copy.deepcopy(cur)
    books = {b["id"]: b for b in out.get("booksData", [])}
    students = {s_["id"].upper(): s_ for s_ in out.get("studentsData", [])}
    recs = out.setdefault("recordsData", [])
    existing = {r.get("id"): r for r in recs}
    for r in inc.get("recordsData", []):
        rid = r.get("id")
        old = existing.get(rid)
        sid = s(r.get("studentId"), 40).upper()
        stu = students.get(sid)
        mine = stu is not None and own(stu.get("name")) == me_
        if old is not None:
            if mine and old.get("status") == "Issued" and r.get("status") == "Pending Approval":
                old["status"] = "Pending Approval"
                old["returnRequestedAt"] = datetime.now().strftime("%Y-%m-%d")
        elif mine and r.get("status") == "Issued":
            b = books.get(s(r.get("bookId"), 40).upper())
            if b and not b.get("archived") and b.get("qty", 0) > 0:
                b["qty"] -= 1
                now = datetime.now()
                recs.insert(0, {"id": rid if isinstance(rid, int) else int(time.time() * 1000), "studentId": sid,
                                "bookId": b["id"], "bookTitle": b["title"], "issueDate": now.strftime("%Y-%m-%d"),
                                "dueDate": (now + timedelta(days=7)).strftime("%Y-%m-%d"),
                                "status": "Issued", "fine": 0, "finePaid": True})
    ks = {json.dumps(x, sort_keys=True) for x in cur.get("reservationsData", [])}
    for r in reversed(inc.get("reservationsData", [])):
        if json.dumps(r, sort_keys=True) not in ks and s(r.get("studentId")) and s(r.get("bookId")):
            out.setdefault("reservationsData", []).insert(0, {
                "studentId": s(r["studentId"], 60), "bookId": s(r["bookId"], 40),
                "title": s(r.get("title")), "date": s(r.get("date"), 30)})
    iseat = {x.get("id"): x for x in inc.get("seatsData", [])}
    for seat in out.get("seatsData", []):
        n = iseat.get(seat.get("id"))
        if n is not None and "bookedBy" in n:
            seat["bookedBy"] = s(n["bookedBy"], 60) or None
    ids = {x.get("id") for x in cur.get("suggestionsData", [])}
    for sg in reversed(inc.get("suggestionsData", [])):
        if sg.get("id") not in ids and s(sg.get("title")):
            out.setdefault("suggestionsData", []).insert(0, {
                "id": to_int(sg.get("id"), int(time.time() * 1000)), "title": s(sg["title"]),
                "author": s(sg.get("author")), "category": s(sg.get("category"), 40),
                "by": s(sg.get("by"), 80) or user["name"], "status": "Pending"})
    for st in inc.get("studentsData", []):
        sid = s(st.get("id"), 40).upper()
        if sid and sid not in students and s(st.get("name")):
            ent = {"id": sid, "name": s(st["name"], 80), "branch": s(st.get("branch"), 40)}
            out.setdefault("studentsData", []).append(ent)
            students[sid] = ent
    seen = {(a.get("at"), a.get("text")) for a in cur.get("activityData", [])}
    for a in reversed(inc.get("activityData", [])[:20]):
        if (a.get("at"), a.get("text")) not in seen and s(a.get("text")):
            out.setdefault("activityData", []).insert(0, {"at": s(a.get("at"), 40), "by": user["name"],
                                                          "text": s(a["text"], 300)})
    if "activityData" in out:
        out["activityData"] = out["activityData"][:200]
    return out


@app.route("/api/data")
@require()
def get_all_data(user):
    with LOCK:
        try:
            data = read_json(DATA_FILE, None)
        except Exception:
            return jsonify({"error": "Data file unreadable"}), 500
    if data is None:
        return jsonify({"initialized": False})
    return jsonify({"initialized": True, "data": data})


@app.route("/api/save", methods=["POST"])
@require()
def save_all_data(user):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not payload:
        return jsonify({"error": "No data received"}), 400
    with LOCK:
        try:
            current = read_json(DATA_FILE, {}) or {}
        except Exception:
            return jsonify({"error": "Existing data unreadable; refusing to overwrite"}), 500
        try:
            if user["role"] == "admin":
                new = dict(current)
                new.update(validate_library(payload))
            else:
                inc = {k: payload[k] for k in COLLECTIONS if isinstance(payload.get(k), list)}
                new = merge_student(current, inc, user)
            validate_library(new)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        save_library(new)
    return jsonify({"status": "saved"})


@app.route("/api/books/<bid>/location", methods=["PUT"])
@require("admin")
def set_location(user, bid):
    d = body()
    with LOCK:
        data = read_json(DATA_FILE, {}) or {}
        b = next((x for x in data.get("booksData", []) if x.get("id") == bid.upper()), None)
        if not b:
            return jsonify({"error": "Book not found"}), 404
        for k in ("block", "floor", "rack", "shelf"):
            if k in d:
                b[k] = s(d[k], 20).upper() if k == "block" else s(d[k], 20)
        if s(d.get("branch"), 40):
            b["branch"] = s(d["branch"], 40).upper()
        save_library(data)
    return jsonify({"status": "saved", "book": b})


@app.route("/api/server-ip")
@require()
def get_server_ip(user):
    pub = os.environ.get("LIBRA_PUBLIC_URL", "").rstrip("/")
    if pub:
        return jsonify({"url": pub + "/#register-student"})
    try:
        so = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        so.connect(("8.8.8.8", 80))
        ip = so.getsockname()[0]
        so.close()
    except Exception:
        ip = "127.0.0.1"
    return jsonify({"url": f"http://{ip}:5000/#register-student"})


# ---------------------------------------------------------------- complaints
STATUSES = {"Pending", "In Progress", "Resolved"}


def load_complaints():
    d = read_json(COMPLAINTS_FILE, [])
    return d if isinstance(d, list) else []


def expire_reactions(items):
    now, changed = time.time(), False
    for c in items:  # clear temporary reactions only; the complaint record is kept
        if c.get("expire_at") and c["expire_at"] <= now:
            c["reaction"] = ""
            c["expire_at"] = None
            changed = True
    return changed


def cview(c):
    return {k: v for k, v in c.items() if k != "owner_id"}


@app.route("/api/complaints", methods=["GET", "POST"])
@require()
def complaints(user):
    with LOCK:
        items = load_complaints()
        if expire_reactions(items):
            atomic_write(COMPLAINTS_FILE, items)
        if request.method == "GET":
            vis = items if user["role"] == "admin" else [c for c in items if c.get("owner_id") == user["id"]]
            return jsonify([cview(c) for c in vis])
        if user["role"] == "admin":
            return jsonify({"error": "Complaints are submitted by students"}), 403
        d = body()
        details = s(d.get("details"), 2000)
        if not details:
            return jsonify({"error": "Details are required"}), 400
        cid = int(time.time() * 1000)
        while any(c["id"] == cid for c in items):
            cid += 1
        item = {"id": cid, "owner_id": user["id"], "student": user["name"],
                "category": s(d.get("category"), 40) or "Other", "location": s(d.get("location"), 120),
                "details": details, "status": "Pending", "eta": "", "admin_note": "", "reaction": "",
                "expire_at": None, "created_at": datetime.now().strftime("%d %b, %I:%M %p")}
        items.insert(0, item)
        atomic_write(COMPLAINTS_FILE, items)
    return jsonify(cview(item)), 201


@app.route("/api/complaints/<int:cid>", methods=["PUT"])
@require()
def update_complaint(user, cid):
    d = body()
    with LOCK:
        items = load_complaints()
        item = next((c for c in items if c["id"] == cid), None)
        if not item or (user["role"] != "admin" and item.get("owner_id") != user["id"]):
            return jsonify({"error": "Not found"}), 404
        if user["role"] == "admin":
            if "status" in d:
                if d["status"] not in STATUSES:
                    return jsonify({"error": "Invalid status"}), 400
                item["status"] = d["status"]
            if "eta" in d:
                item["eta"] = s(d["eta"], 60)
            if "admin_note" in d:
                item["admin_note"] = s(d["admin_note"], 1000)
            if item["status"] == "Resolved" and not item.get("resolved_at"):
                item["resolved_at"] = datetime.now().strftime("%d %b, %I:%M %p")
        elif any(k in d for k in ("status", "eta", "admin_note")):
            return jsonify({"error": "Only admin can change status, ETA or notes"}), 403
        if d.get("reaction"):
            item["reaction"] = s(d["reaction"], 10)
            item["expire_at"] = time.time() + REACTION_TTL
        atomic_write(COMPLAINTS_FILE, items)
    return jsonify(cview(item))


# ------------------------------------------------------------------ startup
def auto_locate(d, per_shelf=6, shelves=5, racks_per_floor=4):
    """Fill block/floor/rack/shelf for books that have NO location (never overwrites existing ones).
    One block per category (alphabetical: A, B, C...), books ordered by ID. Disable with LIBRA_AUTO_LOCATE=0."""
    if os.environ.get("LIBRA_AUTO_LOCATE", "1") == "0":
        return False
    books = d.get("booksData", [])
    cats = sorted({b.get("category") or "General" for b in books})
    changed = False
    for ci, c in enumerate(cats):
        grp = sorted([b for b in books if (b.get("category") or "General") == c], key=lambda b: str(b.get("id")))
        for i, b in enumerate(grp):
            if any(b.get(k) for k in ("block", "floor", "rack", "shelf")):
                continue
            sn = i // per_shelf
            rn = sn // shelves
            b.update(block=chr(65 + ci % 26), floor=str(rn // racks_per_floor + 1), rack=str(rn + 1), shelf=str(sn % shelves + 1))
            changed = True
    return changed


def init_storage():
    with LOCK:
        if os.path.exists(DATA_FILE):
            backup(DATA_FILE, "startup", keep=40)
        load_users()
        if not os.path.exists(COMPLAINTS_FILE):
            atomic_write(COMPLAINTS_FILE, [])
        d = read_json(DATA_FILE, None)
        if isinstance(d, dict):  # safe defaults for old records
            changed = False
            for b in d.get("booksData", []):
                for k in ("block", "floor", "rack", "shelf"):
                    if k not in b:
                        b[k] = ""
                        changed = True
                if "branch" not in b:
                    b["branch"] = "General"
                    changed = True
            if auto_locate(d):
                changed = True
            if changed:
                atomic_write(DATA_FILE, d)


init_storage()

if __name__ == "__main__":
    app.run(host=os.environ.get("LIBRA_HOST", "0.0.0.0"), port=int(os.environ.get("LIBRA_PORT", "5000")),
            debug=os.environ.get("LIBRA_DEBUG") == "1")
