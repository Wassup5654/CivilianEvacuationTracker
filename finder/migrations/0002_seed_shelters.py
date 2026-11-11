from django.db import migrations


def seed_shelters(apps, schema_editor):
    Shelter = apps.get_model('finder', 'Shelter')
    shelters_data = [
        # Bakhmut, Ukraine
        {
            "name": "Bakhmut School No. 11 Basement Shelter",
            "latitude": 48.5942,
            "longitude": 38.0015,
            "shelter_type": "bunker",
            "source": "civil_defense",
            "verified": True,
        },
        {
            "name": "Bakhmut City Culture Palace Shelter",
            "latitude": 48.5975,
            "longitude": 37.9982,
            "shelter_type": "civil_defense",
            "source": "civil_defense",
            "verified": True,
        },
        {
            "name": "Bakhmut Central Hospital Underground Facility",
            "latitude": 48.5880,
            "longitude": 38.0090,
            "shelter_type": "medical_bunker",
            "source": "civil_defense",
            "verified": True,
        },
        {
            "name": "Railway Station Civil Defense Shelter",
            "latitude": 48.6040,
            "longitude": 38.0120,
            "shelter_type": "assembly_point",
            "source": "civil_defense",
            "verified": True,
        },
        # Brambleton / Loudoun County, VA
        {
            "name": "Brambleton Community Center",
            "latitude": 38.9660,
            "longitude": -77.5380,
            "shelter_type": "assembly_point",
            "source": "manual",
            "verified": True,
        },
        {
            "name": "Briar Woods High School Shelter",
            "latitude": 38.9740,
            "longitude": -77.5270,
            "shelter_type": "school_shelter",
            "source": "manual",
            "verified": True,
        },
        {
            "name": "Brambleton Public Safety Center",
            "latitude": 38.9610,
            "longitude": -77.5320,
            "shelter_type": "emergency_center",
            "source": "manual",
            "verified": True,
        },
    ]
    for data in shelters_data:
        Shelter.objects.get_or_create(
            name=data["name"],
            defaults=data
        )


def unseed_shelters(apps, schema_editor):
    Shelter = apps.get_model('finder', 'Shelter')
    Shelter.objects.filter(source__in=["civil_defense", "manual"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('finder', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(seed_shelters, unseed_shelters),
    ]
