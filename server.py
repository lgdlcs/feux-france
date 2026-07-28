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
    url = ("https://api.open-meteo.com/v1/forecast?latitude=" + lats +
           "&longitude=" + lons +
           "&current=wind_speed_10m,wind_direction_10m,wind_gusts_10m" +
           "&hourly=wind_speed_10m,wind_direction_10m,wind_gusts_10m" +
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


def refresh_loop():
    """Rafraîchit les caches en tâche de fond toutes les CACHE_TTL secondes :
    aucun visiteur n'attend jamais FIRMS (~plusieurs secondes) ni Open-Meteo.
    En cas d'échec d'un cycle, l'ancien cache reste servi (avec son
    fetched_at_utc d'origine — pas de fausse fraîcheur) et on réessaie au
    cycle suivant."""
    while True:
        try:
            _store_fires(build_payload())
        except Exception:
            pass
        try:
            _store_wind(build_wind())
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
    print(f"Carte des feux : http://localhost:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
