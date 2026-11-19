from django import forms
from django.contrib import admin, messages

from .models import ConflictRegion, DamageDataset, DamageFeature, Shelter


@admin.register(ConflictRegion)
class ConflictRegionAdmin(admin.ModelAdmin):
    list_display = ("name", "reference_date", "west", "south", "east", "north", "active")
    list_editable = ("active",)


@admin.register(Shelter)
class ShelterAdmin(admin.ModelAdmin):
    list_display = ("name", "shelter_type", "latitude", "longitude", "source", "verified", "created_at")
    list_filter = ("shelter_type", "source", "verified")
    search_fields = ("name",)
    list_editable = ("verified",)


class DamageDatasetForm(forms.ModelForm):
    data_file = forms.FileField(
        required=False,
        help_text=(
            "Zipped shapefile, GeoJSON, GeoPackage, KML or zipped File Geodatabase "
            "(e.g. a UNOSAT damage assessment from data.humdata.org). Required when "
            "adding a dataset; uploading again replaces its features."
        ),
    )

    grade_if_missing = forms.ChoiceField(
        required=False,
        choices=[("", "— file has a damage column —"), ("destroyed", "Destroyed"),
                 ("damaged", "Damaged"), ("possibly_damaged", "Possibly damaged")],
        help_text="Only for files without a damage column (e.g. UNOSAT El Fasher, where every polygon is destroyed).",
    )

    class Meta:
        model = DamageDataset
        fields = ("name", "source", "imagery_date", "attribution", "license", "source_url", "active",
                  "data_file", "grade_if_missing")

    def clean(self):
        cleaned = super().clean()
        if not self.instance.pk and not cleaned.get("data_file"):
            self.add_error("data_file", "Upload the damage file for a new dataset.")
        return cleaned


@admin.register(DamageDataset)
class DamageDatasetAdmin(admin.ModelAdmin):
    form = DamageDatasetForm
    list_display = ("name", "source", "imagery_date", "feature_count", "active", "created_at")
    list_filter = ("source", "active")
    search_fields = ("name",)
    readonly_fields = ("feature_count", "west", "south", "east", "north", "created_at")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        uploaded = form.cleaned_data.get("data_file")
        if not uploaded:
            return
        from official_damage import import_uploaded_file

        try:
            summary = import_uploaded_file(uploaded, obj, form.cleaned_data.get("grade_if_missing") or None)
        except Exception as exc:
            messages.error(request, f"Could not import '{uploaded.name}': {exc}")
            return
        counts = ", ".join(f"{n} {k}" for k, n in sorted(summary["counts"].items())) or "no damaged features"
        messages.success(
            request,
            f"Imported {summary['analysed']} assessed features ({counts}) using "
            f"column {summary['grade_column']}; {summary['not_analysed_or_unknown']} "
            "rows were not analysed or had an unrecognised grade.",
        )


@admin.register(DamageFeature)
class DamageFeatureAdmin(admin.ModelAdmin):
    list_display = ("dataset", "kind", "grade", "label")
    list_filter = ("dataset", "kind", "grade")
