import arcpy, csv, json, math

# -------------------------------------------------------
# NZ MTB Trail Design Guidelines - Descending Trail Thresholds
# Source: NZ MTB Trail Design & Construction Guidelines (Aug 2022), Page 5
# "1 in X" ratio converted to percentage = 100/X
# -------------------------------------------------------
GRADES = [
    {'name': 'Grade 1 - Easiest', 'color_nz': 'Green',       'hex': '#00b300', 'avg_max': 100/16.3, 'seg_max': 100/14.3},
    {'name': 'Grade 2 - Easy',    'color_nz': 'Green (Light)','hex': '#66cc00', 'avg_max': 100/11.4, 'seg_max': 100/7.0 },
    {'name': 'Grade 3 - Intermediate','color_nz':'Light Blue','hex': '#00aaff', 'avg_max': 100/10.5, 'seg_max': 100/5.1 },
    {'name': 'Grade 4 - Advanced','color_nz': 'Dark Blue',   'hex': '#0044cc', 'avg_max': 100/6.3,  'seg_max': 100/3.7 },
    {'name': 'Grade 5 - Expert',  'color_nz': 'Black',       'hex': '#222222', 'avg_max': 100/4.0,  'seg_max': 100/2.7 },
    {'name': 'Grade 6 - Extreme', 'color_nz': 'Double Black','hex': '#660000', 'avg_max': None,      'seg_max': None    },
]

def segment_grade(abs_gradient_pct):
    """Return grade index (0-5) for a segment based on absolute gradient %."""
    for i, g in enumerate(GRADES[:-1]):
        if abs_gradient_pct <= g['seg_max']:
            return i
    return 5  # Grade 6

# Read the DTM gradient CSV
pts = []
with open(r'C:\Users\Shea\GIS_Work\askins_dtm_gradient.csv') as f:
    for r in csv.DictReader(f):
        pts.append({
            'x': float(r['x']),
            'y': float(r['y']),
            'elev': float(r['elev']),
            'cum_dist': float(r['cum_dist']),
            'gradient_50m': float(r['gradient_50m']) if r['gradient_50m'] else 0.0
        })

# Convert NZTM -> WGS84
sr_nztm = arcpy.SpatialReference(2193)
sr_wgs84 = arcpy.SpatialReference(4326)
for p in pts:
    g = arcpy.PointGeometry(arcpy.Point(p['x'], p['y']), sr_nztm).projectAs(sr_wgs84)
    p['lon'] = g.centroid.X
    p['lat'] = g.centroid.Y
print(f'Converted {len(pts)} points')

# Assign grade to each segment
segments = []
for i in range(1, len(pts)):
    p0, p1 = pts[i-1], pts[i]
    abs_g = abs(p1['gradient_50m'])
    grade_idx = segment_grade(abs_g)
    segments.append({
        'coords': [[p0['lat'], p0['lon']], [p1['lat'], p1['lon']]],
        'color': GRADES[grade_idx]['hex'],
        'grade_name': GRADES[grade_idx]['name'],
        'gradient': p1['gradient_50m'],
        'dist': p1['cum_dist'],
        'elev': p1['elev'],
        'grade_idx': grade_idx
    })

# -------------------------------------------------------
# Overall trail assessment
# -------------------------------------------------------
total_dist = pts[-1]['cum_dist']
total_descent = pts[-1]['elev'] - pts[0]['elev']
avg_grad_abs = abs(total_descent / total_dist * 100)

# Determine avg grade
avg_grade_idx = 5
for i, g in enumerate(GRADES[:-1]):
    if avg_grad_abs <= g['avg_max']:
        avg_grade_idx = i
        break

# Count segment distribution
seg_counts = [0] * 6
seg_dist = [0.0] * 6
for i, s in enumerate(segments):
    gi = s['grade_idx']
    seg_counts[gi] += 1
    if i < len(segments) - 1:
        seg_dist[gi] += segments[i+1]['dist'] - s['dist']
    else:
        seg_dist[gi] += 0

# Overall grade = max of avg grade and most common segment grade
max_seg_grade = max(s['grade_idx'] for s in segments)
overall_grade = max(avg_grade_idx, max_seg_grade)

# Summary
print('\n=== TRAIL GRADING REPORT: Askins MTB (Christchurch) ===')
print(f'Source: LINZ LiDAR 1m DTM (bare earth), 50m smoothing\n')
print(f'Total length:      {total_dist:.0f} m')
print(f'Total descent:     {total_descent:.1f} m')
print(f'Average gradient:  -{avg_grad_abs:.1f}%')
print(f'Average grade:     {GRADES[avg_grade_idx]["name"]}')
print(f'Max segment grade: {GRADES[max_seg_grade]["name"]}')
print(f'\n>>> OVERALL GRADE: {GRADES[overall_grade]["name"]} ({GRADES[overall_grade]["color_nz"]}) <<<\n')
print('Gradient thresholds (NZ MTB Guidelines, descending trails):')
for g in GRADES[:-1]:
    print(f'  {g["name"]}: avg max {g["avg_max"]:.1f}%, segment max {g["seg_max"]:.1f}%')
print('\nSegment distribution (by 50m-smoothed gradient):')
for i, g in enumerate(GRADES):
    n = seg_counts[i]
    pct = n * 100 // max(len(segments), 1)
    print(f'  {g["name"]}: {n} segments ({pct}%)')

# Write summary to file
with open(r'C:\Users\Shea\GIS_Work\grading_report.txt', 'w') as f:
    f.write('TRAIL GRADING REPORT: Askins MTB (Christchurch)\n')
    f.write('Source: LINZ LiDAR 1m DTM (bare earth), 50m smoothing\n\n')
    f.write(f'Total length:     {total_dist:.0f} m\n')
    f.write(f'Total descent:    {total_descent:.1f} m\n')
    f.write(f'Average gradient: -{avg_grad_abs:.1f}%\n\n')
    f.write(f'OVERALL GRADE: {GRADES[overall_grade]["name"]} ({GRADES[overall_grade]["color_nz"]})\n\n')
    f.write('Segment distribution:\n')
    for i, g in enumerate(GRADES):
        f.write(f'  {g["name"]}: {seg_counts[i]} segments\n')

# -------------------------------------------------------
# Build HTML map with NZ grade colors
# -------------------------------------------------------
mid = pts[len(pts)//2]
html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Askins MTB — NZ Grade Map</title>
<link rel="stylesheet" href="leaflet.css"/>
<script src="leaflet.js"></script>
<style>
  body {{ margin: 0; font-family: sans-serif; }}
  #map {{ height: 100vh; }}
  .legend {{ background: white; padding: 12px 16px; border-radius: 6px; box-shadow: 0 1px 5px rgba(0,0,0,0.3); line-height: 1.9; font-size: 13px; min-width: 220px; }}
  .swatch {{ display: inline-block; width: 22px; height: 10px; border-radius: 2px; margin-right: 7px; vertical-align: middle; border: 1px solid #ccc; }}
  #tooltip {{ position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: rgba(0,0,0,0.8); color: white; padding: 7px 16px; border-radius: 20px; font-size: 13px; pointer-events: none; display: none; white-space: nowrap; }}
</style>
</head>
<body>
<div id="map"></div>
<div id="tooltip"></div>
<script>
const map = L.map('map').setView([{mid['lat']}, {mid['lon']}], 15);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  attribution: '© OpenStreetMap contributors', maxZoom: 19
}}).addTo(map);

const segments = {json.dumps(segments)};
const tooltip = document.getElementById('tooltip');

segments.forEach(seg => {{
  L.polyline(seg.coords, {{ color: seg.color, weight: 6, opacity: 0.92 }}).addTo(map)
    .on('mouseover', () => {{
      const dir = seg.gradient < 0 ? 'descent' : 'ascent';
      tooltip.style.display = 'block';
      tooltip.textContent = `${{seg.dist.toFixed(0)}}m  |  ${{seg.elev.toFixed(1)}}m elev  |  ${{Math.abs(seg.gradient).toFixed(1)}}% ${{dir}}  |  ${{seg.grade_name}}`;
    }})
    .on('mouseout', () => {{ tooltip.style.display = 'none'; }});
}});

L.circleMarker([{pts[0]['lat']}, {pts[0]['lon']}], {{radius:8, color:'#fff', fillColor:'#00b300', fillOpacity:1, weight:2}}).addTo(map).bindPopup('Start — {pts[0]["elev"]:.1f}m');
L.circleMarker([{pts[-1]['lat']}, {pts[-1]['lon']}], {{radius:8, color:'#fff', fillColor:'#cc0000', fillOpacity:1, weight:2}}).addTo(map).bindPopup('End — {pts[-1]["elev"]:.1f}m');

const legend = L.control({{position: 'bottomright'}});
legend.onAdd = () => {{
  const d = L.DomUtil.create('div', 'legend');
  d.innerHTML = `
    <b>Askins MTB — NZ Grade by Gradient</b><br>
    <i style="font-size:11px">NZ MTB Trail Design Guidelines (2022)</i><br><br>
    <span class="swatch" style="background:#00b300"></span> Grade 1 Easiest (Green) — max 7.0%<br>
    <span class="swatch" style="background:#66cc00"></span> Grade 2 Easy (Green) — max 14.3%<br>
    <span class="swatch" style="background:#00aaff"></span> Grade 3 Intermediate (Light Blue) — max 19.6%<br>
    <span class="swatch" style="background:#0044cc"></span> Grade 4 Advanced (Dark Blue) — max 27.0%<br>
    <span class="swatch" style="background:#222222"></span> Grade 5 Expert (Black) — max 37.0%<br>
    <span class="swatch" style="background:#660000"></span> Grade 6 Extreme (Double Black)<br>
    <br>
    <b>Overall: {GRADES[overall_grade]["name"]}</b><br>
    Avg gradient: {avg_grad_abs:.1f}% | Max: {abs(min(s["gradient"] for s in segments)):.1f}%<br>
    <i style="font-size:11px">Hover trail for details</i>
  `;
  return d;
}};
legend.addTo(map);
</script>
</body>
</html>"""

out = r'C:\Users\Shea\GIS_Work\askins_grade_map.html'
with open(out, 'w') as f:
    f.write(html)
print(f'\nMap saved: {out}')
