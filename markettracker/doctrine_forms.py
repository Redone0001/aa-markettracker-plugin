from django import forms
from django.forms import modelformset_factory
from fittings.models import Doctrine, Fitting

from .models import DoctrineFitTarget, TrackedDoctrine


class TrackedDoctrineForm(forms.ModelForm):
    default_quantity = forms.IntegerField(
        min_value=0,
        max_value=1_000_000,
        initial=1,
        label="Default ship target",
        help_text="Newly added doctrine fits inherit this target. Set each existing ship's target below; zero pauses tracking for a ship.",
    )

    class Meta:
        model = TrackedDoctrine
        fields = ["doctrine", "fitting", "default_quantity", "is_shared"]
        labels = {
            "default_quantity": "Default ship target",
            "is_shared": "Share with market members",
        }
        help_texts = {
            "doctrine": "Select a doctrine OR a standalone fit. Newly added doctrine fits inherit the default ship target.",
            "default_quantity": "After saving, set individual ship targets below. A target of zero pauses that ship.",
        }
        widgets = {"fitting": forms.Select(attrs={"class": "select-search-fitting"})}

    def __init__(self, *args, location, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.location = location
        self.fields["doctrine"].queryset = Doctrine.objects.order_by("name")
        fit_id = self.data.get("fitting") if self.is_bound else self.instance.fitting_id
        self.fields["fitting"].queryset = (
            Fitting.objects.filter(pk=fit_id) if str(fit_id).isdigit() else Fitting.objects.none()
        )
        self.fields["doctrine"].widget.attrs["class"] = "form-select"
        self.fields["default_quantity"].widget.attrs["class"] = "form-control w-auto"
        self.fields["is_shared"].widget.attrs["class"] = "form-check-input"

    def clean(self):
        data = super().clean()
        doctrine, fitting = data.get("doctrine"), data.get("fitting")
        if bool(doctrine) == bool(fitting):
            raise forms.ValidationError("Select either a doctrine or a standalone fitting.")
        field, value = ("doctrine", doctrine) if doctrine else ("fitting", fitting)
        if (
            TrackedDoctrine.objects.filter(location=self.instance.location, **{field: value})
            .exclude(pk=self.instance.pk)
            .exists()
        ):
            raise forms.ValidationError("This doctrine/fit is already tracked at this location.")
        return data


class DoctrineFitTargetForm(forms.ModelForm):
    desired_quantity = forms.IntegerField(
        min_value=0,
        max_value=1_000_000,
        label="Ship target",
        widget=forms.NumberInput(attrs={"class": "form-control d-inline-block w-auto"}),
    )

    class Meta:
        model = DoctrineFitTarget
        fields = ["desired_quantity"]


DoctrineFitTargetFormSet = modelformset_factory(
    DoctrineFitTarget, form=DoctrineFitTargetForm, extra=0
)
