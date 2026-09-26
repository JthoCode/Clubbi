from django.contrib import messages
from django.middleware.csrf import get_token


def accounts(request):
    queued_messages = [(message.level_tag, str(message)) for message in messages.get_messages(request)]
    return {
        "current_user": getattr(request, "user_account", None),
        "current_school": getattr(request, "school_account", None),
        "current_teacher": getattr(request, "teacher_account", None),
        "g": type("AccountContext", (), {"user": getattr(request, "user_account", None), "school": getattr(request, "school_account", None), "teacher": getattr(request, "teacher_account", None)})(),
        "csrf_token": get_token(request),
        "get_flashed_messages": lambda with_categories=False: queued_messages if with_categories else [message for _, message in queued_messages],
    }