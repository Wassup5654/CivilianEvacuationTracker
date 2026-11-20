"""Download the live hazard feeds now (NASA FIRMS fire detections + GDACS alerts).

The server also refreshes them in the background on demand; run this before a
demo, or from cron / Task Scheduler, so the first map load already has them:

    python manage.py refresh_hazards
"""

from django.core.management.base import BaseCommand

import live_hazards


class Command(BaseCommand):
    help = "Download the global NASA FIRMS and GDACS live hazard feeds."

    def handle(self, *args, **options):
        live_hazards.refresh(force=True)
        state = live_hazards._state
        self.stdout.write(self.style.SUCCESS(
            f"{len(state['fires'] or [])} fire/heat detections and {len(state['alerts'] or [])} "
            f"disaster alerts downloaded to {live_hazards.HAZARDS_DIR}."))
        for err in state["errors"]:
            self.stdout.write(self.style.WARNING(f"Could not refresh {err}"))
