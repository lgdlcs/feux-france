#!/usr/bin/env python3
"""Serveur local pour la carte des incendies en France.

Récupère les détections thermiques satellites NASA FIRMS (flux publics 24h,
mise à jour continue, sans clé API), filtre les points situés sur le
territoire métropolitain (polygone précis, pas un simple rectangle) et les
sert en JSON au frontend. Cache de 10 minutes pour ne pas surcharger FIRMS.
"""

import csv
import gzip
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

FEEDS = [
    {
        "id": "viirs_snpp",
        "label": "VIIRS Suomi-NPP (375 m)",
        "url": "https://firms.modaps.eosdis.nasa.gov/data/active_fire/suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2_Europe_24h.csv",
    },
    {
        "id": "viirs_noaa20",
        "label": "VIIRS NOAA-20 (375 m)",
        "url": "https://firms.modaps.eosdis.nasa.gov/data/active_fire/noaa-20-viirs-c2/csv/J1_VIIRS_C2_Europe_24h.csv",
    },
    {
        "id": "viirs_noaa21",
        "label": "VIIRS NOAA-21 (375 m)",
        "url": "https://firms.modaps.eosdis.nasa.gov/data/active_fire/noaa-21-viirs-c2/csv/J2_VIIRS_C2_Europe_24h.csv",
    },
    {
        "id": "modis",
        "label": "MODIS Aqua/Terra (1 km)",
        "url": "https://firms.modaps.eosdis.nasa.gov/data/active_fire/modis-c6.1/csv/MODIS_C6_1_Europe_24h.csv",
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
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def in_france(lon, lat):
    return any(point_in_ring(lon, lat, r) for r in FRANCE_RINGS)


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
    req = urllib.request.Request(feed["url"], headers={"User-Agent": "feux-france-local/1.0"})
    with urllib.request.urlopen(req, timeout=45) as resp:
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
        points.append({
            "lat": lat,
            "lon": lon,
            "acq_utc": f"{row['acq_date']}T{acq_time[:2]}:{acq_time[2:]}:00Z",
            "frp": float(row.get("frp") or 0),
            "confidence": normalize_confidence(str(row.get("confidence", "")).strip().lower()),
            "satellite": feed["label"],
            "source": feed["id"],
            "daynight": row.get("daynight", ""),
        })
    return points


def _parse_utc(s):
    """'2026-07-26T14:30:00Z' -> epoch (float). Renvoie 0 si illisible."""
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
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
        req = urllib.request.Request(url, headers={"User-Agent": "feux-france-local/1.0"})
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
        foyers.append({
            "id": fid,
            "lat": round(lat_c, 5),
            "lon": round(lon_c, 5),
            "n": n,
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
        "window_hours": 24,
        "feeds": feeds_status,
        "count": len(all_points),
        "points": all_points,
        "foyers": foyers,
    }


def _store_fires(data):
    with _lock:
        _cache["data"] = data
        _cache["ts"] = time.time()


def get_data():
    """Sert le cache tel quel (le thread d'arrière-plan tient la fraîcheur).
    Ne construit en direct que si le cache est encore vide (tout premier
    instant après le démarrage) — jamais de donnée périmée au-delà du cycle
    de rafraîchissement, jamais d'attente FIRMS pour un visiteur."""
    with _lock:
        data = _cache["data"]
    if data is not None:
        return data
    data = build_payload()
    _store_fires(data)
    return data


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
    req = urllib.request.Request(url, headers={"User-Agent": "feux-france-local/1.0"})
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


def get_wind():
    with _wind_lock:
        data = _wind_cache["data"]
    if data is not None:
        return data
    data = build_wind()
    _store_wind(data)
    return data


# ---------------------------------------------------------------------------
# Cumul national de surface brûlée (année en cours) via EFFIS / Copernicus.
# Source officielle européenne (le portail effis.statistics s'appuie dessus).
# Les valeurs 'area_ha' sont les surfaces cartographiées par satellite semaine
# par semaine ; on somme celles des semaines déjà écoulées pour le cumul YTD.
# NB : EFFIS ne cartographie que les feux ≳30 ha et avec latence — ce cumul
# SOUS-ESTIME le total national réel (stats pompiers), d'où la note côté front.
# Donnée hebdomadaire : rafraîchie une fois par heure, pas toutes les 10 min.
# ---------------------------------------------------------------------------
EFFIS_URL = "https://api2.effis.emergency.copernicus.eu/statistics/v2/effis/weekly?country=FRA"
NATIONAL_TTL = 3600  # secondes
_national_cache = {"data": None, "ts": 0}
_national_lock = threading.Lock()


def build_national():
    """Cumul YTD (ha) et nb d'événements pour la France depuis EFFIS.
    Ne somme que les semaines dont la date est déjà passée (les entrées
    futures sont des placeholders à ignorer)."""
    req = urllib.request.Request(EFFIS_URL, headers={"User-Agent": "feux-france-local/1.0"})
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


def get_national():
    with _national_lock:
        data = _national_cache["data"]
    if data is not None:
        return data
    data = build_national()
    _store_national(data)
    return data


# ---------------------------------------------------------------------------
# Établissements sensibles autour des foyers actifs : santé (hôpitaux, cliniques,
# Ehpad via OSM/Overpass) + sites Seveso (API Géorisques). On n'interroge QUE
# le voisinage des foyers actifs (rayon SENSIBLE_RADIUS_KM) : le layer se peuple
# là où il y a des feux, jamais toute la France. Cache dédié, rafraîchi comme
# les foyers. Échec d'une source (Overpass surchargé…) : on sert ce qu'on a.
# ---------------------------------------------------------------------------
SENSIBLE_RADIUS_KM = 12          # rayon d'analyse autour de chaque centre de requête
SENSIBLE_TTL = 1800              # 30 min : ces établissements ne bougent pas vite
SENSIBLE_GRID = 0.2              # ~20 km : regroupe les foyers d'un même sinistre
SENSIBLE_MAX_CENTERS = 8         # plafond d'appels externes par cycle (latence bornée)
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
GEORISQUES_URL = "https://georisques.gouv.fr/api/v1/installations_classees"
_sensible_cache = {"data": None, "ts": 0}
_sensible_lock = threading.Lock()


def _dedup_key(lat, lon):
    """Cellule ~150 m pour dédoublonner des établissements vus depuis
    plusieurs foyers proches."""
    return (round(lat, 3), round(lon, 3))


def fetch_health_around(lat, lon):
    """Établissements de santé OSM (hôpitaux, cliniques, Ehpad) dans un rayon
    autour de (lat, lon). node+way+relation ; 'out center' donne un centroïde
    pour les emprises. Renvoie une liste de dicts."""
    radius = int(SENSIBLE_RADIUS_KM * 1000)
    q = (
        "[out:json][timeout:25];"
        "nwr[\"amenity\"~\"^(hospital|clinic)$\"](around:%d,%f,%f);"
        "nwr[\"amenity\"=\"social_facility\"][\"social_facility\"~\"nursing_home|assisted_living|group_home\"](around:%d,%f,%f);"
        "out center 60;"
    ) % (radius, lat, lon, radius, lat, lon)
    url = OVERPASS_URL + "?data=" + urllib.parse.quote(q)
    req = urllib.request.Request(url, headers={
        "User-Agent": "feux-france-local/1.0",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=40) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out = []
    for el in data.get("elements", []):
        t = el.get("tags", {})
        plat = el.get("lat") if el.get("lat") is not None else (el.get("center") or {}).get("lat")
        plon = el.get("lon") if el.get("lon") is not None else (el.get("center") or {}).get("lon")
        if plat is None or plon is None:
            continue
        am = t.get("amenity")
        if am == "hospital":
            kind, icon = "hôpital", "🏥"
        elif am == "clinic":
            kind, icon = "clinique", "🏥"
        else:
            kind, icon = "Ehpad / foyer médicalisé", "🧓"
        out.append({
            "cat": "sante",
            "kind": kind,
            "icon": icon,
            "nom": t.get("name") or kind.capitalize(),
            "lat": round(plat, 5),
            "lon": round(plon, 5),
        })
    return out


def fetch_seveso_around(lat, lon):
    """Sites Seveso (installations classées) dans un rayon autour de (lat, lon)
    via l'API Géorisques. On ne garde que les statuts Seveso (seuil bas/haut)."""
    radius = int(SENSIBLE_RADIUS_KM * 1000)
    qs = urllib.parse.urlencode({
        "rayon": radius,
        "latlon": f"{lon},{lat}",   # Géorisques attend lon,lat
        "page": 1,
        "page_size": 100,
    })
    url = f"{GEORISQUES_URL}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "feux-france-local/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out = []
    for it in (data.get("data") or []):
        statut = it.get("statutSeveso") or ""
        # Ne garder que les vrais Seveso : l'API renvoie aussi "Non Seveso".
        if "seuil" not in statut.lower():
            continue
        plat, plon = it.get("latitude"), it.get("longitude")
        if plat is None or plon is None:
            continue
        out.append({
            "cat": "seveso",
            "kind": statut,
            "icon": "☣️",
            "nom": (it.get("raisonSociale") or "Site industriel").strip(),
            "commune": it.get("commune"),
            "lat": round(float(plat), 5),
            "lon": round(float(plon), 5),
        })
    return out


def _query_centers(active):
    """Regroupe les foyers actifs proches en quelques centres de requête.
    Les feux d'un même sinistre (ex. 23 foyers girondins) se recouvrent
    dans le rayon SENSIBLE_RADIUS_KM : inutile de lancer 23 requêtes quasi
    identiques. On grille sur ~SENSIBLE_GRID° et on garde le centroïde de
    chaque cellule occupée, borné à SENSIBLE_MAX_CENTERS."""
    cells = {}
    for f in active:
        lat, lon = f.get("lat"), f.get("lon")
        if lat is None or lon is None:
            continue
        c = (round(lat / SENSIBLE_GRID), round(lon / SENSIBLE_GRID))
        cells.setdefault(c, []).append((lat, lon))
    centers = []
    for pts in cells.values():
        n = len(pts)
        centers.append((sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n, n))
    # Priorité aux zones qui concentrent le plus de foyers.
    centers.sort(key=lambda c: c[2], reverse=True)
    return [(lat, lon) for lat, lon, _ in centers[:SENSIBLE_MAX_CENTERS]]


def build_sensibles():
    """Agrège les établissements sensibles autour des foyers actifs du cache.
    Ne lève pas si une source échoue : on renvoie ce qui a pu être collecté.
    Regroupe d'abord les foyers proches pour limiter les appels externes."""
    with _lock:
        fires = _cache["data"]
    foyers = (fires or {}).get("foyers", []) if fires else []
    active = [f for f in foyers if f.get("active")]
    centers = _query_centers(active)

    seen = set()
    items = []
    for lat, lon in centers:
        for fetch in (fetch_health_around, fetch_seveso_around):
            try:
                for it in fetch(lat, lon):
                    k = (it["cat"], _dedup_key(it["lat"], it["lon"]))
                    if k in seen:
                        continue
                    seen.add(k)
                    items.append(it)
            except Exception:
                # Source momentanément indisponible (Overpass 504, etc.) :
                # on n'invente rien, on garde les autres résultats.
                pass

    return {
        "fetched_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "radius_km": SENSIBLE_RADIUS_KM,
        "centers": len(centers),
        "count": len(items),
        "items": items,
    }


def _store_sensibles(data):
    with _sensible_lock:
        _sensible_cache["data"] = data
        _sensible_cache["ts"] = time.time()


def get_sensibles():
    """Sert le cache tel quel. Ne construit JAMAIS en direct : la collecte
    (jusqu'à SENSIBLE_MAX_CENTERS × 2 appels externes lents) est faite par le
    thread de fond. Cache encore vide (juste après démarrage) → réponse vide
    immédiate plutôt qu'un visiteur qui attend des dizaines de secondes."""
    with _sensible_lock:
        data = _sensible_cache["data"]
    if data is not None:
        return data
    return {
        "fetched_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "radius_km": SENSIBLE_RADIUS_KM,
        "centers": 0,
        "count": 0,
        "items": [],
        "warming": True,   # collecte en cours côté serveur
    }


# ---------------------------------------------------------------------------
# Établissements sensibles PAR EMPRISE (bbox de la carte visible).
# Complète la collecte autour des foyers : l'utilisateur veut voir les zones
# sensibles même sans feu à proximité. Requêté à la demande selon la vue carte,
# avec un cache court par cellule de bbox et un plafond de résultats.
# ---------------------------------------------------------------------------
SENSIBLE_BBOX_MAX_DEG = 4.0     # au-delà (dézoom large), on refuse : trop de points
SENSIBLE_BBOX_TTL = 600         # 10 min de cache par cellule de bbox
SENSIBLE_BBOX_MAX_ITEMS = 800   # plafond de points renvoyés d'un coup
_sensible_bbox_cache = {}       # clé bbox arrondie -> (ts, data)
_sensible_bbox_building = set() # clés en cours de construction (anti-doublon)
_sensible_bbox_lock = threading.Lock()


def fetch_health_bbox(s, w, n, e):
    """Établissements de santé OSM (hôpitaux, cliniques, Ehpad) dans une bbox
    Overpass (south, west, north, east)."""
    bbox = "%f,%f,%f,%f" % (s, w, n, e)
    q = (
        "[out:json][timeout:25];("
        "nwr[\"amenity\"~\"^(hospital|clinic)$\"](%s);"
        "nwr[\"amenity\"=\"social_facility\"][\"social_facility\"~\"nursing_home|assisted_living|group_home\"](%s);"
        ");out center %d;"
    ) % (bbox, bbox, SENSIBLE_BBOX_MAX_ITEMS)
    url = OVERPASS_URL + "?data=" + urllib.parse.quote(q)
    req = urllib.request.Request(url, headers={
        "User-Agent": "feux-france-local/1.0",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=40) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out = []
    for el in data.get("elements", []):
        t = el.get("tags", {})
        plat = el.get("lat") if el.get("lat") is not None else (el.get("center") or {}).get("lat")
        plon = el.get("lon") if el.get("lon") is not None else (el.get("center") or {}).get("lon")
        if plat is None or plon is None:
            continue
        am = t.get("amenity")
        if am == "hospital":
            kind, icon = "hôpital", "🏥"
        elif am == "clinic":
            kind, icon = "clinique", "🏥"
        else:
            kind, icon = "Ehpad / foyer médicalisé", "🧓"
        out.append({
            "cat": "sante", "kind": kind, "icon": icon,
            "nom": t.get("name") or kind.capitalize(),
            "lat": round(plat, 5), "lon": round(plon, 5),
        })
    return out


GEORISQUES_RADIUS_M = 15000      # rayon max fiable de l'API (500 au-delà de ~20 km)
GEORISQUES_MAX_TILES = 12        # plafond d'appels Géorisques par bbox (latence bornée)


def _fetch_seveso_circle(clat, clon):
    """Sites Seveso dans un cercle Géorisques (rayon GEORISQUES_RADIUS_M)."""
    qs = urllib.parse.urlencode({
        "rayon": GEORISQUES_RADIUS_M, "latlon": f"{clon},{clat}",
        "page": 1, "page_size": 100,
    })
    req = urllib.request.Request(f"{GEORISQUES_URL}?{qs}",
                                 headers={"User-Agent": "feux-france-local/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out = []
    for it in (data.get("data") or []):
        statut = it.get("statutSeveso") or ""
        if "seuil" not in statut.lower():
            continue
        plat, plon = it.get("latitude"), it.get("longitude")
        if plat is None or plon is None:
            continue
        out.append({
            "cat": "seveso", "kind": statut, "icon": "☣️",
            "nom": (it.get("raisonSociale") or "Site industriel").strip(),
            "commune": it.get("commune"),
            "lat": round(float(plat), 5), "lon": round(float(plon), 5),
        })
    return out


def fetch_seveso_bbox(s, w, n, e):
    """Sites Seveso dans une bbox. Géorisques ne prend que rayon+latlon (≤~20 km),
    donc on carrèle la bbox en cercles de 15 km (carré inscrit ~21 km de côté),
    plafonné à GEORISQUES_MAX_TILES pour borner la latence. Points reclippés
    sur la bbox stricte et dédoublonnés."""
    step_lat = (GEORISQUES_RADIUS_M * math.sqrt(2)) / 111_320.0
    clat0 = (s + n) / 2.0
    step_lon = (GEORISQUES_RADIUS_M * math.sqrt(2)) / (111_320.0 * math.cos(math.radians(clat0)) or 1.0)
    lats, lat = [], s + step_lat / 2.0
    while lat < n + step_lat / 2.0:
        lats.append(min(lat, n))
        lat += step_lat
    lons, lon = [], w + step_lon / 2.0
    while lon < e + step_lon / 2.0:
        lons.append(min(lon, e))
        lon += step_lon
    centers = [(la, lo) for la in lats for lo in lons][:GEORISQUES_MAX_TILES]
    # Cercles interrogés en parallèle : sinon 12 × ~30 s en série feraient
    # exploser le temps de build. Chaque cercle en échec est simplement ignoré.
    circle_results = [None] * len(centers)

    def _one(i, clat, clon):
        try:
            circle_results[i] = _fetch_seveso_circle(clat, clon)
        except Exception:
            circle_results[i] = []

    ths = [threading.Thread(target=_one, args=(i, cla, clo))
           for i, (cla, clo) in enumerate(centers)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    out, seen = [], set()
    for res in circle_results:
        for it in (res or []):
            if not (s <= it["lat"] <= n and w <= it["lon"] <= e):
                continue
            k = _dedup_key(it["lat"], it["lon"])
            if k in seen:
                continue
            seen.add(k)
            out.append(it)
    return out


def _build_sensibles_bbox(key, s, w, n, e):
    """Construit (lentement) les sensibles d'une bbox et remplit le cache.
    Exécuté en arrière-plan : Overpass (~14 s) et Géorisques carrelé peuvent
    prendre plusieurs dizaines de secondes, jamais dans le fil d'une requête."""
    results = {}

    def _run(name, fn):
        try:
            results[name] = fn(s, w, n, e)
        except Exception:
            results[name] = []  # source indisponible : on garde le reste

    threads = [threading.Thread(target=_run, args=(nm, fn))
               for nm, fn in (("sante", fetch_health_bbox), ("seveso", fetch_seveso_bbox))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    seen, items = set(), []
    for it in results.get("sante", []) + results.get("seveso", []):
        k = (it["cat"], _dedup_key(it["lat"], it["lon"]))
        if k in seen:
            continue
        seen.add(k)
        items.append(it)
        if len(items) >= SENSIBLE_BBOX_MAX_ITEMS:
            break
    data = {
        "fetched_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "bbox": [s, w, n, e], "count": len(items), "items": items,
    }
    with _sensible_bbox_lock:
        # Bornage mémoire simple : purge si le cache enfle trop.
        if len(_sensible_bbox_cache) > 200:
            _sensible_bbox_cache.clear()
        _sensible_bbox_cache[key] = (time.time(), data)
        _sensible_bbox_building.discard(key)


def get_sensibles_bbox(s, w, n, e):
    """Sensibles dans la vue carte, en cache-puis-sert : ne bloque JAMAIS le
    visiteur. Refuse les emprises trop larges (dézoom). Si la cellule de bbox
    n'est pas encore en cache (ou est périmée), on lance une construction en
    arrière-plan et on répond immédiatement (données périmées si dispo, sinon
    `building:true`). Le front repolle jusqu'à obtenir les points."""
    if (n - s) > SENSIBLE_BBOX_MAX_DEG or (e - w) > SENSIBLE_BBOX_MAX_DEG:
        return {"too_wide": True, "count": 0, "items": [],
                "max_deg": SENSIBLE_BBOX_MAX_DEG}
    key = (round(s, 1), round(w, 1), round(n, 1), round(e, 1))
    now = time.time()
    with _sensible_bbox_lock:
        hit = _sensible_bbox_cache.get(key)
        fresh = hit and now - hit[0] < SENSIBLE_BBOX_TTL
        if fresh:
            return hit[1]
        # Pas frais : déclenche un build en fond si aucun n'est déjà en cours.
        launch = key not in _sensible_bbox_building
        if launch:
            _sensible_bbox_building.add(key)
        stale = hit[1] if hit else None
    if launch:
        threading.Thread(target=_build_sensibles_bbox,
                         args=(key, s, w, n, e), daemon=True).start()
    if stale is not None:
        # Sert les données périmées immédiatement pendant que le build tourne.
        return {**stale, "building": True}
    # Rien en cache encore : réponse immédiate, le front repollera.
    return {"building": True, "count": 0, "items": [], "bbox": [s, w, n, e]}


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
AIRCRAFT_TRACE_SEC = 3 * 3600   # fenêtre glissante des traces : 3 h
AIRCRAFT_MIN_MOVE = 0.0015      # ~150 m : on n'ajoute un point que s'il a bougé
# Callsigns Sécurité Civile connus, en secours si l'immat n'est pas diffusée.
AIRCRAFT_CALLSIGNS = ("PELICAN", "MILAN", "DRAGON", "BENGALE", "CANADAIR")
_aircraft_hist = {}             # hex -> {"info": {...}, "trace": [(t,lat,lon),...]}
_aircraft_lock = threading.Lock()


def _classify_aircraft(type_code, reg):
    """(kind, icon) selon le type ICAO. Canadair/Dash = bombardiers d'eau."""
    t = (type_code or "").upper()
    if t in ("CL2T", "CL41", "C415", "CL30"):
        return "Canadair (bombardier d'eau)", "🛩️"
    if t == "DH8D":
        return "Dash 8 (bombardier d'eau)", "🛩️"
    if t in ("B350", "BE20", "TBM9", "PC12", "F406", "C208"):
        return "avion de liaison / reconnaissance", "✈️"
    if t[:2] in ("EC", "AS", "H1", "H2", "SA", "AW") or t in ("B105", "B412"):
        return "hélicoptère", "🚁"
    return "aéronef d'État", "✈️"


def build_aircraft():
    """Relève les positions ADS-B, filtre la flotte Sécurité Civile (immat F-Z*
    ou callsign connu), met à jour les traces glissantes et renvoie l'instantané."""
    req = urllib.request.Request(ADSB_URL, headers={
        "User-Agent": "feux-france/1.0 (carte-incendies.fr)",
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
        kind, icon = _classify_aircraft(a.get("t"), reg)
        alt = a.get("alt_baro")
        info = {
            "hex": hexid, "reg": reg or None, "flight": flight or None,
            "type": a.get("t"), "kind": kind, "icon": icon,
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
    """Instantané : appareils courants + leur trace GPS (liste de [lat,lon])."""
    with _aircraft_lock:
        craft = []
        for rec in _aircraft_hist.values():
            info = dict(rec["info"])
            info["trace"] = [[p[1], p[2]] for p in rec["trace"]]
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
    qu'une fois par heure ; les établissements sensibles toutes les 30 min."""
    last_national = 0.0
    last_sensibles = 0.0
    while True:
        try:
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
        if time.time() - last_sensibles >= SENSIBLE_TTL:
            try:
                _store_sensibles(build_sensibles())
                last_sensibles = time.time()
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
_gz_cache = {}  # chemin résolu -> (mtime, bytes gzip)
_gz_lock = threading.Lock()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC_DIR), **kwargs)

    def end_headers(self):
        # Jamais de HTML/JS périmé côté navigateur : le fichier évolue souvent.
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _accepts_gzip(self):
        return "gzip" in (self.headers.get("Accept-Encoding") or "")

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
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
        mtime = fp.stat().st_mtime
        key = str(fp)
        with _gz_lock:
            cached = _gz_cache.get(key)
        if cached is None or cached[0] != mtime:
            cached = (mtime, gzip.compress(fp.read_bytes(), 6))
            with _gz_lock:
                _gz_cache[key] = cached
        body = cached[1]
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Encoding", "gzip")
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
        elif self.path.startswith("/api/sensibles"):
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                bbox = qs.get("bbox", [None])[0]
                if bbox:
                    # Emprise carte : bbox=lonW,latS,lonE,latN → get_sensibles_bbox(s,w,n,e)
                    w, s, e, n = (float(x) for x in bbox.split(","))
                    self._send_json(get_sensibles_bbox(s, w, n, e))
                else:
                    self._send_json(get_sensibles())
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
    print(f"Carte des feux : http://localhost:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
