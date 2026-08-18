"""Trail grading pipeline ported from TrailAudit_v11.ipynb (Downloads/), cell for
cell — the notebook's exact grading thresholds/algorithm, Folium map, matplotlib
gradient/sideslope profiles, Plotly 3D terrain view. Elevation comes from the
local 1 m Canterbury/Christchurch LiDAR DEM tiles (config.json's dtm_folder),
sampled via the same rasterio pipeline as grader.py/grader3d.py — accurate to
1 m, rather than the live LINZ national DEM API (8 m).

This intentionally replaces grader.py / grader3d.py as the site's pipeline: the
notebook is the newer, authoritative version of the tool.
"""
import io
import json
import math
import base64

import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio import warp
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LightSource
import folium
import plotly.graph_objects as go
from pyproj import Transformer
from shapely.geometry import LineString
from scipy.ndimage import uniform_filter1d
from pykml import parser as kml_parser

from config import get_dtm_folder
from grader import sample_raster
from grader3d import load_dtm_fast

DTM_FOLDER = get_dtm_folder()

to_nztm = Transformer.from_crs("EPSG:4326", "EPSG:2193", always_xy=True)
to_wgs84 = Transformer.from_crs("EPSG:2193", "EPSG:4326", always_xy=True)

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


def parse_kml(kml_bytes):
    root = kml_parser.parse(io.BytesIO(kml_bytes)).getroot()
    nodes = root.findall(".//{http://www.opengis.net/kml/2.2}coordinates")
    if not nodes:
        raise ValueError("No <coordinates> element in the KML.")
    points = []
    for pt in nodes[0].text.strip().split():
        lon, lat, *_ = pt.split(",")
        points.append((float(lon), float(lat)))
    return points


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
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return "data:image/png;base64," + base64.b64encode(buf.read()).decode("ascii")


def _array_png_b64(rgba):
    buf = io.BytesIO()
    plt.imsave(buf, rgba, format="png")
    buf.seek(0)
    return "data:image/png;base64," + base64.b64encode(buf.read()).decode("ascii")


TERRAIN_BUFFER_M = 60
TERRAIN_HS_AZIMUTHS = (225, 270, 315, 360)


def terrain_overlays(points_wgs84, buffer_m=TERRAIN_BUFFER_M, vert_exag=1.5, dem_source=None):
    """Same method as Askins' precomputed hillshade/slope: shade the native
    1 m local DEM directly (no coarse resampling), multidirectional so the
    bench cut reads clearly, rather than a single low-angle light."""
    points_nztm = [to_nztm.transform(lon, lat) for lon, lat in points_wgs84]
    xs = [p[0] for p in points_nztm]
    ys = [p[1] for p in points_nztm]
    xmin, xmax = min(xs) - buffer_m, max(xs) + buffer_m
    ymin, ymax = min(ys) - buffer_m, max(ys) + buffer_m

    arr, transform, nodata = _load_local_dtm([xmin, ymin, xmax, ymax], buffer=0, dem_source=dem_source)
    elev_grid = arr.astype(float)
    if nodata is None:
        elev_grid[elev_grid < -90] = np.nan  # AAIGrid NODATA_value (-99), bundled dataset
    elif not (isinstance(nodata, float) and math.isnan(nodata)):
        elev_grid[elev_grid == nodata] = np.nan
    if np.isnan(elev_grid).all():
        raise ValueError("No local LiDAR DEM coverage for this area.")
    if np.isnan(elev_grid).any():
        elev_grid = np.where(np.isnan(elev_grid), np.nanmean(elev_grid), elev_grid)

    cellsize_x = transform.a
    cellsize_y = -transform.e

    ls_stack = [LightSource(azdeg=az, altdeg=45) for az in TERRAIN_HS_AZIMUTHS]
    hillshade_avg = np.mean(
        [ls.hillshade(elev_grid, vert_exag=vert_exag, dx=cellsize_x, dy=cellsize_y) for ls in ls_stack],
        axis=0)
    hillshade_rgba = plt.cm.gray(hillshade_avg)

    dzdy, dzdx = np.gradient(elev_grid, cellsize_y, cellsize_x)
    slope_deg = np.degrees(np.arctan(np.hypot(dzdx, dzdy)))
    slope_rgba = plt.cm.gray(1 - np.clip(slope_deg / 45, 0, 1))

    ny, nx = elev_grid.shape
    sw_lon, sw_lat = to_wgs84.transform(xmin, ymin)
    ne_lon, ne_lat = to_wgs84.transform(xmax, ymax)

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

    # ---- elevations (local 1 m LiDAR DEM) ----
    note(f"Sampling {len(points_nztm_dense)} elevations from local LiDAR DEM…")
    xs_d = [p[0] for p in points_nztm_dense]
    ys_d = [p[1] for p in points_nztm_dense]
    arr, transform, _nodata = _load_local_dtm(
        [min(xs_d), min(ys_d), max(xs_d), max(ys_d)], dem_source=dem_source)
    elevations_dense = [sample_raster(arr, transform, x, y) for x, y in points_nztm_dense]

    keep = [i for i, e in enumerate(elevations_dense) if not np.isnan(e)]
    if len(keep) < 10:
        if dem_source is not None:
            raise ValueError(
                "Too few elevations came back from the uploaded DEM (%d of %d) — "
                "check the trail falls inside the DEM's extent." % (len(keep), len(elevations_dense)))
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

    # ---- gradient profile (matplotlib PNG) ----
    note("Drawing charts…")
    abs_limit = GRADIENT_ABS_MAX[stated_grade]
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
        "n_corners": len(corners),
        "mean_abs_sideslope": round(mean_abs_sideslope, 1) if mean_abs_sideslope is not None else None,
        "pct_steep_sideslope": round(pct_steep_sideslope, 1) if pct_steep_sideslope is not None else None,
        "sideslopes": sideslopes,
        "sideslope_station_step": station_step,
        "sideslope_exceedances": sideslope_exceedances,
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

    folium.Marker(location=[pts[0][1], pts[0][0]], tooltip="Start",
                  icon=folium.Icon(color="green", icon="play")).add_to(m)
    folium.Marker(location=[pts[-1][1], pts[-1][0]], tooltip="Finish",
                  icon=folium.Icon(color="red", icon="stop")).add_to(m)
    folium.LayerControl().add_to(m)
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
    points_nztm_dense = result["points_nztm_dense"]
    elevations_dense = result["elevations_dense"]
    smoothed_dense = result["smoothed_dense"]
    corners = result["corners"]

    eastings = [p[0] for p in points_nztm_dense]
    northings = [p[1] for p in points_nztm_dense]
    grid_e = np.linspace(min(eastings) - margin, max(eastings) + margin, grid_size)
    grid_n = np.linspace(min(northings) - margin, max(northings) + margin, grid_size)

    arr, transform, _nodata = _load_local_dtm(
        [min(eastings), min(northings), max(eastings), max(northings)], buffer=margin,
        dem_source=dem_source)
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
