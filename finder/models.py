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


class DamageDataset(models.Model):
    """An official damage assessment imported from a file (e.g. UNOSAT via HDX).

    Copernicus EMS data is fetched live and is not stored here.
    """

    SOURCE_CHOICES = [
        ("unosat", "UNOSAT"),
        ("copernicus", "Copernicus EMS"),
        ("other", "Other"),
    ]

    name = models.CharField(max_length=200)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="unosat")
    imagery_date = models.DateField(
        null=True, blank=True, help_text="Date of the satellite imagery the assessment is based on."
    )
    attribution = models.CharField(
        max_length=300, blank=True,
        help_text="Credit line shown on the map, e.g. 'UNOSAT / UNITAR, CC BY-SA'.",
    )
    license = models.CharField(max_length=100, blank=True, help_text="E.g. CC BY-SA, CC BY-IGO.")
    source_url = models.URLField(blank=True)
    active = models.BooleanField(default=True, help_text="Inactive datasets are ignored by the map.")
    # Extent of the analysed area (all graded features, including 'no visible damage').
    west = models.FloatField(default=0)
    south = models.FloatField(default=0)
    east = models.FloatField(default=0)
    north = models.FloatField(default=0)
    feature_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-imagery_date", "name"]

    def __str__(self):
        return f"{self.name} ({self.get_source_display()})"


class ConflictRegion(models.Model):
    """A war zone where the Sentinel-1 radar damage estimate runs.

    The estimate compares the 12 months before `reference_date` with the most
    recent 3 months. When a zone falls inside several regions' boxes, the
    smallest box wins.
    """

    name = models.CharField(max_length=100)
    reference_date = models.DateField(help_text="Start of the war; the year before it is the radar baseline.")
    west = models.FloatField()
    south = models.FloatField()
    east = models.FloatField()
    north = models.FloatField()
    active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} (since {self.reference_date})"

    @property
    def box_area(self) -> float:
        return (self.east - self.west) * (self.north - self.south)


class DamageFeature(models.Model):
    """One damaged road, building or assessment area from a DamageDataset."""

    KIND_CHOICES = [("road", "Road"), ("building", "Building"), ("area", "Area")]
    GRADE_CHOICES = [
        ("destroyed", "Destroyed"),
        ("damaged", "Damaged"),
        ("possibly_damaged", "Possibly damaged"),
    ]

    dataset = models.ForeignKey(DamageDataset, on_delete=models.CASCADE, related_name="features")
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    grade = models.CharField(max_length=20, choices=GRADE_CHOICES)
    geometry = models.TextField(help_text="GeoJSON geometry in WGS84 (lon, lat).")
    label = models.CharField(max_length=200, blank=True)
    west = models.FloatField(db_index=True)
    south = models.FloatField(db_index=True)
    east = models.FloatField(db_index=True)
    north = models.FloatField(db_index=True)

    def __str__(self):
        return f"{self.get_grade_display()} {self.kind} ({self.dataset.name})"
