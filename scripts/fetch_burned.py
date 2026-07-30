#!/usr/bin/env python3
"""Régénère public/burned.geojson depuis le WFS EFFIS / Copernicus.

Le serveur le fait déjà tout seul en tâche de fond (toutes les 6 h). Ce script
sert à forcer une régénération sans lancer le serveur : première installation,
préparation de la démo statique, ou vérification après une panne d'EFFIS.

Usage : python3 scripts/fetch_burned.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402  (le sys.path doit être complété avant l'import)


def main():
    prev_n, was_ours = server._previous_burned_count()
    print(f"En place : {prev_n} périmètres" + ("" if was_ours else " (autre provenance)"))
    t0 = time.time()
    try:
        fc = server.build_burned()
    except Exception as exc:
        print(f"Échec de la collecte EFFIS : {exc}")
        return 1
    dated = sum(1 for f in fc["features"] if f["properties"].get("kind") == "dated")
    nrt = len(fc["features"]) - dated
    try:
        n = server.write_burned(fc)
    except Exception as exc:
        print(f"Écriture écartée : {exc}")
        return 1
    size_mo = server.BURNED_PATH.stat().st_size / 1e6
    print(f"{n} périmètres écrits ({dated} datés + {nrt} NRT), "
          f"{size_mo:.2f} Mo, en {time.time() - t0:.1f} s")
    print(f"Couches : {', '.join(fc['sources'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
