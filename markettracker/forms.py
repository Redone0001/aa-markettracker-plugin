from django import forms
from django.contrib.auth.models import Group
from django.utils.translation import gettext_lazy as _
from eveuniverse.models import EveRegion, EveType

from .bulk_import import BulkImportError, import_tracked_items, parse_bulk_item_list
from .models import (
    HAS_FITTINGS,
    ContractDelivery,
    Delivery,
    DiscordMessage,
    Fitting,
    MarketTrackingConfig,
    TrackedContract,
    TrackedItem,
    TrackedItemGroup,
)
from .sde_items import ensure_local_types
from .stat_groups import matching_type_ids, stat_choices

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


class BulkTrackedItemForm(forms.Form):
    items = forms.CharField(
        label=_("Items and quantities"),
        max_length=100_000,
        widget=forms.Textarea(
            attrs={
                "class": "form-control font-monospace",
                "rows": 12,
                "placeholder": _("Machariel x1\nBarrage L x1000"),
                "spellcheck": "false",
            }
        ),
        help_text=_(
            "One item per line. Use 'Item name x2' or separate the quantity "
            "with a tab or at least two spaces. Missing quantities default to 1."
        ),
    )
    multiplier = forms.IntegerField(
        label=_("Multiplier"),
        min_value=1,
        initial=1,
        widget=forms.NumberInput(
            attrs={"class": "form-control", "min": 1, "inputmode": "numeric"}
        ),
    )
    overwrite_amount = forms.BooleanField(
        label=_("Overwrite amount"),
        required=False,
        help_text=_(
            "Replace the desired quantity of items already tracked at this location. "
            "Leave unchecked to keep existing amounts unchanged."
        ),
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def clean_items(self):
        raw_items = self.cleaned_data["items"]
        entries, errors = parse_bulk_item_list(raw_items)
        if errors:
            raise forms.ValidationError(errors)
        self.parsed_entries = entries
        return raw_items

    def import_items(self, location):
        if not self.is_valid():
            raise ValueError("BulkTrackedItemForm must be valid before importing.")
        try:
            return import_tracked_items(
                location=location,
                entries=self.parsed_entries,
                multiplier=self.cleaned_data["multiplier"],
                overwrite_amount=self.cleaned_data["overwrite_amount"],
            )
        except BulkImportError as error:
            self.add_error("items", str(error))
            return None



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


class TrackedItemGroupForm(forms.ModelForm):
    group_mode = forms.ChoiceField(
        choices=[("manual", _("Select items")), ("stats", _("Match item stats"))],
        required=False,
        initial="manual",
        label=_("Group method"),
    )
    matching_stats = forms.MultipleChoiceField(
        required=False,
        label=_("Stats to match"),
        widget=forms.CheckboxSelectMultiple,
        help_text=_(
            "All stats are selected by default. Clear individual stats to ignore them; an empty selection means all stats. Matches stay within the reference item's SDE group. Save again to refresh matches after an SDE update."
        ),
    )

    class Meta:
        model = TrackedItemGroup
        fields = [
            "name",
            "group_mode",
            "stats_reference",
            "matching_stats",
            "items",
            "desired_quantity",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "items": forms.SelectMultiple(
                attrs={
                    "class": "select-search-item",
                    "data-include-tracked": "1",
                    "data-placeholder": "Search and select items…",
                }
            ),
            "desired_quantity": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
            "stats_reference": forms.Select(
                attrs={"class": "select-search-item", "data-include-tracked": "1"}
            ),
        }
        help_texts = {
            "desired_quantity": _(
                "Shared target for the sum of all selected items on the market at this location."
            ),
            "items": _("Select multiple items. Existing individual targets remain independent."),
        }

    def __init__(self, *args, location, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.location = location
        self.fields["items"].required = False
        reference_id = (
            self.data.get("stats_reference") if self.is_bound else self.instance.stats_reference_id
        )
        reference_id = int(reference_id) if str(reference_id).isdigit() else None
        self.fields["stats_reference"].queryset = EveType.objects.filter(
            pk=reference_id, published=True
        )
        self.fields["matching_stats"].choices = stat_choices(reference_id) if reference_id else []
        if not self.is_bound and reference_id:
            self.initial["group_mode"] = "stats"
            self.initial["matching_stats"] = self.instance.matching_stats or [
                key for key, _ in self.fields["matching_stats"].choices
            ]
        ids = self.instance.items.values_list("pk", flat=True) if self.instance.pk else []
        if self.is_bound:
            ids = [
                int(value)
                for value in self.data.getlist(self.add_prefix("items"))
                if str(value).isdigit()
            ]
        self.fields["items"].queryset = EveType.objects.filter(
            pk__in=ids, published=True, name__isnull=False
        )

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if (
            TrackedItemGroup.objects.filter(location=self.instance.location, name=name)
            .exclude(pk=self.instance.pk)
            .exists()
        ):
            raise forms.ValidationError(
                _("A group with this name already exists at this location.")
            )
        return name

    def clean(self):
        data = super().clean()
        if data.get("group_mode") == "stats":
            reference = data.get("stats_reference")
            if reference is None:
                self.add_error("stats_reference", _("Select a reference item."))
            else:
                try:
                    selected = data.get("matching_stats", [])
                    if set(selected) == {key for key, _ in self.fields["matching_stats"].choices}:
                        # Keep "all" dynamic when the SDE adds reference stats.
                        data["matching_stats"] = []
                    self.matched_ids = matching_type_ids(
                        reference.pk, data.get("matching_stats", [])
                    )
                except ValueError as error:
                    self.add_error("stats_reference", str(error))
        elif not data.get("items"):
            self.add_error("items", _("Select at least one item."))
        return data

    def save(self, commit=True):
        if not commit:
            raise ValueError("Item groups must be saved with commit=True.")
        if self.cleaned_data.get("group_mode") == "stats":
            ensure_local_types(self.matched_ids)
            self.cleaned_data["items"] = EveType.objects.filter(pk__in=self.matched_ids)
        else:
            self.instance.stats_reference = None
            self.instance.matching_stats = []
        return super().save(commit=True)


class MarkupFilterForm(forms.Form):
    markup_min = forms.DecimalField(
        label=_("Minimum markup (%)"),
        required=False,
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"class": "form-control form-control-sm", "step": "0.01"}),
    )
    markup_max = forms.DecimalField(
        label=_("Maximum markup (%)"),
        required=False,
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"class": "form-control form-control-sm", "step": "0.01"}),
    )

    def clean(self):
        data = super().clean()
        minimum, maximum = data.get("markup_min"), data.get("markup_max")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise forms.ValidationError(_("Minimum markup must not exceed maximum markup."))
        return data
