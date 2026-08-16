from django import forms
from django.contrib.auth.models import Group
from django.utils.translation import gettext_lazy as _
from eveuniverse.models import EveRegion, EveType

from .models import (
    HAS_FITTINGS,
    ContractDelivery,
    Delivery,
    DiscordMessage,
    Fitting,
    MarketTrackingConfig,
    TrackedContract,
    TrackedItem,
)

EXCLUDED_GROUP_IDS = [6, 1, 14]
EXCLUDED_CATEGORIES = ["Blueprint", "SKINs"]

# ===== Tracked Items =====

class TrackedItemForm(forms.ModelForm):
    class Meta:
        model = TrackedItem
        fields = ["item", "desired_quantity"]
        widgets = {
            "item": forms.Select(attrs={
                "class": "select-search-item hydrating",
                "data-placeholder": "Search item…",
            })
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance and self.instance.pk:
            self.fields["item"].queryset = EveType.objects.filter(
                id=self.instance.item_id, published=True, name__isnull=False
            )
        else:
            self.fields["item"].queryset = EveType.objects.none()
            if self.is_bound:
                raw = (
                    self.data.get(self.add_prefix("item"))
                    or self.data.get("item")
                )
                try:
                    tid = int(raw)
                except (TypeError, ValueError):
                    tid = None

                if tid:
                    self.fields["item"].queryset = EveType.objects.filter(
                        id=tid, published=True, name__isnull=False
                    )

        self.fields["desired_quantity"].widget.attrs.update({"min": 0, "placeholder": "0"})



# ===== Market Tracking Config (region / structure + thresholds) =====

class MarketTrackingConfigForm(forms.ModelForm):
    LOCATION_TYPE_CHOICES = [("region", "Region"), ("structure", "Structure")]

    location_type = forms.ChoiceField(
        choices=LOCATION_TYPE_CHOICES,
        label=_("Location Type"),
        initial="region",
        help_text=_("Select whether to track a region or a specific structure."),
    )
    region = forms.ModelChoiceField(
        queryset=EveRegion.objects.all(),
        required=False,
        label=_("Region"),
        help_text=_("Select region if tracking entire region."),
    )
    structure_id = forms.CharField(
        required=False,
        label=_("Structure ID"),
        help_text=_("Enter structure ID if tracking a specific structure."),
    )

    class Meta:
        model = MarketTrackingConfig
        fields = [
            "location_type",
            "region",
            "structure_id",
            "yellow_threshold",
            "red_threshold",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # pre-fill current state from instance
        if self.instance and self.instance.pk:
            if self.instance.scope == "region":
                self.initial["location_type"] = "region"
                try:
                    self.initial["region"] = EveRegion.objects.get(id=self.instance.location_id)
                except EveRegion.DoesNotExist:
                    pass
            else:
                self.initial["location_type"] = "structure"
                self.initial["structure_id"] = str(self.instance.location_id)

    def clean(self):
        cleaned = super().clean()
        loc_type = cleaned.get("location_type")
        reg = cleaned.get("region")
        struct = cleaned.get("structure_id")

        if loc_type == "region":
            if not reg:
                raise forms.ValidationError(_("You must select a region."))
            cleaned["location_id"] = reg.id
            cleaned["structure_id"] = "1"
        else:
            try:
                cleaned["location_id"] = int(struct)
            except (ValueError, TypeError) as err:
                raise forms.ValidationError(_("Structure ID must be a number.")) from err


        return cleaned

    def save(self, commit=True):
        inst = super().save(commit=False)
        data = self.cleaned_data
        inst.scope = data["location_type"]
        inst.location_id = data["location_id"]
        if commit:
            inst.save()
        return inst


# ===== Deliveries =====

class DeliveryQuantityForm(forms.ModelForm):
    class Meta:
        model = Delivery
        fields = ["declared_quantity"]
        widgets = {"declared_quantity": forms.NumberInput(attrs={"class": "form-control"})}


class ContractDeliveryQuantityForm(forms.ModelForm):
    class Meta:
        model = ContractDelivery
        fields = ["declared_quantity"]
        widgets = {"declared_quantity": forms.NumberInput(attrs={"class": "form-control"})}


# ===== Tracked Contracts =====

class TrackedContractForm(forms.ModelForm):
    class Meta:
        model = TrackedContract
        fields = ["mode", "title_filter", "fitting", "max_price", "desired_quantity"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["mode"].label = _("Tracking type")
        self.fields["title_filter"].label = _("Title contains")
        self.fields["fitting"].label = _("Doctrine fitting")
        self.fields["max_price"].label = _("Max price (ISK)")
        self.fields["desired_quantity"].label = _("Desired contracts")

        if HAS_FITTINGS:
            self.fields["fitting"].widget = forms.Select(attrs={"class": "select-search-fitting"})
            self.fields["fitting"].queryset = Fitting.objects.all().order_by("name")
        else:
            self.fields["fitting"].widget = forms.Select(attrs={"disabled": "disabled"})
            self.fields["fitting"].help_text = _("Fittings app not installed.")

    def clean(self):
        cleaned = super().clean()
        mode = cleaned.get("mode")
        title = (cleaned.get("title_filter") or "").strip()
        fit = cleaned.get("fitting")

        if mode == TrackedContract.Mode.CUSTOM:
            if not title:
                raise forms.ValidationError(_("For custom tracking please provide 'Title contains'."))

        if mode == TrackedContract.Mode.DOCTRINE:
            if not HAS_FITTINGS:
                raise forms.ValidationError(_("Fittings app required for doctrine tracking."))
            if not fit:
                raise forms.ValidationError(_("Please select a doctrine fitting."))
        return cleaned


# ===== Discord Messages =====

class DiscordMessageForm(forms.ModelForm):
    PING_CHOICES_BASE = [("none", "None"), ("here", "@here"), ("everyone", "@everyone")]

    item_ping_target = forms.ChoiceField(label=_("Items ping target"), required=False)
    contract_ping_target = forms.ChoiceField(label=_("Contracts ping target"), required=False)
    item_restocked_ping_target = forms.ChoiceField(label=_("Item restocked ping target"), required=False)

    class Meta:
        model = DiscordMessage
        fields = [
            "item_alert_header",
            "contract_alert_header",
            "item_restocked_alert_header",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        groups = [(f"group:{g.pk}", g.name) for g in Group.objects.all().order_by("name")]
        self.fields["item_ping_target"].choices = self.PING_CHOICES_BASE + groups
        self.fields["contract_ping_target"].choices = self.PING_CHOICES_BASE + groups
        self.fields["item_restocked_ping_target"].choices = self.PING_CHOICES_BASE + groups


        if self.instance:

            if self.instance.item_ping_group:
                self.initial["item_ping_target"] = f"group:{self.instance.item_ping_group.pk}"
            else:
                self.initial["item_ping_target"] = self.instance.item_ping_choice or "none"

            if self.instance.contract_ping_group:
                self.initial["contract_ping_target"] = f"group:{self.instance.contract_ping_group.pk}"
            else:
                self.initial["contract_ping_target"] = self.instance.contract_ping_choice or "none"

            if self.instance.item_restocked_ping_group:
                self.initial["item_restocked_ping_target"] = f"group:{self.instance.item_restocked_ping_group.pk}"
            else:
                self.initial["item_restocked_ping_target"] = self.instance.item_restocked_ping_choice or "none"

    def save(self, commit=True):
        inst = super().save(commit=False)

        val_i = self.cleaned_data.get("item_ping_target") or "none"
        if val_i.startswith("group:"):
            inst.item_ping_group = Group.objects.get(pk=int(val_i.split(":")[1]))
            inst.item_ping_choice = None
        else:
            inst.item_ping_group = None
            inst.item_ping_choice = val_i

        val_c = self.cleaned_data.get("contract_ping_target") or "none"
        if val_c.startswith("group:"):
            inst.contract_ping_group = Group.objects.get(pk=int(val_c.split(":")[1]))
            inst.contract_ping_choice = None
        else:
            inst.contract_ping_group = None
            inst.contract_ping_choice = val_c

        val_ir = self.cleaned_data.get("item_restocked_ping_target") or "none"
        if val_ir.startswith("group:"):
            inst.item_restocked_ping_group = Group.objects.get(pk=int(val_ir.split(":")[1]))
            inst.item_restocked_ping_choice = None
        else:
            inst.item_restocked_ping_group = None
            inst.item_restocked_ping_choice = val_ir

        if commit:
            inst.save()
        return inst
