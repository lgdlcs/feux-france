#!/usr/bin/env python3
"""Valide les fichiers de données tenus à la main avant commit.

Le serveur lit `evacuations.json` et `situation.json` derrière un `except`
silencieux : un JSON cassé ne lève rien, il vide simplement le panneau en
production. Ce script est le garde-fou — il échoue bruyamment, lui.

    python3 scripts/check_data.py
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHAMPS = ("commune", "dept", "lat", "lon", "population", "statut",
          "annonce", "source", "source_url")
RE_DEPT = re.compile(r"^.+ \(\d{2,3}[AB]?\)$")
RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")

erreurs = []


def err(fichier, ou, msg):
    erreurs.append(f"{fichier} · {ou} : {msg}")


def charge(nom):
    """Signale précisément les deux fautes de frappe qui cassent le JSON."""
    chemin = ROOT / nom
    if not chemin.exists():
        err(chemin, "fichier", "introuvable")
        return None
    brut = chemin.read_text(encoding="utf-8")
    for i, ligne in enumerate(brut.split("\n"), 1):
        if re.match(r"^\s*(//|/\*|#)", ligne):
            err(nom, f"ligne {i}", "commentaire : le JSON n'en accepte pas")
    try:
        return json.loads(brut)
    except json.JSONDecodeError as exc:
        indice = ""
        if "trailing comma" in str(exc).lower():
            indice = " (virgule traînante avant une fermeture)"
        err(nom, f"ligne {exc.lineno}", f"JSON invalide{indice} — {exc.msg}")
        return None


def verifie_entrees(nom, cle, entrees):
    for i, e in enumerate(entrees):
        ou = f"{cle}[{i}]"
        if not isinstance(e, dict):
            err(nom, ou, "n'est pas un objet")
            continue
        if set(e) - set(CHAMPS):
            inconnus = ", ".join(sorted(set(e) - set(CHAMPS)))
            err(nom, ou, f"champ(s) hors format : {inconnus}. "
                         "Pas de séparateur dans les tableaux, `dept` porte le regroupement")
            continue
        ou = f"{cle}[{i}] {e.get('commune') or '?'}"
        for champ in ("commune", "dept", "statut", "source", "source_url"):
            if not e.get(champ):
                err(nom, ou, f"`{champ}` manquant — pas un chiffre sans source")
        if not RE_DEPT.match(str(e.get("dept", ""))):
            err(nom, ou, f"`dept` attendu au format \"Nom (NN)\", reçu {e.get('dept')!r}")
        for champ in ("lat", "lon"):
            v = e.get(champ)
            if not isinstance(v, (int, float)):
                err(nom, ou, f"`{champ}` doit être un nombre (centre geo.api.gouv.fr)")
        if isinstance(e.get("lat"), (int, float)) and not 41 <= e["lat"] <= 52:
            err(nom, ou, f"`lat` {e['lat']} hors métropole")
        if isinstance(e.get("lon"), (int, float)) and not -5.5 <= e["lon"] <= 10:
            err(nom, ou, f"`lon` {e['lon']} hors métropole")
        pop = e.get("population")
        if pop is not None and not isinstance(pop, int):
            err(nom, ou, "`population` doit être un entier ou null — jamais une estimation")
        if not RE_DATE.match(str(e.get("annonce", ""))):
            err(nom, ou, "`annonce` attendue en AAAA-MM-JJ (précision libre entre parenthèses)")
        if not str(e.get("source_url", "")).startswith("http"):
            err(nom, ou, "`source_url` doit être un lien direct vers le communiqué")


def main():
    evac = charge("evacuations.json")
    if evac is not None:
        for cle in ("evacuations", "retours"):
            if cle not in evac:
                if cle == "evacuations":
                    err("evacuations.json", "racine", "clé `evacuations` absente")
                continue
            if not isinstance(evac[cle], list):
                err("evacuations.json", cle, "doit être un tableau")
                continue
            verifie_entrees("evacuations.json", cle, evac[cle])
        if not RE_DATE.match(str(evac.get("updated", ""))):
            err("evacuations.json", "racine", "`updated` attendu en AAAA-MM-JJThh:mm:ssZ")

    charge("situation.json")  # validité JSON seulement : le schéma y est libre

    if erreurs:
        print(f"✗ {len(erreurs)} problème(s) :\n", file=sys.stderr)
        for e in erreurs:
            print(f"  - {e}", file=sys.stderr)
        print("\nVoir le format attendu dans .github/PULL_REQUEST_TEMPLATE.md", file=sys.stderr)
        return 1

    n = len(evac.get("evacuations", [])) + len(evac.get("retours", [])) if evac else 0
    print(f"✓ données valides — {n} entrées vérifiées")
    return 0


if __name__ == "__main__":
    sys.exit(main())
