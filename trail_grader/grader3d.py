"""Detailed trail analysis for the 3D audit view.

Reuses the helpers in grader.py, but returns the full per-station record plus a
terrain grid so the browser can draw the DEM surface with the trail draped on it.
grader.analyse_trail() is left untouched — the older /grade route still uses it.
"""
import io
import json
import math
import os

import numpy as np
import rasterio
from rasterio.merge import merge
from rasterio.transform import rowcol

from grader import (
    GRADES, GRAD_AVG_MAX, GRAD_SEG_MAX, TURN_MIN, EXCEPTION_PCT,
    parse_kml, load_dtm, densify, sample_raster, circumradius,
    grade_by_value, grade_with_exceptions, to_nztm, to_wgs84,
)

# Recreation Aotearoa thresholds in degrees, as published. The percent values in
# grader.py are these converted; keeping both lets the report quote either.
GRADE_TABLE = [
    {"n": 1, "name": "Easiest",      "avg_deg": 3.5,  "max_deg": 4.0,  "exc": 2,    "radius": 6.0},
    {"n": 2, "name": "Easy",         "avg_deg": 5.0,  "max_deg": 8.0,  "exc": 5,    "radius": 4.0},
    {"n": 3, "name": "Intermediate", "avg_deg": 6.0,  "max_deg": 11.0, "exc": 10,   "radius": 2.5},
    {"n": 4, "name": "Advanced",     "avg_deg": 10.0, "max_deg": 15.0, "exc": 20,   "radius": 2.0},
    {"n": 5, "name": "Expert",       "avg_deg": 14.0, "max_deg": 20.0, "exc": 20,   "radius": 1.5},
    {"n": 6, "name": "Extreme",      "avg_deg": None, "max_deg": None, "exc": None, "radius": 1.0},
]

DEFAULTS = {
    "spacing": 5.0,        # station spacing along the line, metres
    "window": 50.0,        # gradient smoothing window, metres
    "chord": 5.0,          # turn-radius chord each side, metres
    "sideslope_offset": 5.0,  # perpendicular transect half-width, metres
    "terrain_buffer": 60.0,   # DEM margin around the trail bbox, metres
    "terrain_max": 190,       # max terrain grid cells per axis
    "radius_cutoff": 500.0,   # above this a turn counts as straight
}


# -------------------------------------------------------
# Fast tile index
# -------------------------------------------------------
# grader.load_dtm() rasterio-opens every .asc in the folder just to read its
# bounds. With 671 tiles that alone costs most of a minute, so read the six-line
# ASCII-grid header directly instead and cache the result beside the config.
INDEX_NAME = ".tile_index.json"


def _asc_bounds(path):
    hdr = {}
    with open(path, "r", errors="replace") as f:
        for _ in range(6):
            line = f.readline()
            if not line:
                break
            parts = line.split()
            if len(parts) != 2:
                break
            try:
                hdr[parts[0].lower()] = float(parts[1])
            except ValueError:
                break
    try:
        ncols, nrows, cell = hdr["ncols"], hdr["nrows"], hdr["cellsize"]
    except KeyError:
        return None
    if "xllcorner" in hdr and "yllcorner" in hdr:
        left, bottom = hdr["xllcorner"], hdr["yllcorner"]
    elif "xllcenter" in hdr and "yllcenter" in hdr:
        left, bottom = hdr["xllcenter"] - cell / 2, hdr["yllcenter"] - cell / 2
    else:
        return None
    return [left, bottom, left + ncols * cell, bottom + nrows * cell]


def _tile_index(dtm_folder):
    """{filename: [left, bottom, right, top]} for every .asc, cached on disk."""
    names = sorted(f for f in os.listdir(dtm_folder) if f.lower().endswith(".asc"))
    cache_path = os.path.join(dtm_folder, INDEX_NAME)
    try:
        with open(cache_path) as f:
            cached = json.load(f)
        if cached.get("names") == len(names) and set(cached["bounds"]) >= set(names):
            return cached["bounds"]
    except Exception:
        pass

    bounds = {}
    for name in names:
        b = _asc_bounds(os.path.join(dtm_folder, name))
        if b:
            bounds[name] = b
    try:
        with open(cache_path, "w") as f:
            json.dump({"names": len(names), "bounds": bounds}, f)
    except OSError:
        pass  # a read-only tile folder is fine, we just re-index next time
    return bounds


def load_dtm_fast(dtm_folder, bbox_nztm, buffer=60):
    """Merge only the tiles overlapping the bbox. Same output as grader.load_dtm."""
    xmin, ymin, xmax, ymax = bbox_nztm
    xmin -= buffer; ymin -= buffer; xmax += buffer; ymax += buffer

    index = _tile_index(dtm_folder)
    if not index:
        raise ValueError("No .asc tiles found in DTM folder: " + dtm_folder)

    relevant = [os.path.join(dtm_folder, n) for n, b in index.items()
                if b[0] < xmax and b[2] > xmin and b[1] < ymax and b[3] > ymin]
    if not relevant:
        raise ValueError(
            "No LiDAR tiles cover this trail. The configured tile folder only "
            "holds the Canterbury/Christchurch DEM.")

    datasets = [rasterio.open(p) for p in relevant]
    try:
        merged, transform = merge(datasets)
    finally:
        for d in datasets:
            d.close()

    buf = io.BytesIO()
    with rasterio.open(buf, "w", driver="GTiff",
                       height=merged.shape[1], width=merged.shape[2],
                       count=1, dtype=merged.dtype,
                       crs="EPSG:2193", transform=transform) as dst:
        dst.write(merged[0], 1)
    buf.seek(0)
    return buf, len(relevant)


def _deg(pct):
    return math.degrees(math.atan(abs(pct) / 100.0))


def _grade_for_gradient(pct):
    d = _deg(pct)
    for g in GRADE_TABLE:
        if g["max_deg"] is None or d <= g["max_deg"]:
            return g["n"]
    return 6


def _grade_for_radius(r):
    if r is None:
        return 1
    for g in GRADE_TABLE:
        if r >= g["radius"]:
            return g["n"]
    return 6


def _sideslope_deg(arr, transform, x, y, dx, dy, offset):
    """Cross-slope angle from a perpendicular transect either side of the tread."""
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return None
    px, py = -dy / length, dx / length
    zl = sample_raster(arr, transform, x + px * offset, y + py * offset)
    zr = sample_raster(arr, transform, x - px * offset, y - py * offset)
    if np.isnan(zl) or np.isnan(zr):
        return None
    return abs(math.degrees(math.atan((zl - zr) / (2 * offset))))


def analyse_trail_detailed(kml_bytes, dtm_folder, opts=None):
    o = dict(DEFAULTS)
    o.update(opts or {})

    pts = parse_kml(kml_bytes)
    nztm = [to_nztm.transform(lon, lat) for lon, lat in pts]
    xs = [p[0] for p in nztm]
    ys = [p[1] for p in nztm]
    bbox = (min(xs), min(ys), max(xs), max(ys))

    tif_buf, tile_count = load_dtm_fast(dtm_folder, bbox,
                                        buffer=int(o["terrain_buffer"]) + 20)
    dense = densify(nztm, step=o["spacing"])

    with rasterio.open(tif_buf) as src:
        transform = src.transform
        arr = src.read(1).astype(float)
        nodata = src.nodata if src.nodata is not None else -9999
        arr[arr == nodata] = np.nan

        elevs = [sample_raster(arr, transform, x, y) for x, y in dense]

        tangents = []
        for i in range(len(dense)):
            if i == 0:
                dx = dense[1][0] - dense[0][0]; dy = dense[1][1] - dense[0][1]
            elif i == len(dense) - 1:
                dx = dense[-1][0] - dense[-2][0]; dy = dense[-1][1] - dense[-2][1]
            else:
                dx = dense[i + 1][0] - dense[i - 1][0]; dy = dense[i + 1][1] - dense[i - 1][1]
            tangents.append((dx, dy))

        sideslopes = [
            _sideslope_deg(arr, transform, dense[i][0], dense[i][1],
                           tangents[i][0], tangents[i][1], o["sideslope_offset"])
            for i in range(len(dense))
        ]

        terrain = _terrain_grid(arr, transform, bbox, o)

    # chainage
    cum = [0.0]
    for i in range(1, len(dense)):
        cum.append(cum[-1] + math.hypot(dense[i][0] - dense[i - 1][0],
                                        dense[i][1] - dense[i - 1][1]))
    total = cum[-1]
    half = o["window"] / 2.0

    def smooth_at(i):
        rear, front = 0, len(dense) - 1
        for j in range(i - 1, -1, -1):
            if cum[i] - cum[j] >= half:
                rear = j
                break
        for j in range(i + 1, len(dense)):
            if cum[j] - cum[i] >= half:
                front = j
                break
        run = cum[front] - cum[rear]
        if run <= 0 or np.isnan(elevs[front]) or np.isnan(elevs[rear]):
            return None
        return (elevs[front] - elevs[rear]) / run * 100.0

    grads = [smooth_at(i) for i in range(len(dense))]

    # turn radius from a fixed chord either side, so it is independent of spacing
    step = max(1, int(round(o["chord"] / o["spacing"])))
    radii = []
    for i in range(len(dense)):
        if i - step < 0 or i + step >= len(dense):
            radii.append(None)
            continue
        r = circumradius(dense[i - step], dense[i], dense[i + step])
        radii.append(None if (r is None or r > o["radius_cutoff"]) else round(r, 1))

    stations = []
    for i, (x, y) in enumerate(dense):
        z = elevs[i]
        g = grads[i]
        edge = cum[i] < half or cum[i] > total - half
        gg = _grade_for_gradient(g) if g is not None else 1
        rg = _grade_for_radius(radii[i])
        lon, lat = to_wgs84.transform(x, y)
        stations.append({
            "i": i,
            "d": round(cum[i], 1),
            "z": None if np.isnan(z) else round(float(z), 2),
            "g": None if g is None else round(g, 2),
            "r": radii[i],
            "ss": None if sideslopes[i] is None else round(sideslopes[i], 1),
            "gg": gg, "rg": rg, "grade": max(gg, rg),
            "edge": 1 if edge else 0,
            "x": round(x, 2), "y": round(y, 2),
            "lon": round(lon, 6), "lat": round(lat, 6),
        })

    core = [s for s in stations if not s["edge"] and s["g"] is not None]
    if not core:
        raise ValueError("No stations had a full smoothing window — is the trail shorter than the window?")

    summary = _summarise(stations, core, total, o)
    summary["tiles_used"] = tile_count
    return {
        "stations": stations,
        "terrain": terrain,
        "summary": summary,
        "grades": GRADE_TABLE,
        "options": o,
    }


def _terrain_grid(arr, transform, bbox, o):
    """Crop the merged DEM to the trail bbox + buffer and downsample for plotting."""
    xmin, ymin, xmax, ymax = bbox
    b = o["terrain_buffer"]
    xmin -= b; ymin -= b; xmax += b; ymax += b

    r0, c0 = rowcol(transform, xmin, ymax)   # top-left
    r1, c1 = rowcol(transform, xmax, ymin)   # bottom-right
    r0 = max(0, min(int(r0), arr.shape[0] - 1))
    r1 = max(0, min(int(r1) + 1, arr.shape[0]))
    c0 = max(0, min(int(c0), arr.shape[1] - 1))
    c1 = max(0, min(int(c1) + 1, arr.shape[1]))
    if r1 <= r0 or c1 <= c0:
        return None

    sub = arr[r0:r1, c0:c1]
    stride = max(1, int(math.ceil(max(sub.shape) / o["terrain_max"])))
    sub = sub[::stride, ::stride]

    # NZTM coordinates of each sampled cell centre
    x0 = transform.c + (c0 + 0.5) * transform.a
    y0 = transform.f + (r0 + 0.5) * transform.e
    xs = [round(x0 + j * stride * transform.a, 2) for j in range(sub.shape[1])]
    ys = [round(y0 + i * stride * transform.e, 2) for i in range(sub.shape[0])]

    z = [[None if np.isnan(v) else round(float(v), 2) for v in row] for row in sub]
    return {"x": xs, "y": ys, "z": z, "stride": stride,
            "shape": [sub.shape[0], sub.shape[1]]}


def _summarise(stations, core, total, o):
    grads = [s["g"] for s in core]
    avg = sum(grads) / len(grads)
    avg_deg = _deg(avg)
    radii = [s["r"] for s in core if s["r"] is not None]
    sides = [s["ss"] for s in core if s["ss"] is not None]

    seg_len = o["spacing"]
    dist = {n: sum(1 for s in core if s["grade"] == n) * seg_len for n in range(1, 7)}

    # exception-aware roll-up: the lowest grade whose ceilings and allowances all hold
    assessed = GRADE_TABLE[-1]
    over_pct = under_pct = 0.0
    for g in GRADE_TABLE:
        if g["max_deg"] is None:
            assessed = g
            break
        if avg_deg > g["avg_deg"]:
            continue
        over = sum(1 for s in core if _deg(s["g"]) > g["max_deg"]) / len(core) * 100
        if over > g["exc"]:
            continue
        under = sum(1 for s in core if s["r"] is not None and s["r"] < g["radius"]) / len(core) * 100
        if under > g["exc"]:
            continue
        assessed, over_pct, under_pct = g, over, under
        break

    steep = min(core, key=lambda s: s["g"])
    climb = max(core, key=lambda s: s["g"])
    tight = min((s for s in core if s["r"] is not None), key=lambda s: s["r"], default=None)
    zs = [s["z"] for s in stations if s["z"] is not None]

    return {
        "length_m": round(total),
        "stations": len(stations),
        "core_stations": len(core),
        "spacing_m": o["spacing"],
        "window_m": o["window"],
        "start_z": zs[0] if zs else None,
        "end_z": zs[-1] if zs else None,
        "descent_m": round(zs[0] - zs[-1], 1) if zs else None,
        "avg_gradient_pct": round(avg, 1),
        "avg_gradient_deg": round(avg_deg, 1),
        "max_descent_pct": round(steep["g"], 1),
        "max_descent_at_m": round(steep["d"]),
        "max_ascent_pct": round(climb["g"], 1),
        "max_ascent_at_m": round(climb["d"]),
        "min_radius_m": tight["r"] if tight else None,
        "min_radius_at_m": round(tight["d"]) if tight else None,
        "median_radius_m": round(sorted(radii)[len(radii) // 2], 1) if radii else None,
        "mean_sideslope_deg": round(sum(sides) / len(sides), 1) if sides else None,
        "max_sideslope_deg": round(max(sides), 1) if sides else None,
        "strict_grade": max(s["grade"] for s in core),
        "assessed_grade": assessed["n"],
        "assessed_name": assessed["name"],
        "assessed_avg_max": assessed["avg_deg"],
        "assessed_peak_max": assessed["max_deg"],
        "assessed_exc": assessed["exc"],
        "assessed_radius": assessed["radius"],
        "over_peak_pct": round(over_pct, 1),
        "under_radius_pct": round(under_pct, 1),
        "grade_distribution": dist,
        "uphill_pct": round(sum(1 for s in core if s["g"] > 0) / len(core) * 100, 1),
    }
