from django import forms
from django.contrib import admin, messages
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from .models import (
    DiscordMessage,
    DiscordWebhook,
    MarketTrackingConfig,
    TrackedItem,
    TrackedLocation,
)
from .security import redact_discord_webhook_url
from .tracked_item_moves import move_tracked_items

# ========= DiscordWebhook =========

@admin.register(DiscordWebhook)
class DiscordWebhookAdmin(admin.ModelAdmin):
    list_display = ("name", "webhook_endpoint")
    search_fields = ("name",)
    list_per_page = 25

    @admin.display(description=_("Webhook endpoint"))
    def webhook_endpoint(self, obj):
        return redact_discord_webhook_url(obj.url)


# ========= DiscordMessage =========

PING_BASE_CHOICES = [
    ("none", "@none"),
    ("here", "@here"),
    ("everyone", "@everyone"),
]

def build_ping_choices():
    choices = list(PING_BASE_CHOICES)
    for g in Group.objects.all().order_by("name"):
        choices.append((f"group:{g.pk}", f"{g.name}"))
    return choices

class DiscordMessageForm(forms.ModelForm):
    item_ping_target = forms.ChoiceField(label=_("Item ping target"), required=False)
    contract_ping_target = forms.ChoiceField(label=_("Contract ping target"), required=False)
    item_restocked_ping_target = forms.ChoiceField(label=_("Item restocked ping target"), required=False)
    contract_restocked_ping_target = forms.ChoiceField(label=_("Contract restocked ping target"), required=False)

    class Meta:
        model = DiscordMessage
        fields = (
            "item_alert_header",
            "contract_alert_header",
            "item_restocked_alert_header",
            "contract_restocked_alert_header",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        choices = build_ping_choices()
        self.fields["item_ping_target"].choices = choices
        self.fields["contract_ping_target"].choices = choices
        self.fields["item_restocked_ping_target"].choices = choices
        self.fields["contract_restocked_ping_target"].choices = choices

        # initial for Items
        inst = self.instance
        if inst and inst.pk:
            if inst.item_ping_choice:
                self.fields["item_ping_target"].initial = inst.item_ping_choice
            elif inst.item_ping_group_id:
                self.fields["item_ping_target"].initial = f"group:{inst.item_ping_group_id}"
            else:
                self.fields["item_ping_target"].initial = "none"

            # initial for Contracts
            if inst.contract_ping_choice:
                self.fields["contract_ping_target"].initial = inst.contract_ping_choice
            elif inst.contract_ping_group_id:
                self.fields["contract_ping_target"].initial = f"group:{inst.contract_ping_group_id}"
            else:
                self.fields["contract_ping_target"].initial = "none"

            if inst.item_restocked_ping_choice:
                self.fields["item_restocked_ping_target"].initial = inst.item_restocked_ping_choice
            elif inst.item_restocked_ping_group_id:
                self.fields["item_restocked_ping_target"].initial = f"group:{inst.item_restocked_ping_group_id}"
            else:
                self.fields["item_restocked_ping_target"].initial = "none"

            if inst.contract_restocked_ping_choice:
                self.fields["contract_restocked_ping_target"].initial = inst.contract_restocked_ping_choice
            elif inst.contract_restocked_ping_group_id:
                self.fields["contract_restocked_ping_target"].initial = f"group:{inst.contract_restocked_ping_group_id}"
            else:
                self.fields["contract_restocked_ping_target"].initial = "none"


        else:
            self.fields["item_ping_target"].initial = "none"
            self.fields["contract_ping_target"].initial = "none"
            self.fields["item_restocked_ping_target"].initial = "none"
            self.fields["contract_restocked_ping_target"].initial = "none"



    def clean(self):
        cleaned = super().clean()

        # split item target
        item_target = cleaned.get("item_ping_target") or "none"
        if item_target.startswith("group:"):
            cleaned["item_ping_choice"] = None
            try:
                gid = int(item_target.split(":", 1)[1])
            except Exception:
                gid = None
            cleaned["item_ping_group"] = Group.objects.filter(pk=gid).first()
        else:
            # none/here/everyone
            cleaned["item_ping_choice"] = item_target
            cleaned["item_ping_group"] = None

        # split contract target
        contract_target = cleaned.get("contract_ping_target") or "none"
        if contract_target.startswith("group:"):
            cleaned["contract_ping_choice"] = None
            try:
                gid = int(contract_target.split(":", 1)[1])
            except Exception:
                gid = None
            cleaned["contract_ping_group"] = Group.objects.filter(pk=gid).first()
        else:
            cleaned["contract_ping_choice"] = contract_target
            cleaned["contract_ping_group"] = None

        item_restocked_target = cleaned.get("item_restocked_ping_target") or "none"
        if item_restocked_target.startswith("group:"):
            cleaned["item_restocked_ping_choice"] = None
            try:
                gid = int(item_restocked_target.split(":", 1)[1])
            except Exception:
                gid = None
            cleaned["item_restocked_ping_group"] = Group.objects.filter(pk=gid).first()
        else:
            cleaned["item_restocked_ping_choice"] = item_restocked_target
            cleaned["item_restocked_ping_group"] = None

        contract_restocked_target = cleaned.get("contract_restocked_ping_target") or "none"
        if contract_restocked_target.startswith("group:"):
            cleaned["contract_restocked_ping_choice"] = None
            try:
                gid = int(contract_restocked_target.split(":", 1)[1])
            except Exception:
                gid = None
            cleaned["contract_restocked_ping_group"] = Group.objects.filter(pk=gid).first()
        else:
            cleaned["contract_restocked_ping_choice"] = contract_restocked_target
            cleaned["contract_restocked_ping_group"] = None

        return cleaned

    def save(self, commit=True):
        inst = super().save(commit=False)
        # assign the already split values from clean()
        inst.item_ping_choice = self.cleaned_data.get("item_ping_choice")
        inst.item_ping_group = self.cleaned_data.get("item_ping_group")
        inst.contract_ping_choice = self.cleaned_data.get("contract_ping_choice")
        inst.contract_ping_group = self.cleaned_data.get("contract_ping_group")
        inst.item_restocked_ping_choice = self.cleaned_data.get("item_restocked_ping_choice")
        inst.item_restocked_ping_group = self.cleaned_data.get("item_restocked_ping_group")
        inst.contract_restocked_ping_choice = self.cleaned_data.get("contract_restocked_ping_choice")
        inst.contract_restocked_ping_group = self.cleaned_data.get("contract_restocked_ping_group")

        if commit:
            inst.save()
        return inst


@admin.register(DiscordMessage)
class DiscordMessageAdmin(admin.ModelAdmin):
    form = DiscordMessageForm
    fieldsets = (
        ("Item alerts", {
            "fields": (
                "item_alert_enabled",
                "item_alert_header",
                "item_ping_target",
            )
        }),
        ("Item restocked alerts", {
            "fields": (
                "item_restocked_alert_enabled",
                "item_restocked_alert_header",
                "item_restocked_ping_target",
            )
        }),
        ("Contract alerts", {
            "fields": (
                "contract_alert_enabled",
                "contract_alert_header",
                "contract_ping_target",
            )
        }),
        ("Contract restocked alerts", {
            "fields": (
                "contract_restocked_alert_enabled",
                "contract_restocked_alert_header",
                "contract_restocked_ping_target",
            )
        }),
    )

    def has_add_permission(self, request):
        return False  # block adding

    def has_delete_permission(self, request, obj=None):
        return False  # block deleting

    def changelist_view(self, request, extra_context=None):
        obj = DiscordMessage.objects.first()
        if not obj:
            # create a default record and redirect to its edit view
            obj = DiscordMessage.objects.create(
                item_alert_header="⚠️ MarketTracker Items",
                contract_alert_header="📦 MarketTracker Contracts",
                item_restocked_alert_header="👍 Items back in stock",
                contract_restocked_alert_header="✅ Contracts restocked",
            )

        return HttpResponseRedirect(
            reverse(f"admin:{self.model._meta.app_label}_{self.model._meta.model_name}_change", args=(obj.pk,))
        )


# ========= MarketTrackingConfig =========

@admin.register(MarketTrackingConfig)
class MarketTrackingConfigAdmin(admin.ModelAdmin):
    # scope/location_id chowamy (żeby nikt nie używał starej logiki),
    # ale pól w MODELU na razie nie usuwamy (bezpieczne dla update + migrate).
    list_display = ("yellow_threshold", "red_threshold", "updated_at")
    list_display_links = ("updated_at",)
    list_editable = ("yellow_threshold", "red_threshold")

    fieldsets = (
        (_("Thresholds"), {"fields": ("yellow_threshold", "red_threshold")}),
        (_("Timestamps"), {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )
    readonly_fields = ("created_at", "updated_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = MarketTrackingConfig.objects.first()
        return HttpResponseRedirect(
            reverse(f"admin:{self.model._meta.app_label}_{self.model._meta.model_name}_change", args=(obj.pk,))
        )





@admin.register(TrackedLocation)
class TrackedLocationAdmin(admin.ModelAdmin):
    list_display = ("name", "scope", "location_id", "is_active", "is_default")
    list_filter = ("scope", "is_active", "is_default")
    search_fields = ("name", "location_id")
    list_editable = ("is_active", "is_default")

    def save_model(self, request, obj, form, change):
        # enforce single default
        super().save_model(request, obj, form, change)
        if obj.is_default:
            TrackedLocation.objects.exclude(pk=obj.pk).update(is_default=False)


class MoveTrackedItemsForm(forms.Form):
    target_location = forms.ModelChoiceField(
        queryset=TrackedLocation.objects.none(),
        label=_("Destination location"),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["target_location"].queryset = TrackedLocation.objects.filter(
            is_active=True
        ).order_by("name")


@admin.register(TrackedItem)
class TrackedItemAdmin(admin.ModelAdmin):
    actions = ("move_to_location",)
    list_display = ("item", "location", "desired_quantity", "last_status")
    list_display_links = None
    list_filter = ("location", "last_status")
    list_per_page = 100
    list_select_related = ("item", "location")
    ordering = ("location__name", "item__name")
    search_fields = ("item__name", "location__name")
    show_full_result_count = False

    def has_module_permission(self, request):
        return self.has_move_tracked_items_permission(request)

    def has_view_permission(self, request, obj=None):
        return self.has_move_tracked_items_permission(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_move_tracked_items_permission(self, request):
        return request.user.has_perm("markettracker.can_move_tracked_items")

    @admin.action(
        description=_("Move selected tracked items to another location"),
        permissions=("move_tracked_items",),
    )
    def move_to_location(self, request, queryset):
        if not self.has_move_tracked_items_permission(request):
            raise PermissionDenied

        if "apply" in request.POST:
            form = MoveTrackedItemsForm(request.POST)
            if form.is_valid():
                target_location = form.cleaned_data["target_location"]
                result = move_tracked_items(queryset, target_location)

                moved_items = TrackedItem.objects.filter(
                    pk__in=result.moved_ids
                ).select_related("item", "location")
                for tracked_item in moved_items:
                    self.log_change(
                        request,
                        tracked_item,
                        str(
                            _("Moved tracked item to %(location)s.")
                            % {"location": target_location}
                        ),
                    )

                self.message_user(
                    request,
                    _(
                        "%(moved)d tracked items moved to %(location)s; "
                        "%(snapshots)d old market-order snapshots cleared."
                    )
                    % {
                        "moved": result.moved_count,
                        "location": target_location,
                        "snapshots": result.deleted_snapshots,
                    },
                    level=messages.SUCCESS if result.moved_count else messages.INFO,
                )
                if result.conflict_count:
                    self.message_user(
                        request,
                        _(
                            "%(count)d tracked items were skipped because the destination "
                            "already tracks the same item."
                        )
                        % {"count": result.conflict_count},
                        level=messages.WARNING,
                    )
                if result.unchanged_count:
                    self.message_user(
                        request,
                        _("%(count)d selected items were already at the destination.")
                        % {"count": result.unchanged_count},
                        level=messages.INFO,
                    )
                return None
        else:
            form = MoveTrackedItemsForm()

        context = {
            **self.admin_site.each_context(request),
            "action_checkbox_name": ACTION_CHECKBOX_NAME,
            "form": form,
            "opts": self.model._meta,
            "queryset": queryset,
            "title": _("Move tracked items to another location"),
        }
        return TemplateResponse(
            request,
            "admin/markettracker/trackeditem/move_location.html",
            context,
        )
