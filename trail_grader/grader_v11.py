"""Trail grading pipeline ported from TrailAudit_v11.ipynb (Downloads/), cell for
cell — the notebook's exact grading thresholds/algorithm, Folium map, matplotlib
gradient/sideslope profiles, Plotly 3D terrain view.

Elevation comes from the local 1 m Canterbury/Christchurch LiDAR DEM tiles
(config.json's dtm_folder) when they cover the trail, sampled via the same
rasterio pipeline as grader.py/grader3d.py — accurate to 1 m and fast, since
it's one local raster merge rather than per-point network calls. Outside that
bundled tile coverage, elevation falls back to LINZ's Export API: layer
121859 ("NZ LiDAR 1m DEM", an amalgamated national mosaic of current LiDAR
surveys, not an 8 m dataset) cropped to the trail's bounding box and
downloaded as one GeoTIFF, then sampled with the exact same code path as the
bundled tiles. One bulk export (measured ~15s end-to-end for a ~1.8 km2 area)
replaces what would otherwise be one HTTP request per elevation point —
thousands of them for even a short trail. An earlier version of this fallback
queried LINZ's point-query API once per point instead; that worked but took
8-9 minutes for a 15km trail, so it was replaced with this bulk-export
approach. Requires the LINZ_API_KEY to carry the exports:read/exports:write
scopes (set at data.linz.govt.nz under API Keys) — without them this fallback
quietly returns no coverage and local-tile-only trails still work fine.

This intentionally replaces grader.py / grader3d.py as the site's pipeline: the
notebook is the newer, authoritative version of the tool.
"""
import hashlib
import io
import json
import math
import os
import time
import zipfile
import base64

import numpy as np
import rasterio
import requests
from rasterio.io import MemoryFile
from rasterio import warp
from rasterio.transform import array_bounds
from pyproj import Transformer
from shapely.geometry import LineString, box

# matplotlib/folium/plotly/scipy/pykml are imported lazily, inside the
# specific functions that use them, instead of here at module level: this
# module gets imported unconditionally by app.py, so an eager import here
# means every worker process pays for all five libraries' memory footprint
# at startup, even for requests (landing page, login, account browsing)
# that never touch grading at all. Deferred imports still only pay that
# cost once per process (sys.modules caches them), just on first actual use
# rather than on process boot.

from config import get_dtm_folder
from grader import sample_raster
from grader3d import load_dtm_fast

DTM_FOLDER = get_dtm_folder()

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(REPO_ROOT, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

to_nztm = Transformer.from_crs("EPSG:4326", "EPSG:2193", always_xy=True)
to_wgs84 = Transformer.from_crs("EPSG:2193", "EPSG:4326", always_xy=True)

EXPORT_LAYER_URL = "https://data.linz.govt.nz/services/api/v1/layers/121859/"
EXPORT_TIMEOUT = 20
EXPORT_POLL_INTERVAL = 2
EXPORT_POLL_TIMEOUT = 180
# Reading the full raster into memory is unavoidable (sampling needs the
# whole array), so an unbounded trail area is an unbounded memory request —
# this is what pushed the Render instance over its limit and triggered an
# OOM restart. 150 MB is comfortably past any normal trail's export size
# (a 15.6 km stress-test trail measured ~54 MB) while still catching a
# pathologically large bbox before it's downloaded, not after.
EXPORT_MAX_UNZIPPED_BYTES = 150 * 1024 * 1024


def _export_polygon_wgs84(polygon_nztm):
    """Reproject a shapely Polygon's exterior ring (NZTM) to WGS84 lon/lat for
    the Export API's extent field. A dense ring — e.g. from LineString.buffer,
    which emits many points around curves — reprojects accurately; a sparse
    4-corner rectangle can bow inward enough after reprojection to clip real
    area off, since NZTM and WGS84 don't share straight lines."""
    return [list(to_wgs84.transform(x, y)) for x, y in polygon_nztm.exterior.coords]


EXPORT_VERTEX_BUDGET = 400  # LINZ caps extent polygons at 1000 vertices; stay well under


def _export_corridor(bbox_nztm, buffer, corridor_nztm=None):
    """Build the NZTM polygon to request from LINZ: a tight buffer around the
    trail's own line when corridor_nztm (a shapely LineString) is given —
    proportional to trail length, not bounding-box area, which matters a lot
    for a winding/looping trail where the bbox can be many times the area the
    trail actually passes through — else a buffered rectangle for callers
    that only have a bbox (e.g. the editor's terrain preview).

    LINZ rejects extent geometries over 1000 vertices (hit in practice: a
    real trail's own KML vertices, buffered at the default curve resolution,
    sailed past that for a 15km trail — and render_plot3d's 1m-densified
    line would be far worse). Simplifying the *line* first — not the
    buffered polygon — keeps the requested area close to the real trail
    while cutting vertex count; this only affects what gets requested from
    LINZ, not the full-resolution centreline actually sampled afterwards."""
    if corridor_nztm is None:
        xmin, ymin, xmax, ymax = bbox_nztm
        return box(xmin - buffer, ymin - buffer, xmax + buffer, ymax + buffer)

    tolerance = 0.0
    for _ in range(10):
        line = corridor_nztm if tolerance == 0 else corridor_nztm.simplify(tolerance)
        polygon = line.buffer(buffer, quad_segs=2)
        if len(polygon.exterior.coords) <= EXPORT_VERTEX_BUDGET:
            return polygon
        tolerance = max(tolerance * 2, 4.0)

    # Simplification didn't converge (shouldn't happen) — a bounding-box
    # rectangle always has few enough vertices and still covers the trail.
    xmin, ymin, xmax, ymax = bbox_nztm
    return box(xmin - buffer, ymin - buffer, xmax + buffer, ymax + buffer)


def _fetch_export_dem(bbox_nztm, buffer, note=None, corridor_nztm=None, cache_path=None):
    """Crop LINZ's national LiDAR mosaic via the Export API and return
    (elevation array, transform, nodata) — one bulk raster download instead
    of one HTTP request per point. Returns None (not an error) on any
    failure — missing/under-scoped API key, LINZ outage, job error, timeout,
    or genuinely no coverage there — so the caller can report "no coverage"
    cleanly rather than surfacing which specific step failed."""
    api_key = os.environ.get("LINZ_API_KEY", "")
    if not api_key:
        return None
    headers = {"Authorization": "key " + api_key, "Content-Type": "application/json"}
    polygon_nztm = _export_corridor(bbox_nztm, buffer, corridor_nztm=corridor_nztm)
    body = {
        "crs": "EPSG:2193",
        "formats": {"grid": "image/tiff;subtype=geotiff"},
        "extent": {"type": "Polygon", "coordinates": [_export_polygon_wgs84(polygon_nztm)]},
        "items": [{"item": EXPORT_LAYER_URL}],
    }
    try:
        if note:
            note("No bundled tile coverage here — requesting an elevation "
                 "export from LINZ's national mosaic…")
        r = requests.post("https://data.linz.govt.nz/services/api/v1/exports/",
                           headers=headers, json=body, timeout=EXPORT_TIMEOUT)
        if r.status_code != 201:
            print(f"[export] create failed: {r.status_code} {r.text[:500]}")
            return None
        job = r.json()
        est = job.get("size_estimate_unzipped")
        if est and est > EXPORT_MAX_UNZIPPED_BYTES:
            print(f"[export] estimated size {est/1e6:.0f} MB exceeds cap "
                  f"{EXPORT_MAX_UNZIPPED_BYTES/1e6:.0f} MB, aborting (trail area too large)")
            if note:
                note(f"That area is too large to export automatically "
                     f"(~{est/1e6:.0f} MB) — try a shorter trail or upload your own DEM.")
            return None
        deadline = time.time() + EXPORT_POLL_TIMEOUT
        while True:
            if time.time() > deadline:
                print(f"[export] poll timed out after {EXPORT_POLL_TIMEOUT}s, "
                      f"last state={job.get('state')}")
                return None
            time.sleep(EXPORT_POLL_INTERVAL)
            jr = requests.get(job["url"], headers=headers, timeout=EXPORT_TIMEOUT)
            if jr.status_code != 200:
                print(f"[export] poll failed: {jr.status_code} {jr.text[:500]}")
                return None
            job = jr.json()
            if job["state"] == "complete":
                break
            if job["state"] in ("error", "cancelled", "gone"):
                print(f"[export] job ended in state={job['state']}: {job}")
                return None

        actual = job.get("size_complete_unzipped")
        if actual and actual > EXPORT_MAX_UNZIPPED_BYTES:
            print(f"[export] actual size {actual/1e6:.0f} MB exceeds cap "
                  f"{EXPORT_MAX_UNZIPPED_BYTES/1e6:.0f} MB, aborting before download")
            if note:
                note(f"That area turned out too large to process (~{actual/1e6:.0f} MB) "
                     f"— try a shorter trail or upload your own DEM.")
            return None
        if note:
            size_mb = (job.get("size_complete_zipped") or 0) / 1e6
            note(f"Export ready ({size_mb:.0f} MB) — downloading…")
        # Bandwidth-bound, not latency-bound: a wide/long trail's export can
        # run tens of MB, so this needs real headroom on a slow connection
        # rather than a short timeout that reads as a hang.
        zr = requests.get(job["download_url"], headers=headers, timeout=300)
        if zr.status_code != 200:
            print(f"[export] download failed: {zr.status_code} {zr.text[:500]}")
            return None
        zip_bytes = zr.content
        del zr  # drop the response object (and any internal buffering) once we have the body
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            tif_name = next((n for n in zf.namelist() if n.lower().endswith(".tif")), None)
            if tif_name is None:
                print(f"[export] no .tif in zip, contents: {zf.namelist()}")
                return None
            tif_bytes = zf.read(tif_name)
        del zip_bytes
        if cache_path is not None:
            # Write the downloaded bytes straight to disk and read back from
            # there, rather than also round-tripping through a second
            # in-memory GeoTIFF encode — a DEM for a long trail can be tens
            # of MB, and holding zip + tif + re-encoded copies at once is
            # exactly what pushed the service over its memory limit.
            with open(cache_path, "wb") as f:
                f.write(tif_bytes)
            del tif_bytes
            with rasterio.open(cache_path) as src:
                return src.read(1), src.transform, src.nodata
        with MemoryFile(tif_bytes) as memfile:
            with memfile.open() as src:
                return src.read(1), src.transform, src.nodata
    except Exception:
        import traceback
        print("[export] unexpected exception:")
        traceback.print_exc()
        return None


def _cached_export_dem(bbox_nztm, trail_name, buffer, note=None, corridor_nztm=None):
    """Disk-cached wrapper around _fetch_export_dem, keyed by trail + rounded
    bbox/corridor, so a single audit's main sample, sideslope sample, terrain
    preview and 3D view share one export instead of each triggering their own
    job."""
    corridor_key = round(corridor_nztm.length, 1) if corridor_nztm is not None else None
    key = hashlib.md5(json.dumps(
        [round(v, 1) for v in bbox_nztm] + [buffer, corridor_key]).encode()).hexdigest()[:10]
    path = os.path.join(CACHE_DIR, f"{trail_name}_export_121859_{key}.tif")
    if os.path.exists(path):
        with rasterio.open(path) as src:
            return src.read(1), src.transform, src.nodata
    return _fetch_export_dem(bbox_nztm, buffer, note=note, corridor_nztm=corridor_nztm,
                              cache_path=path)

# (grade, average gradient max %, absolute gradient max %, out-of-spec tolerance %)
GRADIENT_GRADES = [
    (1, 6.1, 7.0, 2),
    (2, 8.8, 14.1, 5),
    (3, 10.5, 19.3, 10),
    (4, 17.5, 27.0, 20),
    (5, 24.9, 36.0, 20),
    (6, float("inf"), float("inf"), 100),
]
# (grade, minimum turn radius m, out-of-spec tolerance %)
RADIUS_GRADES = [
    (1, 5.0, 2),
    (2, 4.0, 5),
    (3, 2.5, 10),
    (4, 2.0, 20),
    (5, 1.5, 20),
    (6, 1.0, 100),
]
GRADE_COLOURS = {
    1: "Green (Easiest)", 2: "Green (Easy)",
    3: "Light Blue (Intermediate)", 4: "Dark Blue (Advanced)",
    5: "Black (Expert)", 6: "Double Black (Extreme)",
}
GRADIENT_ABS_MAX = {g: m for g, _, m, _ in GRADIENT_GRADES}
RADIUS_MIN = {g: r for g, r, _ in RADIUS_GRADES}

STEEP_SIDESLOPE_DEG = 30


def load_uploaded_dem(dem_bytes):
    """Open a user-uploaded single-file DEM (GeoTIFF), reprojecting to
    EPSG:2193 if needed. Returns (elevation array, raster transform, nodata)
    for the whole raster, unmerged/uncropped — an uploaded file is already
    scoped to one trail's area by whatever the user chose to download, so
    there's no tile index/bbox-cropping step like the bundled dataset."""
    with MemoryFile(dem_bytes) as memfile:
        with memfile.open() as src:
            if src.crs is None:
                raise ValueError(
                    "That file has no coordinate reference system embedded — "
                    "export it as a georeferenced GeoTIFF and try again.")
            nodata = src.nodata
            if src.crs.to_epsg() == 2193:
                return src.read(1).astype(float), src.transform, nodata
            transform, width, height = warp.calculate_default_transform(
                src.crs, "EPSG:2193", src.width, src.height, *src.bounds)
            dest = np.full((height, width), np.nan, dtype="float64")
            warp.reproject(
                source=rasterio.band(src, 1), destination=dest,
                src_transform=src.transform, src_crs=src.crs,
                dst_transform=transform, dst_crs="EPSG:2193",
                src_nodata=nodata, dst_nodata=np.nan,
                resampling=warp.Resampling.bilinear)
            return dest, transform, np.nan


def _load_local_dtm(bbox_nztm, buffer=60, dem_source=None):
    """Merge the local 1 m LiDAR tiles covering bbox_nztm ([xmin, ymin, xmax,
    ymax]) and return (elevation array, raster transform, nodata). If
    dem_source is given (an already-loaded (array, transform, nodata) tuple
    from load_uploaded_dem), it's returned as-is instead — the bundled-tile
    path below is untouched."""
    if dem_source is not None:
        return dem_source
    if not DTM_FOLDER:
        raise ValueError(
            "No local LiDAR DEM configured — set 'dtm_folder' in config.json.")
    buf, _ = load_dtm_fast(DTM_FOLDER, bbox_nztm, buffer=buffer)
    with rasterio.open(buf) as src:
        return src.read(1), src.transform, None


def _try_local_dtm(bbox_nztm, buffer=60):
    """Like _load_local_dtm, but returns None instead of raising when the
    bundled Canterbury/Christchurch tiles aren't configured or don't cover
    this trail — so callers can fall back to the live national mosaic rather
    than fail outright."""
    try:
        return _load_local_dtm(bbox_nztm, buffer=buffer)
    except ValueError:
        return None


def _mask_nodata(arr, nodata):
    """Replace nodata sentinel cells with NaN so sample_raster's existing
    np.isnan() check filters them out, instead of silently treating a
    sentinel (e.g. the Export API's -9999) as a literal, wildly-wrong
    elevation. The bundled local tiles don't carry a nodata value through
    their merge (comes back None even though the source AAIGrid tiles use
    -99), so fall back to the same very-negative-value heuristic already
    used for them — safe here since nothing in NZ sits below -90 m."""
    if arr.dtype.kind != "f":
        return arr
    # In-place rather than np.where (which would allocate a second
    # full-size array) — each caller already holds a freshly-read array,
    # not a shared/cached object, so mutating it is safe.
    if nodata is not None and not (isinstance(nodata, float) and math.isnan(nodata)):
        arr[arr == nodata] = np.nan
    else:
        arr[arr < -90] = np.nan
    return arr


def get_dem_source(bbox_nztm, trail_name, dem_source=None, buffer=60, note=None,
                    corridor_nztm=None):
    """Resolve a DEM for bbox_nztm, trying each source in order: an uploaded
    file, the bundled local Canterbury/Christchurch tiles, then LINZ's Export
    API. corridor_nztm (a shapely LineString) lets the export fallback crop
    to a tight buffer around the trail's own line rather than its bounding
    rectangle — matters for winding/looping trails, where the bbox can cover
    many times the area the trail actually passes through. Returns (array,
    transform, nodata, source) where source is 'uploaded', 'local' or
    'export'; raises ValueError if nothing covers this area."""
    if dem_source is not None:
        arr, transform, nodata = dem_source
        return _mask_nodata(arr, nodata), transform, nodata, "uploaded"
    local = _try_local_dtm(bbox_nztm, buffer=buffer)
    if local is not None:
        arr, transform, nodata = local
        return _mask_nodata(arr, nodata), transform, nodata, "local"
    exported = _cached_export_dem(bbox_nztm, trail_name, buffer, note=note,
                                   corridor_nztm=corridor_nztm)
    if exported is not None:
        arr, transform, nodata = exported
        return _mask_nodata(arr, nodata), transform, nodata, "export"
    raise ValueError(
        "No LiDAR coverage for this area — it's outside the bundled "
        "Canterbury/Christchurch tiles, and LINZ's national mosaic export "
        "found nothing here (either no LiDAR survey covers it, or the "
        "export API is unavailable right now).")


def parse_kml(kml_bytes):
    from pykml import parser as kml_parser
    root = kml_parser.parse(io.BytesIO(kml_bytes)).getroot()
    nodes = root.findall(".//{http://www.opengis.net/kml/2.2}coordinates")
    if not nodes:
        raise ValueError("No <coordinates> element in the KML.")
    points = []
    for pt in nodes[0].text.strip().split():
        lon, lat, *_ = pt.split(",")
        points.append((float(lon), float(lat)))
    return points


def extract_photo_gps(photo_bytes):
    """(lat, lon) from a JPEG's EXIF GPS tags, or None if absent/unreadable.
    Prototype input path for the AI trail camera roadmap item — a
    geotagged photo rather than GPMF video telemetry, which is a much
    bigger lift to parse. Most phones and some action cameras geotag
    still photos directly in EXIF when location is enabled."""
    from PIL import ExifTags, Image

    def _to_deg(value, ref):
        d, m, s = value
        deg = float(d) + float(m) / 60 + float(s) / 3600
        return -deg if ref in ("S", "W") else deg

    try:
        img = Image.open(io.BytesIO(photo_bytes))
        exif = img.getexif()
        gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo)
        if not gps_ifd or 2 not in gps_ifd or 4 not in gps_ifd:
            return None
        lat = _to_deg(gps_ifd[2], gps_ifd.get(1, "N"))
        lon = _to_deg(gps_ifd[4], gps_ifd.get(3, "E"))
        return lat, lon
    except Exception:
        return None


def nearest_chainage(points_wgs84_dense, lat, lon):
    """Index of the 1 m-densified point closest to (lat, lon) — since the
    line is sampled at ~1 m spacing, that index doubles as the chainage in
    metres. Simple nearest-neighbour, not GIS snapping; good enough for
    placing a hazard flagged from a GPS-tagged photo onto the same
    chainage grid as the gradient/radius/sideslope data."""
    best_i, best_d2 = 0, float("inf")
    for i, (plon, plat) in enumerate(points_wgs84_dense):
        d2 = (plon - lon) ** 2 + (plat - lat) ** 2
        if d2 < best_d2:
            best_i, best_d2 = i, d2
    return best_i


def extract_video_frames(video_bytes, filename_hint="clip.mp4", max_frames=6):
    """Evenly-spaced JPEG frames from an uploaded video, via imageio's
    bundled ffmpeg (imageio-ffmpeg ships its own static binary — works the
    same in production as it does locally, no system ffmpeg package
    needed). Frame count is capped regardless of video length by scaling
    the sampling interval to the clip's own duration, not a fixed seconds
    value, which also keeps Claude vision call count (cost/time) bounded.

    No per-frame GPS: that would mean parsing GPMF (GoPro) or similar
    embedded telemetry, a genuinely hard binary format — exactly the
    "accuracy/drift correction" open question already flagged on
    /roadmap/vision. Caller interpolates chainage from a start/end metre
    range instead, if given (see run_vision's video_start_m/video_end_m).

    Returns a list of (jpeg_bytes, fraction_through_video) tuples.
    Writes to a real temp file rather than decoding from bytes in memory —
    imageio's ffmpeg plugin needs a seekable path, and decoding one frame
    at a time (not the whole video) keeps peak memory close to a single
    frame's size regardless of clip length."""
    import io as _io
    import os as _os
    import tempfile

    import imageio.v3 as iio
    from PIL import Image

    suffix = _os.path.splitext(filename_hint)[1] or ".mp4"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(video_bytes)
            tmp_path = tmp.name

        meta = iio.immeta(tmp_path, plugin="FFMPEG")
        duration_s = meta.get("duration") or 0
        fps = meta.get("fps") or 25
        if not duration_s:
            raise ValueError("Could not read that video's duration — is it a valid video file?")

        n_frames = min(max_frames, max(1, int(duration_s)))
        timestamps = [duration_s * (i + 0.5) / n_frames for i in range(n_frames)]

        results = []
        for t in timestamps:
            try:
                arr = iio.imread(tmp_path, plugin="FFMPEG", index=int(t * fps))
            except Exception:
                continue
            img = Image.fromarray(arr)
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            results.append((buf.getvalue(), t / duration_s if duration_s else 0.0))
        return results
    finally:
        if tmp_path and _os.path.exists(tmp_path):
            _os.remove(tmp_path)


def bearing(p1, p2):
    return math.atan2(p2[1] - p1[1], p2[0] - p1[0])


def steep_sideslope_runs(sideslopes, station_step, threshold_deg=STEEP_SIDESLOPE_DEG):
    runs, cur = [], None
    for i, a, _ in sideslopes:
        steep = a is not None and abs(a) >= threshold_deg
        if steep:
            if cur is not None and i - cur["end_m"] <= station_step:
                cur["end_m"] = i
                cur["peak_deg"] = max(cur["peak_deg"], abs(a))
            else:
                if cur is not None:
                    runs.append(cur)
                cur = {"start_m": i, "end_m": i, "peak_deg": abs(a)}
        elif cur is not None:
            runs.append(cur)
            cur = None
    if cur is not None:
        runs.append(cur)
    for r in runs:
        r["length_m"] = r["end_m"] - r["start_m"] + station_step
        r["peak_deg"] = round(r["peak_deg"], 1)
    return runs


def _exceedance_runs(stations_values, station_step, is_bad, worse):
    """Group consecutive out-of-spec samples into contiguous chainage runs —
    same shape as steep_sideslope_runs, generalised for gradient (above a
    limit) and turn radius (below a limit). stations_values is a list of
    (chainage_m, value); worse(a, b) returns whichever of two values is more
    out-of-spec, for tracking each run's peak. DOC asked for this
    specifically: individual out-of-grade sections with their length in
    metres, so an inspector can total it against the guidelines' tolerance
    for the signposted grade, rather than just seeing one aggregate %."""
    runs, cur = [], None
    for i, v in stations_values:
        bad = is_bad(v)
        if bad:
            if cur is not None and i - cur["end_m"] <= station_step:
                cur["end_m"] = i
                cur["peak"] = worse(cur["peak"], v)
            else:
                if cur is not None:
                    runs.append(cur)
                cur = {"start_m": i, "end_m": i, "peak": v}
        elif cur is not None:
            runs.append(cur)
            cur = None
    if cur is not None:
        runs.append(cur)
    for r in runs:
        r["length_m"] = r["end_m"] - r["start_m"] + station_step
        # float(): gradient values come from a numpy array (np.float64),
        # radius values from pure-Python math — normalise both to plain
        # Python floats so the result dict stays JSON-serialisable.
        r["peak"] = float(round(r["peak"], 1))
    return runs


def merge_out_of_grade_sections(gradient_exceedances, radius_exceedances, sideslope_exceedances):
    """Collapse the three separate gradient/radius/sideslope run lists into
    one physical-chainage view — DOC and inspectors think in terms of "this
    stretch of trail is a problem", not which specific metric tripped it.
    Each run's [start_m, start_m+length_m) already accounts for its own
    sampling step (1 m for gradient/radius, 5 m for sideslope), so merging
    on that physical extent — rather than raw start_m/end_m, which use
    different step conventions — keeps the combined lengths honest."""
    tagged = []
    for r in gradient_exceedances:
        tagged.append((r["start_m"], r["start_m"] + r["length_m"], "gradient", r["peak"]))
    for r in radius_exceedances:
        tagged.append((r["start_m"], r["start_m"] + r["length_m"], "radius", r["peak"]))
    for r in sideslope_exceedances:
        tagged.append((r["start_m"], r["start_m"] + r["length_m"], "sideslope", r["peak_deg"]))
    tagged.sort(key=lambda t: t[0])

    worse = {
        "gradient": lambda a, b: a if abs(a) >= abs(b) else b,
        "radius": min,
        "sideslope": lambda a, b: a if abs(a) >= abs(b) else b,
    }
    label = {
        "gradient": lambda v: f"Gradient {v}%",
        "radius": lambda v: f"Turn radius {v} m",
        "sideslope": lambda v: f"Sideslope {v}°",
    }

    merged = []
    for start, end, kind, peak in tagged:
        if merged and start <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], end)
            merged[-1]["peaks"][kind] = worse[kind](merged[-1]["peaks"].get(kind, peak), peak)
        else:
            merged.append({"start": start, "end": end, "peaks": {kind: peak}})

    return [{
        "start_m": round(m["start"]),
        "end_m": round(m["end"]),
        "length_m": round(m["end"] - m["start"], 1),
        "reasons": [{"kind": kind, "label": label[kind](peak)} for kind, peak in m["peaks"].items()],
    } for m in merged]


def circumradius(p1, p2, p3):
    (ax, ay), (bx, by), (cx, cy) = p1, p2, p3
    a, b, c = math.dist(p1, p2), math.dist(p2, p3), math.dist(p1, p3)
    area = abs((ax * (by - cy) + bx * (cy - ay) + cx * (ay - by)) / 2)
    return float("inf") if area == 0 else (a * b * c) / (4 * area)


def find_apex(points, start, end):
    max_change, apex = 0.0, (start + end) // 2
    for i in range(start + 1, end - 1):
        change = abs(bearing(points[i], points[i + 1]) - bearing(points[i - 1], points[i]))
        if change > math.pi:
            change = 2 * math.pi - change
        if change > max_change:
            max_change, apex = change, i
    return apex


def gradient_color(g):
    g = abs(g)
    if g < 10: return "#00cc00"
    if g < 15: return "#ffff00"
    if g < 20: return "#ff8800"
    if g < 27: return "#ff4400"
    return "#cc0000"


def radius_color(r):
    if r > 5.0: return "#00cc00"
    if r > 4.0: return "#ffff00"
    if r > 2.5: return "#ff8800"
    if r > 2.0: return "#ff4400"
    return "#cc0000"


def sideslope_color(a):
    a = abs(a)
    if a < 5: return "#00cc00"
    if a < 15: return "#ffff00"
    if a < 30: return "#ff8800"
    if a < 45: return "#ff4400"
    return "#cc0000"


def _png_b64(fig):
    import matplotlib.pyplot as plt
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return "data:image/png;base64," + base64.b64encode(buf.read()).decode("ascii")


def _array_png_b64(rgba):
    import matplotlib.pyplot as plt
    buf = io.BytesIO()
    plt.imsave(buf, rgba, format="png")
    buf.seek(0)
    return "data:image/png;base64," + base64.b64encode(buf.read()).decode("ascii")


TERRAIN_BUFFER_M = 60
TERRAIN_HS_AZIMUTHS = (225, 270, 315, 360)


def terrain_overlays(points_wgs84, buffer_m=TERRAIN_BUFFER_M, vert_exag=1.5, dem_source=None,
                      trail_name="Trail", progress=None):
    """Same method as Askins' precomputed hillshade/slope: shade the native
    1 m DEM directly (no coarse resampling), multidirectional so the bench
    cut reads clearly, rather than a single low-angle light. Outside the
    bundled Canterbury/Christchurch tiles, get_dem_source transparently pulls
    a 1 m export from LINZ's national mosaic instead — same resolution, same
    hillshade math, just a different source array."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LightSource

    points_nztm = [to_nztm.transform(lon, lat) for lon, lat in points_wgs84]
    xs = [p[0] for p in points_nztm]
    ys = [p[1] for p in points_nztm]
    bbox = [min(xs), min(ys), max(xs), max(ys)]

    arr, transform, nodata, _source = get_dem_source(
        bbox, trail_name, dem_source=dem_source, buffer=buffer_m, note=progress,
        corridor_nztm=LineString(points_nztm))
    # get_dem_source already nodata-masks arr to NaN regardless of source;
    # copy=False is a no-op when it's already float32 (bundled tiles and
    # LINZ exports both are) — avoids silently doubling a large trail's
    # raster to float64 just to be safe for the int-array case.
    elev_grid = arr.astype(np.float32, copy=False)
    cellsize_x = transform.a
    cellsize_y = -transform.e

    if np.isnan(elev_grid).all():
        raise ValueError("No LiDAR coverage (bundled or national) for this area.")
    if np.isnan(elev_grid).any():
        elev_grid = np.where(np.isnan(elev_grid), np.nanmean(elev_grid), elev_grid)

    ls_stack = [LightSource(azdeg=az, altdeg=45) for az in TERRAIN_HS_AZIMUTHS]
    hillshade_avg = np.mean(
        [ls.hillshade(elev_grid, vert_exag=vert_exag, dx=cellsize_x, dy=cellsize_y) for ls in ls_stack],
        axis=0)
    hillshade_rgba = plt.cm.gray(hillshade_avg)

    dzdy, dzdx = np.gradient(elev_grid, cellsize_y, cellsize_x)
    slope_deg = np.degrees(np.arctan(np.hypot(dzdx, dzdy)))
    slope_rgba = plt.cm.gray(1 - np.clip(slope_deg / 45, 0, 1))

    ny, nx = elev_grid.shape
    # Actual raster bounds, not the (pre-buffer) point bbox: with a corridor
    # export the two can differ, and local-tile merges pad internally too.
    left, bottom, right, top = array_bounds(ny, nx, transform)
    sw_lon, sw_lat = to_wgs84.transform(left, bottom)
    ne_lon, ne_lat = to_wgs84.transform(right, top)

    return {
        "hillshade_data_uri": _array_png_b64(hillshade_rgba),
        "slope_data_uri": _array_png_b64(slope_rgba),
        "bounds": [[sw_lat, sw_lon], [ne_lat, ne_lon]],
        "grid": f"{nx}x{ny}",
    }


def run_audit(kml_bytes, trail_name="Trail", stated_grade=4, progress=None, want_sideslope=True,
              dem_source=None):
    """Runs the full v11 pipeline. `progress(msg)` is called with short status
    strings, if given, so a caller can show something better than a blank page
    while the elevation sampling and chart rendering run."""
    def note(msg):
        if progress:
            progress(msg)

    points_wgs84 = parse_kml(kml_bytes)

    # ---- reproject + densify to 1 m spacing ----
    points_nztm = [to_nztm.transform(lon, lat) for lon, lat in points_wgs84]
    line = LineString(points_nztm)
    points_nztm_dense = [(p.x, p.y) for p in
                          (line.interpolate(d) for d in np.arange(0, line.length, 1.0))]
    points_wgs84_dense = [to_wgs84.transform(x, y) for x, y in points_nztm_dense]

    # ---- elevations (local 1 m LiDAR DEM, falling back to a LINZ national-
    # mosaic export outside the bundled Canterbury/Christchurch tiles) ----
    xs_d = [p[0] for p in points_nztm_dense]
    ys_d = [p[1] for p in points_nztm_dense]
    bbox = [min(xs_d), min(ys_d), max(xs_d), max(ys_d)]
    note(f"Sampling {len(points_nztm_dense)} elevations…")
    arr, transform, _nodata, source = get_dem_source(
        bbox, trail_name, dem_source=dem_source, note=note, corridor_nztm=line)
    local_source = (arr, transform, _nodata)
    if source == "export":
        note("Exported elevation data from LINZ's national mosaic — sampling…")
    elevations_dense = [sample_raster(arr, transform, x, y) for x, y in points_nztm_dense]

    keep = [i for i, e in enumerate(elevations_dense) if not np.isnan(e)]
    if len(keep) < 10:
        if source == "uploaded":
            raise ValueError(
                "Too few elevations came back from the uploaded DEM (%d of %d) — "
                "check the trail falls inside the DEM's extent." % (len(keep), len(elevations_dense)))
        if source == "export":
            raise ValueError(
                "Too few elevations came back from LINZ's national LiDAR mosaic "
                "export (%d of %d) — this trail falls outside NZ's current LiDAR "
                "survey coverage." % (len(keep), len(elevations_dense)))
        raise ValueError(
            "Too few elevations came back from the local LiDAR DEM (%d of %d) — "
            "check the trail falls inside the tile coverage configured in "
            "config.json's dtm_folder." % (len(keep), len(elevations_dense)))
    points_nztm_dense = [points_nztm_dense[i] for i in keep]
    points_wgs84_dense = [points_wgs84_dense[i] for i in keep]
    elevations_dense = [float(elevations_dense[i]) for i in keep]

    # ---- gradient ----
    gradients_dense = []
    for i in range(1, len(points_nztm_dense)):
        x1, y1 = points_nztm_dense[i - 1]
        x2, y2 = points_nztm_dense[i]
        run = math.hypot(x2 - x1, y2 - y1)
        rise = elevations_dense[i] - elevations_dense[i - 1]
        gradients_dense.append((rise / run * 100) if run > 0 else 0.0)

    window = 50
    smoothed_dense = np.convolve(gradients_dense, np.ones(window) / window, mode="valid")

    avg_gradient = abs(np.mean(smoothed_dense))
    worst_gradient = float(np.max(np.abs(smoothed_dense)))
    n = len(smoothed_dense)

    gradient_grade, gradient_reason = 6, "exceeds all defined limits"
    for grade, avg_limit, abs_limit, tolerance in GRADIENT_GRADES:
        pct_out = len([g for g in smoothed_dense if abs(g) > abs_limit]) / n * 100
        one_harder = GRADIENT_ABS_MAX.get(grade + 1, float("inf"))
        if avg_gradient <= avg_limit and pct_out <= tolerance and worst_gradient <= one_harder:
            cap = "no cap" if math.isinf(one_harder) else f"cap {one_harder}%"
            gradient_grade = grade
            gradient_reason = (f"avg {avg_gradient:.1f}% (limit {avg_limit}%), "
                                f"{pct_out:.1f}% over {abs_limit}% (tolerance {tolerance}%), "
                                f"steepest {worst_gradient:.1f}% ({cap})")
            break

    # ---- out-of-grade sections (gradient), against the STATED/signposted
    # grade specifically — DOC wants each section individually with its
    # length in metres, so an inspector can total it against the
    # guidelines' tolerance for that grade and see at a glance whether the
    # track passes or needs regrading/realigning. ----
    stated_abs_limit = GRADIENT_ABS_MAX[stated_grade]
    gradient_offset = window // 2  # smoothed_dense[i] ~= chainage i+offset (convolve('valid') drops window-1 samples)
    gradient_exceedances = _exceedance_runs(
        [(i + gradient_offset, v) for i, v in enumerate(smoothed_dense)],
        station_step=1, is_bad=lambda v: abs(v) > stated_abs_limit,
        worse=lambda a, b: a if abs(a) >= abs(b) else b)
    gradient_out_length_m = round(sum(r["length_m"] for r in gradient_exceedances), 1)
    gradient_out_pct = round(gradient_out_length_m / line.length * 100, 1) if line.length else 0.0
    gradient_tolerance_pct = next(t for g, _, _, t in GRADIENT_GRADES if g == stated_grade)

    # ---- corner radius ----
    step = 5
    radii_sampled = [
        circumradius(points_nztm_dense[i - step], points_nztm_dense[i], points_nztm_dense[i + step])
        for i in range(step, len(points_nztm_dense) - step)
    ]
    stated_min = RADIUS_MIN[stated_grade]
    under = len([r for r in radii_sampled if r < stated_min]) / len(radii_sampled) * 100

    radii_filtered = [r for r in radii_sampled if r >= 1.0]
    tightest = min(radii_filtered) if radii_filtered else float("inf")

    radius_grade, radius_reason = 6, "tighter than all defined limits"
    for grade, min_radius, tolerance in RADIUS_GRADES:
        pct_out = len([r for r in radii_filtered if r < min_radius]) / len(radii_filtered) * 100
        one_harder = RADIUS_MIN.get(grade + 1, 0.0)
        if pct_out <= tolerance and tightest >= one_harder:
            radius_grade = grade
            radius_reason = (f"{pct_out:.1f}% under {min_radius} m (tolerance {tolerance}%), "
                              f"tightest {tightest:.1f} m (floor {one_harder} m)")
            break

    # ---- out-of-grade sections (turn radius), same approach as gradient ----
    radius_exceedances = _exceedance_runs(
        [(step + i, v) for i, v in enumerate(radii_sampled)],
        station_step=1, is_bad=lambda v: v < stated_min, worse=min)
    radius_out_length_m = round(sum(r["length_m"] for r in radius_exceedances), 1)
    radius_out_pct = round(radius_out_length_m / line.length * 100, 1) if line.length else 0.0
    radius_tolerance_pct = next(t for g, _, t in RADIUS_GRADES if g == stated_grade)

    overall_grade = max(gradient_grade, radius_grade)

    # ---- corner detection ----
    corner_threshold, min_corner_gap = 10.0, 30
    corners, in_corner, corner_start = [], False, 0
    last_apex = -min_corner_gap * 2
    for i, r in enumerate(radii_sampled):
        if r < corner_threshold and not in_corner:
            in_corner, corner_start = True, i
        elif r >= corner_threshold and in_corner:
            in_corner = False
            apex = find_apex(points_nztm_dense, corner_start + step, i + step)
            if apex - last_apex > min_corner_gap:
                corners.append((apex, min(radii_sampled[corner_start:i])))
                last_apex = apex

    # ---- sideslope ----
    station_step, offsets = 5, [-8, -6, -5, 5, 6, 8]
    sideslopes, valid_ss = [], []
    mean_abs_sideslope = pct_steep_sideslope = None

    if want_sideslope:
        note("Sampling cross-slope…")
        dir_step = 3
        stations, sample_pts = [], []
        for i in range(dir_step, len(points_nztm_dense) - dir_step, station_step):
            stations.append(i)
            b = bearing(points_nztm_dense[i - dir_step], points_nztm_dense[i + dir_step]) + math.pi / 2
            cx, cy = points_nztm_dense[i]
            sample_pts += [(cx + d * math.cos(b), cy + d * math.sin(b)) for d in offsets]

        # Same raster as the main centreline sample (local tiles or export,
        # whichever get_dem_source resolved above) — its buffer already
        # covers the +/-8 m sideslope offsets, so no separate fetch needed.
        arr, transform, _nodata = local_source
        ss_elevs = [sample_raster(arr, transform, x, y) for x, y in sample_pts]

        def sideslope_band(a):
            a = abs(a)
            if a < 5: return "<5"
            if a < 15: return "5-15"
            if a < 30: return "15-30"
            if a < 45: return "30-45"
            return ">45"

        n_off = len(offsets)
        for k, i in enumerate(stations):
            z = ss_elevs[k * n_off:(k + 1) * n_off]
            pairs = [(o, e) for o, e in zip(offsets, z) if not np.isnan(e)]
            if len(pairs) < 2:
                sideslopes.append((i, None, None))
                continue
            slope = np.polyfit([p[0] for p in pairs], [p[1] for p in pairs], 1)[0]
            angle = math.degrees(math.atan(slope))
            sideslopes.append((i, angle, sideslope_band(angle)))

        valid_ss = [(i, a) for i, a, _ in sideslopes if a is not None]
        mean_abs_sideslope = float(np.mean(np.abs([a for _, a in valid_ss]))) if valid_ss else None
        pct_steep_sideslope = (len([a for _, a in valid_ss if abs(a) >= STEEP_SIDESLOPE_DEG]) / len(valid_ss) * 100
                                ) if valid_ss else None
        sideslope_exceedances = steep_sideslope_runs(sideslopes, station_step)
    else:
        sideslope_exceedances = []

    sideslope_out_length_m = round(sum(r["length_m"] for r in sideslope_exceedances), 1)
    sideslope_out_pct = round(sideslope_out_length_m / line.length * 100, 1) if line.length else 0.0

    out_of_grade_sections = merge_out_of_grade_sections(
        gradient_exceedances, radius_exceedances, sideslope_exceedances)
    out_of_grade_length_m = round(sum(s["length_m"] for s in out_of_grade_sections), 1)
    out_of_grade_pct = round(out_of_grade_length_m / line.length * 100, 1) if line.length else 0.0

    # ---- gradient profile (matplotlib PNG) ----
    note("Drawing charts…")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from scipy.ndimage import uniform_filter1d
    abs_limit = stated_abs_limit  # same value, computed earlier alongside gradient_exceedances
    x = np.arange(len(smoothed_dense))
    y = np.asarray(smoothed_dense)
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.fill_between(x, y, 0, where=np.abs(y) <= abs_limit, color="green", alpha=0.4,
                     interpolate=True, label="within spec")
    ax.fill_between(x, y, 0, where=np.abs(y) > abs_limit, color="red", alpha=0.4,
                     interpolate=True, label="over spec")
    ax.plot(x, y, linewidth=0.8, color="#333333")
    ax.axhline(+abs_limit, color="red", linestyle="--", linewidth=1.5,
               label=f"Grade {stated_grade} max ({abs_limit}%)")
    ax.axhline(-abs_limit, color="red", linestyle="--", linewidth=1.5)
    ax.axhline(0, color="gray", linewidth=0.5)
    ax.set_xlabel("Distance along trail (m)")
    ax.set_ylabel("Gradient (%)")
    ax.set_title(f"Gradient Profile — {trail_name} (stated Grade {stated_grade})")
    ax.legend(loc="upper right")
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    gradient_png = _png_b64(fig)

    sideslope_png = None
    if valid_ss:
        dist = [i for i, _ in valid_ss]
        ang = np.array([abs(a) for _, a in valid_ss])
        ang_smooth = uniform_filter1d(ang, size=5)
        fig2, ax2 = plt.subplots(figsize=(14, 5))
        ax2.fill_between(dist, ang_smooth, STEEP_SIDESLOPE_DEG,
                          where=ang_smooth >= STEEP_SIDESLOPE_DEG,
                          color="red", alpha=0.25, interpolate=True,
                          label=f"≥{STEEP_SIDESLOPE_DEG}° steep")
        ax2.plot(dist, ang, color="#ffaa66", linewidth=0.6, alpha=0.5, label="raw")
        ax2.plot(dist, ang_smooth, color="#cc4400", linewidth=2, label="smoothed (25 m)")
        ymax = max(float(ang.max()) * 1.1, 5.0)
        for yv in (5, 15, 30, 45):
            if yv <= ymax:
                ax2.axhline(yv, color="gray", linestyle="--", linewidth=0.8)
                ax2.text(dist[-1], yv, f" {yv}°", va="center", fontsize=8, color="gray")
        ax2.set_xlabel("Distance along trail (m)")
        ax2.set_ylabel("Sideslope (°)")
        ax2.set_title(f"{trail_name} — sideslope profile")
        ax2.set_ylim(0, ymax)
        ax2.grid(True, alpha=0.3)
        ax2.legend(loc="upper right")
        plt.tight_layout()
        sideslope_png = _png_b64(fig2)

    result = {
        "trail_name": trail_name,
        "stated_grade": stated_grade,
        "gradient_grade": gradient_grade, "gradient_reason": gradient_reason,
        "radius_grade": radius_grade, "radius_reason": radius_reason,
        "overall_grade": overall_grade,
        "avg_gradient": round(avg_gradient, 1),
        "worst_gradient": round(worst_gradient, 1),
        "tightest_corner": None if math.isinf(tightest) else round(tightest, 1),
        "pct_under_stated_min": round(under, 1),
        "gradient_exceedances": gradient_exceedances,
        "gradient_out_length_m": gradient_out_length_m,
        "gradient_out_pct": gradient_out_pct,
        "gradient_tolerance_pct": gradient_tolerance_pct,
        "radius_exceedances": radius_exceedances,
        "radius_out_length_m": radius_out_length_m,
        "radius_out_pct": radius_out_pct,
        "radius_tolerance_pct": radius_tolerance_pct,
        "n_corners": len(corners),
        "mean_abs_sideslope": round(mean_abs_sideslope, 1) if mean_abs_sideslope is not None else None,
        "pct_steep_sideslope": round(pct_steep_sideslope, 1) if pct_steep_sideslope is not None else None,
        "sideslopes": sideslopes,
        "sideslope_station_step": station_step,
        "sideslope_exceedances": sideslope_exceedances,
        "sideslope_out_length_m": sideslope_out_length_m,
        "sideslope_out_pct": sideslope_out_pct,
        "out_of_grade_sections": out_of_grade_sections,
        "out_of_grade_length_m": out_of_grade_length_m,
        "out_of_grade_pct": out_of_grade_pct,
        "steep_sideslope_deg": STEEP_SIDESLOPE_DEG,
        "trail_length_m": round(line.length, 1),
        "points_wgs84_dense": points_wgs84_dense,
        "points_nztm_dense": points_nztm_dense,
        "elevations_dense": elevations_dense,
        "smoothed_dense": smoothed_dense.tolist(),
        "window": window,
        "corners": corners,
        "gradient_png": gradient_png,
        "sideslope_png": sideslope_png,
    }
    return result


def render_folium_map(result):
    import folium
    pts = result["points_wgs84_dense"]
    smoothed = result["smoothed_dense"]
    window = result["window"]
    corners = result["corners"]

    centre_lat = float(np.mean([lat for _, lat in pts]))
    centre_lon = float(np.mean([lon for lon, _ in pts]))
    m = folium.Map(location=[centre_lat, centre_lon], zoom_start=15)

    gradient_layer = folium.FeatureGroup(name="Gradient", show=True)
    offset = window // 2
    aligned = pts[offset: offset + len(smoothed)]
    for i in range(len(aligned) - 1):
        lon1, lat1 = aligned[i]
        lon2, lat2 = aligned[i + 1]
        folium.PolyLine(locations=[[lat1, lon1], [lat2, lon2]],
                         color=gradient_color(smoothed[i]), weight=4,
                         tooltip=f"{i + offset} m — gradient {smoothed[i]:+.1f}%"
                         ).add_to(gradient_layer)
    gradient_layer.add_to(m)

    sideslopes = result.get("sideslopes", [])
    ss_step = result.get("sideslope_station_step", 5)
    sideslope_layer = folium.FeatureGroup(name="Sideslope", show=True)
    for k in range(len(sideslopes) - 1):
        i1, a1, _ = sideslopes[k]
        i2, a2, _ = sideslopes[k + 1]
        if a1 is None or a2 is None or (i2 - i1) > ss_step * 2:
            continue
        lon1, lat1 = pts[i1]
        lon2, lat2 = pts[i2]
        folium.PolyLine(locations=[[lat1, lon1], [lat2, lon2]],
                         color=sideslope_color(a1), weight=5, opacity=0.85,
                         tooltip=f"{i1} m — sideslope {a1:+.0f}°"
                         ).add_to(sideslope_layer)
    sideslope_layer.add_to(m)

    radius_layer = folium.FeatureGroup(name="Turn radius", show=True)
    for idx, r in corners:
        lon, lat = pts[idx]
        folium.CircleMarker(location=[lat, lon], radius=10, color=radius_color(r),
                             fill=True, fill_opacity=0.9,
                             tooltip=f"{idx} m — turn radius {r:.1f} m").add_to(radius_layer)
    radius_layer.add_to(m)

    # ---- out-of-grade sections — a dedicated, high-contrast layer, drawn
    # last so it sits on top of the continuous gradient/sideslope colouring.
    # The colour ramps above already use green-through-red for severity, so
    # this deliberately uses colours outside that ramp (magenta, cyan) —
    # "out of grade against what's signposted" is a different question from
    # "how steep is this bit", and needs to read as obviously different, not
    # just another shade of red. Thick + dashed + a marker at the midpoint
    # of each run so it's still visible zoomed out, not just as a thin line. ----
    # control=False: keep it out of the generic LayerControl dropdown (which
    # already lists Gradient/Sideslope/Turn radius) — this gets its own
    # dedicated on-map button below instead of being one more checkbox
    # buried in a menu, since this is the one layer that answers "where
    # does this fail its signposted grade" and should be a single click.
    # Separate layers per aspect — gradient sections are OVER the stated
    # grade's max, radius sections are UNDER its minimum. One combined
    # button couldn't show just one or the other; this can.
    gradient_exceed_layer = folium.FeatureGroup(name="Gradient out-of-grade", show=True, control=False)
    radius_exceed_layer = folium.FeatureGroup(name="Turn radius under-grade", show=True, control=False)
    sideslope_exceed_layer = folium.FeatureGroup(name="Steep sideslope", show=True, control=False)

    def _add_exceed_runs(runs, colour, label, unit, layer, peak_key="peak"):
        for run in runs:
            lo, hi = max(0, run["start_m"]), min(len(pts) - 1, run["end_m"])
            seg = pts[lo:hi + 1]
            if len(seg) < 2:
                continue
            tip = (f"{label}: {run['start_m']}-{run['end_m']} m "
                   f"({run['length_m']} m), peak {run[peak_key]}{unit}")
            folium.PolyLine(locations=[[lat, lon] for lon, lat in seg],
                             color=colour, weight=9, opacity=0.95, dash_array="6,8",
                             tooltip=tip).add_to(layer)
            mid_lon, mid_lat = seg[len(seg) // 2]
            folium.CircleMarker(location=[mid_lat, mid_lon], radius=6, color=colour,
                                 fill=True, fill_opacity=1.0, weight=2,
                                 tooltip=tip).add_to(layer)

    _add_exceed_runs(result.get("gradient_exceedances", []), "#ff00ff",
                      "OUT OF GRADE — gradient", "%", gradient_exceed_layer)
    _add_exceed_runs(result.get("radius_exceedances", []), "#00e5ff",
                      "UNDER GRADE — turn radius", " m", radius_exceed_layer)
    _add_exceed_runs(result.get("sideslope_exceedances", []), "#8800ff",
                      f"STEEP SIDESLOPE (≥{result.get('steep_sideslope_deg', 30)}°)", "°",
                      sideslope_exceed_layer, peak_key="peak_deg")
    gradient_exceed_layer.add_to(m)
    radius_exceed_layer.add_to(m)
    sideslope_exceed_layer.add_to(m)

    # ---- manual field notes — a point an inspector drops for something
    # they've spotted on the ground (a root, loose rock, a jump needing
    # maintenance) that LiDAR can't see at all, separate from the
    # automatically-detected gradient/radius sections above. Client-side
    # only for now (not saved server-side) — "Download field notes" exports
    # them as KML before the tab closes. ----
    field_notes_layer = folium.FeatureGroup(name="Field notes", show=True, control=False)
    field_notes_layer.add_to(m)

    folium.Marker(location=[pts[0][1], pts[0][0]], tooltip="Start",
                  icon=folium.Icon(color="green", icon="play")).add_to(m)
    folium.Marker(location=[pts[-1][1], pts[-1][0]], tooltip="Finish",
                  icon=folium.Icon(color="red", icon="stop")).add_to(m)
    folium.LayerControl().add_to(m)

    # Standalone buttons (not the LayerControl checkbox above) — one per
    # aspect, so gradient and radius can be shown/hidden independently
    # instead of one combined on/off, plus an "Add field note" mode for
    # pointing at something an inspector spots on the ground that none of
    # the automated layers cover.
    exceed_toggle_js = f"""
    <script>
    document.addEventListener('DOMContentLoaded', function() {{
        var map = {m.get_name()};
        var gradientLayer = {gradient_exceed_layer.get_name()};
        var radiusLayer = {radius_exceed_layer.get_name()};
        var sideslopeLayer = {sideslope_exceed_layer.get_name()};
        var fieldNotesLayer = {field_notes_layer.get_name()};
        var trailName = {json.dumps(result.get("trail_name", "Trail"))};

        function makeToggleControl(layer, colourOn, labelOn, labelOff) {{
            return L.Control.extend({{
                options: {{ position: 'topleft' }},
                onAdd: function() {{
                    var btn = L.DomUtil.create('button');
                    var on = true;
                    function render() {{
                        btn.textContent = (on ? 'Hide ' : 'Show ') + labelOn;
                        btn.style.cssText = 'font: 13px/1 -apple-system,Segoe UI,sans-serif; font-weight: 700;'
                            + 'padding: 8px 12px; border: 0; border-radius: 4px; cursor: pointer; display: block;'
                            + 'box-shadow: 0 1px 4px rgba(0,0,0,0.4); margin: 10px 10px 0;'
                            + (on ? ('background:' + colourOn + '; color:#fff;') : 'background:#888; color:#fff;');
                    }}
                    render();
                    L.DomEvent.on(btn, 'click', function(e) {{
                        L.DomEvent.stopPropagation(e);
                        if (on) {{ map.removeLayer(layer); }} else {{ map.addLayer(layer); }}
                        on = !on;
                        render();
                    }});
                    L.DomEvent.disableClickPropagation(btn);
                    return btn;
                }}
            }});
        }}
        map.addControl(new (makeToggleControl(gradientLayer, '#ff00ff', 'gradient out-of-grade'))());
        map.addControl(new (makeToggleControl(radiusLayer, '#00e5ff', 'turn radius under-grade'))());
        map.addControl(new (makeToggleControl(sideslopeLayer, '#8800ff', 'steep sideslope'))());

        // ---- Field notes: click-to-place pins for anything an inspector
        // spots on the ground (surface, roots, rocks, a jump needing work)
        // that none of the automated layers can see. Stays in "add" mode
        // after each pin so several can be dropped in one pass. Not sent
        // to the server — Download exports them as KML before the tab closes.
        var fieldNotes = [];
        var addingNote = false;
        var addBtn, countEl;

        function fieldNoteIcon() {{
            return L.divIcon({{
                html: '<div style="background:#ffcc00;width:16px;height:16px;border-radius:50%;'
                    + 'border:2px solid #222;box-shadow:0 1px 3px rgba(0,0,0,0.5)"></div>',
                className: '', iconSize: [16, 16], iconAnchor: [8, 8]
            }});
        }}

        function updateAddBtn() {{
            addBtn.textContent = addingNote ? 'Click map to drop a pin (on)' : 'Add field note';
            addBtn.style.background = addingNote ? '#ffcc00' : '#888';
            addBtn.style.color = addingNote ? '#222' : '#fff';
        }}

        var AddNoteControl = L.Control.extend({{
            options: {{ position: 'topleft' }},
            onAdd: function() {{
                addBtn = L.DomUtil.create('button');
                addBtn.style.cssText = 'font: 13px/1 -apple-system,Segoe UI,sans-serif; font-weight: 700;'
                    + 'padding: 8px 12px; border: 0; border-radius: 4px; cursor: pointer; display: block;'
                    + 'box-shadow: 0 1px 4px rgba(0,0,0,0.4); margin: 10px 10px 0;';
                updateAddBtn();
                L.DomEvent.on(addBtn, 'click', function(e) {{
                    L.DomEvent.stopPropagation(e);
                    addingNote = !addingNote;
                    updateAddBtn();
                }});
                L.DomEvent.disableClickPropagation(addBtn);
                return addBtn;
            }}
        }});
        map.addControl(new AddNoteControl());

        var DownloadNotesControl = L.Control.extend({{
            options: {{ position: 'topleft' }},
            onAdd: function() {{
                var btn = L.DomUtil.create('button');
                countEl = document.createElement('span');
                function render() {{
                    btn.textContent = 'Download field notes (' + fieldNotes.length + ')';
                }}
                render();
                btn.style.cssText = 'font: 13px/1 -apple-system,Segoe UI,sans-serif; font-weight: 700;'
                    + 'padding: 8px 12px; border: 0; border-radius: 4px; cursor: pointer; display: block;'
                    + 'box-shadow: 0 1px 4px rgba(0,0,0,0.4); margin: 10px 10px 0;'
                    + 'background:#2a7a2a; color:#fff;';
                L.DomEvent.on(btn, 'click', function(e) {{
                    L.DomEvent.stopPropagation(e);
                    if (!fieldNotes.length) {{ return; }}
                    var placemarks = fieldNotes.map(function(n) {{
                        return '<Placemark><name>' + n.label.replace(/[<&]/g, '') + '</name>'
                            + '<Point><coordinates>' + n.lng + ',' + n.lat + ',0</coordinates></Point></Placemark>';
                    }}).join('');
                    var kml = '<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2">'
                        + '<Document><name>' + trailName + ' — field notes</name>' + placemarks + '</Document></kml>';
                    var blob = new Blob([kml], {{ type: 'application/vnd.google-earth.kml+xml' }});
                    var a = document.createElement('a');
                    a.href = URL.createObjectURL(blob);
                    a.download = trailName.replace(/[^a-z0-9]+/gi, '_') + '_field_notes.kml';
                    a.click();
                    btn._render = render;
                }});
                btn._render = render;
                L.DomEvent.disableClickPropagation(btn);
                return btn;
            }}
        }});
        var downloadCtl = new DownloadNotesControl();
        map.addControl(downloadCtl);

        map.on('click', function(e) {{
            if (!addingNote) return;
            var label = window.prompt('What did you see here? (e.g. "loose rock on tread", "root ball", "jump needs maintenance")');
            if (!label) return;
            var marker = L.marker(e.latlng, {{ icon: fieldNoteIcon() }})
                .bindTooltip('FIELD NOTE: ' + label)
                .addTo(fieldNotesLayer);
            fieldNotes.push({{ lat: e.latlng.lat, lng: e.latlng.lng, label: label }});
            if (downloadCtl._container) {{
                var btn = downloadCtl._container;
                btn.textContent = 'Download field notes (' + fieldNotes.length + ')';
            }}
        }});

    }});
    </script>
    """
    m.get_root().html.add_child(folium.Element(exceed_toggle_js))

    return m.get_root().render()


def _flythrough_frames(points_nztm_dense, elevations_dense, xlo, xhi, ylo, yhi, zlo, zhi, aspect,
                        step_m=4, dir_step=5, chase_back_m=20, chase_height_m=28,
                        lookahead_m=22, look_height_m=6):
    """Chase-cam keyframes trailing behind and above the trail (points_nztm_dense
    is ~1 m spaced), converted into Plotly's normalized scene-camera coordinate
    system. Riding at true eye height clips through the terrain wherever the
    coarse 60x60 Surface mesh disagrees with the trail's own point-sampled
    elevation, so the camera instead stays well clear of the surface and looks
    down at the line. The coordinate mapping matches the manual xaxis/yaxis/
    zaxis ranges and aspectratio set in render_plot3d, so this only works
    because those are set explicitly rather than left on Plotly's own "data"
    autorange."""
    def to_scene(x, y, z):
        return {
            "x": (x - (xlo + xhi) / 2) / (max(xhi - xlo, 1e-6) / 2) * aspect["x"],
            "y": (y - (ylo + yhi) / 2) / (max(yhi - ylo, 1e-6) / 2) * aspect["y"],
            "z": (z - (zlo + zhi) / 2) / (max(zhi - zlo, 1e-6) / 2) * aspect["z"],
        }

    n = len(points_nztm_dense)
    idx = list(range(0, n, step_m))
    if idx[-1] != n - 1:
        idx.append(n - 1)

    frames = []
    for i in idx:
        b0, b1 = max(0, i - dir_step), min(n - 1, i + dir_step)
        heading = bearing(points_nztm_dense[b0], points_nztm_dense[b1])

        px, py = points_nztm_dense[i]
        ex = px - math.cos(heading) * chase_back_m
        ey = py - math.sin(heading) * chase_back_m
        ez = elevations_dense[i] + chase_height_m

        look_i = min(n - 1, i + lookahead_m)
        lx, ly = points_nztm_dense[look_i]
        lz = elevations_dense[look_i] + look_height_m

        frames.append({"eye": to_scene(ex, ey, ez), "center": to_scene(lx, ly, lz),
                        "up": {"x": 0, "y": 0, "z": 1}})
    return frames


_FLYTHROUGH_HTML = """
<div id="flythrough-bar" style="position:fixed;left:16px;bottom:16px;z-index:1000;
  background:rgba(20,20,20,0.82);color:#fff;padding:10px 14px;border-radius:8px;
  font:13px/1.4 -apple-system,Segoe UI,sans-serif;display:flex;gap:10px;align-items:center;">
  <button id="fly-play" style="cursor:pointer;padding:6px 12px;border:0;border-radius:5px;
    background:#3388ff;color:#fff;font-weight:600;">&#9654; Fly through</button>
  <label style="display:flex;align-items:center;gap:6px;">
    Speed <input id="fly-speed" type="range" min="1" max="4" step="1" value="2" style="width:80px;">
  </label>
  <span id="fly-progress" style="min-width:36px;color:#ccc;"></span>
</div>
<script>
(function () {
  var gd = document.getElementById("__DIV_ID__");
  var frames = __FRAMES_JSON__;
  var playing = false, i = 0, timer = null;
  var btn = document.getElementById("fly-play");
  var speedInput = document.getElementById("fly-speed");
  var progress = document.getElementById("fly-progress");

  function stop() {
    playing = false; btn.innerHTML = "&#9654; Fly through";
    clearTimeout(timer);
  }
  function step() {
    if (!playing) return;
    Plotly.relayout(gd, {"scene.camera": frames[i]});
    progress.textContent = Math.round((i / (frames.length - 1)) * 100) + "%";
    i++;
    if (i >= frames.length) { stop(); return; }
    var fps = 6 + Number(speedInput.value) * 6;
    timer = setTimeout(step, 1000 / fps);
  }
  function play() {
    playing = true; btn.innerHTML = "&#10074;&#10074; Pause";
    if (i >= frames.length - 1) i = 0;
    step();
  }
  btn.addEventListener("click", function () { if (playing) stop(); else play(); });
})();
</script>
"""


def render_plot3d(result, grid_size=60, margin=150, dem_source=None):
    import plotly.graph_objects as go
    points_nztm_dense = result["points_nztm_dense"]
    elevations_dense = result["elevations_dense"]
    smoothed_dense = result["smoothed_dense"]
    corners = result["corners"]

    eastings = [p[0] for p in points_nztm_dense]
    northings = [p[1] for p in points_nztm_dense]
    grid_e = np.linspace(min(eastings) - margin, max(eastings) + margin, grid_size)
    grid_n = np.linspace(min(northings) - margin, max(northings) + margin, grid_size)

    bbox = [min(eastings), min(northings), max(eastings), max(northings)]
    arr, transform, _nodata, _source = get_dem_source(
        bbox, result["trail_name"], dem_source=dem_source, buffer=margin,
        corridor_nztm=LineString(points_nztm_dense))
    Z = np.array([[sample_raster(arr, transform, e, n) for e in grid_e] for n in grid_n],
                 dtype=float)
    E_grid, N_grid = np.meshgrid(grid_e, grid_n)

    trail_z = [z + 2 for z in elevations_dense]
    pad = (len(elevations_dense) - len(smoothed_dense)) // 2
    trail_colours = ([0] * pad + list(smoothed_dense)
                      + [0] * (len(elevations_dense) - len(smoothed_dense) - pad))

    # Explicit ranges + a manual aspect ratio (rather than aspectmode="data",
    # whose internal ratio Plotly doesn't expose) so the flythrough camera
    # maths below lands on exactly what's rendered.
    xlo, xhi = float(E_grid.min()), float(E_grid.max())
    ylo, yhi = float(N_grid.min()), float(N_grid.max())
    zlo = min(float(np.nanmin(Z)), min(trail_z))
    zhi = max(float(np.nanmax(Z)), max(trail_z))
    dx, dy, dz = xhi - xlo, yhi - ylo, max(zhi - zlo, 1e-6)
    m = max(dx, dy, 1e-6)
    aspect = {"x": dx / m, "y": dy / m, "z": dz / m}

    fig = go.Figure()
    fig.add_trace(go.Surface(x=E_grid, y=N_grid, z=Z, colorscale="Earth",
                              opacity=0.95, showscale=False, name="Terrain"))
    fig.add_trace(go.Scatter3d(
        x=eastings, y=northings, z=trail_z, mode="lines",
        line=dict(color=[abs(g) for g in trail_colours], colorscale="RdYlGn_r",
                  cmin=0, cmax=27, width=6, colorbar=dict(title="Gradient %")),
        name=f"{result['trail_name']} trail"))
    if corners:
        fig.add_trace(go.Scatter3d(
            x=[points_nztm_dense[i][0] for i, _ in corners],
            y=[points_nztm_dense[i][1] for i, _ in corners],
            z=[elevations_dense[i] + 5 for i, _ in corners],
            mode="markers",
            marker=dict(size=8, color=[radius_color(r) for _, r in corners]),
            text=[f"{r:.1f} m radius" for _, r in corners],
            hoverinfo="text", name="Corners"))
    fig.update_layout(
        title=f"{result['trail_name']} — 3D terrain & trail",
        scene=dict(xaxis=dict(title="Easting (m)", range=[xlo, xhi]),
                   yaxis=dict(title="Northing (m)", range=[ylo, yhi]),
                   zaxis=dict(title="Elevation (m)", range=[zlo, zhi]),
                   aspectmode="manual", aspectratio=aspect),
        height=800)

    frames = _flythrough_frames(points_nztm_dense, elevations_dense,
                                 xlo, xhi, ylo, yhi, zlo, zhi, aspect)
    div_id = "scene3d"
    html = fig.to_html(full_html=True, include_plotlyjs="cdn", div_id=div_id)
    controls = _FLYTHROUGH_HTML.replace("__DIV_ID__", div_id).replace(
        "__FRAMES_JSON__", json.dumps(frames))
    return html.replace("</body>", controls + "</body>")
