from django.contrib import admin

from .models import Shelter


@admin.register(Shelter)
class ShelterAdmin(admin.ModelAdmin):
    list_display = ("name", "shelter_type", "latitude", "longitude", "source", "verified", "created_at")
    list_filter = ("shelter_type", "source", "verified")
    search_fields = ("name",)
    list_editable = ("verified",)
