import json
from datetime import datetime

from django.urls import reverse
from jinja2 import Environment


def environment(**options):
    env = Environment(**options)

    def url_for(name, **kwargs):
        external = kwargs.pop("_external", False)
        if name == "static":
            path = f"/static/{kwargs.get('filename', '')}"
        else:
            path = reverse(name, kwargs={key: value for key, value in kwargs.items() if key not in {"next", "plan", "month", "school_id"}})
            query = {key: value for key, value in kwargs.items() if key in {"next", "plan", "month", "school_id"} and value is not None}
            if query:
                from urllib.parse import urlencode
                path = f"{path}?{urlencode(query)}"
        if external:
            return path
        return path

    env.globals.update(
        url_for=url_for,
        zip=zip,
    )
    env.filters["tojson"] = lambda value: json.dumps(value)
    def time12(value):
        for time_format in ("%I:%M %p", "%I %p", "%H:%M"):
            try:
                return datetime.strptime(str(value).strip().upper(), time_format).strftime("%I:%M %p").lstrip("0")
            except ValueError:
                continue
        return value
    env.filters["time12"] = time12
    return env