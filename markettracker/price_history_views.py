"""Permission-gated history browsing and explicit recording configuration."""

from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Prefetch
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from .models import (
    DoctrineFitTarget,
    PriceHistoryRecording,
    PriceHistorySettings,
    TrackedDoctrine,
    TrackedItem,
    TrackedItemGroup,
    TrackedLocation,
)
from .price_history import SOURCE_FIELDS, HistoryMarket, purge_expired_history, visible_recordings
from .utils import get_selected_location, location_display_name

SECTIONS = {"doctrines": "fit", "items": "item", "groups": "group"}


class PriceHistorySettingsForm(forms.ModelForm):
    class Meta:
        model = PriceHistorySettings
        fields = ("interval_hours", "retention_days")
        labels = {
            "interval_hours": _("Sample at most once every"),
            "retention_days": _("Keep history for"),
        }
        widgets = {
            "interval_hours": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "retention_days": forms.Select(attrs={"class": "form-select form-select-sm"}),
        }


def _sources(location, include_private):
    fits = (
        DoctrineFitTarget.objects.filter(tracking__location=location)
        .select_related(
            "tracking__doctrine", "tracking__fitting", "fitting__ship_type", "price_recording"
        )
        .prefetch_related("fitting__items__type_fk")
        .order_by("fitting__name", "pk")
    )
    if not include_private:
        fits = fits.filter(tracking__is_shared=True)
    return {
        "fit": fits,
        "item": TrackedItem.objects.filter(location=location)
        .select_related("item", "price_recording")
        .order_by("item__name", "pk"),
        "group": TrackedItemGroup.objects.filter(location=location)
        .select_related("price_recording")
        .prefetch_related("items")
        .order_by("name", "pk"),
    }


def _url(location, section, *, source_id=None, days=7, page=1):
    query = {"loc": location.pk, "section": section, "days": days, "page": page}
    if source_id is not None:
        query["record"] = source_id
    return reverse("markettracker:price_history") + "?" + urlencode(query)


def _integer(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@login_required
@permission_required("markettracker.basic_access", raise_exception=True)
@require_http_methods(["GET", "POST"])
def price_history(request):
    can_manage = request.user.has_perm("markettracker.can_manage_price_history")
    if not (can_manage or request.user.has_perm("markettracker.can_view_price_history")):
        raise PermissionDenied
    if request.method == "POST" and not can_manage:
        raise PermissionDenied
    location = get_selected_location(request)
    if location is None:
        messages.error(request, _("Configure a market location first."))
        return redirect("markettracker:list_items")
    section = request.GET.get("section", "doctrines")
    if section not in SECTIONS:
        section = "doctrines"
    kind = SECTIONS[section]
    days = _integer(request.GET.get("days"), 7)
    if days not in (7, 30, 90):
        days = 7
    include_private = request.user.has_perm("markettracker.can_manage_stocks")
    sources = _sources(location, include_private)
    config = PriceHistorySettings.objects.filter(location=location).first()
    config = config or PriceHistorySettings(location=location)
    interval_hours, retention_days = config.interval_hours, config.retention_days
    settings_form = PriceHistorySettingsForm(instance=config)
    settings_open = False
    status = 200

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "settings":
            settings_form = PriceHistorySettingsForm(request.POST, instance=config)
            if settings_form.is_valid():
                with transaction.atomic():
                    locked, _created = (
                        PriceHistorySettings.objects.select_for_update().get_or_create(
                            location=location
                        )
                    )
                    for field, value in settings_form.cleaned_data.items():
                        setattr(locked, field, value)
                    locked.save(update_fields=["interval_hours", "retention_days"])
                    purge_expired_history(location_id=location.pk)
                messages.success(request, _("Recording settings saved."))
                return redirect(
                    _url(
                        location,
                        section,
                        days=days,
                        source_id=_integer(request.GET.get("record")),
                        page=_integer(request.GET.get("page"), 1),
                    )
                )
            settings_open, status = True, 400
        elif action == "toggle":
            source_id = _integer(request.POST.get("source_id"))
            enabled = request.POST.get("enabled", "0")
            if source_id is None or enabled not in ("0", "1"):
                return HttpResponseBadRequest("Invalid recording selection.")
            source = get_object_or_404(sources[kind], pk=source_id)
            with transaction.atomic():
                PriceHistorySettings.objects.select_for_update().get_or_create(location=location)
                PriceHistoryRecording.objects.update_or_create(
                    **{SOURCE_FIELDS[kind]: source},
                    defaults={"location": location, "is_enabled": enabled == "1"},
                )
            messages.success(
                request,
                _("Recording enabled. Sampling starts with the next successful market refresh.")
                if enabled == "1"
                else _("Recording paused. Existing history remains until retention expires."),
            )
            return redirect(
                _url(
                    location,
                    section,
                    source_id=source.pk,
                    days=days,
                    page=_integer(request.GET.get("page"), 1),
                )
            )
        else:
            return HttpResponseBadRequest("Unknown price history action.")

    plans = TrackedDoctrine.objects.filter(location=location)
    if not include_private:
        plans = plans.filter(is_shared=True)
    counts = {
        "doctrines": plans.count(),
        "items": sources["item"].count(),
        "groups": sources["group"].count(),
    }
    if kind == "fit":
        listed = (
            plans.select_related("doctrine", "fitting")
            .prefetch_related(Prefetch("fit_targets", queryset=sources["fit"]))
            .order_by("doctrine__name", "fitting__name", "pk")
        )
    else:
        listed = sources[kind]
    page = Paginator(listed, 10 if kind == "fit" else 50).get_page(request.GET.get("page"))
    market = HistoryMarket(location.pk)
    groups, rows = [], []

    def row(source):
        recording = getattr(source, "price_recording", None)
        if recording and recording.location_id != location.pk:
            recording = None
        name = (
            source.fitting.name
            if kind == "fit"
            else source.item.name
            if kind == "item"
            else source.name
        )
        subtitle = (
            source.fitting.ship_type.name
            if kind == "fit"
            else _("Individual item")
            if kind == "item"
            else _("%(count)s interchangeable items") % {"count": len(source.items.all())}
        )
        return {
            "source": source,
            "name": name,
            "subtitle": subtitle,
            "recording": recording,
            "is_enabled": bool(recording and recording.is_enabled),
            "url": _url(location, section, source_id=source.pk, days=days, page=page.number),
            **market.quote(kind, source),
        }

    for entry in page:
        if kind == "fit":
            fit_rows = [row(target) for target in entry.fit_targets.all()]
            groups.append({"plan": entry, "name": str(entry), "rows": fit_rows})
            rows.extend(fit_rows)
        else:
            rows.append(row(entry))
    selected_id = _integer(request.GET.get("record"))
    selected = next((entry for entry in rows if entry["source"].pk == selected_id), None)
    if "record" in request.GET and selected is None:
        if selected_id is None:
            raise Http404
        selected = row(get_object_or_404(sources[kind], pk=selected_id))
    selected = selected or (rows[0] if rows else None)
    for entry in rows:
        entry["is_selected"] = bool(selected and entry["source"].pk == selected["source"].pk)
    for group in groups:
        group["is_open"] = any(entry["is_selected"] for entry in group["rows"])
    samples = []
    if selected and selected["recording"]:
        samples = list(
            selected["recording"]
            .samples.filter(observed_at__gte=timezone.now() - timedelta(days=days))
            .order_by("observed_at")
        )
    prices = [sample.price for sample in samples if sample.price is not None]
    average_lowest_price = sum(prices, Decimal(0)) / len(prices) if prices else None
    recording_qs = visible_recordings(location.pk, include_private=include_private).filter(
        is_enabled=True
    )
    enabled_count = recording_qs.filter(**{SOURCE_FIELDS[kind] + "__isnull": False}).count()
    total_enabled = recording_qs.count()
    locations = list(TrackedLocation.objects.filter(is_active=True).order_by("-is_default", "name"))
    for entry in locations:
        entry.display_name = location_display_name(entry)
    return render(
        request,
        "markettracker/price_history.html",
        {
            "selected_location": location,
            "can_manage_price_history": can_manage,
            "location_title": location_display_name(location),
            "locations": locations,
            "section": section,
            "kind": kind,
            "days": days,
            "groups": groups,
            "rows": rows,
            "page_obj": page,
            "selected": selected,
            "average_lowest_price": average_lowest_price,
            "settings_form": settings_form,
            "settings_open": settings_open,
            "enabled_count": enabled_count,
            "total_enabled": total_enabled,
            "sample_ceiling": total_enabled * (24 // interval_hours) * retention_days,
            "tabs": [
                {
                    "key": key,
                    "label": label,
                    "icon": icon,
                    "count": counts[key],
                    "url": _url(location, key, days=days),
                    "active": key == section,
                }
                for key, label, icon in (
                    ("doctrines", _("Doctrines"), "fa-folder-open"),
                    ("items", _("Items"), "fa-cube"),
                    ("groups", _("Item groups"), "fa-layer-group"),
                )
            ],
            "periods": [
                {
                    "days": value,
                    "url": _url(
                        location,
                        section,
                        days=value,
                        source_id=selected["source"].pk if selected else None,
                        page=page.number,
                    ),
                }
                for value in (7, 30, 90)
            ],
            "history_data": [
                {
                    "time": timezone.localtime(sample.observed_at).isoformat(),
                    "price": str(sample.price) if sample.price is not None else None,
                    "volume": sample.volume,
                }
                for sample in samples
            ],
            "samples": list(reversed(samples[-24:])),
            "sample_count": len(samples),
        },
        status=status,
    )
