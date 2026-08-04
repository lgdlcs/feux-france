#!/usr/bin/env python3
"""Serveur local pour la carte des incendies en France.

Récupère les détections thermiques satellites NASA FIRMS (flux publics 7 jours,
mise à jour continue, sans clé API), filtre les points situés sur le
territoire métropolitain (polygone précis, pas un simple rectangle) et les
sert en JSON au frontend. Cache de 10 minutes pour ne pas surcharger FIRMS.

Régénère aussi les périmètres brûlés EFFIS et suit la flotte aérienne d'État.
"""

import calendar
import csv
import gzip
import hashlib
import io
import os
import json
import math
import threading
import time
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

# PORT/HOST configurables pour l'hébergement (Render, Fly, Docker…) ;
# par défaut, comportement local inchangé.
PORT = int(os.environ.get("PORT", 8741))
HOST = os.environ.get("HOST", "127.0.0.1" if "PORT" not in os.environ else "0.0.0.0")
ROOT = Path(__file__).parent
CACHE_TTL = 600  # secondes

# Clustering des détections en "foyers"
FOYER_GRID = 0.02        # grille de regroupement (degrés)
FOYER_FINE_GRID = 0.00375  # ~375 m : empreinte pixel VIIRS pour l'estimation de surface
FOYER_CELL_HA = 14.06    # ha couverts par une cellule fine (~375 m × 375 m)
FOYER_MIN_POINTS = 3     # nb minimal de détections pour retenir un foyer
FOYER_ACTIVE_HOURS = 6   # foyer "actif" si dernière détection < 6h
FOYER_GEOCODE_MIN_N = 10  # géocode aussi les gros foyers même inactifs

# Cache mémoire du reverse-geocoding, clé = cellule 0.02° arrondie, pour ne pas
# rappeler geo.api.gouv.fr à chaque refresh.
_geo_cache = {}
_geo_lock = threading.Lock()

# Fenêtre d'historique. Les flux CSV publics FIRMS n'existent qu'en 24h / 48h
# / 7d : c'est 7d le maximum sans clé API. Environ 11 000 détections sur la
# métropole en pleine saison, soit ~2 Mo de JSON mais seulement ~140 Ko gzippés
# (le handler compresse) — et la frise devient lisible sur toute la durée d'un
# feu, au lieu de n'en montrer que le dernier jour.
FIRMS_WINDOW = "7d"
WINDOW_HOURS = 168
FIRMS_BASE = "https://firms.modaps.eosdis.nasa.gov/data/active_fire"

FEEDS = [
    {
        "id": "viirs_snpp",
        "label": "VIIRS Suomi-NPP (375 m)",
        "url": f"{FIRMS_BASE}/suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2_Europe_{FIRMS_WINDOW}.csv",
    },
    {
        "id": "viirs_noaa20",
        "label": "VIIRS NOAA-20 (375 m)",
        "url": f"{FIRMS_BASE}/noaa-20-viirs-c2/csv/J1_VIIRS_C2_Europe_{FIRMS_WINDOW}.csv",
    },
    {
        "id": "viirs_noaa21",
        "label": "VIIRS NOAA-21 (375 m)",
        "url": f"{FIRMS_BASE}/noaa-21-viirs-c2/csv/J2_VIIRS_C2_Europe_{FIRMS_WINDOW}.csv",
    },
    {
        "id": "modis",
        "label": "MODIS Aqua/Terra (1 km)",
        "url": f"{FIRMS_BASE}/modis-c6.1/csv/MODIS_C6_1_Europe_{FIRMS_WINDOW}.csv",
    },
]

# Pré-filtre grossier avant le test polygone (métropole + Corse)
BBOX = (41.2, -5.6, 51.3, 9.9)  # lat_min, lon_min, lat_max, lon_max


def load_industrial_sites():
    """Sites industriels à chaleur permanente (raffineries, aciéries,
    torchères…) que FIRMS détecte comme des feux en continu. On écarte toute
    détection tombant dans leur rayon. Liste éditable dans sites_industriels.json.
    Renvoie une liste de (lat, lon, rayon_km)."""
    path = ROOT / "sites_industriels.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    sites = []
    for s in data.get("sites", []):
        try:
            sites.append((float(s["lat"]), float(s["lon"]), float(s.get("rayon_km", 3.0))))
        except (KeyError, ValueError, TypeError):
            continue
    return sites


INDUSTRIAL_SITES = load_industrial_sites()


def is_industrial(lat, lon):
    """True si (lat, lon) tombe dans le rayon d'un site industriel connu.
    Distance approchée en km (équirectangulaire, suffisant à cette échelle)."""
    for slat, slon, rayon_km in INDUSTRIAL_SITES:
        dlat = (lat - slat) * 111.0
        dlon = (lon - slon) * 111.0 * math.cos(math.radians((lat + slat) / 2.0))
        if dlat * dlat + dlon * dlon <= rayon_km * rayon_km:
            return True
    return False


def load_france_rings():
    geo = json.loads((ROOT / "metropole.geojson").read_text())
    geom = geo["geometry"]
    rings = []
    if geom["type"] == "Polygon":
        rings.append(geom["coordinates"][0])
    else:  # MultiPolygon
        for poly in geom["coordinates"]:
            rings.append(poly[0])
    return rings


FRANCE_RINGS = load_france_rings()

def point_in_ring(lon, lat, ring):
    """Lancer de rayon même-impair sur un seul anneau. Conservée telle quelle :
    elle sert de référence au test d'équivalence de l'index ci-dessous."""
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


# Index par bandes de latitude. Testé naïvement, un point était comparé aux
# 31 292 sommets du contour métropolitain — 3,4 ms, dont l'essentiel dans le
# seul anneau continental (les 166 autres sont minuscules). En fenêtre 7 jours
# cela faisait une minute de calcul par cycle sur une machine de bureau, et
# plusieurs minutes sur le demi-cœur de l'hébergement : le cache n'était jamais
# rempli et /api/fires ne répondait plus.
#
# Une arête ne peut être franchie par le rayon horizontal d'un point que si
# celui-ci est dans sa plage de latitudes. On range donc les arêtes par bande de
# 0,05° et on ne teste que celles de la bande du point. Les anneaux étant
# disjoints (uniquement des contours extérieurs, aucun trou), compter les
# croisements de TOUS les anneaux d'un coup donne le même résultat que tester
# chaque anneau séparément : le point est dedans si le total est impair.
BAND = 0.05
LAT0 = min(p[1] for r in FRANCE_RINGS for p in r)


def build_france_index():
    buckets = {}
    for ring in FRANCE_RINGS:
        j = len(ring) - 1
        for i in range(len(ring)):
            x1, y1 = ring[j][0], ring[j][1]
            x2, y2 = ring[i][0], ring[i][1]
            j = i
            if y1 == y2:
                continue  # arête horizontale : jamais franchie par un rayon horizontal
            b0 = int((min(y1, y2) - LAT0) / BAND)
            b1 = int((max(y1, y2) - LAT0) / BAND)
            edge = (x1, y1, x2, y2)
            for b in range(b0, b1 + 1):
                buckets.setdefault(b, []).append(edge)
    return buckets


FRANCE_BANDS = build_france_index()


def in_france(lon, lat):
    edges = FRANCE_BANDS.get(int((lat - LAT0) / BAND))
    if not edges:
        return False   # hors de l'emprise : aucune arête à franchir
    inside = False
    for x1, y1, x2, y2 in edges:
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


_cache = {"data": None, "ts": 0}
_lock = threading.Lock()


def normalize_confidence(raw):
    """FIRMS: VIIRS = low/nominal/high, MODIS = 0-100."""
    if raw in ("l", "low"):
        return "faible"
    if raw in ("n", "nominal"):
        return "normale"
    if raw in ("h", "high"):
        return "élevée"
    try:
        v = int(raw)
        return "faible" if v < 30 else ("normale" if v < 80 else "élevée")
    except ValueError:
        return "normale"


def fetch_feed(feed):
    req = urllib.request.Request(feed["url"], headers={"User-Agent": "carte-incendies-local/1.0"})
    # Les flux 7 jours pèsent plusieurs Mo : marge plus large qu'en 24 h.
    with urllib.request.urlopen(req, timeout=120) as resp:
        text = resp.read().decode("utf-8")
    points = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            lat = float(row["latitude"])
            lon = float(row["longitude"])
        except (KeyError, ValueError):
            continue
        if not (BBOX[0] <= lat <= BBOX[2] and BBOX[1] <= lon <= BBOX[3]):
            continue
        if not in_france(lon, lat):
            continue
        if is_industrial(lat, lon):
            continue  # torchère / raffinerie / aciérie : chaleur permanente, pas un feu
        acq_time = row["acq_time"].zfill(4)
        # 4 décimales (~11 m) sur un pixel satellite de 375 m à 1 km : aucune
        # perte utile, et ~25 Ko de JSON gzippé en moins sur les 11 000 points.
        # `satellite` n'est pas transmis : le libellé se déduit de `source` via
        # la liste `feeds` du même payload, au lieu d'être répété par point.
        points.append({
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "acq_utc": f"{row['acq_date']}T{acq_time[:2]}:{acq_time[2:]}:00Z",
            "frp": round(float(row.get("frp") or 0), 2),
            "confidence": normalize_confidence(str(row.get("confidence", "")).strip().lower()),
            "source": feed["id"],
            "daynight": row.get("daynight", ""),
        })
    return points


def _parse_utc(s):
    """'2026-07-26T14:30:00Z' -> epoch (float). Renvoie 0 si illisible.

    timegm interprète le struct_time comme de l'UTC, ce qu'il est. La variante
    mktime() - time.timezone se trompait d'une heure pendant tout l'été :
    time.timezone est le décalage HORS heure d'été (altzone s'applique alors),
    et les foyers quittaient donc la liste des actifs une heure trop tôt.
    """
    try:
        return float(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return 0.0


def reverse_geocode(lat, lon):
    """Nom de la commune la plus proche via geo.api.gouv.fr.

    Cache en mémoire par cellule 0.02° arrondie. Échec / timeout -> None.
    """
    key = (round(lat / FOYER_GRID), round(lon / FOYER_GRID))
    with _geo_lock:
        if key in _geo_cache:
            return _geo_cache[key]
    nom = None
    try:
        qs = urllib.parse.urlencode({"lat": f"{lat:.5f}", "lon": f"{lon:.5f}", "fields": "nom"})
        url = f"https://geo.api.gouv.fr/communes?{qs}"
        req = urllib.request.Request(url, headers={"User-Agent": "carte-incendies-local/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, list) and data:
            nom = data[0].get("nom")
    except Exception:
        nom = None
    with _geo_lock:
        _geo_cache[key] = nom
    return nom


def cluster_foyers(points):
    """Regroupe les détections en foyers (composantes connexes 8-connexité
    sur une grille 0.02°). Ne conserve que les foyers de >= FOYER_MIN_POINTS
    détections. Trie par nombre de détections décroissant."""
    if not points:
        return []

    # Répartition des points par cellule 0.02°
    cells = {}
    for p in points:
        c = (int(math.floor(p["lat"] / FOYER_GRID)), int(math.floor(p["lon"] / FOYER_GRID)))
        cells.setdefault(c, []).append(p)

    # Composantes connexes (8-connexité) sur les cellules occupées
    remaining = set(cells)
    components = []
    while remaining:
        seed = remaining.pop()
        stack = [seed]
        comp = [seed]
        while stack:
            cy, cx = stack.pop()
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dy == 0 and dx == 0:
                        continue
                    neigh = (cy + dy, cx + dx)
                    if neigh in remaining:
                        remaining.discard(neigh)
                        stack.append(neigh)
                        comp.append(neigh)
        components.append(comp)

    now = time.time()
    foyers = []
    fid = 0
    for comp in components:
        comp_points = [p for c in comp for p in cells[c]]
        n = len(comp_points)
        if n < FOYER_MIN_POINTS:
            continue
        fid += 1
        lat_c = sum(p["lat"] for p in comp_points) / n
        lon_c = sum(p["lon"] for p in comp_points) / n
        utcs = [p["acq_utc"] for p in comp_points]
        first_utc = min(utcs)
        last_utc = max(utcs)
        max_frp = max(p["frp"] for p in comp_points)
        fine = {(int(math.floor(p["lat"] / FOYER_FINE_GRID)),
                 int(math.floor(p["lon"] / FOYER_FINE_GRID))) for p in comp_points}
        est_area_ha = round(len(fine) * FOYER_CELL_HA)
        active = (now - _parse_utc(last_utc)) < FOYER_ACTIVE_HOURS * 3600
        # `n` couvre toute la fenêtre (7 j) : c'est le cumul du feu. `n_24h`
        # isole le dernier jour, pour distinguer un foyer qui court encore d'un
        # grand feu déjà éteint mais toujours dans la fenêtre.
        n_24h = sum(1 for p in comp_points if now - _parse_utc(p["acq_utc"]) < 86400)
        foyers.append({
            "id": fid,
            "lat": round(lat_c, 5),
            "lon": round(lon_c, 5),
            "n": n,
            "n_24h": n_24h,
            "first_utc": first_utc,
            "last_utc": last_utc,
            "max_frp": round(max_frp, 2),
            "est_area_ha": est_area_ha,
            "active": active,
            "nom": None,
            # Rétention temporaire des points de la composante (retirée avant
            # le return pour rester JSON-sérialisable).
            "_pts": comp_points,
        })

    foyers.sort(key=lambda f: f["n"], reverse=True)
    # Réattribue des id stables selon l'ordre de tri
    for i, f in enumerate(foyers, start=1):
        f["id"] = i

    # Tague chaque détection avec l'id définitif de son foyer (après tri), pour
    # permettre au frontend de tracer l'emprise réelle (enveloppe convexe).
    for f in foyers:
        for p in f["_pts"]:
            p["foyer_id"] = f["id"]
        del f["_pts"]

    # Reverse-geocoding limité aux foyers actifs ou >= FOYER_GEOCODE_MIN_N
    for f in foyers:
        if f["active"] or f["n"] >= FOYER_GEOCODE_MIN_N:
            f["nom"] = reverse_geocode(f["lat"], f["lon"])

    return foyers


def build_payload():
    all_points, feeds_status = [], []
    for feed in FEEDS:
        try:
            pts = fetch_feed(feed)
            all_points.extend(pts)
            feeds_status.append({"id": feed["id"], "label": feed["label"], "ok": True, "count": len(pts)})
        except Exception as exc:  # flux indisponible : on le signale, on ne l'invente pas
            feeds_status.append({"id": feed["id"], "label": feed["label"], "ok": False, "error": str(exc)})
    all_points.sort(key=lambda p: p["acq_utc"], reverse=True)
    foyers = cluster_foyers(all_points)
    return {
        "fetched_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "window_hours": WINDOW_HOURS,
        "feeds": feeds_status,
        "count": len(all_points),
        "points": all_points,
        "foyers": foyers,
    }


# Les deux gros builds — FIRMS 7 jours et périmètres EFFIS — tournent dans des
# threads indépendants avec des périodes différentes (minutes contre 6 h) : ils
# finissent forcément par tomber en même temps. Chacun pèse quelques centaines
# de Mo au pic, pour 512 Mo sur l'hébergement, et un OOM tue tout le service.
# Ce verrou les met simplement bout à bout : personne n'attend, ces builds
# n'étant jamais dans le chemin d'une requête.
_heavy_lock = threading.Lock()


def _store_fires(data):
    with _lock:
        _cache["data"] = data
        _cache["ts"] = time.time()


def cold_start(cache, cache_lock, build_lock, build, store):
    """Sert le cache tel quel (le thread d'arrière-plan tient la fraîcheur) et
    ne construit en direct que s'il est encore vide — le tout premier instant
    après le démarrage.

    Un seul constructeur à la fois, et on relit le cache après avoir obtenu le
    verrou. Sans ça, chaque visiteur arrivé pendant ce trou lançait sa PROPRE
    collecte complète : sur le petit CPU de l'hébergement elles se ralentissaient
    mutuellement et aucune ne se terminait jamais, si bien que le cache restait
    vide indéfiniment et que /api/fires ne répondait plus du tout. Ici les
    visiteurs attendent la même construction au lieu de la multiplier."""
    with cache_lock:
        data = cache["data"]
    if data is not None:
        return data
    with build_lock:
        with cache_lock:
            data = cache["data"]
        if data is not None:
            return data      # un autre l'a construit pendant qu'on attendait
        data = build()
        store(data)
    return data


_fires_build_lock = threading.Lock()


def get_data():
    return cold_start(_cache, _lock, _fires_build_lock, build_payload, _store_fires)


# ---------------------------------------------------------------------------
# Vents v2 : grille régulière sur la métropole (pas 0.5°, ~330 points),
# un seul appel batch Open-Meteo (conditions actuelles + prévisions 13h),
# cache 10 min. Aucune donnée inventée : on ne renvoie que les points
# effectivement retournés par Open-Meteo avec mesure courante.
# ---------------------------------------------------------------------------
WIND_BBOX = (41.0, -5.5, 51.4, 9.7)  # lat_min, lon_min, lat_max, lon_max
WIND_STEP = 0.5                      # pas de la grille en degrés
WIND_COAST = 0.4                     # tolérance côtière (accepte un voisin ±0.4°)
_wind_cache = {"data": None, "ts": 0}
_wind_lock = threading.Lock()


def wind_grid_points():
    """Points de la grille dont le point OU un voisin à ±0.4° est en France
    (pour ne pas vider les côtes). Renvoie une liste de (lat, lon)."""
    lat_min, lon_min, lat_max, lon_max = WIND_BBOX
    pts = []
    lat = lat_min
    while lat <= lat_max + 1e-9:
        lon = lon_min
        while lon <= lon_max + 1e-9:
            if (in_france(lon, lat)
                    or in_france(lon + WIND_COAST, lat) or in_france(lon - WIND_COAST, lat)
                    or in_france(lon, lat + WIND_COAST) or in_france(lon, lat - WIND_COAST)):
                pts.append((round(lat, 4), round(lon, 4)))
            lon += WIND_STEP
        lat += WIND_STEP
    return pts


def build_wind():
    pts = wind_grid_points()
    if not pts:
        return {"fetched_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "count": 0, "points": [], "hourly_times": []}
    lats = ",".join(f"{p[0]}" for p in pts)
    lons = ",".join(f"{p[1]}" for p in pts)
    # Commas littérales (Open-Meteo les attend telles quelles), un seul appel avec courant et prévisions.
    # AROME France HD (~1,5 km, Météo-France) plutôt que le modèle global : bien plus fin sur la métropole.
    url = ("https://api.open-meteo.com/v1/forecast?latitude=" + lats +
           "&longitude=" + lons +
           "&current=wind_speed_10m,wind_direction_10m,wind_gusts_10m" +
           "&hourly=wind_speed_10m,wind_direction_10m,wind_gusts_10m" +
           "&models=meteofrance_arome_france_hd" +
           "&forecast_hours=13" +
           "&timezone=Europe%2FParis")
    req = urllib.request.Request(url, headers={"User-Agent": "carte-incendies-local/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    arr = data if isinstance(data, list) else [data]
    out = []
    hourly_times = []
    for item in arr:
        cur = item.get("current") or {}
        spd, drc = cur.get("wind_speed_10m"), cur.get("wind_direction_10m")
        if spd is None or drc is None:
            continue  # on n'affiche pas un point sans mesure
        # Capture hourly_times du premier item qui en a (heure locale Europe/Paris, 13 entrées)
        if not hourly_times:
            hourly_times = item.get("hourly", {}).get("time", [])
        out.append({
            "lat": item.get("latitude"),
            "lon": item.get("longitude"),
            "speed": spd,
            "dir": drc,
            "gust": cur.get("wind_gusts_10m"),
            "h_speed": item.get("hourly", {}).get("wind_speed_10m", []),
            "h_dir": item.get("hourly", {}).get("wind_direction_10m", []),
            "h_gust": item.get("hourly", {}).get("wind_gusts_10m", []),
        })
    return {
        "fetched_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(out),
        "points": out,
        "hourly_times": hourly_times,
    }


def _store_wind(data):
    with _wind_lock:
        _wind_cache["data"] = data
        _wind_cache["ts"] = time.time()


_wind_build_lock = threading.Lock()


def get_wind():
    return cold_start(_wind_cache, _wind_lock, _wind_build_lock, build_wind, _store_wind)


# ---------------------------------------------------------------------------
# Cumul national de surface brûlée (année en cours) via EFFIS / Copernicus.
# Source officielle européenne (le portail effis.statistics s'appuie dessus).
# Les valeurs 'area_ha' sont les surfaces cartographiées par satellite semaine
# par semaine ; on somme celles des semaines déjà écoulées pour le cumul YTD.
# NB : EFFIS ne cartographie que les feux ≳30 ha et avec latence — ce cumul
# SOUS-ESTIME le total national réel (stats pompiers), d'où la note côté front.
# Donnée hebdomadaire : rafraîchie une fois par heure, pas toutes les 10 min.
# ---------------------------------------------------------------------------
# Sans paramètre year, l'API renvoie la dernière année COMPLÈTE (52 semaines
# remplies, ex. 2025) avec des mddate relabellisées sur l'année courante — il
# faut donc toujours passer year explicitement.
EFFIS_URL = "https://api2.effis.emergency.copernicus.eu/statistics/v2/effis/weekly?country=FRA&year={year}"
NATIONAL_TTL = 3600  # secondes
_national_cache = {"data": None, "ts": 0}
_national_lock = threading.Lock()


def build_national():
    """Cumul YTD (ha) et nb d'événements pour la France depuis EFFIS.
    Ne somme que les semaines dont la date est déjà passée (les entrées
    futures sont des placeholders à ignorer)."""
    url = EFFIS_URL.format(year=time.strftime("%Y", time.gmtime()))
    req = urllib.request.Request(url, headers={"User-Agent": "carte-incendies-local/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    weeks = data.get("banfweekly") if isinstance(data, dict) else None
    if not isinstance(weeks, list) or not weeks:
        raise ValueError("réponse EFFIS inattendue")
    today = time.strftime("%Y%m%d", time.gmtime())
    ba = avg = events = 0.0
    counted = 0
    year = None
    for w in weeks:
        md = w.get("mddate")
        if not isinstance(md, str) or md > today:
            continue
        ba += float(w.get("area_ha") or 0)
        avg += float(w.get("area_ha_avg") or 0)
        events += float(w.get("events") or 0)
        counted += 1
        if year is None and len(md) >= 4:
            year = md[:4]
    # Garde-fou : au moins une semaine et bornes physiques plausibles.
    if not counted or ba < 0 or ba > 1e7:
        raise ValueError("cumul EFFIS hors bornes")
    return {
        "fetched_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "year": int(year) if year else None,
        "burned_ha": round(ba),
        "avg_ha": round(avg),          # climatologie 2006→ à la même date
        "events": round(events),
        "source": "EFFIS / Copernicus (surface cartographiée par satellite, feux ≳30 ha)",
    }


def _store_national(data):
    with _national_lock:
        _national_cache["data"] = data
        _national_cache["ts"] = time.time()


_national_build_lock = threading.Lock()


def get_national():
    return cold_start(_national_cache, _national_lock, _national_build_lock,
                      build_national, _store_national)


# ---------------------------------------------------------------------------
# Zones brûlées — périmètres EFFIS / Copernicus, régénérés en tâche de fond.
#
# Le WFS EFFIS est ouvert, sans clé, mais deux pièges :
#   1. il renvoie les coordonnées en [lat, lon] alors que la spec GeoJSON veut
#      [lon, lat] — sans permutation, tout atterrit en Somalie ;
#   2. il peut mettre de 5 à 250 s à répondre : jamais depuis le front, toujours
#      en tâche de fond.
#
# Deux couches complémentaires :
#   - modis.ba.poly.season : polygones DATÉS de la saison en cours, avec
#     attributs (FIREDATE, LASTUPDATE, AREA_HA, COMMUNE, PROVINCE, COUNTRY) ;
#   - effis.nrt.ba.poly    : contour plus frais, GÉOMÉTRIE SEULE (aucun
#     attribut, donc ni date ni pays — filtrage par le polygone métropole).
#
# Le résultat écrit public/burned.geojson, que le front charge en lazy. Le cache
# gzip du serveur étant indexé sur le mtime, la réécriture suffit à le publier.
# ---------------------------------------------------------------------------
EFFIS_WFS = "https://maps.effis.emergency.copernicus.eu/effis"
EFFIS_BBOX = (-5.5, 41.0, 10.0, 51.5)   # west, south, east, north
EFFIS_DATED_LAYER = "modis.ba.poly.season"
EFFIS_NRT_LAYER = "effis.nrt.ba.poly"
EFFIS_VIEWER = "https://forest-fire.emergency.copernicus.eu/effis-current-situation"
EFFIS_TIMEOUT = 300         # le WFS peut réellement mettre plusieurs minutes
BURNED_TTL = 6 * 3600       # EFFIS ne republie que 1 à 2 fois par jour
BURNED_PATH = ROOT / "public" / "burned.geojson"
BURNED_DP = 4               # ~11 m : sans commune mesure avec la résolution EFFIS
                            # (~250 m), et ~80 Ko gzip de moins qu'en 5 décimales
BURNED_SIMPLIFY = 0.0004    # ~45 m : très en dessous de la résolution EFFIS (~250 m)
BURNED_MIN_FEATURES = 50    # en dessous, on suspecte une réponse tronquée
BURNED_KEEP_RATIO = 0.5     # un build deux fois plus pauvre n'écrase pas l'acquis
BURNED_MARKER = "effis-wfs"  # trace notre propre génération dans le fichier


def effis_wfs(typename, timeout=EFFIS_TIMEOUT):
    """Interroge le WFS EFFIS sur l'emprise métropole et renvoie les features
    brutes (coordonnées encore en [lat, lon])."""
    w, s, e, n = EFFIS_BBOX
    url = (f"{EFFIS_WFS}?service=WFS&version=1.0.0&request=GetFeature"
           f"&typename=ms:{typename}&outputformat=geojson"
           f"&bbox={w},{s},{e},{n}")
    req = urllib.request.Request(url, headers={
        "User-Agent": "carte-incendies/1.0 (carte-incendies.fr)",
        "Accept": "application/json",
    })
    # Les octets bruts sont libérés dès le décodage, et le texte dès le parsing :
    # la réponse datée fait plusieurs Mo et garder les trois représentations en
    # même temps coûte cher pour rien.
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", "replace")
    data = json.loads(text)
    del text
    feats = data.get("features")
    return feats if isinstance(feats, list) else []


def swap_round(node):
    """Permute [lat, lon] -> [lon, lat] et arrondit, récursivement dans les
    tableaux de coordonnées GeoJSON (profondeur variable selon le type)."""
    if not isinstance(node, list) or not node:
        return node
    if isinstance(node[0], (int, float)):
        # Feuille : une position [lat, lon] à permuter.
        if len(node) < 2:
            return node
        return [round(float(node[1]), BURNED_DP), round(float(node[0]), BURNED_DP)]
    return [swap_round(child) for child in node]


def _seg_dist2(p, a, b):
    """Carré de la distance du point p au segment [a, b], en degrés²."""
    px, py = p[0], p[1]
    ax, ay = a[0], a[1]
    bx, by = b[0], b[1]
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return (px - ax) ** 2 + (py - ay) ** 2
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    return (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2


def simplify_ring(ring, tol):
    """Douglas-Peucker itératif (pas de récursion : certains périmètres ont des
    milliers de sommets). Les contours EFFIS sont tracés sur une grille de
    pixels : ils montent en escalier et la tolérance, choisie très en dessous de
    la résolution source, ne fait que retirer ces marches. Un anneau reste fermé
    et garde au moins 4 positions, sinon il est renvoyé tel quel."""
    if len(ring) < 5:
        return ring
    closed = ring[0] == ring[-1]
    pts = ring[:-1] if closed else ring
    if len(pts) < 4:
        return ring
    tol2 = tol * tol
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        worst, worst_d = -1, -1.0
        for k in range(i + 1, j):
            d = _seg_dist2(pts[k], pts[i], pts[j])
            if d > worst_d:
                worst, worst_d = k, d
        if worst_d > tol2:
            keep[worst] = True
            stack.append((i, worst))
            stack.append((worst, j))
    out = [p for p, k in zip(pts, keep) if k]
    if len(out) < (4 if closed else 2):
        return ring
    return out + [out[0]] if closed else out


def dedupe_ring(ring):
    """Retire les positions consécutives identiques. L'arrondi à BURNED_DP peut
    faire coïncider deux sommets voisins : ils ne dessinent plus rien mais
    pèsent encore une vingtaine d'octets chacun. L'anneau reste fermé."""
    if len(ring) < 2:
        return ring
    out = [ring[0]]
    for pt in ring[1:]:
        if pt != out[-1]:
            out.append(pt)
    if len(out) < 3:
        return ring
    if out[0] != out[-1]:
        out.append(out[0])
    return out if len(out) >= 4 else ring


def dedupe_geometry(coords, depth):
    if depth <= 1:
        return dedupe_ring(coords)
    return [dedupe_geometry(child, depth - 1) for child in coords]


def simplify_geometry(coords, depth):
    """Applique simplify_ring à chaque anneau. `depth` est le nombre de niveaux
    de listes restant à traverser avant d'atteindre un anneau (2 pour un
    Polygon : [anneaux][positions], 3 pour un MultiPolygon)."""
    if depth <= 1:
        return simplify_ring(coords, BURNED_SIMPLIFY)
    return [simplify_geometry(child, depth - 1) for child in coords]


def clean_geometry(geometry):
    """Permute les axes puis simplifie, selon le type de géométrie. Les types
    ponctuels ou linéaires ne sont pas simplifiés (aucun ne sort du WFS
    aujourd'hui, mais on ne les abîme pas s'ils apparaissent)."""
    gtype = geometry.get("type")
    coords = swap_round(geometry.get("coordinates"))
    if gtype == "Polygon":
        coords = dedupe_geometry(simplify_geometry(coords, 2), 2)
    elif gtype == "MultiPolygon":
        coords = dedupe_geometry(simplify_geometry(coords, 3), 3)
    return {"type": gtype, "coordinates": coords}


def geometry_positions(geometry):
    """Toutes les positions [lon, lat] d'une géométrie, à plat."""
    out = []

    def walk(node):
        if isinstance(node, list) and node:
            if isinstance(node[0], (int, float)):
                if len(node) >= 2:
                    out.append(node)
            else:
                for child in node:
                    walk(child)

    walk((geometry or {}).get("coordinates"))
    return out


def geometry_in_france(geometry):
    """True si la géométrie touche la métropole. On teste le barycentre des
    sommets puis, s'il tombe à l'eau (polygone concave, île, trait de côte),
    un échantillon de sommets — un périmètre frontalier reste ainsi retenu."""
    pos = geometry_positions(geometry)
    if not pos:
        return False
    lon_c = sum(p[0] for p in pos) / len(pos)
    lat_c = sum(p[1] for p in pos) / len(pos)
    if in_france(lon_c, lat_c):
        return True
    stride = max(1, len(pos) // 12)
    return any(in_france(p[0], p[1]) for p in pos[::stride])


def _effis_num(raw):
    try:
        return round(float(raw))
    except (TypeError, ValueError):
        return None


def _effis_day(raw):
    """'2026-02-24 12:01:00' -> '2026-02-24'. None si illisible."""
    if not isinstance(raw, str) or len(raw) < 10:
        return None
    day = raw[:10]
    return day if day[4] == "-" and day[7] == "-" else None


def _drain(feats):
    """Parcourt la liste en la vidant : chaque feature brute est libérée dès
    qu'elle est traitée, au lieu de garder tout le brut ET tout le simplifié en
    mémoire simultanément. L'ordre est inversé, sans conséquence : les
    périmètres sont tous tracés dans la même couleur, il n'y a pas d'ordre
    d'empilement à préserver."""
    while feats:
        yield feats.pop()


def build_burned():
    """Construit le GeoJSON des zones brûlées : périmètres datés de la saison
    en cours (filtrés sur COUNTRY=FR) + contours NRT plus frais (filtrés sur le
    polygone métropole, faute d'attributs). Une couche indisponible ne fait pas
    échouer le build tant que l'autre répond."""
    features, sources = [], []

    try:
        dated = effis_wfs(EFFIS_DATED_LAYER)
    except Exception:
        dated = None
    if dated:
        sources.append(EFFIS_DATED_LAYER)
        for f in _drain(dated):
            props = f.get("properties") or {}
            if (props.get("COUNTRY") or "").strip().upper() != "FR":
                continue
            geom = f.get("geometry") or {}
            if not geom.get("coordinates"):
                continue
            geom = clean_geometry(geom)
            area = _effis_num(props.get("AREA_HA"))
            commune = (props.get("COMMUNE") or "").strip() or None
            province = (props.get("PROVINCE") or "").strip() or None
            label = " ".join(p for p in [
                commune,
                f"({province})" if province else None,
                f"— {area} ha" if area else None,
            ] if p) or None
            features.append({
                "type": "Feature",
                "geometry": geom,
                "properties": {
                    "label": label,
                    "date": _effis_day(props.get("FIREDATE")),
                    "updated": _effis_day(props.get("LASTUPDATE")),
                    "area_ha": area,
                    "commune": commune,
                    "province": province,
                    "kind": "dated",
                },
            })

    dated = None

    try:
        nrt = effis_wfs(EFFIS_NRT_LAYER)
    except Exception:
        nrt = None
    if nrt:
        sources.append(EFFIS_NRT_LAYER)
        for f in _drain(nrt):
            geom = f.get("geometry") or {}
            if not geom.get("coordinates"):
                continue
            geom = clean_geometry(geom)
            if not geometry_in_france(geom):
                continue
            # Aucun attribut n'est exposé sur cette couche : pas de date, pas de
            # surface. On ne comble rien — les champs restent absents.
            features.append({
                "type": "Feature",
                "geometry": geom,
                "properties": {"kind": "nrt"},
            })

    nrt = None

    if len(features) < BURNED_MIN_FEATURES:
        raise ValueError(f"EFFIS : seulement {len(features)} périmètres, build écarté")
    # `source_url` / `source_label` sont identiques pour toutes les entités :
    # ils sont portés par la collection, pas répétés 4 000 fois (le client les
    # reconstruit par `kind`).
    return {
        "type": "FeatureCollection",
        "generator": BURNED_MARKER,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sources": sources,
        "source_url": EFFIS_VIEWER,
        "source_label": {"dated": "EFFIS / Copernicus", "nrt": "EFFIS / Copernicus (NRT)"},
        "features": features,
    }


def _previous_burned_count():
    """(count, was_ours) du fichier déjà en place. Le ratio de garde ne
    s'applique qu'à un fichier que nous avons nous-mêmes généré : le tout
    premier remplacement d'un jeu d'une autre provenance doit passer."""
    try:
        prev = json.loads(BURNED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return 0, False
    feats = prev.get("features")
    return (len(feats) if isinstance(feats, list) else 0,
            prev.get("generator") == BURNED_MARKER)


def write_burned(fc):
    """Écrit public/burned.geojson de façon atomique. Un build nettement plus
    pauvre que le précédent n'écrase pas l'acquis (leçon déjà apprise sur la
    couche « sensibles ») : on garde l'ancien et on retentera au cycle suivant."""
    prev_n, was_ours = _previous_burned_count()
    n = len(fc["features"])
    if was_ours and prev_n and n < prev_n * BURNED_KEEP_RATIO:
        raise ValueError(f"EFFIS : {n} périmètres contre {prev_n} en place, build écarté")
    tmp = BURNED_PATH.with_suffix(".geojson.tmp")
    tmp.write_text(json.dumps(fc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, BURNED_PATH)
    return n


def _burned_age():
    """Âge du fichier en place, en secondes. Infini s'il n'existe pas ou s'il
    vient d'ailleurs que de nous (fichier importé à la main)."""
    try:
        if not _previous_burned_count()[1]:
            return float("inf")
        return time.time() - BURNED_PATH.stat().st_mtime
    except Exception:
        return float("inf")


def burned_loop():
    """Régénère les périmètres EFFIS toutes les BURNED_TTL secondes.

    Le premier passage est sauté si le fichier livré avec le dépôt est encore
    dans sa fenêtre de fraîcheur. La simplification des périmètres est un gros
    calcul Python (plus de 300 000 sommets) : la lancer au démarrage, c'est-à-dire
    au moment précis où l'hébergeur vérifie la santé du service et où les caches
    sont encore vides, revient à saturer le seul cœur disponible quand on en a
    le plus besoin. Un déploiement embarque déjà un fichier à jour ; il n'y a
    rien à recalculer avant l'échéance normale."""
    while True:
        age = _burned_age()
        if age < BURNED_TTL:
            time.sleep(BURNED_TTL - age)
            continue
        try:
            with _heavy_lock:
                write_burned(build_burned())
        except Exception:
            pass
        time.sleep(BURNED_TTL)


# ---------------------------------------------------------------------------
# Suivi aérien — flotte Sécurité Civile (Canadair, Dash, hélicos Dragon…).
# Source ADS-B communautaire adsb.fi (sans quota). On identifie les aéronefs
# d'État français par immatriculation F-Z*. Traces GPS accumulées en mémoire
# sur une fenêtre glissante de 3 h (le poll de fond ajoute les positions).
# ---------------------------------------------------------------------------
# adsb.fi plafonne dist à 250 nm (~463 km) : centré sur la France, ça couvre
# la métropole continentale (la Corse tombe en limite sud-est, acceptable).
ADSB_URL = "https://opendata.adsb.fi/api/v2/lat/46.6/lon/2.5/dist/250"
AIRCRAFT_POLL = 45              # secondes entre deux relevés de positions
# Fenêtre glissante des traces. À 3 h, un Canadair en rotation accumulait des
# dizaines d'allers-retours sur le même axe : le trait se repliait sur lui-même
# et traversait la moitié du pays sans rien apprendre de plus. 1 h suffit à lire
# la noria en cours et à voir d'où vient l'appareil.
AIRCRAFT_TRACE_SEC = 3600
AIRCRAFT_MIN_MOVE = 0.0015      # ~150 m : on n'ajoute un point que s'il a bougé
# Callsigns Sécurité Civile connus, en secours si l'immat n'est pas diffusée.
AIRCRAFT_CALLSIGNS = ("PELICAN", "MILAN", "DRAGON", "BENGALE", "CANADAIR")
_aircraft_hist = {}             # hex -> {"info": {...}, "trace": [(t,lat,lon),...]}
_aircraft_lock = threading.Lock()


def _classify_aircraft(type_code, reg):
    """(kind, icon, category) selon le type ICAO. Canadair/Dash = bombardiers d'eau."""
    t = (type_code or "").upper()
    if t in ("CL2T", "CL41", "C415", "CL30"):
        return "Canadair (bombardier d'eau)", "🛩️", "canadair"
    if t == "DH8D":
        return "Dash 8 (bombardier d'eau)", "🛩️", "dash"
    if t in ("B350", "BE20", "TBM9", "PC12", "F406", "C208"):
        return "avion de liaison / reconnaissance", "✈️", "liaison"
    if t[:2] in ("EC", "AS", "H1", "H2", "SA", "AW") or t in ("B105", "B412"):
        return "hélicoptère", "🚁", "helico"
    return "aéronef d'État", "✈️", "autre"


def build_aircraft():
    """Relève les positions ADS-B, filtre la flotte Sécurité Civile (immat F-Z*
    ou callsign connu), met à jour les traces glissantes et renvoie l'instantané."""
    req = urllib.request.Request(ADSB_URL, headers={
        "User-Agent": "carte-incendies/1.0 (carte-incendies.fr)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    now = time.time()
    for a in (data.get("aircraft") or data.get("ac") or []):
        reg = (a.get("r") or "").upper()
        flight = (a.get("flight") or "").strip().upper()
        is_secu = reg.startswith("F-Z") or any(c in flight for c in AIRCRAFT_CALLSIGNS)
        if not is_secu:
            continue
        lat, lon = a.get("lat"), a.get("lon")
        if lat is None or lon is None:
            continue
        hexid = a.get("hex") or reg or flight
        kind, icon, category = _classify_aircraft(a.get("t"), reg)
        alt = a.get("alt_baro")
        info = {
            "hex": hexid, "reg": reg or None, "flight": flight or None,
            "type": a.get("t"), "kind": kind, "icon": icon, "category": category,
            "lat": round(lat, 5), "lon": round(lon, 5),
            "alt_ft": None if alt in (None, "ground") else alt,
            "track": a.get("track"),
            "gs_kt": a.get("gs"),
            "on_ground": alt == "ground",
        }
        with _aircraft_lock:
            rec = _aircraft_hist.get(hexid)
            if rec is None:
                rec = {"info": info, "trace": []}
                _aircraft_hist[hexid] = rec
            rec["info"] = info
            tr = rec["trace"]
            # N'ajoute un point que si l'appareil a bougé (trace lisible, mémoire bornée).
            if not tr or abs(tr[-1][1] - lat) > AIRCRAFT_MIN_MOVE or abs(tr[-1][2] - lon) > AIRCRAFT_MIN_MOVE:
                tr.append((now, round(lat, 5), round(lon, 5)))
    # Purge des positions et appareils hors fenêtre de 3 h.
    with _aircraft_lock:
        for hexid in list(_aircraft_hist.keys()):
            tr = [p for p in _aircraft_hist[hexid]["trace"] if now - p[0] <= AIRCRAFT_TRACE_SEC]
            if not tr:
                del _aircraft_hist[hexid]
            else:
                _aircraft_hist[hexid]["trace"] = tr
    return get_aircraft()


def get_aircraft():
    """Instantané : appareils courants + leur trace GPS (liste de [t_epoch_s, lat, lon])."""
    with _aircraft_lock:
        craft = []
        for rec in _aircraft_hist.values():
            info = dict(rec["info"])
            info["trace"] = [[int(p[0]), p[1], p[2]] for p in rec["trace"]]
            craft.append(info)
    return {
        "fetched_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "trace_hours": AIRCRAFT_TRACE_SEC // 3600,
        "count": len(craft),
        "aircraft": craft,
    }


def aircraft_loop():
    """Poll dédié (plus rapide que refresh_loop) pour des traces fluides."""
    while True:
        try:
            build_aircraft()
        except Exception:
            pass
        time.sleep(AIRCRAFT_POLL)


def refresh_loop():
    """Rafraîchit les caches en tâche de fond toutes les CACHE_TTL secondes :
    aucun visiteur n'attend jamais FIRMS (~plusieurs secondes) ni Open-Meteo.
    En cas d'échec d'un cycle, l'ancien cache reste servi (avec son
    fetched_at_utc d'origine — pas de fausse fraîcheur) et on réessaie au
    cycle suivant. Le cumul national EFFIS (hebdomadaire) n'est rafraîchi
    qu'une fois par heure."""
    last_national = 0.0
    while True:
        try:
            with _heavy_lock:
                _store_fires(build_payload())
        except Exception:
            pass
        try:
            _store_wind(build_wind())
        except Exception:
            pass
        if time.time() - last_national >= NATIONAL_TTL:
            try:
                _store_national(build_national())
                last_national = time.time()
            except Exception:
                pass
        time.sleep(CACHE_TTL)


# Fichiers statiques servis compressés (gzip mis en cache mémoire par mtime).
# Gros gains : index.html (~120 Ko) et burned.geojson (~3,3 Mo → ~15 %).
PUBLIC_DIR = (ROOT / "public").resolve()
COMPRESSIBLE = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".geojson": "application/geo+json",
    ".svg": "image/svg+xml",
}
# Binaires figés d'un déploiement à l'autre : cache navigateur long, pas même
# de revalidation. L'og-image seule pèse ~155 Ko à chaque partage social.
LONG_CACHE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".ico", ".woff2"}
_gz_cache = {}  # chemin résolu -> (mtime, bytes gzip)
_gz_lock = threading.Lock()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC_DIR), **kwargs)

    def end_headers(self):
        # Jamais de HTML/JS périmé côté navigateur : le fichier évolue souvent.
        # Les images, elles, ne changent qu'au déploiement : on les laisse en
        # cache long, sinon chaque visite repaie l'og-image (~155 Ko).
        if not self.path.startswith("/api/"):
            ext = os.path.splitext(urllib.parse.urlparse(self.path).path)[1].lower()
            if ext in LONG_CACHE_EXT:
                self.send_header("Cache-Control", "public, max-age=604800")
            else:
                self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _accepts_gzip(self):
        return "gzip" in (self.headers.get("Accept-Encoding") or "")

    def _not_modified(self, etag):
        """Renvoie True (et répond 304) si le client a déjà cette version.
        Un 304 pèse ~200 octets contre plusieurs centaines de Ko pour le corps :
        c'est le principal levier de réduction de la bande passante sortante."""
        inm = self.headers.get("If-None-Match") or ""
        if etag not in [t.strip().lstrip("W/") for t in inm.split(",")]:
            return False
        self.send_response(304)
        self.send_header("ETag", etag)
        self.send_header("Vary", "Accept-Encoding")
        self.end_headers()
        return True

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        etag = None
        if status == 200:
            etag = '"%s"' % hashlib.md5(body).hexdigest()
            if self._not_modified(etag):
                return
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        # no-cache (et non no-store) : le navigateur garde la réponse et la
        # revalide par If-None-Match, ce qui rend les 304 possibles.
        self.send_header("Cache-Control", "no-cache")
        if etag:
            self.send_header("ETag", etag)
        self.send_header("Vary", "Accept-Encoding")
        if status == 200 and len(body) > 860 and self._accepts_gzip():
            body = gzip.compress(body, 6)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _try_send_static_gzip(self):
        """Sert un fichier statique compressible en gzip. Renvoie True si servi."""
        if not self._accepts_gzip():
            return False
        path = urllib.parse.urlparse(self.path).path
        if path.endswith("/"):
            path += "index.html"
        ctype = COMPRESSIBLE.get(os.path.splitext(path)[1].lower())
        if ctype is None:
            return False
        try:
            fp = (PUBLIC_DIR / path.lstrip("/")).resolve()
        except OSError:
            return False
        if not str(fp).startswith(str(PUBLIC_DIR) + os.sep) or not fp.is_file():
            return False
        st = fp.stat()
        mtime = st.st_mtime
        key = str(fp)
        with _gz_lock:
            cached = _gz_cache.get(key)
        if cached is None or cached[0] != mtime:
            cached = (mtime, gzip.compress(fp.read_bytes(), 6))
            with _gz_lock:
                _gz_cache[key] = cached
        body = cached[1]
        # ETag sur (mtime, taille, taille gzip) : stable tant que le fichier ne
        # bouge pas, donc un visiteur qui revient ne retélécharge pas
        # burned.geojson (~450 Ko gzip) tant qu'EFFIS n'a pas republié.
        etag = '"%x-%x-%x"' % (int(mtime), st.st_size, len(body))
        if self._not_modified(etag):
            return True
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Encoding", "gzip")
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", self.date_time_string(int(mtime)))
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def do_GET(self):
        if self.path.startswith("/api/fires"):
            try:
                self._send_json(get_data())
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=502)
        elif self.path.startswith("/api/wind"):
            try:
                self._send_json(get_wind())
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=502)
        elif self.path.startswith("/api/national"):
            try:
                self._send_json(get_national())
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=502)
        elif self.path.startswith("/api/aircraft"):
            try:
                self._send_json(get_aircraft())
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=502)
        elif self.path.startswith("/api/situation"):
            # Relecture disque à chaque requête (pas de cache) : mise à jour à chaud.
            try:
                evacuations = json.loads((ROOT / "evacuations.json").read_text())
            except Exception:
                evacuations = None
            sit_path = ROOT / "situation.json"
            situation, zones = None, []
            if sit_path.exists():
                try:
                    situation = json.loads(sit_path.read_text())
                    zones = situation.get("zones", [])
                except Exception:
                    pass
            self._send_json({
                "evacuations": evacuations,
                "zones": zones,
                "zones_updated": situation.get("updated") if situation else None,
                "total_national_ha": situation.get("total_national_ha") if situation else None,
                "total_national_note": situation.get("total_national_note") if situation else None,
                "total_national_source": situation.get("total_national_source") if situation else None,
                "served_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
        else:
            if not self._try_send_static_gzip():
                super().do_GET()

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    # Préchauffe puis rafraîchit les caches en continu : le premier visiteur
    # après un déploiement n'attend plus la collecte FIRMS.
    threading.Thread(target=refresh_loop, daemon=True).start()
    threading.Thread(target=aircraft_loop, daemon=True).start()
    threading.Thread(target=burned_loop, daemon=True).start()
    print(f"Carte des feux : http://localhost:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
