# =============================================================================
#  LANDSLIDE SUSCEPTIBILITY GRID  -  Dima Hasao District, Assam, India
# =============================================================================
#  Single-file Google Colab script. Free, key-less, account-less APIs only:
#    * Open-Meteo Elevation API        -> terrain
#    * Overpass API / OpenStreetMap    -> streams, roads, villages
#    * NASA COOLR ArcGIS REST          -> landslide inventory (best effort)
#
#  Outputs
#    data/output/dummy_grid.geojson  <- written FIRST, within seconds
#    data/output/risk_grid.geojson   <- the real product
#
#  Nothing here is allowed to kill the run: every network step is wrapped and
#  degrades to a documented fallback.
#
#  Run:  paste into one Colab cell and execute.
# =============================================================================

import os
import json
import math
import time
import random
import hashlib
import subprocess
import urllib.parse
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
LAT_MIN, LAT_MAX = 25.00, 25.80
LON_MIN, LON_MAX = 92.60, 93.40

CELL_DEG   = 0.02      # grid spacing / polygon edge length
NB_OFFSET  = 0.002     # neighbour offset used for the terrain gradient
M_PER_DEG  = 111320.0  # metres per degree of latitude

ELEV_BATCH   = 100     # Open-Meteo hard limit: 100 coordinate pairs / request
ELEV_SLEEP   = 0.4     # politeness pause between batches (seconds)
ELEV_RETRIES = 3

COOLR_MIN_POINTS = 40  # below this, the ML branch is not attempted
COOLR_POS_RADIUS_M = 2000.0
AUC_THRESHOLD = 0.70

OUT_DIR    = os.path.join("data", "output")
RISK_PATH  = os.path.join(OUT_DIR, "risk_grid.geojson")
DUMMY_PATH = os.path.join(OUT_DIR, "dummy_grid.geojson")

# Exact property order the frontend contract expects.
PROPERTY_ORDER = [
    "cell_id", "lat", "lon", "elevation_m", "slope_deg", "aspect_deg",
    "relief_m", "dist_stream_m", "dist_road_m", "susceptibility",
    "suscept_class", "nearest_village", "population", "population_estimated",
]

CLASS_LABELS = ["Very Low", "Low", "Moderate", "High", "Very High"]

# Set when a batch has to fall back to Open-Elevation; reported in the metadata.
ELEV_FALLBACK_USED = False

os.makedirs(OUT_DIR, exist_ok=True)


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def log(step, msg):
    print("[%s] %-8s %s" % (datetime.now().strftime("%H:%M:%S"), step, msg), flush=True)


def warn(step, msg):
    print("[%s] %-8s !! %s" % (datetime.now().strftime("%H:%M:%S"), step, msg), flush=True)


def stable_hash(text):
    """Deterministic stand-in for Python's per-process-randomised hash()."""
    return int(hashlib.md5(str(text).encode("utf-8")).hexdigest(), 16)


def _round(value, ndigits):
    """Round, but keep JSON null for missing / non-finite values."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, ndigits)


def r2(v):
    return _round(v, 2)


def r4(v):
    return _round(v, 4)


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres. Scalars or numpy arrays."""
    R = 6371000.0
    p1 = np.radians(np.asarray(lat1, dtype="float64"))
    p2 = np.radians(np.asarray(lat2, dtype="float64"))
    dp = p2 - p1
    dl = np.radians(np.asarray(lon2, dtype="float64") - np.asarray(lon1, dtype="float64"))
    a = np.sin(dp / 2.0) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2.0) ** 2
    return 2.0 * R * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def square_polygon(lat, lon, size=CELL_DEG):
    """Closed square ring of `size` degrees centred on (lat, lon)."""
    h = size / 2.0
    w, e = lon - h, lon + h
    s, n = lat - h, lat + h
    return [[[round(w, 6), round(s, 6)],
             [round(e, 6), round(s, 6)],
             [round(e, 6), round(n, 6)],
             [round(w, 6), round(n, 6)],
             [round(w, 6), round(s, 6)]]]


def build_feature(props):
    """One GeoJSON Feature, properties in the exact contract order."""
    ordered = {k: props.get(k) for k in PROPERTY_ORDER}
    return {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": square_polygon(props["lat"], props["lon"]),
        },
        "properties": ordered,
    }


def write_geojson(path, features, metadata):
    fc = {"type": "FeatureCollection", "metadata": metadata, "features": features}
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(fc, fh, ensure_ascii=False)
    log("EXPORT", "wrote %s  (%d features, %.1f KB)"
        % (path, len(features), os.path.getsize(path) / 1024.0))
    return path


def classify_quantile(values):
    """Five quantile bins. rank(first) breaks ties so the bins never collapse
    on a plateau of identical scores."""
    s = pd.Series(values, dtype="float64")
    if s.notna().sum() == 0:
        return [CLASS_LABELS[0]] * len(s)
    try:
        cats = pd.qcut(s.rank(method="first"), 5, labels=CLASS_LABELS)
        return [str(c) for c in cats]
    except Exception:
        order = s.rank(method="first").to_numpy()
        idx = np.clip(((order - 1) / max(len(s), 1) * 5).astype(int), 0, 4)
        return [CLASS_LABELS[i] for i in idx]


def minmax(arr):
    """Min-max normalise to 0..1; constant or empty input -> 0.5 / 0.0."""
    a = np.asarray(arr, dtype="float64")
    out = np.zeros_like(a)
    finite = np.isfinite(a)
    if not finite.any():
        return out
    lo = float(a[finite].min())
    hi = float(a[finite].max())
    if hi - lo < 1e-12:
        out[finite] = 0.5
        return out
    out[finite] = (a[finite] - lo) / (hi - lo)
    return out


VILLAGE_POOL = [
    "Haflong", "Maibang", "Umrangso", "Harangajao", "Mahur", "Jatinga",
    "Diyungbra", "Langting", "Dehangi", "New Sangbar", "Boro Nogaon",
    "Wajao", "Digrik", "Samparidisa", "Thaijuwari", "Gunjung", "Laisong",
]


# =============================================================================
# STEP 8 (RUNS FIRST) - Dummy generator for the frontend team
# =============================================================================
def make_dummy_grid(n=60, path=DUMMY_PATH, seed=42):
    """Synthetic grid with the identical schema, written in milliseconds so the
    frontend has something to render before any API is touched."""
    rng = random.Random(seed)
    feats = []
    for i in range(n):
        lat = round(rng.uniform(LAT_MIN + 0.01, LAT_MAX - 0.01), 4)
        lon = round(rng.uniform(LON_MIN + 0.01, LON_MAX - 0.01), 4)
        village = rng.choice(VILLAGE_POOL)
        estimated = rng.random() < 0.7
        population = (800 + stable_hash(village) % 3000) if estimated \
            else rng.randint(400, 12000)
        feats.append(build_feature({
            "cell_id": "DM_%04d" % (i + 1),
            "lat": r2(lat),
            "lon": r2(lon),
            "elevation_m": r2(rng.uniform(120, 1850)),
            "slope_deg": r2(min(65.0, abs(rng.gauss(14, 9)))),
            "aspect_deg": r2(rng.uniform(0, 360)),
            "relief_m": r2(abs(rng.gauss(45, 30))),
            "dist_stream_m": r2(rng.uniform(20, 4200)),
            "dist_road_m": r2(rng.uniform(50, 9000)),
            "susceptibility": r4(min(0.999, max(0.001, rng.betavariate(2, 2)))),
            "suscept_class": None,          # filled below
            "nearest_village": village,
            "population": int(population),
            "population_estimated": bool(estimated),
        }))

    classes = classify_quantile([f["properties"]["susceptibility"] for f in feats])
    for feat, cls in zip(feats, classes):
        feat["properties"]["suscept_class"] = cls

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "DUMMY",
        "auc": None,
        "cell_count": len(feats),
        "data_sources": ["synthetic - placeholder for frontend development"],
    }
    return write_geojson(path, feats, meta)


# =============================================================================
# STEP 1 - Grid
# =============================================================================
def build_grid():
    lats = np.arange(LAT_MIN + CELL_DEG / 2.0, LAT_MAX, CELL_DEG)
    lons = np.arange(LON_MIN + CELL_DEG / 2.0, LON_MAX, CELL_DEG)
    rows = []
    n = 0
    for la in lats:
        for lo in lons:
            n += 1
            rows.append({
                "cell_id": "DH_%04d" % n,
                "lat": float(round(la, 6)),
                "lon": float(round(lo, 6)),
            })
    df = pd.DataFrame(rows)
    log("STEP 1", "grid built: %d rows x %d cols = %d cells @ %.2f deg"
        % (len(lats), len(lons), len(df), CELL_DEG))
    return df


# =============================================================================
# STEP 2 - Elevation and terrain
# =============================================================================
def _open_elevation_batch(chunk):
    """Fallback source, also key-less. Only used for a batch Open-Meteo refused
    (its hourly cap is per-IP, and Colab IPs are shared)."""
    resp = requests.post(
        "https://api.open-elevation.com/api/v1/lookup",
        json={"locations": [{"latitude": p[0], "longitude": p[1]} for p in chunk]},
        timeout=120,
    )
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if len(results) != len(chunk):
        raise ValueError("expected %d elevations, got %d" % (len(chunk), len(results)))
    return [r.get("elevation") for r in results]


def fetch_elevations(points, batch=ELEV_BATCH, sleep=ELEV_SLEEP, retries=ELEV_RETRIES):
    """points: list of (lat, lon). Returns list of float|None in the same order.
    Open-Meteo takes at most 100 coordinate pairs per request; a batch that
    fails all its retries falls back to Open-Elevation, and if that fails too it
    yields None and the run continues."""
    out = [None] * len(points)
    n_batches = int(math.ceil(len(points) / float(batch)))
    failed = 0
    fallback_used = 0
    rate_limited = 0
    prefer_fallback = False     # flips once Open-Meteo is clearly capped

    for b in range(n_batches):
        chunk = points[b * batch:(b + 1) * batch]
        values = None

        if not prefer_fallback:
            query = urllib.parse.urlencode({
                "latitude": ",".join("%.6f" % p[0] for p in chunk),
                "longitude": ",".join("%.6f" % p[1] for p in chunk),
            })
            url = "https://api.open-meteo.com/v1/elevation?" + query
            hit_429 = False
            for attempt in range(1, retries + 1):
                try:
                    resp = requests.get(url, timeout=60)
                    if resp.status_code == 429:
                        hit_429 = True
                        raise RuntimeError("429 hourly rate limit for this IP")
                    resp.raise_for_status()
                    elev = resp.json().get("elevation")
                    if not isinstance(elev, list) or len(elev) != len(chunk):
                        raise ValueError("expected %d elevations, got %r"
                                         % (len(chunk), type(elev)))
                    values = elev
                    break
                except Exception as exc:
                    warn("STEP 2", "batch %d/%d attempt %d/%d: %s"
                         % (b + 1, n_batches, attempt, retries, exc))
                    # A rate limit needs real time to clear, not a 1.5 s nap -
                    # but don't sit through it more than twice, the fallback is
                    # right there.
                    time.sleep(8.0 * attempt if hit_429 else 1.5 * attempt)
            if values is None and hit_429:
                rate_limited += 1

        if values is None:
            for attempt in range(1, 3):
                try:
                    values = _open_elevation_batch(chunk)
                    fallback_used += 1
                    globals()["ELEV_FALLBACK_USED"] = True
                    break
                except Exception as exc:
                    warn("STEP 2", "Open-Elevation fallback batch %d/%d attempt %d/2: %s"
                         % (b + 1, n_batches, attempt, exc))
                    time.sleep(2.0 * attempt)
            if values is None:
                failed += 1
                values = [None] * len(chunk)

        # Two rate-limited batches means the hour is gone; stop hammering
        # Open-Meteo and serve the rest from the fallback.
        if rate_limited >= 2 and not prefer_fallback:
            prefer_fallback = True
            warn("STEP 2", "Open-Meteo is rate-limiting this IP - the remaining "
                           "%d batch(es) go straight to Open-Elevation" % (n_batches - b - 1))
        for i, v in enumerate(values):
            out[b * batch + i] = float(v) if v is not None else None
        if (b + 1) % 10 == 0 or b + 1 == n_batches:
            log("STEP 2", "elevation batches %d/%d" % (b + 1, n_batches))
        time.sleep(sleep)

    ok = sum(1 for v in out if v is not None)
    log("STEP 2", "elevations retrieved: %d/%d points (%d batch(es) via fallback, "
                  "%d unrecoverable)" % (ok, len(points), fallback_used, failed))
    return out


def terrain_from_five(lat, z_c, z_n, z_s, z_e, z_w):
    """Slope (deg), aspect (deg, compass, downslope) and relief (m) from the
    centre plus the four neighbour elevations."""
    vals = [v for v in (z_c, z_n, z_s, z_e, z_w) if v is not None]
    if not vals:
        return None, None, None, None

    centre = z_c if z_c is not None else float(np.mean(vals))
    relief = float(max(vals) - min(vals))

    # Degree offsets -> metres.
    dy_m = 2.0 * NB_OFFSET * M_PER_DEG
    dx_m = 2.0 * NB_OFFSET * M_PER_DEG * math.cos(math.radians(lat))

    dzdy = (z_n - z_s) / dy_m if (z_n is not None and z_s is not None and dy_m > 0) else 0.0
    dzdx = (z_e - z_w) / dx_m if (z_e is not None and z_w is not None and dx_m > 0) else 0.0

    slope = math.degrees(math.atan(math.hypot(dzdx, dzdy)))

    if abs(dzdx) < 1e-12 and abs(dzdy) < 1e-12:
        aspect = 0.0                                   # flat
    else:
        # ESRI convention, and it wants the raster-order y gradient (south minus
        # north), hence the -dzdy. Result is the DOWNSLOPE compass direction.
        a = math.degrees(math.atan2(-dzdy, -dzdx))
        if a < 0:
            aspect = 90.0 - a
        elif a > 90.0:
            aspect = 450.0 - a
        else:
            aspect = 90.0 - a
        aspect = aspect % 360.0

    return float(centre), float(slope), float(aspect), relief


def add_terrain(df):
    log("STEP 2", "assembling centre + 4 neighbour points per cell ...")
    pts = []
    for row in df.itertuples(index=False):
        la, lo = row.lat, row.lon
        pts.append((la, lo))                  # 0 centre
        pts.append((la + NB_OFFSET, lo))      # 1 north
        pts.append((la - NB_OFFSET, lo))      # 2 south
        pts.append((la, lo + NB_OFFSET))      # 3 east
        pts.append((la, lo - NB_OFFSET))      # 4 west

    n_batches = int(math.ceil(len(pts) / float(ELEV_BATCH)))
    log("STEP 2", "%d points -> %d API batches (~%.1f min)"
        % (len(pts), n_batches, n_batches * (ELEV_SLEEP + 0.6) / 60.0))

    elevs = fetch_elevations(pts)

    elevation, slope, aspect, relief = [], [], [], []
    for i, row in enumerate(df.itertuples(index=False)):
        z = elevs[i * 5:(i + 1) * 5]
        e, s, a, r = terrain_from_five(row.lat, z[0], z[1], z[2], z[3], z[4])
        elevation.append(e)
        slope.append(s)
        aspect.append(a)
        relief.append(r)

    df["elevation_m"] = elevation
    df["slope_deg"] = slope
    df["aspect_deg"] = aspect
    df["relief_m"] = relief

    # A cell the API never answered for keeps its place in the grid rather than
    # punching a hole in the map.
    for col in ("elevation_m", "slope_deg", "aspect_deg", "relief_m"):
        n_missing = int(df[col].isna().sum())
        if n_missing:
            med = df[col].median()
            df[col] = df[col].fillna(0.0 if pd.isna(med) else med)
            warn("STEP 2", "%s: %d cell(s) filled with the column median" % (col, n_missing))

    log("STEP 2", "terrain done | elevation %.0f-%.0f m | slope mean %.1f deg | relief max %.0f m"
        % (df.elevation_m.min(), df.elevation_m.max(),
           df.slope_deg.mean(), df.relief_m.max()))
    return df


# =============================================================================
# STEP 3 - OSM features via Overpass
# =============================================================================
OVERPASS_QUERY = """[out:json][timeout:90];
(
  node["place"~"village|town|city"](25.0,92.6,25.8,93.4);
  way["waterway"~"river|stream"](25.0,92.6,25.8,93.4);
  way["highway"~"trunk|primary|secondary"](25.0,92.6,25.8,93.4);
);
out geom;"""

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",          # primary, as specified
    "https://overpass.kumi.systems/api/interpreter",    # mirror, only if primary fails
]


def fetch_overpass():
    """Returns (streams, roads, villages) or (None, None, None) on total failure.
    streams/roads: list of (lat, lon) vertices.  villages: list of dicts."""
    encoded = urllib.parse.urlencode({"data": OVERPASS_QUERY})
    payload = None
    for endpoint in OVERPASS_ENDPOINTS:
        for attempt in range(1, 3):
            try:
                log("STEP 3", "Overpass %s (attempt %d) ..." % (endpoint, attempt))
                resp = requests.get(endpoint + "?" + encoded, timeout=180,
                                    headers={"User-Agent": "dima-hasao-susceptibility/1.0"})
                resp.raise_for_status()
                payload = resp.json()
                break
            except Exception as exc:
                warn("STEP 3", "Overpass failed: %s" % exc)
                time.sleep(3.0 * attempt)
        if payload is not None:
            break

    if payload is None:
        warn("STEP 3", "all Overpass endpoints failed - distances will be null")
        return None, None, None

    streams, roads, villages = [], [], []
    for el in payload.get("elements", []):
        tags = el.get("tags") or {}
        if el.get("type") == "node" and tags.get("place"):
            villages.append({
                "name": tags.get("name") or "Unnamed",
                "lat": el.get("lat"),
                "lon": el.get("lon"),
                "population": tags.get("population"),
            })
        elif el.get("type") == "way":
            geom = [(g["lat"], g["lon"]) for g in (el.get("geometry") or [])]
            if not geom:
                continue
            if tags.get("waterway"):
                streams.extend(geom)
            elif tags.get("highway"):
                roads.extend(geom)

    villages = [v for v in villages if v["lat"] is not None and v["lon"] is not None]
    log("STEP 3", "OSM: %d stream vertices, %d road vertices, %d place nodes"
        % (len(streams), len(roads), len(villages)))
    return streams, roads, villages


def _nearest_index(cell_lat, cell_lon, targets):
    """Index of the nearest target for every cell.  Projects to local metres for
    a fast KD-tree lookup; the reported distance is then true haversine."""
    tgt = np.asarray(targets, dtype="float64")
    lat0 = float(np.mean(cell_lat))
    kx = M_PER_DEG * math.cos(math.radians(lat0))
    cells_xy = np.column_stack([np.asarray(cell_lon) * kx,
                                np.asarray(cell_lat) * M_PER_DEG])
    tgt_xy = np.column_stack([tgt[:, 1] * kx, tgt[:, 0] * M_PER_DEG])
    try:
        from scipy.spatial import cKDTree
        _, idx = cKDTree(tgt_xy).query(cells_xy, k=1)
        return np.asarray(idx, dtype=int)
    except Exception:
        # No scipy: chunked brute force, still fine at this grid size.
        idx = np.empty(len(cells_xy), dtype=int)
        for start in range(0, len(cells_xy), 256):
            block = cells_xy[start:start + 256]
            d2 = ((block[:, None, 0] - tgt_xy[None, :, 0]) ** 2
                  + (block[:, None, 1] - tgt_xy[None, :, 1]) ** 2)
            idx[start:start + 256] = np.argmin(d2, axis=1)
        return idx


def nearest_distance(df, targets):
    """Haversine distance (m) from each cell centre to the nearest vertex."""
    if not targets:
        return [None] * len(df)
    tgt = np.asarray(targets, dtype="float64")
    idx = _nearest_index(df["lat"].to_numpy(), df["lon"].to_numpy(), targets)
    d = haversine_m(df["lat"].to_numpy(), df["lon"].to_numpy(),
                    tgt[idx, 0], tgt[idx, 1])
    return [float(x) for x in d]


def add_osm(df):
    streams, roads, villages = fetch_overpass()

    if streams is None:
        # Graceful fallback: the run continues with no OSM-derived context.
        df["dist_stream_m"] = None
        df["dist_road_m"] = None
        df["nearest_village"] = "Unnamed"
        df["population"] = [800 + stable_hash(c) % 3000 for c in df["cell_id"]]
        df["population_estimated"] = True
        return df, False

    df["dist_stream_m"] = nearest_distance(df, streams)
    df["dist_road_m"] = nearest_distance(df, roads)

    if villages:
        vpts = [(v["lat"], v["lon"]) for v in villages]
        vidx = _nearest_index(df["lat"].to_numpy(), df["lon"].to_numpy(), vpts)
        names, pops, estimated = [], [], []
        for j in vidx:
            v = villages[int(j)]
            name = v["name"] or "Unnamed"
            pop_tag = v.get("population")
            pop_val = None
            if pop_tag is not None:
                try:
                    pop_val = int(float(str(pop_tag).replace(",", "").strip()))
                except (TypeError, ValueError):
                    pop_val = None
            if pop_val is None:
                # stable_hash instead of hash(): Python randomises hash() per
                # process, which would make the output non-reproducible.
                pop_val = 800 + stable_hash(name) % 3000
                estimated.append(True)
            else:
                estimated.append(False)
            names.append(name)
            pops.append(int(pop_val))
        df["nearest_village"] = names
        df["population"] = pops
        df["population_estimated"] = estimated
    else:
        warn("STEP 3", "no place nodes returned - villages set to 'Unnamed'")
        df["nearest_village"] = "Unnamed"
        df["population"] = [800 + stable_hash(c) % 3000 for c in df["cell_id"]]
        df["population_estimated"] = True

    n_est = int(pd.Series(df["population_estimated"]).sum())
    log("STEP 3", "distances + villages attached | %d/%d populations estimated"
        % (n_est, len(df)))
    return df, True


# =============================================================================
# STEP 4 - Landslide inventory (NASA COOLR, optional / best effort)
# =============================================================================
COOLR_ROOT = "https://maps.nccs.nasa.gov/mapping/rest/services/COOLR"
COOLR_ENVELOPE = {"xmin": 89.0, "ymin": 22.0, "xmax": 97.0, "ymax": 29.0}


def _coolr_query_layer(layer_url, max_records=8000):
    """Query one ArcGIS point layer with the lat 22-29 / lon 89-97 envelope."""
    points = []
    offset = 0
    while offset < max_records:
        params = {
            "geometry": json.dumps(dict(COOLR_ENVELOPE, spatialReference={"wkid": 4326})),
            "geometryType": "esriGeometryEnvelope",
            "inSR": 4326,
            "outSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "*",
            "returnGeometry": "true",
            "resultOffset": offset,
            "resultRecordCount": 2000,
            "f": "geojson",
        }
        resp = requests.get(layer_url + "/query", params=params, timeout=90)
        resp.raise_for_status()
        data = resp.json()
        feats = data.get("features") or []
        if not feats:
            break
        for ft in feats:
            geom = ft.get("geometry") or {}
            if geom.get("type") != "Point":
                continue
            coords = geom.get("coordinates") or []
            if len(coords) >= 2 and coords[0] is not None and coords[1] is not None:
                points.append((float(coords[1]), float(coords[0])))  # (lat, lon)
        if len(feats) < 2000:
            break
        offset += len(feats)
    return points


def fetch_coolr():
    """Discover the COOLR services, find the point layers, pull events in the
    envelope. Returns a list of (lat, lon); [] if anything at all goes wrong."""
    try:
        log("STEP 4", "listing COOLR services ...")
        resp = requests.get(COOLR_ROOT, params={"f": "pjson"}, timeout=60)
        resp.raise_for_status()
        catalog = resp.json()

        services = catalog.get("services") or []
        log("STEP 4", "COOLR advertises %d service(s): %s"
            % (len(services), ", ".join(s.get("name", "?") for s in services) or "none"))

        layer_urls = []
        for svc in services:
            svc_name = svc.get("name", "")
            svc_type = svc.get("type", "MapServer")
            short = svc_name.split("/")[-1]
            svc_url = "https://maps.nccs.nasa.gov/mapping/rest/services/COOLR/%s/%s" \
                % (short, svc_type)
            try:
                s_resp = requests.get(svc_url, params={"f": "pjson"}, timeout=60)
                s_resp.raise_for_status()
                s_info = s_resp.json()
            except Exception as exc:
                warn("STEP 4", "cannot read %s: %s" % (svc_url, exc))
                continue

            for lyr in (s_info.get("layers") or []):
                lname = str(lyr.get("name", ""))
                gtype = str(lyr.get("geometryType", ""))
                is_point = ("Point" in gtype) or (not gtype and "point" in lname.lower())
                log("STEP 4", "  layer %s: id=%s type=%s%s"
                    % (lname, lyr.get("id"), gtype or "?", "  <- point" if is_point else ""))
                if is_point or gtype == "":
                    layer_urls.append("%s/%s" % (svc_url, lyr.get("id")))

        all_points = []
        for lurl in layer_urls:
            try:
                pts = _coolr_query_layer(lurl)
                if pts:
                    log("STEP 4", "%d point(s) from %s" % (len(pts), lurl))
                    all_points.extend(pts)
            except Exception as exc:
                warn("STEP 4", "query failed on %s: %s" % (lurl, exc))

        # De-duplicate identical coordinates across overlapping layers.
        deduped = sorted(set((round(la, 5), round(lo, 5)) for la, lo in all_points))
        log("STEP 4", "COOLR points in envelope (lat 22-29, lon 89-97): %d" % len(deduped))
        return [(float(a), float(b)) for a, b in deduped]

    except Exception as exc:
        warn("STEP 4", "COOLR unavailable (%s) - continuing without an inventory" % exc)
        return []


def points_in_bbox(points):
    return [(la, lo) for la, lo in points
            if LAT_MIN <= la <= LAT_MAX and LON_MIN <= lo <= LON_MAX]


# =============================================================================
# STEP 5 - Susceptibility scoring
# =============================================================================
def score_ahp(df):
    """Weighted overlay (AHP):
       0.45 slope + 0.25 relief + 0.20 elevation + 0.10 stream proximity."""
    slope_n = minmax(df["slope_deg"].to_numpy(dtype="float64"))
    relief_n = minmax(df["relief_m"].to_numpy(dtype="float64"))
    elev_n = minmax(df["elevation_m"].to_numpy(dtype="float64"))

    dist = pd.to_numeric(pd.Series(df["dist_stream_m"]), errors="coerce").to_numpy()
    if np.isfinite(dist).any():
        prox = 1.0 / (1.0 + dist)
        stream_n = minmax(prox)
        # Cells with no distance sit at the mean rather than skewing the score.
        missing = ~np.isfinite(dist)
        if missing.any():
            stream_n[missing] = float(np.nanmean(stream_n[~missing]))
    else:
        warn("STEP 5", "no stream distances - the 0.10 stream term contributes a constant")
        stream_n = np.zeros(len(df))

    score = (0.45 * slope_n + 0.25 * relief_n + 0.20 * elev_n + 0.10 * stream_n)
    log("STEP 5", "AHP score computed | min %.4f  mean %.4f  max %.4f"
        % (score.min(), score.mean(), score.max()))
    return score


def _ensure_xgboost():
    try:
        import xgboost  # noqa: F401
        return True
    except ImportError:
        log("STEP 5", "installing xgboost ...")
        try:
            subprocess.run(["pip", "install", "-q", "xgboost"], check=True, timeout=600)
            import xgboost  # noqa: F401
            return True
        except Exception as exc:
            warn("STEP 5", "xgboost unavailable (%s) - staying with AHP" % exc)
            return False


def score_xgboost(df, coolr_pts):
    """Returns (probabilities|None, mean_auc|None).

    Positives: cells within 2 km of a COOLR point.
    Negatives: an equal number drawn from the same slope-decile distribution.
    Validation: GroupKFold(5) on 0.1-degree spatial blocks."""
    if not _ensure_xgboost():
        return None, None
    try:
        from xgboost import XGBClassifier
        from sklearn.model_selection import GroupKFold
        from sklearn.metrics import roc_auc_score
    except Exception as exc:
        warn("STEP 5", "ML imports failed (%s) - staying with AHP" % exc)
        return None, None

    lat = df["lat"].to_numpy(dtype="float64")
    lon = df["lon"].to_numpy(dtype="float64")

    # --- labels ------------------------------------------------------------
    pts = np.asarray(coolr_pts, dtype="float64")
    idx = _nearest_index(lat, lon, coolr_pts)
    d_event = haversine_m(lat, lon, pts[idx, 0], pts[idx, 1])
    pos_mask = d_event <= COOLR_POS_RADIUS_M
    n_pos = int(pos_mask.sum())
    log("STEP 5", "positives (cells within %.0f m of an event): %d"
        % (COOLR_POS_RADIUS_M, n_pos))
    if n_pos < 20 or n_pos >= len(df) - 20:
        warn("STEP 5", "not enough separable positives/negatives - staying with AHP")
        return None, None

    # Negatives matched to the positives' slope-decile distribution.
    rng = np.random.default_rng(42)
    decile = pd.qcut(df["slope_deg"].rank(method="first"), 10,
                     labels=False, duplicates="drop").to_numpy()
    neg_choice = []
    all_idx = np.arange(len(df))
    for d in np.unique(decile):
        want = int(((decile == d) & pos_mask).sum())
        if want == 0:
            continue
        pool = all_idx[(decile == d) & (~pos_mask)]
        take = min(want, len(pool))
        if take:
            neg_choice.extend(rng.choice(pool, size=take, replace=False).tolist())
    # Top up any shortfall from the remaining negatives.
    shortfall = n_pos - len(neg_choice)
    if shortfall > 0:
        remaining = np.setdiff1d(all_idx[~pos_mask], np.asarray(neg_choice, dtype=int))
        if len(remaining):
            neg_choice.extend(
                rng.choice(remaining, size=min(shortfall, len(remaining)),
                           replace=False).tolist())
    neg_idx = np.asarray(sorted(set(neg_choice)), dtype=int)
    pos_idx = all_idx[pos_mask]
    log("STEP 5", "training set: %d positives / %d negatives" % (len(pos_idx), len(neg_idx)))

    # --- features ----------------------------------------------------------
    aspect_rad = np.radians(pd.to_numeric(df["aspect_deg"], errors="coerce")
                            .fillna(0.0).to_numpy(dtype="float64"))
    stream = pd.to_numeric(pd.Series(df["dist_stream_m"]), errors="coerce").to_numpy()
    if np.isfinite(stream).any():
        stream = np.where(np.isfinite(stream), stream, np.nanmedian(stream[np.isfinite(stream)]))
    else:
        stream = np.zeros(len(df))

    X_all = np.column_stack([
        df["elevation_m"].to_numpy(dtype="float64"),
        df["slope_deg"].to_numpy(dtype="float64"),
        np.sin(aspect_rad),
        np.cos(aspect_rad),
        df["relief_m"].to_numpy(dtype="float64"),
        stream,
    ])

    train_idx = np.concatenate([pos_idx, neg_idx])
    X = X_all[train_idx]
    y = np.concatenate([np.ones(len(pos_idx)), np.zeros(len(neg_idx))])
    # Spatial blocks of 0.1 degrees keep train and test folds geographically apart.
    groups = np.array(["%d_%d" % (math.floor(a / 0.1), math.floor(b / 0.1))
                       for a, b in zip(lat[train_idx], lon[train_idx])])
    log("STEP 5", "spatial blocks in the training set: %d" % len(np.unique(groups)))

    def new_model():
        return XGBClassifier(
            n_estimators=300, max_depth=4, learning_rate=0.05,
            subsample=0.9, colsample_bytree=0.9, reg_lambda=1.0,
            eval_metric="logloss", tree_method="hist",
            random_state=42, n_jobs=2,
        )

    n_splits = int(min(5, len(np.unique(groups))))
    if n_splits < 2:
        warn("STEP 5", "too few spatial blocks for cross-validation - staying with AHP")
        return None, None

    aucs = []
    gkf = GroupKFold(n_splits=n_splits)
    for fold, (tr, te) in enumerate(gkf.split(X, y, groups), start=1):
        if len(np.unique(y[tr])) < 2 or len(np.unique(y[te])) < 2:
            warn("STEP 5", "fold %d has a single class - skipped" % fold)
            continue
        model = new_model()
        model.fit(X[tr], y[tr])
        proba = model.predict_proba(X[te])[:, 1]
        auc = float(roc_auc_score(y[te], proba))
        aucs.append(auc)
        log("STEP 5", "fold %d ROC-AUC = %.4f" % (fold, auc))

    if not aucs:
        warn("STEP 5", "no usable folds - staying with AHP")
        return None, None

    mean_auc = float(np.mean(aucs))
    print("\n  >>> Spatial block CV mean ROC-AUC = %.4f  (%d folds)\n" % (mean_auc, len(aucs)))

    if mean_auc <= AUC_THRESHOLD:
        warn("STEP 5", "AUC %.4f <= %.2f - keeping the AHP score" % (mean_auc, AUC_THRESHOLD))
        return None, mean_auc

    final = new_model()
    final.fit(X, y)
    proba_all = final.predict_proba(X_all)[:, 1]
    log("STEP 5", "XGBoost probabilities predicted for all %d cells" % len(df))
    return proba_all.astype("float64"), mean_auc


# =============================================================================
# Main pipeline
# =============================================================================
def main():
    t0 = time.time()

    # ---- STEP 8 first, so the frontend has a file within seconds -----------
    print("=" * 78)
    log("STEP 8", "generating the dummy grid before anything else ...")
    make_dummy_grid(60, DUMMY_PATH)
    print("=" * 78)

    # ---- STEP 1 ------------------------------------------------------------
    df = build_grid()

    # ---- STEP 2 ------------------------------------------------------------
    df = add_terrain(df)

    # ---- STEP 3 ------------------------------------------------------------
    df, osm_ok = add_osm(df)

    # ---- STEP 4 ------------------------------------------------------------
    coolr_all = fetch_coolr()
    coolr_bbox = points_in_bbox(coolr_all)
    log("STEP 4", "COOLR points inside the Dima Hasao bbox: %d" % len(coolr_bbox))

    # ---- STEP 5 ------------------------------------------------------------
    ahp = score_ahp(df)
    susceptibility = ahp
    METHOD_USED = "AHP"
    auc = None

    if len(coolr_bbox) >= COOLR_MIN_POINTS:
        log("STEP 5", "%d COOLR points (>= %d) - attempting the XGBoost model"
            % (len(coolr_bbox), COOLR_MIN_POINTS))
        try:
            proba, auc = score_xgboost(df, coolr_bbox)
            if proba is not None:
                susceptibility = proba
                METHOD_USED = "XGBoost"
        except Exception as exc:
            warn("STEP 5", "XGBoost branch crashed (%s) - keeping AHP" % exc)
    else:
        log("STEP 5", "only %d COOLR points in the bbox (< %d) - AHP only"
            % (len(coolr_bbox), COOLR_MIN_POINTS))

    df["susceptibility"] = np.asarray(susceptibility, dtype="float64")

    print("\n" + "-" * 78)
    print("  FINAL SCORING METHOD: %s%s" % (METHOD_USED,
          ("  (spatial-block CV ROC-AUC %.4f)" % auc) if (auc is not None and METHOD_USED == "XGBoost") else ""))
    print("-" * 78 + "\n")

    # ---- STEP 6 ------------------------------------------------------------
    df["suscept_class"] = classify_quantile(df["susceptibility"])
    dist = df["suscept_class"].value_counts().reindex(CLASS_LABELS).fillna(0).astype(int)
    log("STEP 6", "classes: " + " | ".join("%s %d" % (k, v) for k, v in dist.items()))

    # ---- STEP 7 ------------------------------------------------------------
    sources = ["Open-Meteo Elevation API (https://api.open-meteo.com)"]
    if ELEV_FALLBACK_USED:
        sources.append("Open-Elevation API fallback (https://api.open-elevation.com)")
    if osm_ok:
        sources.append("OpenStreetMap via Overpass API (https://overpass-api.de)")
    if coolr_all:
        sources.append("NASA COOLR (https://maps.nccs.nasa.gov/mapping/rest/services/COOLR)")

    features = []
    for row in df.itertuples(index=False):
        features.append(build_feature({
            "cell_id": row.cell_id,
            "lat": r2(row.lat),
            "lon": r2(row.lon),
            "elevation_m": r2(row.elevation_m),
            "slope_deg": r2(row.slope_deg),
            "aspect_deg": r2(row.aspect_deg),
            "relief_m": r2(row.relief_m),
            "dist_stream_m": r2(row.dist_stream_m),
            "dist_road_m": r2(row.dist_road_m),
            "susceptibility": r4(row.susceptibility),
            "suscept_class": row.suscept_class,
            "nearest_village": row.nearest_village,
            "population": int(row.population),
            "population_estimated": bool(row.population_estimated),
        }))

    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": METHOD_USED,
        "auc": (round(float(auc), 4) if (auc is not None and METHOD_USED == "XGBoost") else None),
        "cell_count": len(features),
        "data_sources": sources,
    }
    write_geojson(RISK_PATH, features, metadata)

    # ---- Summary -----------------------------------------------------------
    print("\n" + "=" * 78)
    print("  SUMMARY - Dima Hasao landslide susceptibility")
    print("=" * 78)
    print("  bbox            : lat %.2f-%.2f, lon %.2f-%.2f" % (LAT_MIN, LAT_MAX, LON_MIN, LON_MAX))
    print("  cells           : %d  (%.2f deg grid)" % (len(df), CELL_DEG))
    print("  method used     : %s" % METHOD_USED)
    print("  ROC-AUC         : %s" % ("%.4f" % auc if auc is not None else "n/a (AHP)"))
    print("  COOLR points    : %d in bbox / %d in envelope" % (len(coolr_bbox), len(coolr_all)))
    print("  OSM context     : %s" % ("available" if osm_ok else "UNAVAILABLE - distances null"))
    print("  class distribution:")
    for label in CLASS_LABELS:
        n = int(dist.get(label, 0))
        bar = "#" * int(round(40.0 * n / max(len(df), 1)))
        print("    %-10s %5d  %s" % (label, n, bar))
    print("  files written   :")
    for p in (DUMMY_PATH, RISK_PATH):
        if os.path.exists(p):
            print("    %-32s %8.1f KB" % (p, os.path.getsize(p) / 1024.0))
    print("  elapsed         : %.1f min" % ((time.time() - t0) / 60.0))
    print("=" * 78 + "\n")

    # ---- Colab download ----------------------------------------------------
    try:
        from google.colab import files
        for p in (DUMMY_PATH, RISK_PATH):
            if os.path.exists(p):
                files.download(p)
    except Exception as exc:
        log("DONE", "not in Colab or download unavailable (%s) - files are on disk" % exc)

    return df, METHOD_USED, auc


if __name__ == "__main__":
    main()
