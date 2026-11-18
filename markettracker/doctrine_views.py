from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.db import transaction
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from eveuniverse.models import EveType

from .doctrine_forms import DoctrineFitTargetFormSet, TrackedDoctrineForm
from .doctrines import (
    doctrine_stock,
    location_groups,
    multibuy,
    sell_orders,
    sync_doctrine_tracking,
    tracking_queryset,
)
from .models import DoctrineFitTarget, TrackedDoctrine, TrackedLocation
from .stat_groups import stat_choices
from .stock_management import management_context, management_return_url
from .utils import get_selected_location, location_display_name


def _context(location):
    locations = list(TrackedLocation.objects.filter(is_active=True).order_by("-is_default", "name"))
    for entry in locations:
        entry.display_name = location_display_name(entry)
    return {
        "selected_location": location,
        "location_title": location_display_name(location),
        "locations": locations,
    }


@login_required
@permission_required("markettracker.basic_access", raise_exception=True)
def doctrine_list(request):
    location = get_selected_location(request)
    if not location:
        messages.error(request, "Configure a market location first.")
        return redirect("markettracker:list_items")
    plans = tracking_queryset(location.pk)
    if not request.user.has_perm("markettracker.can_manage_stocks"):
        plans = plans.filter(is_shared=True)
    groups, orders = location_groups(location.pk), sell_orders(location.pk)
    return render(
        request,
        "markettracker/doctrines.html",
        {
            **_context(location),
            "doctrines": [doctrine_stock(plan, groups, orders) for plan in plans],
        },
    )


@login_required
@permission_required("markettracker.can_manage_stocks", raise_exception=True)
def manage_doctrines(request):
    location = get_selected_location(request)
    if not location:
        messages.error(request, "Configure a market location first.")
        return redirect("markettracker:manage_stock")
    plan_id = request.POST.get("plan_id") if request.method == "POST" else request.GET.get("edit")
    plan = get_object_or_404(TrackedDoctrine, pk=plan_id, location=location) if plan_id else None
    form = TrackedDoctrineForm(instance=plan, location=location)
    targets = (
        DoctrineFitTarget.objects.filter(tracking=plan)
        .select_related("fitting__ship_type")
        .order_by("fitting__name")
        if plan
        else DoctrineFitTarget.objects.none()
    )
    formset = DoctrineFitTargetFormSet(queryset=targets)

    def render_page():
        page = Paginator(tracking_queryset(location.pk).order_by("pk"), 50).get_page(
            request.GET.get("page")
        )
        return render(
            request,
            "markettracker/manage_doctrines.html",
            {
                **management_context(request, location, "doctrines"),
                "tracking_form": form,
                "target_formset": formset,
                "editing": plan,
                "show_tracking_form": bool(
                    plan
                    or "add" in request.GET
                    or "save" in request.POST
                    or "save_targets" in request.POST
                ),
                "tracked_doctrines": page.object_list,
                "page_obj": page,
            },
        )

    original_plan_id = plan.pk if plan else None
    if request.method == "POST":
        try:
            with transaction.atomic():
                if "save_settings" in request.POST:
                    location.remove_unused_doctrine_items = (
                        request.POST.get("remove_unused_doctrine_items") == "on"
                    )
                    location.save(update_fields=["remove_unused_doctrine_items"])
                    sync_doctrine_tracking(location.pk)
                elif "delete" in request.POST and plan:
                    plan.delete()
                    sync_doctrine_tracking(location.pk)
                elif "save_targets" in request.POST and plan:
                    formset = DoctrineFitTargetFormSet(request.POST, queryset=targets)
                    if formset.is_valid():
                        # ModelChoiceField ids must belong to this location/plan.
                        allowed = set(targets.values_list("pk", flat=True))
                        if any(
                            entry.cleaned_data.get("id") is None
                            or entry.cleaned_data["id"].pk not in allowed
                            for entry in formset
                        ):
                            return HttpResponseBadRequest("Invalid ship target.")
                        formset.save()
                        sync_doctrine_tracking(location.pk)
                        messages.success(request, "Ship targets saved.")
                        return redirect(
                            f"{management_return_url(request, location, 'doctrines')}&edit={plan.pk}"
                        )
                    return render_page()
                elif "save" in request.POST:
                    form = TrackedDoctrineForm(request.POST, instance=plan, location=location)
                    if form.is_valid():
                        plan = form.save()
                        sync_doctrine_tracking(location.pk)
                        messages.success(
                            request, "Doctrine tracking saved. Set individual ship targets below."
                        )
                        return redirect(
                            f"{management_return_url(request, location, 'doctrines')}&edit={plan.pk}"
                        )
                elif "sync" in request.POST:
                    sync_doctrine_tracking(location.pk)
                else:
                    return HttpResponseBadRequest("Unknown action.")
            if not form.errors:
                messages.success(request, "Doctrine tracking updated.")
                return redirect(management_return_url(request, location, "doctrines"))
        except ValueError as error:
            messages.error(request, str(error))
            # A failed sync rolls back the plan/settings too. Keep the form
            # pointed at the original row, never an id from a rolled-back insert.
            location.refresh_from_db()
            plan = TrackedDoctrine.objects.filter(pk=original_plan_id).first()
            form = TrackedDoctrineForm(
                request.POST if "save" in request.POST else None,
                instance=plan,
                location=location,
            )
    return render_page()


@login_required
@permission_required("markettracker.basic_access", raise_exception=True)
@require_POST
def doctrine_multibuy(request, target_id):
    location = get_selected_location(request)
    targets = (
        DoctrineFitTarget.objects.filter(tracking__location=location)
        .select_related("tracking", "fitting__ship_type")
        .prefetch_related("fitting__items__type_fk")
    )
    if not request.user.has_perm("markettracker.can_manage_stocks"):
        targets = targets.filter(tracking__is_shared=True)
    target = get_object_or_404(targets, pk=target_id)
    mode = request.POST.get("mode", "all")
    try:
        amount = int(request.POST.get("amount", 1))
    except (ValueError, TypeError):
        return HttpResponseBadRequest("Invalid quantity.")
    if (
        mode not in {"all", "missing"}
        or not 1 <= amount <= 1_000_000
        or target.desired_quantity == 0
    ):
        return HttpResponseBadRequest("Invalid purchase request.")
    quantities, unpriced = multibuy(
        target,
        location_groups(location.pk),
        sell_orders(location.pk),
        missing=mode == "missing",
        amount=amount,
    )
    names = dict(EveType.objects.filter(pk__in=quantities).values_list("pk", "name"))
    if set(quantities) - names.keys():
        return JsonResponse(
            {"error": "Sync doctrine tracking before copying this fit."}, status=409
        )
    lines = [
        f"{names[type_id]}\t{quantity}"
        for type_id, quantity in sorted(quantities.items(), key=lambda pair: names[pair[0]])
        if quantity
    ]
    return JsonResponse({"text": "\n".join(lines), "unpriced": unpriced})


@login_required
@permission_required("markettracker.can_manage_stocks", raise_exception=True)
def item_stat_choices(request):
    reference_id = request.GET.get("reference", "")
    if not reference_id.isdigit():
        return HttpResponseBadRequest("Invalid reference item.")
    return JsonResponse(
        {"stats": [{"id": key, "name": name} for key, name in stat_choices(int(reference_id))]}
    )
