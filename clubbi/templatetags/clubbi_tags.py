from datetime import datetime

from django import template

register = template.Library()


@register.filter
def time12(value):
    for time_format in ("%I:%M %p", "%I %p", "%H:%M"):
        try:
            normalized = datetime.strptime(str(value).strip().upper(), time_format)
            return normalized.strftime("%I:%M %p").lstrip("0")
        except ValueError:
            continue
    return value