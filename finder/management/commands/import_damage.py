"""Import an official damage assessment file (e.g. UNOSAT from HDX).

Example:
    python manage.py import_damage kharkiv_23march2022_rda.zip \
        --name "UNOSAT North Kharkiv RDA" --source unosat --date 2022-03-23 \
        --attribution "UNOSAT / UNITAR, CC BY-SA" --license "CC BY-SA"
"""

from datetime import date

from django.core.management.base import BaseCommand, CommandError

from finder.models import DamageDataset


class Command(BaseCommand):
    help = "Import a damage assessment file (zipped shapefile, GeoJSON, GeoPackage, KML, geodatabase)."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--name", required=True)
        parser.add_argument("--source", choices=["unosat", "copernicus", "other"], default="unosat")
        parser.add_argument("--date", help="Imagery date, YYYY-MM-DD")
        parser.add_argument("--attribution", default="")
        parser.add_argument("--license", default="")
        parser.add_argument("--url", default="")
        parser.add_argument("--grade", choices=["destroyed", "damaged", "possibly_damaged"],
                            help="Grade for every feature, only if the file has no damage column")

    def handle(self, *args, **opts):
        from official_damage import import_damage_file

        try:
            imagery_date = date.fromisoformat(opts["date"]) if opts["date"] else None
        except ValueError:
            raise CommandError("--date must be YYYY-MM-DD")

        dataset = DamageDataset.objects.create(
            name=opts["name"], source=opts["source"], imagery_date=imagery_date,
            attribution=opts["attribution"], license=opts["license"], source_url=opts["url"],
        )
        try:
            summary = import_damage_file(opts["path"], dataset, opts.get("grade"))
        except Exception as exc:
            dataset.delete()
            raise CommandError(f"Import failed: {exc}")
        self.stdout.write(self.style.SUCCESS(f"Imported '{dataset.name}' (id {dataset.pk}): {summary}"))
