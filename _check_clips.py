"""Temporary signed-in check for /api/clips. Deleted after it runs."""

import os
import struct
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth import get_user_model
from django.test import Client

User = get_user_model()
name = "evac-clips-check"
User.objects.filter(username=name).delete()
User.objects.create_user(username=name, password="clips-check-pass-91")
try:
    client = Client(HTTP_HOST="127.0.0.1")
    assert client.login(username=name, password="clips-check-pass-91")
    page = client.get("/dashboard/").content.decode()
    print("dashboard calls clips:", "/api/clips?lat=" in page)

    bad = client.get("/api/clips", {"lat": "x", "lon": "y"})
    print("bad input:", bad.status_code, bad.json())

    for label, lat, lon in (
        ("Kyiv", "50.4502431", "30.5240622"),
        ("Tehran", "35.7366807", "51.4093358"),
        ("Quibdo", "5.6912838", "-76.6531337"),
    ):
        reply = client.get("/api/clips", {"lat": lat, "lon": lon})
        data = reply.json()
        print("\n" + label, reply.status_code)
        if reply.status_code != 200:
            print(" error:", data.get("error"))
            continue
        print(" latest :", data["latest"]["captured"], data["latest"]["cloud_cover"])
        if data["earlier"]:
            print(" earlier:", data["earlier"]["captured"], data["earlier"]["cloud_cover"])
        else:
            print(" earlier: none -", data["earlier_note"])
        folder = Path(data["folder"])
        for tif in sorted(folder.glob("*.tif")):
            raw = tif.read_bytes()
            order = "<" if raw[:2] == b"II" else ">"
            off = struct.unpack(order + "I", raw[4:8])[0]
            n = struct.unpack(order + "H", raw[off:off + 2])[0]
            tags = {}
            for i in range(n):
                e = raw[off + 2 + 12 * i: off + 14 + 12 * i]
                tag, typ, cnt, val = struct.unpack(order + "HHII", e)
                tags[tag] = val
            print(f" {tif.name}: {len(raw)} bytes, {tags.get(256)}x{tags.get(257)}, georef={33922 in tags}")

    again = client.get("/api/clips", {"lat": "50.4502431", "lon": "30.5240622"})
    print("\nrepeat Kyiv:", again.status_code)
finally:
    User.objects.filter(username=name).delete()
    print("temp user removed:", not User.objects.filter(username=name).exists())
