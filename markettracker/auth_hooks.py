"""Hook into Alliance Auth"""

# Django
# Alliance Auth
from allianceauth import hooks
from allianceauth.services.hooks import MenuItemHook, UrlHook
from django.utils.translation import gettext_lazy as _

# AA Market Tracker App
from markettracker import urls


class MarketTrackerMenuItem(MenuItemHook):
    """This class ensures only authorized users will see the menu entry"""

    def __init__(self):
        # setup menu entry for sidebar
        MenuItemHook.__init__(
            self,
            _("Market Tracker"),
            "fas fa-cube fa-fw",
            "markettracker:list_items",
            navactive=["markettracker:"],
        )

    def render(self, request):
        """Render the menu item"""

        if request.user.has_perm("markettracker.basic_access"):
            return MenuItemHook.render(self, request)

        return ""


@hooks.register("menu_item_hook")
def register_menu():
    """Register the menu item"""

    return MarketTrackerMenuItem()


@hooks.register("url_hook")
def register_urls():
    """Register app urls"""

    return UrlHook(urls, "markettracker", r"^markettracker/")

@hooks.register("charlink")
def register_charlink_hook():
    return "markettracker.thirdparty.charlink_hook"
