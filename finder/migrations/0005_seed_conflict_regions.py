from datetime import date

from django.db import migrations

# Approximate country/territory bounding boxes (west, south, east, north).
# Boxes overlap neighbours; the smallest matching box wins, and they can be
# refined in the admin.
REGIONS = [
    ("Ukraine", date(2022, 2, 24), (22.1, 44.3, 40.3, 52.4),
     "Full-scale invasion."),
    ("Gaza Strip", date(2023, 10, 7), (34.21, 31.21, 34.57, 31.60), ""),
    ("Lebanon", date(2024, 9, 23), (35.09, 33.05, 36.63, 34.70),
     "Main escalation of strikes."),
    ("Iran", date(2025, 6, 13), (44.0, 25.0, 63.4, 39.8),
     "June 2025 strikes; damage at scattered single sites is hard to detect at 10 m."),
    ("Sudan", date(2023, 4, 15), (21.8, 8.6, 38.6, 22.3), ""),
    ("Afghanistan", date(2021, 5, 1), (60.5, 29.3, 75.0, 38.5),
     "Taliban offensive; most earlier war damage predates the radar baseline."),
]


def seed(apps, schema_editor):
    ConflictRegion = apps.get_model("finder", "ConflictRegion")
    for name, ref, (w, s, e, n), notes in REGIONS:
        ConflictRegion.objects.get_or_create(
            name=name,
            defaults={"reference_date": ref, "west": w, "south": s, "east": e, "north": n, "notes": notes},
        )


def unseed(apps, schema_editor):
    apps.get_model("finder", "ConflictRegion").objects.filter(name__in=[r[0] for r in REGIONS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("finder", "0004_conflict_regions"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
