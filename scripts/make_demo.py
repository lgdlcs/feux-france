#!/usr/bin/env python3
"""Génère la démo statique GitHub Pages dans docs/.

Prend un instantané daté des API locales (le serveur doit tourner sur :8741)
+ un appel météo Open-Meteo pour les foyers, et copie le frontend.
Le frontend en mode DEMO (hostname github.io) fige son horloge sur
fetched_at_utc et affiche un bandeau « démo, données non temps réel ».

Usage : python3 scripts/make_demo.py   (puis commit de docs/)
"""
import json
import shutil
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
API = DOCS / "demo-api"
BASE = "http://localhost:8741"


def get(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    API.mkdir(parents=True, exist_ok=True)
    (DOCS / ".nojekyll").write_text("")

    fires = get(BASE + "/api/fires")
    (API / "fires.json").write_text(json.dumps(fires, ensure_ascii=False))
    (API / "situation.json").write_text(json.dumps(get(BASE + "/api/situation"), ensure_ascii=False))
    (API / "wind.json").write_text(json.dumps(get(BASE + "/api/wind"), ensure_ascii=False))

    # Météo par foyer (mêmes variables que le frontend), appariée par coordonnées.
    foyers = fires.get("foyers", [])[:16]
    entries = []
    if foyers:
        lats = ",".join(f"{f['lat']:.4f}" for f in foyers)
        lons = ",".join(f"{f['lon']:.4f}" for f in foyers)
        v = "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_gusts_10m,wind_direction_10m"
        data = get("https://api.open-meteo.com/v1/forecast?latitude=" + lats +
                   "&longitude=" + lons + "&current=" + v + "&hourly=" + v +
                   "&forecast_hours=12&timezone=Europe%2FParis")
        arr = data if isinstance(data, list) else [data]
        for f, item in zip(foyers, arr):
            entries.append({"lat": f["lat"], "lon": f["lon"],
                            "current": item.get("current"), "hourly": item.get("hourly")})
    (API / "meteo.json").write_text(json.dumps(
        {"fetched_at_utc": fires.get("fetched_at_utc"), "entries": entries}, ensure_ascii=False))

    shutil.copy(ROOT / "public" / "index.html", DOCS / "index.html")
    shutil.copy(ROOT / "public" / "burned.geojson", DOCS / "burned.geojson")

    print(f"Démo générée dans docs/ — instantané {fires.get('fetched_at_utc')}, "
          f"{fires.get('count')} détections, {len(entries)} foyers météo.")


if __name__ == "__main__":
    main()
