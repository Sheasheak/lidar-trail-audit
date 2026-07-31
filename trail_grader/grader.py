"""Core grading logic — no ArcPy, uses pyproj + rasterio."""
import math, re, io, json, os, urllib.request
import numpy as np
import rasterio
from rasterio.merge import merge
from rasterio.transform import rowcol
from pyproj import Transformer

# -------------------------------------------------------
# NZ MTB Trail Design Guidelines (Aug 2022) - Descending
# -------------------------------------------------------
GRADES = [
    {'name': 'Grade 1', 'label': 'Easiest',      'color_nz': 'Green',        'hex': '#00b300'},
    {'name': 'Grade 2', 'label': 'Easy',          'color_nz': 'Green',        'hex': '#66cc00'},
    {'name': 'Grade 3', 'label': 'Intermediate',  'color_nz': 'Light Blue',   'hex': '#00aaff'},
    {'name': 'Grade 4', 'label': 'Advanced',      'color_nz': 'Dark Blue',    'hex': '#0044cc'},
    {'name': 'Grade 5', 'label': 'Expert',        'color_nz': 'Black',        'hex': '#444444'},
    {'name': 'Grade 6', 'label': 'Extreme',       'color_nz': 'Double Black', 'hex': '#660000'},
]
GRAD_AVG_MAX  = [6.1, 8.8, 9.5, 15.9, 25.0, None]
GRAD_SEG_MAX  = [7.0, 14.3, 19.6, 27.0, 37.0, None]
TURN_MIN      = [6.0, 4.0, 2.5, 2.0, 1.5, 1.0]
EXCEPTION_PCT = [2.0, 2.0, 5.0, 10.0, 20.0, None]

to_nztm  = Transformer.from_crs("EPSG:4326", "EPSG:2193", always_xy=True)
to_wgs84 = Transformer.from_crs("EPSG:2193", "EPSG:4326", always_xy=True)

# -------------------------------------------------------
# KML parsing
# -------------------------------------------------------
def parse_kml(kml_bytes):
    content = kml_bytes.decode('utf-8', errors='replace')
    match = re.search(r'<coordinates>(.*?)</coordinates>', content, re.DOTALL)
    if not match:
        raise ValueError("No <coordinates> found in KML")
    pts = []
    for c in match.group(1).strip().split():
        parts = c.strip().split(',')
        if len(parts) >= 2:
            pts.append((float(parts[0]), float(parts[1])))
    if len(pts) < 2:
        raise ValueError("Not enough coordinates in KML")
    return pts  # list of (lon, lat)

# -------------------------------------------------------
# Local DTM tile loader
# -------------------------------------------------------
def load_dtm(dtm_folder, bbox_nztm, buffer=50):
    xmin, ymin, xmax, ymax = bbox_nztm
    xmin -= buffer; ymin -= buffer; xmax += buffer; ymax += buffer

    asc_files = [os.path.join(dtm_folder, f)
                 for f in os.listdir(dtm_folder) if f.endswith('.asc')]
    if not asc_files:
        raise ValueError("No .asc files found in DTM folder: " + dtm_folder)

    relevant = []
    for path in asc_files:
        with rasterio.open(path) as src:
            b = src.bounds
            if b.left < xmax and b.right > xmin and b.bottom < ymax and b.top > ymin:
                relevant.append(path)

    if not relevant:
        raise ValueError("No DTM tiles overlap with this trail. Is it a Christchurch trail?")

    datasets = [rasterio.open(p) for p in relevant]
    merged, transform = merge(datasets)
    for d in datasets:
        d.close()

    # Write merged raster to in-memory bytes
    buf = io.BytesIO()
    with rasterio.open(buf, 'w', driver='GTiff',
                       height=merged.shape[1], width=merged.shape[2],
                       count=1, dtype=merged.dtype,
                       crs='EPSG:2193', transform=transform) as dst:
        dst.write(merged[0], 1)
    buf.seek(0)
    return buf

# -------------------------------------------------------
# Geometry helpers
# -------------------------------------------------------
def densify(pts_nztm, step=5.0):
    dense = [pts_nztm[0]]
    for i in range(1, len(pts_nztm)):
        x1, y1 = pts_nztm[i-1]
        x2, y2 = pts_nztm[i]
        seg = math.sqrt((x2-x1)**2 + (y2-y1)**2)
        if seg > 0:
            n = int(seg / step)
            for j in range(1, n+1):
                t = j * step / seg
                if t < 1.0:
                    dense.append((x1 + t*(x2-x1), y1 + t*(y2-y1)))
        dense.append(pts_nztm[i])
    return dense

def sample_raster(arr, transform, x, y):
    """Sample raster value at NZTM coordinate. Returns nan if out of bounds."""
    try:
        row, col = rowcol(transform, x, y)
        if 0 <= row < arr.shape[0] and 0 <= col < arr.shape[1]:
            v = float(arr[row, col])
            return np.nan if np.isnan(v) else v
    except:
        pass
    return np.nan

def exposure_at_point(arr, transform, x, y, trail_dir_x, trail_dir_y, distances=(1.5, 3.0, 5.0, 8.0)):
    """
    Sample terrain perpendicular to trail direction on both sides.
    Returns max drop (positive = terrain falls away) within the sample distances.
    NZ MTB Guidelines p.10: fall height measured 1.5m out from trail edge.
    """
    trail_elev = sample_raster(arr, transform, x, y)
    if np.isnan(trail_elev):
        return 0.0

    # Perpendicular directions (left and right of travel)
    length = math.sqrt(trail_dir_x**2 + trail_dir_y**2)
    if length < 1e-6:
        return 0.0
    perp_x = -trail_dir_y / length
    perp_y =  trail_dir_x / length

    max_drop = 0.0
    for side in (1, -1):
        for d in distances:
            sx = x + side * perp_x * d
            sy = y + side * perp_y * d
            elev = sample_raster(arr, transform, sx, sy)
            if not np.isnan(elev):
                drop = trail_elev - elev  # positive = terrain drops away
                max_drop = max(max_drop, drop)
    return max_drop

def drop_to_exposure_grade(drop_m):
    """
    Map fall height to exposure grade.
    Based on NZ MTB Guidelines p.10 fall height thresholds.
    """
    if drop_m < 1.0:  return 0  # Grade 1 — no exposure
    if drop_m < 2.0:  return 1  # Grade 2 — minor
    if drop_m < 3.5:  return 2  # Grade 3 — moderate
    if drop_m < 5.0:  return 3  # Grade 4 — significant
    return 4                     # Grade 5 — severe

def circumradius(p1, p2, p3):
    ax, ay = p1; bx, by = p2; cx, cy = p3
    a = math.sqrt((bx-ax)**2 + (by-ay)**2)
    b = math.sqrt((cx-bx)**2 + (cy-by)**2)
    c = math.sqrt((ax-cx)**2 + (ay-cy)**2)
    area = abs((bx-ax)*(cy-ay) - (cx-ax)*(by-ay)) / 2.0
    if area < 1e-6:
        return None
    return (a * b * c) / (4 * area)

def grade_by_value(v, thresholds, lower_is_harder=False):
    for i, t in enumerate(thresholds):
        if t is None: return i
        if lower_is_harder:
            if v >= t: return i
        else:
            if v <= t: return i
    return 5

def grade_with_exceptions(grade_dist, total_dist):
    for candidate in range(6):
        exc = EXCEPTION_PCT[candidate]
        if exc is None: return candidate
        over = sum(grade_dist[candidate+1:])
        if over / total_dist * 100 <= exc:
            return candidate
    return 5

# -------------------------------------------------------
# Rainfall
# -------------------------------------------------------
def fetch_rainfall(lat, lon):
    url = ('https://archive-api.open-meteo.com/v1/archive'
           '?latitude=%.4f&longitude=%.4f'
           '&start_date=2019-01-01&end_date=2023-12-31'
           '&daily=precipitation_sum&timezone=Pacific/Auckland' % (lat, lon))
    with urllib.request.urlopen(url, timeout=15) as r:
        data = json.loads(r.read())
    dates  = data['daily']['time']
    precip = data['daily']['precipitation_sum']
    yearly = {}
    for d, p in zip(dates, precip):
        y = d[:4]; yearly[y] = yearly.get(y, 0) + (p or 0)
    return sum(yearly.values()) / len(yearly)

def rainfall_bump(mm): return 2 if mm > 2000 else 1 if mm > 1200 else 0
def rainfall_label(mm):
    if mm > 2000: return 'Very high (>2000mm) — +2 grades'
    if mm > 1200: return 'High (>1200mm) — +1 grade'
    if mm > 800:  return 'Moderate (800-1200mm) — no adjustment'
    return 'Low (<800mm) — no adjustment'

# -------------------------------------------------------
# Main pipeline
# -------------------------------------------------------
def analyse_trail(kml_bytes, dtm_folder):
    # 1. Parse KML
    kml_pts = parse_kml(kml_bytes)  # (lon, lat)

    # 2. Project to NZTM
    nztm_pts = [to_nztm.transform(lon, lat) for lon, lat in kml_pts]

    # 3. Trail extent
    xs = [p[0] for p in nztm_pts]; ys = [p[1] for p in nztm_pts]
    bbox = (min(xs), min(ys), max(xs), max(ys))

    # 4. Load DTM from local tiles
    tif_buf = load_dtm(dtm_folder, bbox)

    # 5. Sample elevation along densified trail
    dense = densify(nztm_pts, step=5.0)
    with rasterio.open(tif_buf) as src:
        transform = src.transform
        arr = src.read(1).astype(float)
        nodata = src.nodata or -9999
        arr[arr == nodata] = np.nan

        elevs = []
        for x, y in dense:
            try:
                row, col = rowcol(transform, x, y)
                if 0 <= row < arr.shape[0] and 0 <= col < arr.shape[1]:
                    elevs.append(float(arr[row, col]))
                else:
                    elevs.append(np.nan)
            except:
                elevs.append(np.nan)

        # Exposure: sample terrain perpendicular to trail at each point
        exposure_drops = []
        for i in range(len(dense)):
            if i == 0:
                dx = dense[1][0] - dense[0][0]
                dy = dense[1][1] - dense[0][1]
            elif i == len(dense) - 1:
                dx = dense[-1][0] - dense[-2][0]
                dy = dense[-1][1] - dense[-2][1]
            else:
                dx = dense[i+1][0] - dense[i-1][0]
                dy = dense[i+1][1] - dense[i-1][1]
            drop = exposure_at_point(arr, transform, dense[i][0], dense[i][1], dx, dy)
            exposure_drops.append(drop)

    # 6. Cumulative distance + raw gradient
    cum_dist = [0.0]
    for i in range(1, len(dense)):
        d = math.sqrt((dense[i][0]-dense[i-1][0])**2 + (dense[i][1]-dense[i-1][1])**2)
        cum_dist.append(cum_dist[-1] + d)
    total_dist = cum_dist[-1]

    raw_grads = [None]
    for i in range(1, len(dense)):
        if np.isnan(elevs[i]) or np.isnan(elevs[i-1]):
            raw_grads.append(None)
            continue
        horiz = cum_dist[i] - cum_dist[i-1]
        raw_grads.append((elevs[i] - elevs[i-1]) / horiz * 100 if horiz > 0 else 0.0)

    # 7. 50m smoothed gradient
    def smooth_grad(i, half=25):
        ci = cum_dist[i]
        rear = 0
        for j in range(i-1, -1, -1):
            if ci - cum_dist[j] >= half: rear = j; break
        front = len(dense) - 1
        for j in range(i+1, len(dense)):
            if cum_dist[j] - ci >= half: front = j; break
        horiz = cum_dist[front] - cum_dist[rear]
        if horiz <= 0 or np.isnan(elevs[front]) or np.isnan(elevs[rear]): return None
        return (elevs[front] - elevs[rear]) / horiz * 100

    smooth_grads = [smooth_grad(i) for i in range(len(dense))]

    # 8. Gradient grading
    valid_smooth = [g for g in smooth_grads if g is not None]
    avg_grad = abs((elevs[-1] - elevs[0]) / total_dist * 100) if not np.isnan(elevs[-1]) and not np.isnan(elevs[0]) else 0
    grad_avg_gi = grade_by_value(avg_grad, GRAD_AVG_MAX)

    grade_dist = [0.0] * 6
    for i in range(1, len(dense)):
        g = smooth_grads[i]
        if g is None: continue
        seg_len = cum_dist[i] - cum_dist[i-1]
        gi = grade_by_value(abs(g), GRAD_SEG_MAX)
        grade_dist[gi] += seg_len

    grad_naive_gi  = grade_by_value(max(abs(g) for g in valid_smooth), GRAD_SEG_MAX)
    grad_seg_gi    = grade_with_exceptions(grade_dist, total_dist)
    over_pct       = sum(grade_dist[grad_seg_gi+1:]) / total_dist * 100
    gradient_grade = max(grad_avg_gi, grad_seg_gi)

    # 9. Turn radius
    turn_radii = []
    for i in range(1, len(nztm_pts) - 1):
        r = circumradius(nztm_pts[i-1], nztm_pts[i], nztm_pts[i+1])
        if r is not None and r < 200:
            turn_radii.append(r)
    min_radius = min(turn_radii) if turn_radii else 999
    p10_radius = sorted(turn_radii)[int(len(turn_radii)*0.10)] if turn_radii else 999
    turn_grade     = grade_by_value(p10_radius, TURN_MIN, lower_is_harder=True)
    turn_grade_min = grade_by_value(min_radius,  TURN_MIN, lower_is_harder=True)

    # 10. Rainfall
    mid_lon, mid_lat = to_wgs84.transform(*nztm_pts[len(nztm_pts)//2])
    try:
        avg_rain  = fetch_rainfall(mid_lat, mid_lon)
        rain_ok   = True
    except:
        avg_rain  = 0; rain_ok = False
    rain_b = rainfall_bump(avg_rain)

    # 11. Exposure grading (from DTM perpendicular sampling)
    # Use 90th percentile drop to avoid single-point noise
    valid_drops = sorted([d for d in exposure_drops if d > 0])
    if valid_drops:
        p90_drop = valid_drops[int(len(valid_drops) * 0.90)]
        max_drop = valid_drops[-1]
    else:
        p90_drop = 0.0
        max_drop = 0.0
    exposure_grade = drop_to_exposure_grade(p90_drop)
    exposure_labels = ['None (<1m)', 'Minor (1-2m)', 'Moderate (2-3.5m)', 'Significant (3.5-5m)', 'Severe (>5m)']
    exposure_label  = exposure_labels[exposure_grade]

    # 12. Overall (data-derived)
    overall_grade = min(5, max(gradient_grade, turn_grade, exposure_grade) + rain_b)

    # 12. Build map segments (WGS84)
    grad_segs = []
    for i in range(1, len(dense)):
        g = smooth_grads[i]
        if g is None: continue
        gi = grade_by_value(abs(g), GRAD_SEG_MAX)
        lon0, lat0 = to_wgs84.transform(*dense[i-1])
        lon1, lat1 = to_wgs84.transform(*dense[i])
        grad_segs.append({
            'coords': [[lat0, lon0], [lat1, lon1]],
            'color': GRADES[gi]['hex'],
            'grade': '%s %s' % (GRADES[gi]['name'], GRADES[gi]['label']),
            'gradient': round(g, 1),
            'dist': round(cum_dist[i], 0),
            'elev': round(elevs[i], 1) if not np.isnan(elevs[i]) else 0,
        })

    tight_corners = []
    cum = 0.0
    for i in range(1, len(nztm_pts) - 1):
        cum += math.sqrt((nztm_pts[i][0]-nztm_pts[i-1][0])**2 + (nztm_pts[i][1]-nztm_pts[i-1][1])**2)
        r = circumradius(nztm_pts[i-1], nztm_pts[i], nztm_pts[i+1])
        if r is not None and r < 10:
            rgi = grade_by_value(r, TURN_MIN, lower_is_harder=True)
            lon, lat = to_wgs84.transform(*nztm_pts[i])
            tight_corners.append({'lat': lat, 'lon': lon, 'radius': round(r,1),
                                   'grade': '%s %s' % (GRADES[rgi]['name'], GRADES[rgi]['label']),
                                   'color': GRADES[rgi]['hex'], 'dist': round(cum, 0)})

    mid_lon, mid_lat = to_wgs84.transform(*nztm_pts[len(nztm_pts)//2])
    start_lon, start_lat = to_wgs84.transform(*nztm_pts[0])
    end_lon,   end_lat   = to_wgs84.transform(*nztm_pts[-1])

    return {
        'grad_segs':      grad_segs,
        'tight_corners':  tight_corners,
        'map_center':     [mid_lat, mid_lon],
        'start':          {'lat': start_lat, 'lon': start_lon, 'elev': round(elevs[0], 1) if not np.isnan(elevs[0]) else 0},
        'end':            {'lat': end_lat,   'lon': end_lon,   'elev': round(elevs[-1], 1) if not np.isnan(elevs[-1]) else 0},
        'total_dist':     round(total_dist, 0),
        'total_descent':  round(elevs[-1] - elevs[0], 1) if not np.isnan(elevs[-1]) and not np.isnan(elevs[0]) else 0,
        'avg_grad':       round(avg_grad, 1),
        'avg_grad_grade': '%s %s' % (GRADES[grad_avg_gi]['name'], GRADES[grad_avg_gi]['label']),
        'max_grad':       round(max(abs(g) for g in valid_smooth), 1),
        'max_grad_grade_raw': '%s %s' % (GRADES[grad_naive_gi]['name'], GRADES[grad_naive_gi]['label']),
        'max_grad_grade': '%s %s' % (GRADES[grad_seg_gi]['name'], GRADES[grad_seg_gi]['label']),
        'over_pct':       round(over_pct, 1),
        'p10_radius':     round(p10_radius, 1),
        'p10_radius_grade': '%s %s' % (GRADES[turn_grade]['name'], GRADES[turn_grade]['label']),
        'min_radius':     round(min_radius, 1),
        'min_radius_grade': '%s %s' % (GRADES[turn_grade_min]['name'], GRADES[turn_grade_min]['label']),
        'avg_rainfall':    round(avg_rain, 0) if rain_ok else 'N/A',
        'rain_label':      rainfall_label(avg_rain) if rain_ok else 'Could not fetch',
        'max_drop':        round(max_drop, 1),
        'p90_drop':        round(p90_drop, 1),
        'exposure_grade':  '%s %s' % (GRADES[exposure_grade]['name'], GRADES[exposure_grade]['label']),
        'exposure_label':  exposure_label,
        'overall_grade':   overall_grade,
        'overall_label':   '%s - %s (%s)' % (GRADES[overall_grade]['name'], GRADES[overall_grade]['label'], GRADES[overall_grade]['color_nz']),
        'grades_json':     json.dumps(GRADES),
    }
