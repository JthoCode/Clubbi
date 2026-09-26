from django.conf import settings
from django.conf.urls.static import static
from django.urls import path

from . import views


urlpatterns = [
    path("", views.index, name="index"),
    path("pricing", views.pricing, name="pricing"),
    path("school/verify/<str:token>", views.verify_school_email, name="verify_school_email"),
    path("school/verification-pending", views.school_verification_pending, name="school_verification_pending"),
    path("school/password-reset", views.school_password_reset_request, name="school_password_reset_request"),
    path("school/password-reset/<str:token>", views.school_password_reset, name="school_password_reset"),
    path("school/login", views.school_login, name="school_login"),
    path("school/logout", views.school_logout, name="school_logout"),
    path("teacher/login", views.teacher_login, name="teacher_login"),
    path("teacher/register", views.teacher_register, name="teacher_register"),
    path("teacher/dashboard", views.teacher_dashboard, name="teacher_dashboard"),
    path("teacher/logout", views.teacher_logout, name="teacher_logout"),
    path("teacher/events/<int:event_id>/head", views.assign_event_head, name="assign_event_head"),
    path("teacher/events/<int:event_id>/attendees", views.event_attendees, name="event_attendees"),
    path("teacher/clubs", views.teacher_clubs, name="teacher_clubs"),
    path("teacher/clubs/create", views.create_club, name="create_club"),
    path("teacher/clubs/<int:club_id>/roles", views.assign_club_role, name="assign_club_role"),
    path("school/purchase", views.purchase_plan, name="purchase_plan"),
    path("search", views.search, name="search"),
    path("events", views.events, name="events"),
    path("events/all", views.all_events, name="all_events"),
    path("events/<int:event_id>/star", views.toggle_event_star, name="toggle_event_star"),
    path("events/<int:event_id>/signup", views.signup_for_event, name="signup_for_event"),
    path("events/<int:event_id>/cancel", views.cancel_event_signup, name="cancel_event_signup"),
    path("schools", views.schools, name="schools"),
    path("schools/register", views.register_school_event, name="register_school_event"),
    path("school/register", views.register_school_event),
    path("events/register", views.register_event, name="register_event"),
    path("login", views.login_view, name="login"),
    path("register", views.register_view, name="register"),
    path("logout", views.logout_view, name="logout"),
    path("admin", views.admin_dashboard, name="admin_dashboard"),
    path("admin/events/<int:event_id>/delete", views.admin_delete_event, name="admin_delete_event"),
    path("admin/schools/<int:school_id>/delete", views.admin_delete_school, name="admin_delete_school"),
    path("feedback", views.feedback, name="feedback"),
    path("report-problem", lambda request: views.feedback(request, "problem"), name="report_problem"),
    path("terms", lambda request: views.legal_page(request, "terms"), name="terms"),
    path("privacy", lambda request: views.legal_page(request, "privacy"), name="privacy"),
]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.BASE_DIR / "static")