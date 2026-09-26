from .views import ensure_schema, get_current_accounts


class CurrentAccountMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        ensure_schema()
        request.user_account, request.school_account, request.teacher_account = get_current_accounts(request)
        request.form = request.POST
        request.args = request.GET
        return self.get_response(request)