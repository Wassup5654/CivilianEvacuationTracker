from django.db import models


class Shelter(models.Model):
    """A known shelter, bunker, or assembly point for civilian evacuation."""

    name = models.CharField(max_length=200)
    latitude = models.FloatField()
    longitude = models.FloatField()
    shelter_type = models.CharField(
        max_length=100,
        blank=True,
        help_text="E.g. bunker, assembly_point, shelter",
    )
    source = models.CharField(
        max_length=50,
        default="manual",
        help_text="'osm' for OpenStreetMap imports, 'manual' for admin-entered",
    )
    verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.shelter_type or 'shelter'})"
