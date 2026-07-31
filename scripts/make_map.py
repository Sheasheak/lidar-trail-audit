import arcpy, csv, json

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

# Convert NZTM2000 -> WGS84
sr_nztm = arcpy.SpatialReference(2193)
sr_wgs84 = arcpy.SpatialReference(4326)

for p in pts:
    geom = arcpy.PointGeometry(arcpy.Point(p['x'], p['y']), sr_nztm).projectAs(sr_wgs84)
    p['lon'] = geom.centroid.X
    p['lat'] = geom.centroid.Y

print(f'Converted {len(pts)} points to WGS84')

# Build segments: each segment is a pair of consecutive points, colored by gradient
def gradient_color(g):
    if g > 2:      return '#3388ff'   # uphill - blue
    elif g > -5:   return '#00c44f'   # flat - green
    elif g > -10:  return '#ffe000'   # gentle - yellow
    elif g > -15:  return '#ff9900'   # moderate - orange
    elif g > -20:  return '#ff5500'   # steep - dark orange
    elif g > -27:  return '#ff0000'   # very steep - red
    else:          return '#990000'   # extreme - dark red

segments = []
for i in range(1, len(pts)):
    p0, p1 = pts[i-1], pts[i]
    g = p1['gradient_50m']
    segments.append({
        'coords': [[p0['lat'], p0['lon']], [p1['lat'], p1['lon']]],
        'color': gradient_color(g),
        'gradient': g,
        'dist': p1['cum_dist'],
        'elev': p1['elev']
    })

# Center of trail
mid = pts[len(pts)//2]

html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Askins MTB Gradient Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  body {{ margin: 0; font-family: sans-serif; }}
  #map {{ height: 100vh; }}
  .legend {{ background: white; padding: 12px 16px; border-radius: 6px; box-shadow: 0 1px 5px rgba(0,0,0,0.3); line-height: 1.8; font-size: 13px; }}
  .legend-swatch {{ display: inline-block; width: 14px; height: 14px; border-radius: 2px; margin-right: 6px; vertical-align: middle; }}
  #tooltip {{ position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: rgba(0,0,0,0.75); color: white; padding: 6px 14px; border-radius: 20px; font-size: 13px; pointer-events: none; display: none; }}
</style>
</head>
<body>
<div id="map"></div>
<div id="tooltip"></div>
<script>
const map = L.map('map').setView([{mid['lat']}, {mid['lon']}], 15);

L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  attribution: '© OpenStreetMap contributors',
  maxZoom: 19
}}).addTo(map);

const segments = {json.dumps(segments)};
const tooltip = document.getElementById('tooltip');

segments.forEach(seg => {{
  const line = L.polyline(seg.coords, {{
    color: seg.color,
    weight: 5,
    opacity: 0.9
  }}).addTo(map);
  line.on('mouseover', e => {{
    const dir = seg.gradient < 0 ? 'descent' : 'ascent';
    tooltip.style.display = 'block';
    tooltip.textContent = `${{seg.dist.toFixed(0)}}m from start  |  ${{seg.elev.toFixed(1)}}m elev  |  ${{Math.abs(seg.gradient).toFixed(1)}}% ${{dir}}`;
  }});
  line.on('mouseout', () => {{ tooltip.style.display = 'none'; }});
}});

// Start/end markers
const startPt = [{pts[0]['lat']}, {pts[0]['lon']}];
const endPt   = [{pts[-1]['lat']}, {pts[-1]['lon']}];
L.circleMarker(startPt, {{radius:8, color:'#fff', fillColor:'#00c44f', fillOpacity:1, weight:2}}).addTo(map).bindPopup('Start — {pts[0]["elev"]:.1f}m');
L.circleMarker(endPt,   {{radius:8, color:'#fff', fillColor:'#ff0000', fillOpacity:1, weight:2}}).addTo(map).bindPopup('End — {pts[-1]["elev"]:.1f}m');

// Legend
const legend = L.control({{position: 'bottomright'}});
legend.onAdd = () => {{
  const d = L.DomUtil.create('div', 'legend');
  d.innerHTML = `
    <b>Askins MTB — Gradient</b><br>
    <span class="legend-swatch" style="background:#3388ff"></span> Uphill<br>
    <span class="legend-swatch" style="background:#00c44f"></span> Flat (0–5%)<br>
    <span class="legend-swatch" style="background:#ffe000"></span> Gentle (5–10%)<br>
    <span class="legend-swatch" style="background:#ff9900"></span> Moderate (10–15%)<br>
    <span class="legend-swatch" style="background:#ff5500"></span> Steep (15–20%)<br>
    <span class="legend-swatch" style="background:#ff0000"></span> Very steep (20–27%)<br>
    <br><i style="font-size:11px">Hover over trail for details</i>
  `;
  return d;
}};
legend.addTo(map);
</script>
</body>
</html>"""

out = r'C:\Users\Shea\GIS_Work\askins_gradient_map.html'
with open(out, 'w') as f:
    f.write(html)
print(f'Map saved: {out}')
