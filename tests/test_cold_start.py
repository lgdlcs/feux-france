#!/usr/bin/env python3
"""Vérifie que cold_start ne relance pas la source amont à chaque requête.

Le 4 août 2026, /api/wind est resté bloqué en 429 en prod : Open-Meteo refusait,
le cache restait vide, donc chaque visiteur relançait une collecte complète de la
grille (327 points) — ce qui entretenait le rejet au lieu d'en sortir. Le garde-fou
est un délai après échec ; ce test le tient.

    python3 tests/test_cold_start.py
"""

import importlib.util
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("srv", ROOT / "server.py")
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

echecs = []


def verifie(condition, message):
    if not condition:
        echecs.append(message)


def neuf():
    return {"data": None, "ts": 0}, threading.Lock(), threading.Lock()


def test_echec_amont_non_repete():
    cache, cl, bl = neuf()
    appels = {"n": 0}

    def build():
        appels["n"] += 1
        raise RuntimeError("HTTP Error 429: Too Many Requests")

    for _ in range(20):
        try:
            srv.cold_start(cache, cl, bl, build, lambda d: cache.update(data=d))
        except Exception:
            pass
    verifie(appels["n"] == 1,
            f"20 requêtes sur une source en erreur → {appels['n']} appels amont, attendu 1")


def test_cache_rempli_sert_sans_appel():
    cache, cl, bl = neuf()
    appels = {"n": 0}

    def build():
        appels["n"] += 1
        raise RuntimeError("ne devrait pas être appelé")

    cache["data"] = {"count": 327}          # le thread de fond a fait son travail
    got = srv.cold_start(cache, cl, bl, build, lambda d: cache.update(data=d))
    verifie(got == {"count": 327}, f"cache non servi tel quel : {got!r}")
    verifie(appels["n"] == 0, "la source a été appelée alors que le cache était plein")


def test_succes_construit_une_seule_fois():
    cache, cl, bl = neuf()
    appels = {"n": 0}

    def build():
        appels["n"] += 1
        return {"count": 1}

    for _ in range(5):
        srv.cold_start(cache, cl, bl, build, lambda d: cache.update(data=d))
    verifie(appels["n"] == 1,
            f"5 requêtes après un succès → {appels['n']} constructions, attendu 1")


if __name__ == "__main__":
    for fn in (test_echec_amont_non_repete,
               test_cache_rempli_sert_sans_appel,
               test_succes_construit_une_seule_fois):
        fn()
    if echecs:
        print(f"✗ {len(echecs)} problème(s) :\n", file=sys.stderr)
        for e in echecs:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)
    print("✓ cold_start : un échec amont ne se rejoue pas à chaque requête")
