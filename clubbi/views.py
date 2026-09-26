import hmac
import os
import secrets
import smtplib
import sqlite3
from calendar import month_name, monthrange
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps
from urllib.parse import urlencode

from django.contrib import messages
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from werkzeug.security import check_password_hash, generate_password_hash

from django.conf import settings


PLANS = {
    "small": {"label": "Growing campus", "amount_cents": 200000},
    "medium": {"label": "Active campus", "amount_cents": 450000},
    "large": {"label": "Big campus", "amount_cents": 650000},
}
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX_REQUESTS = 120
request_log = {}


def db_connection():
    connection = sqlite3.connect(settings.DATABASES["default"]["NAME"])
    connection.row_factory = sqlite3.Row
    return connection


def ensure_schema():
    os.makedirs(settings.BASE_DIR / "instance", exist_ok=True)
    db = db_connection()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, school_id INTEGER);
        CREATE TABLE IF NOT EXISTS schools (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, city TEXT NOT NULL, contact_email TEXT NOT NULL, password_hash TEXT, school_passcode_hash TEXT, school_login_phrase_hash TEXT, school_login_phrase_key TEXT, verified_at TEXT, plan TEXT, plan_status TEXT NOT NULL DEFAULT 'inactive', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, school_id INTEGER NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, category TEXT NOT NULL, event_date TEXT NOT NULL, start_time TEXT NOT NULL, location TEXT NOT NULL, capacity INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (school_id) REFERENCES schools (id));
        CREATE TABLE IF NOT EXISTS event_signups (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL, user_id INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE (event_id, user_id));
        CREATE TABLE IF NOT EXISTS event_stars (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL, user_id INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE (event_id, user_id));
        CREATE TABLE IF NOT EXISTS teachers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, school_id INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (school_id) REFERENCES schools (id));
        CREATE TABLE IF NOT EXISTS event_heads (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL UNIQUE, user_id INTEGER NOT NULL, assigned_by INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (event_id) REFERENCES events (id), FOREIGN KEY (user_id) REFERENCES users (id), FOREIGN KEY (assigned_by) REFERENCES teachers (id));
        CREATE TABLE IF NOT EXISTS school_verification_tokens (id INTEGER PRIMARY KEY AUTOINCREMENT, school_id INTEGER NOT NULL, token_hash TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL, used_at TEXT);
        CREATE TABLE IF NOT EXISTS school_password_reset_tokens (id INTEGER PRIMARY KEY AUTOINCREMENT, school_id INTEGER NOT NULL, token_hash TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL, used_at TEXT);
        CREATE TABLE IF NOT EXISTS school_purchases (id INTEGER PRIMARY KEY AUTOINCREMENT, school_id INTEGER NOT NULL, plan TEXT NOT NULL, amount_cents INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS clubs (id INTEGER PRIMARY KEY AUTOINCREMENT, school_id INTEGER NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL, created_by INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (school_id) REFERENCES schools (id), FOREIGN KEY (created_by) REFERENCES teachers (id));
        CREATE TABLE IF NOT EXISTS club_roles (id INTEGER PRIMARY KEY AUTOINCREMENT, club_id INTEGER NOT NULL, user_id INTEGER NOT NULL, role_name TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE (club_id, user_id), FOREIGN KEY (club_id) REFERENCES clubs (id), FOREIGN KEY (user_id) REFERENCES users (id));
        CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, kind TEXT NOT NULL, message TEXT NOT NULL, page_url TEXT, user_agent TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        """
    )
    for table, column, definition in (
        ("users", "school_id", "INTEGER"),
        ("schools", "school_passcode_hash", "TEXT"),
        ("schools", "school_login_phrase_hash", "TEXT"),
        ("schools", "school_login_phrase_key", "TEXT"),
    ):
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_schools_login_phrase_key ON schools (school_login_phrase_key) WHERE school_login_phrase_key IS NOT NULL AND school_login_phrase_key != ''")
    db.commit()
    db.close()


def get_current_accounts(request):
    db = db_connection()
    user_id = request.session.get("user_id")
    school_id = request.session.get("school_id")
    teacher_id = request.session.get("teacher_id")
    user = db.execute("SELECT id, name, email FROM users WHERE id = ?", (user_id,)).fetchone() if user_id else None
    school = db.execute("SELECT * FROM schools WHERE id = ?", (school_id,)).fetchone() if school_id else None
    teacher = db.execute("SELECT teachers.*, schools.name AS school_name FROM teachers JOIN schools ON schools.id = teachers.school_id WHERE teachers.id = ?", (teacher_id,)).fetchone() if teacher_id else None
    db.close()
    return (dict(user) if user else None, dict(school) if school else None, dict(teacher) if teacher else None)


def row_dicts(rows):
    return [dict(row) for row in rows]


def generate_school_passcode():
    return f"{secrets.randbelow(1_000_000):06d}"


def valid_school_passcode(school, passcode):
    return bool(school and school["school_passcode_hash"] and passcode and check_password_hash(school["school_passcode_hash"], passcode))


def normalize_school_phrase(value):
    return " ".join(value.strip().casefold().split())


def school_phrase_key(value):
    normalized = normalize_school_phrase(value)
    return hmac.new(settings.SECRET_KEY.encode(), normalized.encode(), "sha256").hexdigest() if normalized else ""


def valid_school_login_phrase(school, phrase):
    if school and school["school_login_phrase_hash"] and phrase:
        return check_password_hash(school["school_login_phrase_hash"], normalize_school_phrase(phrase))
    return valid_school_passcode(school, phrase)


def normalize_time(value):
    for time_format in ("%I:%M %p", "%I %p", "%H:%M"):
        try:
            return datetime.strptime(value.strip().upper(), time_format).strftime("%H:%M")
        except ValueError:
            continue
    return None


def build_recurrence_dates(start_date, recurrence, end_date=None):
    if recurrence == "one_time":
        return [start_date]
    dates = []
    occurrence = 0
    while occurrence < 500:
        if recurrence in {"weekly", "biweekly"}:
            event_date = start_date + timedelta(days=7 * (2 if recurrence == "biweekly" else 1) * occurrence)
        else:
            month_offset = occurrence * (12 if recurrence == "yearly" else 1)
            month_index = start_date.month - 1 + month_offset
            year = start_date.year + month_index // 12
            month = month_index % 12 + 1
            event_date = date(year, month, min(start_date.day, monthrange(year, month)[1]))
        if event_date > end_date:
            break
        dates.append(event_date)
        occurrence += 1
    return dates


def safe_next(request, fallback):
    value = request.GET.get("next") or fallback
    return value if value.startswith("/") and not value.startswith("//") else fallback


def login_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user_account:
            messages.warning(request, "Please log in to continue.")
            return redirect(f"{reverse('login')}?{urlencode({'next': request.get_full_path()})}")
        return view(request, *args, **kwargs)
    return wrapped


def verified_school_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.school_account:
            messages.warning(request, "Please log in as a school to continue.")
            return redirect(f"{reverse('school_login')}?{urlencode({'next': request.get_full_path()})}")
        if not request.school_account["verified_at"]:
            messages.warning(request, "Verify your school email before continuing.")
            return redirect("school_verification_pending")
        return view(request, *args, **kwargs)
    return wrapped


def school_required(view):
    @verified_school_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.school_account["plan_status"] != "active":
            messages.warning(request, "Purchase a Clubbi plan before creating events.")
            return redirect("pricing")
        return view(request, *args, **kwargs)
    return wrapped


def teacher_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.teacher_account:
            messages.warning(request, "Please log in as a teacher to continue.")
            return redirect(f"{reverse('teacher_login')}?{urlencode({'next': request.get_full_path()})}")
        return view(request, *args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        allowed = {email.strip().lower() for email in os.environ.get("ADMIN_EMAILS", "").split(",") if email.strip()}
        if not request.user_account or request.user_account["email"].lower() not in allowed:
            messages.error(request, "Administrator access is required.")
            return redirect("login")
        return view(request, *args, **kwargs)
    return wrapped


def index(request):
    db = db_connection()
    school_count = db.execute("SELECT COUNT(*) AS count FROM schools").fetchone()["count"]
    event_count = db.execute("SELECT COUNT(*) AS count FROM events").fetchone()["count"]
    dashboard_events, school_events, starred_ids = [], [], set()
    if request.user_account:
        user_id = request.user_account["id"]
        dashboard_events = row_dicts(db.execute("""SELECT events.*, schools.name AS school_name, COUNT(event_signups.id) AS signup_count FROM events JOIN schools ON schools.id = events.school_id JOIN event_signups mine ON mine.event_id = events.id AND mine.user_id = ? LEFT JOIN event_signups ON event_signups.event_id = events.id GROUP BY events.id ORDER BY event_date, start_time LIMIT 100""", (user_id,)).fetchall())
        school_events = row_dicts(db.execute("""SELECT events.*, schools.name AS school_name, COUNT(event_signups.id) AS signup_count FROM events JOIN schools ON schools.id = events.school_id LEFT JOIN event_signups ON event_signups.event_id = events.id WHERE events.school_id IN (SELECT DISTINCT events.school_id FROM events JOIN event_signups ON event_signups.event_id = events.id WHERE event_signups.user_id = ?) GROUP BY events.id ORDER BY event_date, start_time LIMIT 100""", (user_id,)).fetchall())
        starred_ids = {row["event_id"] for row in db.execute("SELECT event_id FROM event_stars WHERE user_id = ?", (user_id,)).fetchall()}
    db.close()
    return render(request, "index.html", {"school_count": school_count, "event_count": event_count, "dashboard_events": dashboard_events, "school_events": school_events, "starred_ids": starred_ids})


def pricing(request):
    return render(request, "pricing.html", {"plans": PLANS})


def verify_school_email(request, token):
    db = db_connection()
    candidates = db.execute("SELECT * FROM school_verification_tokens WHERE used_at IS NULL AND expires_at > CURRENT_TIMESTAMP").fetchall()
    verification = next((row for row in candidates if check_password_hash(row["token_hash"], token)), None)
    if verification is None:
        db.close()
        messages.error(request, "That verification link is invalid or expired.")
        return redirect("school_login")
    db.execute("UPDATE school_verification_tokens SET used_at = CURRENT_TIMESTAMP WHERE id = ?", (verification["id"],))
    db.execute("UPDATE schools SET verified_at = CURRENT_TIMESTAMP WHERE id = ?", (verification["school_id"],))
    db.commit(); db.close()
    messages.success(request, "School email verified. You can now purchase a plan and create events.")
    return redirect("school_login")


def school_verification_pending(request):
    return render(request, "school_verification.html")


def school_password_reset_request(request):
    reset_message = "If that school account exists, a password reset link has been sent."
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        db = db_connection()
        school = db.execute("SELECT id FROM schools WHERE contact_email = ?", (email,)).fetchone()
        if school:
            token = secrets.token_urlsafe(32)
            expires = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            db.execute("UPDATE school_password_reset_tokens SET used_at = CURRENT_TIMESTAMP WHERE school_id = ? AND used_at IS NULL", (school["id"],))
            db.execute("INSERT INTO school_password_reset_tokens (school_id, token_hash, expires_at) VALUES (?, ?, ?)", (school["id"], generate_password_hash(token), expires))
            db.commit()
            reset_url = request.build_absolute_uri(reverse("school_password_reset", args=[token]))
            message = reset_message if send_password_reset_email(email, reset_url) else f"{reset_message} Password reset link for development: {reset_url}"
            messages.success(request, message)
        else:
            messages.success(request, reset_message)
        db.close()
        return render(request, "school_password_reset_request.html", {"reset_message": reset_message})
    return render(request, "school_password_reset_request.html")


def school_password_reset(request, token):
    db = db_connection()
    candidates = db.execute("SELECT * FROM school_password_reset_tokens WHERE used_at IS NULL AND expires_at > CURRENT_TIMESTAMP").fetchall()
    reset = next((row for row in candidates if check_password_hash(row["token_hash"], token)), None)
    if reset is None:
        db.close()
        messages.error(request, "That password reset link is invalid or expired.")
        return redirect("school_password_reset_request")
    reset_error = None
    if request.method == "POST":
        password = request.POST.get("password", "")
        confirmation = request.POST.get("password_confirmation", "")
        school_phrase = normalize_school_phrase(request.POST.get("school_phrase", ""))
        if len(password) < 8 or len(password) > 128:
            reset_error = "Password must be between 8 and 128 characters."
        elif password != confirmation:
            reset_error = "Passwords do not match."
        elif not 4 <= len(school_phrase) <= 80:
            reset_error = "Your school login phrase must be between 4 and 80 characters."
        else:
            phrase_key = school_phrase_key(school_phrase)
            phrase_owner = db.execute("SELECT id FROM schools WHERE school_login_phrase_key = ? AND id != ?", (phrase_key, reset["school_id"])).fetchone()
            if phrase_owner:
                reset_error = "That school login phrase is already in use. Choose another one."
            else:
                db.execute("UPDATE schools SET password_hash = ?, school_login_phrase_hash = ?, school_login_phrase_key = ? WHERE id = ?", (generate_password_hash(password), generate_password_hash(school_phrase), phrase_key, reset["school_id"]))
                db.execute("UPDATE school_password_reset_tokens SET used_at = CURRENT_TIMESTAMP WHERE id = ?", (reset["id"],))
                db.commit()
                db.close()
                messages.success(request, "School password and login phrase reset.")
                return redirect("school_login")
    db.close()
    return render(request, "school_password_reset.html", {"reset_error": reset_error})


def school_login(request):
    login_error = None
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        db = db_connection()
        school = db.execute("SELECT * FROM schools WHERE contact_email = ?", (email,)).fetchone()
        db.close()
        if (school is None or not school["password_hash"]
                or not check_password_hash(school["password_hash"], request.POST.get("password", ""))
                or not valid_school_login_phrase(school, request.POST.get("school_phrase", ""))):
            login_error = "Incorrect email, password, or school login phrase. Please try again."
        else:
            request.session.flush(); request.session["school_id"] = school["id"]
            return redirect(safe_next(request, reverse("register_event")))
    return render(request, "school_login.html", {"login_error": login_error})


@require_POST
def school_logout(request):
    request.session.pop("school_id", None)
    messages.success(request, "School account logged out.")
    return redirect("index")


def teacher_login(request):
    login_error = None
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        db = db_connection()
        teacher = db.execute("SELECT teachers.*, schools.school_passcode_hash, schools.school_login_phrase_hash FROM teachers JOIN schools ON schools.id = teachers.school_id WHERE teachers.email = ?", (email,)).fetchone()
        db.close()
        if (teacher is None or not check_password_hash(teacher["password_hash"], request.POST.get("password", ""))
                or not valid_school_login_phrase(teacher, request.POST.get("school_phrase", ""))):
            login_error = "Incorrect email, password, or school passcode. Please try again."
        else:
            request.session.flush()
            request.session["teacher_id"] = teacher["id"]
            return redirect(safe_next(request, reverse("teacher_dashboard")))
    return render(request, "teacher_login.html", {"login_error": login_error})


def teacher_register(request):
    db = db_connection()
    school_rows = row_dicts(db.execute("SELECT id, name, city FROM schools ORDER BY name").fetchall())
    db.close()
    registration_error = None
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        email = request.POST.get("email", "").strip().lower()
        password = request.POST.get("password", "")
        confirmation = request.POST.get("password_confirmation", "")
        school_phrase = request.POST.get("school_phrase", "")
        school_id = int(request.POST.get("school_id")) if request.POST.get("school_id", "").isdigit() else None
        if not name or not email or not school_id:
            registration_error = "Please complete every required field."
        elif len(name) > 120 or len(email) > 254:
            registration_error = "The provided details are too long."
        elif "@" not in email:
            registration_error = "Enter a valid email address."
        elif len(password) < 8 or len(password) > 128:
            registration_error = "Password must be between 8 and 128 characters."
        elif password != confirmation:
            registration_error = "Passwords do not match."
        else:
            db = db_connection()
            school = db.execute("SELECT id, school_passcode_hash, school_login_phrase_hash FROM schools WHERE id = ?", (school_id,)).fetchone()
            existing = db.execute("SELECT id FROM teachers WHERE email = ?", (email,)).fetchone()
            if school is None:
                registration_error = "Choose a valid school."
            elif not valid_school_login_phrase(school, school_phrase):
                registration_error = "Enter the correct login phrase for your school."
            elif existing:
                registration_error = "That teacher account already exists. Log in instead."
            else:
                db.execute("INSERT INTO teachers (name, email, password_hash, school_id) VALUES (?, ?, ?, ?)", (name, email, generate_password_hash(password), school_id))
                db.commit()
                db.close()
                messages.success(request, "Teacher account created. You can now log in.")
                return redirect("teacher_login")
            db.close()
        messages.error(request, registration_error)
    return render(request, "teacher_register.html", {"schools": school_rows, "registration_error": registration_error})


@teacher_required
def teacher_dashboard(request):
    db = db_connection()
    event_rows = row_dicts(db.execute("""
        SELECT events.id, events.title, events.description, events.category,
               events.event_date, events.start_time, events.location,
               schools.name AS school_name, event_heads.user_id AS head_id,
               users.name AS head_name
        FROM events
        JOIN schools ON schools.id = events.school_id
        LEFT JOIN event_heads ON event_heads.event_id = events.id
        LEFT JOIN users ON users.id = event_heads.user_id
        WHERE events.school_id = ?
        ORDER BY events.event_date, events.start_time, events.title
    """, (request.teacher_account["school_id"],)).fetchall())
    students = row_dicts(db.execute("SELECT id, name, email FROM users WHERE school_id = ? ORDER BY name", (request.teacher_account["school_id"],)).fetchall())
    db.close()
    return render(request, "teacher_dashboard.html", {"events": event_rows, "students": students})


@require_POST
@teacher_required
def assign_event_head(request, event_id):
    selected_user_id = request.POST.get("user_id", "").strip()
    db = db_connection()
    event = db.execute("SELECT id FROM events WHERE id = ? AND school_id = ?", (event_id, request.teacher_account["school_id"])).fetchone()
    if event is None:
        db.close()
        messages.error(request, "That event is not managed by your school.")
        return redirect("teacher_dashboard")
    if selected_user_id:
        student = db.execute("SELECT id FROM users WHERE id = ? AND school_id = ?", (selected_user_id, request.teacher_account["school_id"])).fetchone()
        if student is None:
            db.close()
            messages.error(request, "Choose a valid student account.")
            return redirect("teacher_dashboard")
        db.execute("""
            INSERT INTO event_heads (event_id, user_id, assigned_by)
            VALUES (?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET user_id = excluded.user_id, assigned_by = excluded.assigned_by
        """, (event_id, student["id"], request.teacher_account["id"]))
        messages.success(request, "Event head updated.")
    else:
        db.execute("DELETE FROM event_heads WHERE event_id = ?", (event_id,))
        messages.success(request, "Event head removed.")
    db.commit()
    db.close()
    return redirect("teacher_dashboard")


@teacher_required
def teacher_clubs(request):
    db = db_connection()
    clubs = row_dicts(db.execute("SELECT clubs.*, COUNT(club_roles.id) AS member_count FROM clubs LEFT JOIN club_roles ON club_roles.club_id = clubs.id WHERE clubs.school_id = ? GROUP BY clubs.id ORDER BY clubs.name", (request.teacher_account["school_id"],)).fetchall())
    students = row_dicts(db.execute("SELECT id, name FROM users WHERE school_id = ? ORDER BY name", (request.teacher_account["school_id"],)).fetchall())
    db.close()
    return render(request, "teacher_clubs.html", {"clubs": clubs, "students": students})


@require_POST
@teacher_required
def create_club(request):
    name = request.POST.get("name", "").strip()
    description = request.POST.get("description", "").strip()
    if not name or not description or len(name) > 120 or len(description) > 2000:
        messages.error(request, "Enter a club name and a description within the allowed lengths.")
        return redirect("teacher_clubs")
    db = db_connection()
    db.execute("INSERT INTO clubs (school_id, name, description, created_by) VALUES (?, ?, ?, ?)", (request.teacher_account["school_id"], name, description, request.teacher_account["id"]))
    db.commit()
    db.close()
    messages.success(request, "Club created.")
    return redirect("teacher_clubs")


@require_POST
@teacher_required
def assign_club_role(request, club_id):
    user_id = request.POST.get("user_id", "").strip()
    role_name = request.POST.get("role_name", "").strip()
    db = db_connection()
    club = db.execute("SELECT id FROM clubs WHERE id = ? AND school_id = ?", (club_id, request.teacher_account["school_id"])).fetchone()
    student = db.execute("SELECT id FROM users WHERE id = ? AND school_id = ?", (user_id, request.teacher_account["school_id"])).fetchone()
    if not club or not student or not role_name or len(role_name) > 80:
        db.close()
        messages.error(request, "Choose a valid student and role.")
        return redirect("teacher_clubs")
    db.execute("INSERT INTO club_roles (club_id, user_id, role_name) VALUES (?, ?, ?) ON CONFLICT(club_id, user_id) DO UPDATE SET role_name = excluded.role_name", (club_id, student["id"], role_name))
    db.commit()
    db.close()
    messages.success(request, "Club role saved.")
    return redirect("teacher_clubs")


@teacher_required
def event_attendees(request, event_id):
    db = db_connection()
    event = db.execute("SELECT events.id, events.title, events.school_id FROM events WHERE events.id = ? AND events.school_id = ?", (event_id, request.teacher_account["school_id"])).fetchone()
    attendees = row_dicts(db.execute("SELECT users.name, users.email, event_signups.created_at FROM event_signups JOIN users ON users.id = event_signups.user_id WHERE event_signups.event_id = ? ORDER BY users.name", (event_id,)).fetchall()) if event else []
    db.close()
    if not event:
        messages.error(request, "That event is not managed by your school.")
        return redirect("teacher_dashboard")
    return render(request, "event_attendees.html", {"event": dict(event), "attendees": attendees})


@require_POST
def teacher_logout(request):
    request.session.pop("teacher_id", None)
    messages.success(request, "Teacher account logged out.")
    return redirect("index")


@verified_school_required
def purchase_plan(request):
    selected_plan = request.GET.get("plan", "medium")
    if request.method == "POST":
        selected_plan = request.POST.get("plan", "")
        if selected_plan not in PLANS:
            messages.error(request, "Choose a valid plan.")
        else:
            db = db_connection(); plan = PLANS[selected_plan]
            db.execute("INSERT INTO school_purchases (school_id, plan, amount_cents, status) VALUES (?, ?, ?, 'paid')", (request.school_account["id"], selected_plan, plan["amount_cents"]))
            db.execute("UPDATE schools SET plan = ?, plan_status = 'active' WHERE id = ?", (selected_plan, request.school_account["id"]))
            db.commit(); db.close()
            messages.success(request, "Plan purchased. Your school can now create events.")
            return redirect("register_event")
    return render(request, "purchase.html", {"plans": PLANS, "selected_plan": selected_plan})


def search(request):
    query = request.GET.get("q", "").strip()[:100]
    schools, events = [], []
    if query:
        pattern = f"%{query}%"; db = db_connection()
        schools = row_dicts(db.execute("SELECT id, name, city FROM schools WHERE name LIKE ? OR city LIKE ? ORDER BY name LIMIT 50", (pattern, pattern)).fetchall())
        events = row_dicts(db.execute("SELECT events.id, events.title, events.event_date, events.location, schools.name AS school_name FROM events JOIN schools ON schools.id = events.school_id WHERE events.title LIKE ? OR events.description LIKE ? OR events.category LIKE ? OR events.location LIKE ? OR schools.name LIKE ? ORDER BY events.event_date, events.start_time LIMIT 50", (pattern, pattern, pattern, pattern, pattern)).fetchall()); db.close()
    return render(request, "search.html", {"query": query, "schools": schools, "events": events})


def events(request):
    selected_month = request.GET.get("month", date.today().strftime("%Y-%m"))
    try:
        month_date = datetime.strptime(selected_month, "%Y-%m").date().replace(day=1)
    except ValueError:
        month_date = date.today().replace(day=1); selected_month = month_date.strftime("%Y-%m")
    selected_school_id = request.GET.get("school_id")
    selected_school_id = int(selected_school_id) if selected_school_id and selected_school_id.isdigit() else None
    last_day = date(month_date.year, month_date.month, monthrange(month_date.year, month_date.month)[1])
    db = db_connection()
    event_rows = row_dicts(db.execute("SELECT events.*, schools.name AS school_name, COUNT(event_signups.id) AS signup_count FROM events JOIN schools ON schools.id = events.school_id LEFT JOIN event_signups ON event_signups.event_id = events.id WHERE event_date BETWEEN ? AND ? AND (? IS NULL OR events.school_id = ?) GROUP BY events.id ORDER BY event_date, start_time", (month_date.isoformat(), last_day.isoformat(), selected_school_id, selected_school_id)).fetchall())
    signed_up_ids, starred_ids = set(), set()
    if request.user_account:
        user_id = request.user_account["id"]
        signed_up_ids = {row["event_id"] for row in db.execute("SELECT event_id FROM event_signups WHERE user_id = ?", (user_id,)).fetchall()}
        starred_ids = {row["event_id"] for row in db.execute("SELECT event_id FROM event_stars WHERE user_id = ?", (user_id,)).fetchall()}
    db.close()
    for event in event_rows:
        event.pop("chat_enabled", None)
        event["start_time_display"] = normalize_time(event["start_time"]) or event["start_time"]
    previous_month = (month_date - timedelta(days=1)).strftime("%Y-%m")
    next_month = (date(month_date.year + 1, 1, 1) if month_date.month == 12 else date(month_date.year, month_date.month + 1, 1)).strftime("%Y-%m")
    if request.GET.get("format") == "json":
        return JsonResponse({"month": selected_month, "month_label": f"{month_name[month_date.month]} {month_date.year}", "previous_month": previous_month, "next_month": next_month, "events": [{**event, "start_time": event["start_time_display"], "signed_up": event["id"] in signed_up_ids, "starred": event["id"] in starred_ids} for event in event_rows]})
    first_weekday, days_in_month = monthrange(month_date.year, month_date.month)
    calendar_days = [None] * first_weekday + [date(month_date.year, month_date.month, day).isoformat() for day in range(1, days_in_month + 1)]
    return render(request, "events.html", {"events": event_rows, "calendar_days": calendar_days, "event_days": {event["event_date"] for event in event_rows}, "month_label": f"{month_name[month_date.month]} {month_date.year}", "selected_month": selected_month, "signed_up_ids": signed_up_ids, "previous_month": previous_month, "next_month": next_month, "selected_school_id": selected_school_id, "starred_ids": starred_ids})


@login_required
def all_events(request):
    db = db_connection(); events_rows = row_dicts(db.execute("SELECT events.*, schools.name AS school_name, COUNT(event_signups.id) AS signup_count FROM events JOIN schools ON schools.id = events.school_id LEFT JOIN event_signups ON event_signups.event_id = events.id GROUP BY events.id ORDER BY event_date, start_time, events.title LIMIT 500").fetchall())
    user_id = request.user_account["id"]
    signed_up_ids = {row["event_id"] for row in db.execute("SELECT event_id FROM event_signups WHERE user_id = ?", (user_id,)).fetchall()}
    starred_ids = {row["event_id"] for row in db.execute("SELECT event_id FROM event_stars WHERE user_id = ?", (user_id,)).fetchall()}; db.close()
    return render(request, "all_events.html", {"events": events_rows, "signed_up_ids": signed_up_ids, "starred_ids": starred_ids})


@require_POST
@login_required
def toggle_event_star(request, event_id):
    db = db_connection(); event = db.execute("SELECT id FROM events WHERE id = ?", (event_id,)).fetchone()
    if event is None:
        db.close(); return JsonResponse({"error": "That event could not be found."}, status=404) if request.headers.get("Accept") == "application/json" else redirect("events")
    existing = db.execute("SELECT id FROM event_stars WHERE event_id = ? AND user_id = ?", (event_id, request.user_account["id"])).fetchone()
    if existing: db.execute("DELETE FROM event_stars WHERE id = ?", (existing["id"],))
    else: db.execute("INSERT INTO event_stars (event_id, user_id) VALUES (?, ?)", (event_id, request.user_account["id"]))
    db.commit(); db.close()
    if request.headers.get("Accept") == "application/json": return JsonResponse({"starred": existing is None})
    return redirect(request.POST.get("return_to") if request.POST.get("return_to", "").startswith("/") and not request.POST.get("return_to", "").startswith("//") else reverse("events"))


@require_POST
def signup_for_event(request, event_id):
    db = db_connection(); event = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if event is None: db.close(); messages.error(request, "That event could not be found."); return redirect("events")
    if not request.user_account:
        db.close(); messages.warning(request, "Please log in to sign up for an event."); return redirect(f"{reverse('login')}?{urlencode({'next': reverse('events')})}")
    existing = db.execute("SELECT id FROM event_signups WHERE event_id = ? AND user_id = ?", (event_id, request.user_account["id"])).fetchone()
    count = db.execute("SELECT COUNT(*) AS count FROM event_signups WHERE event_id = ?", (event_id,)).fetchone()["count"]
    if existing: messages.warning(request, "You are already signed up for this event.")
    elif event["capacity"] and count >= event["capacity"]: messages.warning(request, "This event is full.")
    else:
        db.execute("INSERT INTO event_signups (event_id, user_id) VALUES (?, ?)", (event_id, request.user_account["id"])); db.commit()
        send_event_email(request.user_account["email"], "Clubbi event signup", f"You are signed up for {event['title']} on {event['event_date']} at {event['start_time']}.")
        messages.success(request, f"You are signed up for {event['title']}.")
    db.close(); return redirect(f"{reverse('events')}?{urlencode({'month': event['event_date'][:7]})}")


@require_POST
@login_required
def cancel_event_signup(request, event_id):
    db = db_connection()
    event = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if event:
        deleted = db.execute("DELETE FROM event_signups WHERE event_id = ? AND user_id = ?", (event_id, request.user_account["id"])).rowcount
        db.commit()
    else:
        deleted = 0
    db.close()
    if deleted:
        send_event_email(request.user_account["email"], "Clubbi signup cancelled", f"Your signup for {event['title']} on {event['event_date']} has been cancelled.")
        messages.success(request, "Your signup was cancelled.")
    else:
        messages.warning(request, "You were not signed up for that event.")
    return redirect(f"{reverse('events')}?{urlencode({'month': event['event_date'][:7] if event else date.today().strftime('%Y-%m')})}")


def send_event_email(recipient, subject, body):
    return send_email(recipient, subject, body)


def schools(request):
    query = request.GET.get("q", "").strip(); db = db_connection()
    rows = db.execute("SELECT * FROM schools WHERE name LIKE ? OR city LIKE ? ORDER BY name", (f"%{query}%", f"%{query}%")).fetchall() if query else db.execute("SELECT * FROM schools ORDER BY name").fetchall()
    result = row_dicts(rows); db.close(); return render(request, "schools.html", {"schools": result, "query": query})


@admin_required
def admin_dashboard(request):
    db = db_connection()
    schools_rows = row_dicts(db.execute("SELECT id, name, city, contact_email FROM schools ORDER BY name").fetchall())
    events_rows = row_dicts(db.execute("SELECT events.id, events.title, events.event_date, schools.name AS school_name FROM events JOIN schools ON schools.id = events.school_id ORDER BY events.event_date DESC").fetchall())
    feedback_rows = row_dicts(db.execute("SELECT * FROM feedback ORDER BY created_at DESC LIMIT 50").fetchall())
    db.close()
    return render(request, "admin_dashboard.html", {"schools": schools_rows, "events": events_rows, "feedback": feedback_rows})


@require_POST
@admin_required
def admin_delete_event(request, event_id):
    db = db_connection()
    db.execute("DELETE FROM event_signups WHERE event_id = ?", (event_id,))
    db.execute("DELETE FROM event_stars WHERE event_id = ?", (event_id,))
    db.execute("DELETE FROM event_heads WHERE event_id = ?", (event_id,))
    db.execute("DELETE FROM events WHERE id = ?", (event_id,))
    db.commit(); db.close()
    messages.success(request, "Event removed.")
    return redirect("admin_dashboard")


@require_POST
@admin_required
def admin_delete_school(request, school_id):
    db = db_connection()
    event_ids = [row["id"] for row in db.execute("SELECT id FROM events WHERE school_id = ?", (school_id,)).fetchall()]
    for event_id in event_ids:
        db.execute("DELETE FROM event_signups WHERE event_id = ?", (event_id,))
        db.execute("DELETE FROM event_stars WHERE event_id = ?", (event_id,))
        db.execute("DELETE FROM event_heads WHERE event_id = ?", (event_id,))
    db.execute("DELETE FROM events WHERE school_id = ?", (school_id,))
    db.execute("DELETE FROM club_roles WHERE club_id IN (SELECT id FROM clubs WHERE school_id = ?)", (school_id,))
    db.execute("DELETE FROM clubs WHERE school_id = ?", (school_id,))
    db.execute("DELETE FROM teachers WHERE school_id = ?", (school_id,))
    db.execute("DELETE FROM users WHERE school_id = ?", (school_id,))
    db.execute("DELETE FROM schools WHERE id = ?", (school_id,))
    db.commit(); db.close()
    messages.success(request, "School and its data were removed.")
    return redirect("admin_dashboard")


def legal_page(request, page):
    if page not in {"terms", "privacy"}:
        raise Http404
    return render(request, f"{page}.html")


def feedback(request, kind="feedback"):
    if request.method == "POST":
        message = request.POST.get("message", "").strip()
        if not message or len(message) > 4000:
            messages.error(request, "Please enter a message under 4,000 characters.")
        else:
            db = db_connection()
            db.execute("INSERT INTO feedback (user_id, kind, message, page_url, user_agent) VALUES (?, ?, ?, ?, ?)", (request.user_account["id"] if request.user_account else None, kind, message, request.POST.get("page_url", "")[:500], request.META.get("HTTP_USER_AGENT", "")[:500]))
            db.commit(); db.close()
            messages.success(request, "Thanks. Your message was sent.")
            return redirect("index")
    return render(request, "feedback.html", {"kind": kind})


def send_verification_email(recipient, verification_url):
    return send_email(recipient, "Verify your Clubbi school account", f"Verify your school account by opening this link:\n\n{verification_url}\n\nThis link expires in 24 hours.")


def send_password_reset_email(recipient, reset_url):
    return send_email(recipient, "Reset your Clubbi school password", f"Reset your school password by opening this link:\n\n{reset_url}\n\nThis link expires in 1 hour.")


def send_email(recipient, subject, body):
    smtp_host = os.environ.get("SMTP_HOST")
    if not smtp_host: return False
    message = EmailMessage(); message["Subject"] = subject; message["From"] = os.environ.get("SMTP_FROM", "no-reply@clubbi.local"); message["To"] = recipient; message.set_content(body)
    try:
        with smtplib.SMTP(smtp_host, int(os.environ.get("SMTP_PORT", "587")), timeout=10) as smtp:
            smtp.starttls(); smtp.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"]); smtp.send_message(message)
    except (KeyError, OSError, smtplib.SMTPException, ValueError): return False
    return True


def register_school_event(request):
    if request.method == "POST":
        school_name = request.POST.get("school_name", "").strip(); city = request.POST.get("city", "").strip(); contact_email = request.POST.get("contact_email", "").strip().lower(); password = request.POST.get("password", ""); confirmation = request.POST.get("password_confirmation", ""); school_phrase = normalize_school_phrase(request.POST.get("school_phrase", "")); error = None
        if not all((school_name, city, contact_email)): error = "Please complete every required field."
        elif any(len(value) > 200 for value in (school_name, city, contact_email)): error = "School details must be 200 characters or fewer."
        elif "@" not in contact_email: error = "Enter a valid school contact email."
        elif len(password) < 8 or len(password) > 128: error = "School password must be between 8 and 128 characters."
        elif password != confirmation: error = "School passwords do not match."
        elif not 4 <= len(school_phrase) <= 80: error = "Your school login phrase must be between 4 and 80 characters."
        if error is None:
            db = db_connection(); school = db.execute("SELECT * FROM schools WHERE name = ? OR contact_email = ?", (school_name, contact_email)).fetchone(); phrase_key = school_phrase_key(school_phrase)
            phrase_owner = db.execute("SELECT id FROM schools WHERE school_login_phrase_key = ?", (phrase_key,)).fetchone()
            if school and school["password_hash"]: error = "That school account already exists. Log in instead."
            elif phrase_owner and (not school or phrase_owner["id"] != school["id"]): error = "That school login phrase is already in use. Choose another one."
            elif school: school_id = school["id"]; db.execute("UPDATE schools SET city = ?, contact_email = ?, password_hash = ?, school_login_phrase_hash = ?, school_login_phrase_key = ? WHERE id = ?", (city, contact_email, generate_password_hash(password), generate_password_hash(school_phrase), phrase_key, school_id))
            else: school_id = db.execute("INSERT INTO schools (name, city, contact_email, password_hash, school_login_phrase_hash, school_login_phrase_key) VALUES (?, ?, ?, ?, ?, ?)", (school_name, city, contact_email, generate_password_hash(password), generate_password_hash(school_phrase), phrase_key)).lastrowid
            if error is None:
                token = secrets.token_urlsafe(32); expires = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
                db.execute("INSERT INTO school_verification_tokens (school_id, token_hash, expires_at) VALUES (?, ?, ?)", (school_id, generate_password_hash(token), expires)); db.commit(); db.close()
                verification_url = request.build_absolute_uri(reverse("verify_school_email", args=[token]))
                message = "Verification email sent. Check the school contact inbox." if send_verification_email(contact_email, verification_url) else f"Verification link for development: {verification_url}"
                message += " Your school login phrase is ready. Share it only with your school community."
                messages.success(request, message); return redirect("school_verification_pending")
            db.close()
        messages.error(request, error)
    return render(request, "school_register.html")


@school_required
def register_event(request):
    db = db_connection(); school_rows = row_dicts(db.execute("SELECT id, name, city FROM schools ORDER BY name").fetchall()); selected_school_id = request.school_account["id"]
    if request.method == "POST":
        selected_school_id = int(request.POST.get("school_id")) if request.POST.get("school_id", "").isdigit() else None
        title = request.POST.get("title", "").strip(); description = request.POST.get("description", "").strip(); category = request.POST.get("category", "").strip(); event_date = request.POST.get("event_date", "").strip(); start_time = request.POST.get("start_time", "").strip(); normalized = normalize_time(start_time); location = request.POST.get("location", "").strip(); capacity_text = request.POST.get("capacity", "").strip(); recurrence = request.POST.get("recurrence", "one_time"); end_text = request.POST.get("recurrence_end", "").strip(); error = None; event_dates = []
        if selected_school_id != request.school_account["id"]: error = "You can only create events for your school account."
        elif not all((selected_school_id, title, description, category, event_date, start_time, location)): error = "Please complete every required field."
        elif normalized is None: error = "Enter a valid time such as 3:00 PM."
        elif any(len(value) > limit for value, limit in ((title, 200), (description, 2000), (category, 60), (location, 200))): error = "Event details exceed the allowed length."
        elif recurrence not in {"one_time", "weekly", "biweekly", "monthly", "yearly"}: error = "Choose a valid repeat option."
        else:
            try:
                start_date = datetime.strptime(event_date, "%Y-%m-%d").date(); capacity = int(capacity_text) if capacity_text else None
                if capacity is not None and not 1 <= capacity <= 100000: error = "Capacity must be a positive number."
                elif recurrence == "one_time": event_dates = [start_date]
                elif not end_text: error = "Choose an end date for a repeating event."
                else:
                    end_date = datetime.strptime(end_text, "%Y-%m-%d").date(); event_dates = build_recurrence_dates(start_date, recurrence, end_date)
                    if end_date < start_date: error = "The repeat-until date must be on or after the event date."
                    elif len(event_dates) >= 500: error = "That repeat schedule creates too many events. Choose an earlier end date."
            except ValueError: error = "Enter valid dates and capacity."
        if error is None:
            db.executemany("INSERT INTO events (school_id, title, description, category, event_date, start_time, location, capacity) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [(selected_school_id, title, description, category, event_day.isoformat(), normalized, location, int(capacity_text) if capacity_text else None) for event_day in event_dates]); db.commit(); db.close(); messages.success(request, f"{len(event_dates)} event{'s' if len(event_dates) != 1 else ''} listed on Clubbi."); return redirect(f"{reverse('register_event')}?{urlencode({'school_id': selected_school_id})}")
        messages.error(request, error)
    db.close(); return render(request, "event_register.html", {"schools": school_rows, "selected_school_id": selected_school_id})


def login_view(request):
    if request.user_account: return redirect("index")
    login_error = None
    db = db_connection()
    school_rows = row_dicts(db.execute("SELECT id, name, city FROM schools ORDER BY name").fetchall())
    db.close()
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        school_id = int(request.POST.get("school_id")) if request.POST.get("school_id", "").isdigit() else None
        db = db_connection()
        user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        school = db.execute("SELECT * FROM schools WHERE id = ?", (school_id,)).fetchone() if school_id else None
        db.close()
        if (user is None or school is None or user["school_id"] != school_id
                or not check_password_hash(user["password_hash"], request.POST.get("password", ""))
                or not valid_school_login_phrase(school, request.POST.get("school_phrase", ""))):
            login_error = "Incorrect email, password, or school login phrase. Please try again."
        else:
            request.session.flush()
            request.session["user_id"] = user["id"]
            return redirect(safe_next(request, reverse("index")))
    return render(request, "login.html", {"mode": "login", "login_error": login_error, "schools": school_rows})


def register_view(request):
    if request.user_account: return redirect("index")
    db = db_connection()
    school_rows = row_dicts(db.execute("SELECT id, name, city FROM schools ORDER BY name").fetchall())
    db.close()
    if request.method == "POST":
        name = request.POST.get("name", "").strip(); email = request.POST.get("email", "").strip().lower(); password = request.POST.get("password", ""); school_phrase = request.POST.get("school_phrase", ""); school_id = int(request.POST.get("school_id")) if request.POST.get("school_id", "").isdigit() else None; error = None
        if len(name) > 120 or len(email) > 254: error = "The provided details are too long."
        elif not name: error = "Name is required."
        elif not email or "@" not in email: error = "Enter a valid email address."
        elif len(password) < 8 or len(password) > 128: error = "Password must be at least 8 characters."
        elif not school_id: error = "Choose your school."
        if error is None:
            db = db_connection()
            school = db.execute("SELECT * FROM schools WHERE id = ?", (school_id,)).fetchone()
            if not valid_school_login_phrase(school, school_phrase):
                error = "Enter the correct login phrase for your school."
            try:
                if error is None:
                    db.execute("INSERT INTO users (name, email, password_hash, school_id) VALUES (?, ?, ?, ?)", (name, email, generate_password_hash(password), school_id)); db.commit()
            except sqlite3.IntegrityError: error = "An account with that email already exists."
            db.close()
        if error is None: messages.success(request, "Account created. You can now log in with your school login phrase."); return redirect("login")
        messages.error(request, error)
    return render(request, "login.html", {"mode": "register", "schools": school_rows})


@require_POST
def logout_view(request):
    request.session.flush(); messages.success(request, "You have been logged out."); return redirect("login")