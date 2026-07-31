import arcpy, csv, json, math, re, sys, urllib.request

# -------------------------------------------------------
# NZ MTB Trail Design Guidelines (Aug 2022) - Descending trails
# -------------------------------------------------------
GRADES = [
    {'name': 'Grade 1', 'label': 'Easiest',      'color_nz': 'Green',        'hex': '#00b300'},
    {'name': 'Grade 2', 'label': 'Easy',          'color_nz': 'Green',        'hex': '#66cc00'},
    {'name': 'Grade 3', 'label': 'Intermediate',  'color_nz': 'Light Blue',   'hex': '#00aaff'},
    {'name': 'Grade 4', 'label': 'Advanced',      'color_nz': 'Dark Blue',    'hex': '#0044cc'},
    {'name': 'Grade 5', 'label': 'Expert',        'color_nz': 'Black',        'hex': '#444444'},
    {'name': 'Grade 6', 'label': 'Extreme',       'color_nz': 'Double Black', 'hex': '#660000'},
]

# Gradient thresholds (abs %) - descending trails, page 5
GRAD_AVG_MAX = [6.1, 8.8, 9.5, 15.9, 25.0, None]
GRAD_SEG_MAX = [7.0, 14.3, 19.6, 27.0, 37.0, None]

# Turn radius thresholds (metres, minimum) - descending trails, page 5
TURN_MIN = [6.0, 4.0, 2.5, 2.0, 1.5, 1.0]

# Exceptions allowance - max % of trail allowed to exceed the grade threshold
# NZ MTB Guidelines p.4-5: G1/G2=2%, G3=5%, G4=10%, G5=20%, G6=no limit
EXCEPTION_PCT = [2.0, 2.0, 5.0, 10.0, 20.0, None]

# Rainfall grade adjustment (mm/yr) - based on NZ MTB Guidelines p.12
# "Very high and very low rainfall areas may need gentler grades"
# Assumes severe rainfall events 1-3x/yr (guideline baseline)
# >1200mm: +1 grade | >2000mm: +2 grades | <=1200mm: no adjustment
def rainfall_grade_bump(mm_per_year):
    if mm_per_year > 2000: return 2
    if mm_per_year > 1200: return 1
    return 0

def rainfall_label(mm_per_year):
    if mm_per_year > 2000: return 'Very high (>2000mm) — +2 grades'
    if mm_per_year > 1200: return 'High (>1200mm) — +1 grade'
    if mm_per_year > 800:  return 'Moderate (800-1200mm) — no adjustment'
    return 'Low (<800mm) — no adjustment'

def grade_by_value(value, thresholds, lower_is_harder=False):
    for i, t in enumerate(thresholds):
        if t is None:
            return i
        if lower_is_harder:
            if value >= t:
                return i
        else:
            if value <= t:
                return i
    return 5

# -------------------------------------------------------
# 1. Gradient analysis (DTM data)
# -------------------------------------------------------
pts = []
with open(r'C:\Users\Shea\GIS_Work\askins_dtm_gradient.csv') as f:
    for r in csv.DictReader(f):
        pts.append({
            'x': float(r['x']), 'y': float(r['y']),
            'elev': float(r['elev']),
            'cum_dist': float(r['cum_dist']),
            'gradient_50m': float(r['gradient_50m']) if r['gradient_50m'] else 0.0
        })

total_dist = pts[-1]['cum_dist']
total_drop = pts[-1]['elev'] - pts[0]['elev']
avg_grad   = abs(total_drop / total_dist * 100)
max_grad   = max(abs(p['gradient_50m']) for p in pts)

grad_avg_gi = grade_by_value(avg_grad, GRAD_AVG_MAX)

# Calculate distance exceeding each grade threshold
grade_dist = [0.0] * 6
for i in range(1, len(pts)):
    seg_len = pts[i]['cum_dist'] - pts[i-1]['cum_dist']
    gi = grade_by_value(abs(pts[i]['gradient_50m']), GRAD_SEG_MAX)
    grade_dist[gi] += seg_len

# Apply exceptions rule: find the lowest grade where out-of-spec distance
# is within the allowed exception percentage
def grade_with_exceptions(grade_dist, total_dist, thresholds):
    for candidate in range(len(thresholds)):
        exc_pct = EXCEPTION_PCT[candidate]
        if exc_pct is None:
            return candidate
        over = sum(grade_dist[candidate+1:])
        if over / total_dist * 100 <= exc_pct:
            return candidate
    return 5

grad_seg_gi    = grade_with_exceptions(grade_dist, total_dist, GRAD_SEG_MAX)
grad_naive_gi  = grade_by_value(max_grad, GRAD_SEG_MAX)  # without exceptions
gradient_grade = max(grad_avg_gi, grad_seg_gi)

over_pct = sum(grade_dist[grad_seg_gi+1:]) / total_dist * 100

print('--- GRADIENT ---')
print('Average: %.1f%%  -> %s' % (avg_grad, GRADES[grad_avg_gi]['name']))
print('Max seg: %.1f%%  -> %s (raw, no exceptions)' % (max_grad, GRADES[grad_naive_gi]['name']))
print('Seg grade (with exceptions): %s  (%.1f%% out-of-spec, allowance %.1f%%)' % (
    GRADES[grad_seg_gi]['name'], over_pct, EXCEPTION_PCT[grad_seg_gi] or 0))
print('Gradient grade: %s %s' % (GRADES[gradient_grade]['name'], GRADES[gradient_grade]['label']))

# -------------------------------------------------------
# 2. Turn radius (from KML GPS)
# -------------------------------------------------------
sr_wgs84 = arcpy.SpatialReference(4326)
sr_nztm  = arcpy.SpatialReference(2193)

with open(r'C:\Users\Shea\Downloads\askins.kml') as f:
    content = f.read()
coords_raw = re.search(r'<coordinates>(.*?)</coordinates>', content, re.DOTALL).group(1).strip()

kml_pts = []
for c in coords_raw.split():
    parts = c.strip().split(',')
    if len(parts) >= 2:
        kml_pts.append((float(parts[0]), float(parts[1])))

nztm_pts = []
for lon, lat in kml_pts:
    g = arcpy.PointGeometry(arcpy.Point(lon, lat), sr_wgs84).projectAs(sr_nztm)
    nztm_pts.append((g.centroid.X, g.centroid.Y))

def circumradius(p1, p2, p3):
    ax, ay = p1; bx, by = p2; cx, cy = p3
    a = math.sqrt((bx-ax)**2 + (by-ay)**2)
    b = math.sqrt((cx-bx)**2 + (cy-by)**2)
    c = math.sqrt((ax-cx)**2 + (ay-cy)**2)
    area = abs((bx-ax)*(cy-ay) - (cx-ax)*(by-ay)) / 2.0
    if area < 1e-6:
        return None
    return (a * b * c) / (4 * area)

turn_radii = []
for i in range(1, len(nztm_pts) - 1):
    r = circumradius(nztm_pts[i-1], nztm_pts[i], nztm_pts[i+1])
    if r is not None and r < 200:
        turn_radii.append(r)

min_radius = min(turn_radii) if turn_radii else 999
p10_radius = sorted(turn_radii)[int(len(turn_radii) * 0.10)] if turn_radii else 999

turn_grade = grade_by_value(p10_radius, TURN_MIN, lower_is_harder=True)
turn_grade_min = grade_by_value(min_radius, TURN_MIN, lower_is_harder=True)

print('\n--- TURN RADIUS ---')
print('Min radius:  %.1fm -> %s' % (min_radius, GRADES[turn_grade_min]['name']))
print('10th pct:    %.1fm -> %s' % (p10_radius, GRADES[turn_grade]['name']))
print('Turn grade: %s %s' % (GRADES[turn_grade]['name'], GRADES[turn_grade]['label']))

# -------------------------------------------------------
# 3. Rainfall (Open-Meteo, 5-year average)
# -------------------------------------------------------
# Use trail midpoint coords (WGS84) — fetched after gradient conversion below,
# so use hardcoded approx from our known trail location
trail_lat, trail_lon = -43.555, 172.633
url = ('https://archive-api.open-meteo.com/v1/archive'
       '?latitude=%.4f&longitude=%.4f'
       '&start_date=2019-01-01&end_date=2023-12-31'
       '&daily=precipitation_sum&timezone=Pacific/Auckland' % (trail_lat, trail_lon))
try:
    with urllib.request.urlopen(url, timeout=15) as r:
        rain_data = json.loads(r.read())
    dates  = rain_data['daily']['time']
    precip = rain_data['daily']['precipitation_sum']
    yearly = {}
    for d, p in zip(dates, precip):
        y = d[:4]
        yearly[y] = yearly.get(y, 0) + (p or 0)
    avg_rainfall = sum(yearly.values()) / len(yearly)
    rain_ok = True
except Exception as e:
    avg_rainfall = 0
    rain_ok = False
    print('Rainfall fetch failed: ' + str(e))

rain_bump  = rainfall_grade_bump(avg_rainfall)
rain_grade = rain_bump  # relative bump, not an absolute grade index

print('\n--- RAINFALL ---')
if rain_ok:
    print('5-yr avg: %.0f mm/yr' % avg_rainfall)
    print('Rainfall label: ' + rainfall_label(avg_rainfall))
    print('Grade bump: +%d' % rain_bump)

# -------------------------------------------------------
# 4. Overall grade
# -------------------------------------------------------
overall_grade = min(5, max(gradient_grade, turn_grade) + rain_bump)

print('\n--- OVERALL GRADE (gradient + turn radius + rainfall) ---')
print('>>> %s - %s (%s) <<<' % (GRADES[overall_grade]['name'], GRADES[overall_grade]['label'], GRADES[overall_grade]['color_nz']))
print('\nRequires field assessment by trail builder:')
print('  - Tread obstacles (rock/root height)')
print('  - Trail surface quality')
print('  - Technical features (jumps, drops, stepped drops)')
print('  - Exposure / fall height')

# -------------------------------------------------------
# 4. Build gradient segments for map
# -------------------------------------------------------
for p in pts:
    g = arcpy.PointGeometry(arcpy.Point(p['x'], p['y']), sr_nztm).projectAs(sr_wgs84)
    p['lon'] = g.centroid.X
    p['lat'] = g.centroid.Y

grad_segs = []
for i in range(1, len(pts)):
    p0, p1 = pts[i-1], pts[i]
    gi = grade_by_value(abs(p1['gradient_50m']), GRAD_SEG_MAX)
    grad_segs.append({
        'coords': [[p0['lat'], p0['lon']], [p1['lat'], p1['lon']]],
        'color': GRADES[gi]['hex'],
        'grade': '%s %s' % (GRADES[gi]['name'], GRADES[gi]['label']),
        'gradient': round(p1['gradient_50m'], 1),
        'dist': round(p1['cum_dist'], 0),
        'elev': round(p1['elev'], 1),
    })

# Build tight corner markers
tight_corners = []
cum = 0.0
for i in range(1, len(nztm_pts) - 1):
    cum += math.sqrt((nztm_pts[i][0]-nztm_pts[i-1][0])**2 + (nztm_pts[i][1]-nztm_pts[i-1][1])**2)
    r = circumradius(nztm_pts[i-1], nztm_pts[i], nztm_pts[i+1])
    if r is not None and r < 10:
        g = arcpy.PointGeometry(arcpy.Point(nztm_pts[i][0], nztm_pts[i][1]), sr_nztm).projectAs(sr_wgs84)
        rgi = grade_by_value(r, TURN_MIN, lower_is_harder=True)
        tight_corners.append({
            'lat': g.centroid.Y, 'lon': g.centroid.X,
            'radius': round(r, 1),
            'grade': '%s %s' % (GRADES[rgi]['name'], GRADES[rgi]['label']),
            'color': GRADES[rgi]['hex'],
            'dist': round(cum, 0),
        })

mid = pts[len(pts)//2]
overall_label = '%s - %s (%s)' % (GRADES[overall_grade]['name'], GRADES[overall_grade]['label'], GRADES[overall_grade]['color_nz'])

# -------------------------------------------------------
# 5. Write HTML map
# -------------------------------------------------------
html = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Askins MTB - Full Grade Assessment</title>
<link rel="stylesheet" href="leaflet.css"/>
<script src="leaflet.js"></script>
<style>
  body{margin:0;font-family:sans-serif;}
  #map{height:100vh;}
  .legend{background:white;padding:12px 16px;border-radius:6px;box-shadow:0 1px 5px rgba(0,0,0,0.3);line-height:1.9;font-size:12px;min-width:230px;}
  .sw{display:inline-block;width:22px;height:10px;border-radius:2px;margin-right:6px;vertical-align:middle;border:1px solid #ccc;}
  #stats-panel{position:fixed;top:16px;left:50%%;transform:translateX(-50%%);background:rgba(255,255,255,0.97);border-radius:8px;box-shadow:0 2px 10px rgba(0,0,0,0.25);padding:14px 18px;z-index:1000;font-size:13px;min-width:420px;}
  #stats-panel h3{margin:0 0 10px 0;font-size:14px;text-align:center;}
  #stats-panel table{border-collapse:collapse;width:100%%;}
  #stats-panel td,#stats-panel th{padding:5px 12px;border:1px solid #ddd;text-align:left;}
  #stats-panel th{background:#f5f5f5;font-weight:600;}
  #stats-panel tr.overall td{font-weight:700;background:#f0f4ff;}
  #stats-panel .tbd{color:#aaa;font-style:italic;}
  /* Questionnaire modal */
  #modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.55);z-index:2000;display:flex;align-items:center;justify-content:center;}
  #modal{background:white;border-radius:10px;padding:28px 32px;max-width:520px;width:90%%;box-shadow:0 4px 24px rgba(0,0,0,0.3);max-height:90vh;overflow-y:auto;}
  #modal h2{margin:0 0 6px 0;font-size:17px;}
  #modal .subtitle{color:#666;font-size:12px;margin-bottom:20px;}
  .q-block{margin-bottom:18px;}
  .q-block label.q-title{display:block;font-weight:600;margin-bottom:6px;font-size:13px;}
  .q-block .hint{color:#888;font-size:11px;margin-bottom:6px;}
  .q-option{display:flex;align-items:center;gap:8px;padding:5px 8px;border-radius:5px;cursor:pointer;font-size:13px;}
  .q-option:hover{background:#f5f5f5;}
  .q-option input{cursor:pointer;}
  #modal-submit{margin-top:8px;width:100%%;padding:10px;background:#0044cc;color:white;border:none;border-radius:6px;font-size:14px;font-weight:600;cursor:pointer;}
  #modal-submit:hover{background:#0033aa;}
  .grade-badge{display:inline-block;padding:2px 10px;border-radius:12px;color:white;font-weight:700;font-size:12px;}
</style>
</head>
<body>
<!-- Questionnaire modal -->
<div id="modal-overlay">
  <div id="modal">
    <h2>Trail Field Assessment</h2>
    <p class="subtitle">Answer based on your observations riding Askins. This combines with the LiDAR + rainfall data to give an overall NZ grade.</p>

    <div class="q-block">
      <label class="q-title">1. Tread obstacles — what is the typical max height of rocks, roots or ruts across the full trail width?</label>
      <div class="hint">NZ MTB Guidelines p.6 — key grading factor</div>
      <label class="q-option"><input type="radio" name="obstacles" value="0"> None — completely clear</label>
      <label class="q-option"><input type="radio" name="obstacles" value="1"> Up to 50mm (about a thumb width)</label>
      <label class="q-option"><input type="radio" name="obstacles" value="2"> Up to 100mm (about a fist)</label>
      <label class="q-option"><input type="radio" name="obstacles" value="3"> Up to 200mm (about a shoe length)</label>
      <label class="q-option"><input type="radio" name="obstacles" value="4"> Up to 600mm (knee height)</label>
      <label class="q-option"><input type="radio" name="obstacles" value="5"> No limit / bigger than that</label>
    </div>

    <div class="q-block">
      <label class="q-title">2. Trail surface — how would you describe the riding surface?</label>
      <div class="hint">NZ MTB Guidelines p.6-7</div>
      <label class="q-option"><input type="radio" name="surface" value="0"> Hardened and smooth (concrete / asphalt / sealed)</label>
      <label class="q-option"><input type="radio" name="surface" value="1"> Firm and stable (compacted gravel)</label>
      <label class="q-option"><input type="radio" name="surface" value="2"> Mostly stable with some loose or rough sections</label>
      <label class="q-option"><input type="radio" name="surface" value="3"> Generally firm but loose / broken in places</label>
      <label class="q-option"><input type="radio" name="surface" value="4"> Highly variable — natural surface, mud, ruts, roots</label>
    </div>

    <div class="q-block">
      <label class="q-title">3. Technical features — what best describes the built features on this trail?</label>
      <div class="hint">NZ MTB Guidelines p.5 — jumps, drops, gap jumps</div>
      <label class="q-option"><input type="radio" name="features" value="0"> All rollable — no jumps or drops</label>
      <label class="q-option"><input type="radio" name="features" value="1"> Rollable — stepped drops up to 200mm, no built jumps</label>
      <label class="q-option"><input type="radio" name="features" value="2"> Rollable — stepped drops up to 400mm, small tabletops</label>
      <label class="q-option"><input type="radio" name="features" value="3"> Mostly rollable — stepped drops up to 600mm, tabletops / gap jumps with B-lines</label>
      <label class="q-option"><input type="radio" name="features" value="4"> Stepped drops up to 1.5m, large jumps</label>
      <label class="q-option"><input type="radio" name="features" value="5"> No limits — anything goes</label>
    </div>

    <div class="q-block">
      <label class="q-title">4. Exposure — what happens if you crash off the side of the trail?</label>
      <div class="hint">NZ MTB Guidelines p.10 — fall height and landing surface</div>
      <label class="q-option"><input type="radio" name="exposure" value="0"> Nothing — flat or gentle terrain either side</label>
      <label class="q-option"><input type="radio" name="exposure" value="1"> Minor — soft vegetation, shallow slope</label>
      <label class="q-option"><input type="radio" name="exposure" value="2"> Moderate — some drop or rocky landing possible</label>
      <label class="q-option"><input type="radio" name="exposure" value="3"> Significant — steep hillside, large drop potential</label>
      <label class="q-option"><input type="radio" name="exposure" value="4"> Severe — cliff edge, river, or extended fall terrain</label>
    </div>

    <button id="modal-submit">Calculate Grade</button>
  </div>
</div>

<div id="map"></div>
<div id="stats-panel">
  <h3>Askins MTB &mdash; NZ Grade Assessment</h3>
  <table>
    <tr><th>Criterion</th><th>Result</th><th>Grade</th></tr>
    <tr><td>Average gradient</td><td>%(avg_grad)s%%</td><td>%(avg_grad_grade)s</td></tr>
    <tr><td>Max gradient (50m smooth)</td><td>%(max_grad)s%%</td><td>%(max_grad_grade_raw)s (raw)</td></tr>
    <tr><td>Gradient — with exceptions rule</td><td>%(over_pct)s%% out-of-spec</td><td>%(max_grad_grade)s</td></tr>
    <tr><td>Turn radius (typical)</td><td>%(p10_radius)sm</td><td>%(p10_radius_grade)s</td></tr>
    <tr><td>Turn radius (tightest corner)</td><td>%(min_radius)sm</td><td>%(min_radius_grade)s</td></tr>
    <tr><td>Annual rainfall (5yr avg)</td><td>%(avg_rainfall)s mm/yr</td><td>%(rain_label)s</td></tr>
    <tr id="row-obstacles"><td class="tbd">Tread obstacles</td><td class="tbd">—</td><td class="tbd">Answer questionnaire</td></tr>
    <tr id="row-surface"><td class="tbd">Trail surface</td><td class="tbd">—</td><td class="tbd">Answer questionnaire</td></tr>
    <tr id="row-features"><td class="tbd">Technical features</td><td class="tbd">—</td><td class="tbd">Answer questionnaire</td></tr>
    <tr id="row-exposure"><td class="tbd">Exposure</td><td class="tbd">—</td><td class="tbd">Answer questionnaire</td></tr>
    <tr class="overall" id="row-overall"><td><b>Overall grade</b></td><td colspan="2"><b>Complete questionnaire to calculate</b></td></tr>
  </table>
</div>
<script>
const map = L.map('map').setView([%(lat)s, %(lon)s], 15);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{attribution:'© OpenStreetMap contributors',maxZoom:19}).addTo(map);
const gradSegs = %(grad_segs)s;
const corners = %(corners)s;

gradSegs.forEach(s=>{
  const dir=s.gradient<0?'descent':'ascent';
  const popup=`
    <b>${s.grade}</b><br>
    <table style="font-size:13px;border-collapse:collapse;min-width:170px">
      <tr><td style="color:#888;padding-right:10px">Distance</td><td><b>${s.dist} m</b> from start</td></tr>
      <tr><td style="color:#888;padding-right:10px">Elevation</td><td><b>${s.elev} m</b></td></tr>
      <tr><td style="color:#888;padding-right:10px">Gradient</td><td><b>${Math.abs(s.gradient)}%%</b> ${dir}</td></tr>
    </table>`;
  L.polyline(s.coords,{color:s.color,weight:6,opacity:0.92}).addTo(map).bindPopup(popup);
});

corners.forEach(c=>{
  const popup=`
    <b>${c.grade} (turn radius)</b><br>
    <table style="font-size:13px;border-collapse:collapse;min-width:170px">
      <tr><td style="color:#888;padding-right:10px">Distance</td><td><b>${c.dist} m</b> from start</td></tr>
      <tr><td style="color:#888;padding-right:10px">Turn radius</td><td><b>${c.radius} m</b></td></tr>
    </table>`;
  L.circleMarker([c.lat,c.lon],{radius:7,color:'#fff',fillColor:c.color,fillOpacity:0.9,weight:2}).addTo(map).bindPopup(popup);
});

L.circleMarker([%(start_lat)s,%(start_lon)s],{radius:8,color:'#fff',fillColor:'#00b300',fillOpacity:1,weight:2}).addTo(map).bindPopup('Start - %(start_elev)s m');
L.circleMarker([%(end_lat)s,%(end_lon)s],{radius:8,color:'#fff',fillColor:'#cc0000',fillOpacity:1,weight:2}).addTo(map).bindPopup('End - %(end_elev)s m');

const leg=L.control({position:'bottomright'});
leg.onAdd=()=>{
  const d=L.DomUtil.create('div','legend');
  d.innerHTML=`
    <b>Askins MTB - Grade Assessment</b><br>
    <i style="font-size:11px">NZ MTB Trail Design Guidelines (2022)</i><br><br>
    <span class="sw" style="background:#00b300"></span>Grade 1 Easiest<br>
    <span class="sw" style="background:#66cc00"></span>Grade 2 Easy<br>
    <span class="sw" style="background:#00aaff"></span>Grade 3 Intermediate<br>
    <span class="sw" style="background:#0044cc"></span>Grade 4 Advanced<br>
    <span class="sw" style="background:#444444"></span>Grade 5 Expert<br>
    <span class="sw" style="background:#660000"></span>Grade 6 Extreme<br>
    <hr style="margin:6px 0">
    <b>Trail line</b> = gradient grade<br>
    <b>Circles</b> = tight corners (turn radius)<br>
    <hr style="margin:6px 0">
    <b style="font-size:13px">Overall: %(overall)s</b><br>
    <i style="font-size:10px;color:#666">Based on gradient + turn radius.<br>Jumps, obstacles &amp; surface<br>require field assessment.</i>
  `;
  return d;
};
leg.addTo(map);

// ---- Questionnaire logic ----
const GRADES = [
  {name:'Grade 1', label:'Easiest',      hex:'#00b300', color_nz:'Green'},
  {name:'Grade 2', label:'Easy',         hex:'#66cc00', color_nz:'Green'},
  {name:'Grade 3', label:'Intermediate', hex:'#00aaff', color_nz:'Light Blue'},
  {name:'Grade 4', label:'Advanced',     hex:'#0044cc', color_nz:'Dark Blue'},
  {name:'Grade 5', label:'Expert',       hex:'#444444', color_nz:'Black'},
  {name:'Grade 6', label:'Extreme',      hex:'#660000', color_nz:'Double Black'},
];

// Data-derived grades (from Python)
const dataGrade = %(overall_grade)d;  // gradient + turn radius + rainfall

document.getElementById('modal-submit').addEventListener('click', () => {
  const get = name => {
    const el = document.querySelector('input[name="'+name+'"]:checked');
    return el ? parseInt(el.value) : null;
  };
  const obstacles = get('obstacles');
  const surface   = get('surface');
  const features  = get('features');
  const exposure  = get('exposure');

  if ([obstacles, surface, features, exposure].some(v => v === null)) {
    alert('Please answer all questions before calculating.');
    return;
  }

  // Surface grade: 0-4 maps to G1-G5
  const surfaceGrade = surface;
  // Exposure grade: 0-4 maps roughly to G1-G5
  const exposureGrade = exposure;

  const overall = Math.min(5, Math.max(dataGrade, obstacles, surfaceGrade, features, exposureGrade));
  const g = GRADES[overall];

  // Update stats table rows
  document.getElementById('row-obstacles').innerHTML =
    '<td>Tread obstacles</td><td>'+['None','Up to 50mm','Up to 100mm','Up to 200mm','Up to 600mm','No limit'][obstacles]+'</td><td>'+GRADES[obstacles].name+' '+GRADES[obstacles].label+'</td>';
  document.getElementById('row-surface').innerHTML =
    '<td>Trail surface</td><td>'+['Hardened/smooth','Firm & stable','Mostly stable','Generally firm, loose patches','Highly variable'][surface]+'</td><td>'+GRADES[surfaceGrade].name+' '+GRADES[surfaceGrade].label+'</td>';
  document.getElementById('row-features').innerHTML =
    '<td>Technical features</td><td>'+['All rollable','Drops &lt;=200mm','Drops &lt;=400mm','Drops &lt;=600mm','Drops &lt;=1.5m','No limit'][features]+'</td><td>'+GRADES[features].name+' '+GRADES[features].label+'</td>';
  document.getElementById('row-exposure').innerHTML =
    '<td>Exposure</td><td>'+['None','Minor','Moderate','Significant','Severe'][exposure]+'</td><td>'+GRADES[exposureGrade].name+' '+GRADES[exposureGrade].label+'</td>';

  document.getElementById('row-overall').innerHTML =
    '<td><b>Overall grade</b></td><td colspan="2"><b style="font-size:14px;color:'+g.hex+'">'+g.name+' — '+g.label+' ('+g.color_nz+')</b></td>';

  // Close modal
  document.getElementById('modal-overlay').style.display = 'none';
});
</script>
</body>
</html>""" % {
    'lat': mid['lat'], 'lon': mid['lon'],
    'grad_segs': json.dumps(grad_segs),
    'corners': json.dumps(tight_corners),
    'start_lat': pts[0]['lat'], 'start_lon': pts[0]['lon'], 'start_elev': '%.1f' % pts[0]['elev'],
    'end_lat': pts[-1]['lat'], 'end_lon': pts[-1]['lon'], 'end_elev': '%.1f' % pts[-1]['elev'],
    'overall': overall_label,
    'avg_grad': '%.1f' % avg_grad,
    'avg_grad_grade': '%s %s' % (GRADES[grad_avg_gi]['name'], GRADES[grad_avg_gi]['label']),
    'max_grad': '%.1f' % max_grad,
    'max_grad_grade_raw': '%s %s' % (GRADES[grad_naive_gi]['name'], GRADES[grad_naive_gi]['label']),
    'max_grad_grade': '%s %s' % (GRADES[grad_seg_gi]['name'], GRADES[grad_seg_gi]['label']),
    'over_pct': '%.1f' % over_pct,
    'p10_radius': '%.1f' % p10_radius,
    'p10_radius_grade': '%s %s' % (GRADES[turn_grade]['name'], GRADES[turn_grade]['label']),
    'min_radius': '%.1f' % min_radius,
    'min_radius_grade': '%s %s' % (GRADES[turn_grade_min]['name'], GRADES[turn_grade_min]['label']),
    'avg_rainfall': '%.0f' % avg_rainfall if rain_ok else 'N/A',
    'rain_label': rainfall_label(avg_rainfall) if rain_ok else 'Could not fetch',
    'overall_grade': overall_grade,
}

out = r'C:\Users\Shea\GIS_Work\askins_full_grade_map.html'
with open(out, 'w', encoding='utf-8') as f:
    f.write(html)
print('\nMap saved: ' + out)
