import os
import re
import sqlite3
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, override_settings
from django.db import connections
from django.conf import settings
from werkzeug.security import check_password_hash, generate_password_hash

from . import views


class ClubbiFlowTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        file_descriptor, database_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(file_descriptor)
        cls.database_file = Path(database_path)
        cls.database_override = override_settings(DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": cls.database_file}})
        cls.database_override.enable()

    @classmethod
    def tearDownClass(cls):
        connections.close_all()
        cls.database_override.disable()
        cls.database_file.unlink(missing_ok=True)
        super().tearDownClass()

    def setUp(self):
        connections.close_all()
        self.database_file.unlink(missing_ok=True)
        views.ensure_schema()

    def add_school(self, name, email, verified=True, active=True):
        db = views.db_connection()
        school_id = db.execute(
            "INSERT INTO schools (name, city, contact_email, password_hash, school_passcode_hash, verified_at, plan, plan_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, "Test City", email, generate_password_hash("school-password"), generate_password_hash("123456"), "2026-01-01" if verified else None, "medium" if active else None, "active" if active else "inactive"),
        ).lastrowid
        db.commit()
        db.close()
        return school_id

    def add_user(self, name, email, school_id, password="student-password"):
        db = views.db_connection()
        user_id = db.execute(
            "INSERT INTO users (name, email, password_hash, school_id) VALUES (?, ?, ?, ?)",
            (name, email, generate_password_hash(password), school_id),
        ).lastrowid
        db.commit()
        db.close()
        return user_id

    def add_teacher(self, name, email, school_id):
        db = views.db_connection()
        teacher_id = db.execute(
            "INSERT INTO teachers (name, email, password_hash, school_id) VALUES (?, ?, ?, ?)",
            (name, email, generate_password_hash("teacher-password"), school_id),
        ).lastrowid
        db.commit()
        db.close()
        return teacher_id

    def add_event(self, school_id, title="Test event", capacity=None):
        db = views.db_connection()
        event_id = db.execute(
            "INSERT INTO events (school_id, title, description, category, event_date, start_time, location, capacity) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (school_id, title, "A test event", "Club", "2026-10-01", "15:00", "Room 1", capacity),
        ).lastrowid
        db.commit()
        db.close()
        return event_id

    def login_session(self, **values):
        session = self.client.session
        session.flush()
        session.update(values)
        session.save()
        self.client.cookies[settings.SESSION_COOKIE_NAME] = session.session_key

    def test_student_registration_rejects_wrong_school_passcode(self):
        school_id = self.add_school("North High", "north@example.com")
        response = self.client.post("/register", {"name": "A Student", "email": "student@example.com", "password": "student-password", "school_id": school_id, "school_phrase": "000000"})
        self.assertEqual(response.status_code, 200)
        db = views.db_connection()
        self.assertIsNone(db.execute("SELECT id FROM users WHERE email = ?", ("student@example.com",)).fetchone())
        db.close()

    def test_student_can_register_and_login(self):
        school_id = self.add_school("North High", "north@example.com")
        response = self.client.post("/register", {"name": "A Student", "email": "student@example.com", "password": "student-password", "school_id": school_id, "school_phrase": "123456"})
        self.assertRedirects(response, "/login")
        response = self.client.post("/login", {"email": "student@example.com", "password": "student-password", "school_id": school_id, "school_phrase": "123456"})
        self.assertRedirects(response, "/")
        self.assertEqual(self.client.session.get("user_id"), 1)

    def test_teacher_dashboard_requires_teacher_login(self):
        response = self.client.get("/teacher/dashboard")
        self.assertRedirects(response, "/teacher/login?next=%2Fteacher%2Fdashboard")

    def test_teacher_dashboard_only_lists_students_from_its_school(self):
        first_school = self.add_school("North High", "north@example.com")
        second_school = self.add_school("South High", "south@example.com")
        teacher_id = self.add_teacher("North Teacher", "teacher@example.com", first_school)
        self.add_user("North Student", "north-student@example.com", first_school)
        self.add_user("South Student", "south-student@example.com", second_school)
        self.add_event(first_school)
        self.login_session(teacher_id=teacher_id)
        response = self.client.get("/teacher/dashboard")
        self.assertContains(response, "North Student")
        self.assertNotContains(response, "South Student")

    def test_teacher_cannot_assign_student_from_another_school(self):
        first_school = self.add_school("North High", "north@example.com")
        second_school = self.add_school("South High", "south@example.com")
        teacher_id = self.add_teacher("North Teacher", "teacher@example.com", first_school)
        foreign_student_id = self.add_user("South Student", "south-student@example.com", second_school)
        event_id = self.add_event(first_school)
        self.login_session(teacher_id=teacher_id)
        response = self.client.post(f"/teacher/events/{event_id}/head", {"user_id": foreign_student_id})
        self.assertRedirects(response, "/teacher/dashboard")
        db = views.db_connection()
        self.assertIsNone(db.execute("SELECT id FROM event_heads WHERE event_id = ?", (event_id,)).fetchone())
        db.close()

    def test_event_signup_requires_login(self):
        school_id = self.add_school("North High", "north@example.com")
        event_id = self.add_event(school_id)
        response = self.client.post(f"/events/{event_id}/signup")
        self.assertRedirects(response, "/login?next=%2Fevents")

    def test_event_capacity_is_enforced(self):
        school_id = self.add_school("North High", "north@example.com")
        first_user = self.add_user("First Student", "first@example.com", school_id)
        second_user = self.add_user("Second Student", "second@example.com", school_id)
        event_id = self.add_event(school_id, capacity=1)
        self.login_session(user_id=first_user)
        self.assertEqual(self.client.post(f"/events/{event_id}/signup").status_code, 302)
        self.login_session(user_id=second_user)
        response = self.client.post(f"/events/{event_id}/signup")
        self.assertEqual(response.status_code, 302)
        db = views.db_connection()
        signups = db.execute("SELECT user_id FROM event_signups WHERE event_id = ?", (event_id,)).fetchall()
        self.assertEqual([row["user_id"] for row in signups], [first_user])
        db.close()

    def test_student_can_cancel_event_signup(self):
        school_id = self.add_school("North High", "north@example.com")
        user_id = self.add_user("Student", "student@example.com", school_id)
        event_id = self.add_event(school_id)
        self.login_session(user_id=user_id)
        self.client.post(f"/events/{event_id}/signup")
        response = self.client.post(f"/events/{event_id}/cancel")
        self.assertEqual(response.status_code, 302)
        db = views.db_connection()
        self.assertIsNone(db.execute("SELECT id FROM event_signups WHERE event_id = ? AND user_id = ?", (event_id, user_id)).fetchone())
        db.close()

    def test_teacher_can_create_club_and_assign_role(self):
        school_id = self.add_school("North High", "north@example.com")
        teacher_id = self.add_teacher("Teacher", "teacher@example.com", school_id)
        student_id = self.add_user("Student", "student@example.com", school_id)
        self.login_session(teacher_id=teacher_id)
        response = self.client.post("/teacher/clubs/create", {"name": "Robotics", "description": "Build and learn."})
        self.assertRedirects(response, "/teacher/clubs")
        db = views.db_connection()
        club_id = db.execute("SELECT id FROM clubs WHERE name = ?", ("Robotics",)).fetchone()["id"]
        db.close()
        response = self.client.post(f"/teacher/clubs/{club_id}/roles", {"user_id": student_id, "role_name": "President"})
        self.assertRedirects(response, "/teacher/clubs")
        db = views.db_connection()
        role = db.execute("SELECT role_name FROM club_roles WHERE club_id = ? AND user_id = ?", (club_id, student_id)).fetchone()
        self.assertEqual(role["role_name"], "President")
        db.close()

    def test_school_phrase_login_is_case_insensitive(self):
        response = self.client.post("/schools/register", {"school_name": "Phrase North", "city": "Test City", "contact_email": "phrase@example.com", "password": "school-password", "password_confirmation": "school-password", "school_phrase": "Campus Key"})
        self.assertRedirects(response, "/school/verification-pending")
        response = self.client.post("/school/login", {"email": "phrase@example.com", "password": "school-password", "school_phrase": "  CAMPUS   KEY "})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/events/register")

    def test_feedback_records_page_and_browser_details(self):
        response = self.client.post("/feedback", {"message": "The beta is useful.", "page_url": "/events"}, HTTP_USER_AGENT="Clubbi Test Browser")
        self.assertRedirects(response, "/")
        db = views.db_connection()
        item = db.execute("SELECT kind, page_url, user_agent FROM feedback ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(item["kind"], "feedback")
        self.assertEqual(item["page_url"], "/events")
        self.assertEqual(item["user_agent"], "Clubbi Test Browser")
        db.close()

    def test_school_password_reset_changes_password_and_passcode(self):
        school_id = self.add_school("North High", "north@example.com")
        response = self.client.post("/school/password-reset", {"email": "north@example.com"})
        match = re.search(r"/school/password-reset/([A-Za-z0-9_-]+)", response.content.decode())
        self.assertIsNotNone(match)
        token = match.group(1)
        response = self.client.post(f"/school/password-reset/{token}", {"password": "new-school-password", "password_confirmation": "new-school-password", "school_phrase": "north campus"})
        self.assertRedirects(response, "/school/login")
        db = views.db_connection()
        school = db.execute("SELECT * FROM schools WHERE id = ?", (school_id,)).fetchone()
        self.assertTrue(check_password_hash(school["password_hash"], "new-school-password"))
        self.assertTrue(check_password_hash(school["school_login_phrase_hash"], "north campus"))
        self.assertIsNotNone(db.execute("SELECT used_at FROM school_password_reset_tokens WHERE school_id = ? AND used_at IS NOT NULL", (school_id,)).fetchone())
        db.close()

    def test_school_password_reset_does_not_reveal_unknown_email(self):
        response = self.client.post("/school/password-reset", {"email": "unknown@example.com"})
        self.assertContains(response, "If that school account exists, a password reset link has been sent.")

    def test_school_phrase_is_unique_after_normalization(self):
        response = self.client.post("/schools/register", {"school_name": "Phrase North", "city": "Test City", "contact_email": "phrase-north@example.com", "password": "school-password", "password_confirmation": "school-password", "school_phrase": "  CAMPUS  "})
        self.assertRedirects(response, "/school/verification-pending")
        response = self.client.post("/schools/register", {"school_name": "South High", "city": "Test City", "contact_email": "new-south@example.com", "password": "school-password", "password_confirmation": "school-password", "school_phrase": "campus"})
        self.assertContains(response, "already in use")
        db = views.db_connection()
        self.assertIsNotNone(db.execute("SELECT school_login_phrase_key FROM schools WHERE contact_email = ?", ("phrase-north@example.com",)).fetchone()["school_login_phrase_key"])
        self.assertIsNone(db.execute("SELECT school_login_phrase_key FROM schools WHERE contact_email = ?", ("new-south@example.com",)).fetchone())
        db.close()

    def test_phrase_input_is_parameterized_data(self):
        response = self.client.post("/schools/register", {"school_name": "Injection Academy", "city": "Test City", "contact_email": "injection@example.com", "password": "school-password", "password_confirmation": "school-password", "school_phrase": "'); DROP TABLE schools; --"})
        self.assertRedirects(response, "/school/verification-pending")
        db = views.db_connection()
        self.assertIsNotNone(db.execute("SELECT id FROM schools WHERE contact_email = ?", ("injection@example.com",)).fetchone())
        self.assertIsNotNone(db.execute("SELECT name FROM schools", ()).fetchone())
        db.close()
