from django import template

from markettracker.models import TrackedItem

register = template.Library()

@register.filter
def tracked_item(type_id):
    try:
        return TrackedItem.objects.select_related("item").get(item__id=type_id)
    except TrackedItem.DoesNotExist:
        return None

