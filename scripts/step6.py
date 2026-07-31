import math, csv, re

# Parse coordinates from KML
kml_path = r'C:\Users\Shea\Downloads\askins.kml'
with open(kml_path) as f:
    content = f.read()

# Extract coordinate block
coords_raw = re.search(r'<coordinates>(.*?)</coordinates>', content, re.DOTALL).group(1).strip()
coord_tuples = []
for c in coords_raw.split():
    parts = c.strip().split(',')
    if len(parts) >= 3:
        lon, lat, elev = float(parts[0]), float(parts[1]), float(parts[2])
        coord_tuples.append((lon, lat, elev))

print(f'Parsed {len(coord_tuples)} KML coordinate points')
print(f'Start: lon={coord_tuples[0][0]}, lat={coord_tuples[0][1]}, elev={coord_tuples[0][2]}m')
print(f'End:   lon={coord_tuples[-1][0]}, lat={coord_tuples[-1][1]}, elev={coord_tuples[-1][2]}m')

# Convert to NZTM2000 for accurate distance calculation using arcpy
import arcpy
sr_wgs84 = arcpy.SpatialReference(4326)
sr_nztm = arcpy.SpatialReference(2193)

# Project each point
pts_nztm = []
for lon, lat, elev in coord_tuples:
    pt = arcpy.PointGeometry(arcpy.Point(lon, lat), sr_wgs84)
    pt_proj = pt.projectAs(sr_nztm)
    pts_nztm.append((pt_proj.centroid.X, pt_proj.centroid.Y, elev))

# Calculate cumulative distance and gradient
rows = []
cum_dist = 0.0
for i, (x, y, elev) in enumerate(pts_nztm):
    if i == 0:
        rows.append({'seq': i, 'cum_dist': 0.0, 'elev_m': elev, 'gradient_pct': None, 'x': x, 'y': y})
    else:
        px, py, pe = pts_nztm[i-1]
        horiz = math.sqrt((x-px)**2 + (y-py)**2)
        vert = elev - pe
        cum_dist += horiz
        grad = (vert / horiz * 100) if horiz > 0 else None
        rows.append({'seq': i, 'cum_dist': round(cum_dist, 1), 'elev_m': elev, 'gradient_pct': round(grad, 2) if grad else None, 'x': x, 'y': y})

# Rolling average smoothing (5-point window ~50m since points are ~10-20m apart)
window = 5
for i in range(len(rows)):
    start = max(0, i - window // 2)
    end = min(len(rows), i + window // 2 + 1)
    gs = [rows[j]['gradient_pct'] for j in range(start, end) if rows[j]['gradient_pct'] is not None]
    rows[i]['gradient_smooth'] = round(sum(gs)/len(gs), 2) if gs else None

# Write CSV
csv_path = r'C:\Users\Shea\GIS_Work\askins_gradient_kml.csv'
with open(csv_path, 'w', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=['seq', 'cum_dist', 'elev_m', 'gradient_pct', 'gradient_smooth', 'x', 'y'])
    writer.writeheader()
    writer.writerows(rows)

# Stats
grads_raw = [r['gradient_pct'] for r in rows if r['gradient_pct'] is not None]
grads_smooth = [r['gradient_smooth'] for r in rows if r['gradient_smooth'] is not None]
total_dist = rows[-1]['cum_dist']
total_descent = rows[-1]['elev_m'] - rows[0]['elev_m']
avg_grad = (total_descent / total_dist * 100) if total_dist > 0 else 0

# Find steepest sections
steepest_pt = min(rows[1:], key=lambda r: r['gradient_pct'] or 0)
steepest_up_pt = max(rows[1:], key=lambda r: r['gradient_pct'] or 0)

# Grade distribution
bands = {
    'flat (0-5%)': 0, 'moderate (5-15%)': 0, 'steep (15-30%)': 0,
    'very steep (>30%)': 0, 'uphill': 0
}
for r in rows[1:]:
    g = r['gradient_pct']
    if g is None: continue
    if g >= 0: bands['uphill'] += 1
    elif abs(g) <= 5: bands['flat (0-5%)'] += 1
    elif abs(g) <= 15: bands['moderate (5-15%)'] += 1
    elif abs(g) <= 30: bands['steep (15-30%)'] += 1
    else: bands['very steep (>30%)'] += 1

summary = [
    'Trail: Askins MTB (Christchurch)',
    f'Source: KML GPS elevations (ground-level)',
    f'',
    f'Total length:      {total_dist:.0f} m',
    f'Start elevation:   {rows[0]["elev_m"]:.0f} m',
    f'End elevation:     {rows[-1]["elev_m"]:.0f} m',
    f'Total descent:     {total_descent:.0f} m',
    f'Average gradient:  {avg_grad:.1f}%',
    f'',
    f'Max descent:       {steepest_pt["gradient_pct"]:.1f}% at {steepest_pt["cum_dist"]:.0f}m from start',
    f'Max ascent:        {steepest_up_pt["gradient_pct"]:.1f}% at {steepest_up_pt["cum_dist"]:.0f}m from start',
    f'',
    f'Gradient distribution:',
    f'  Flat (0-5%):      {bands["flat (0-5%)"] * 100 // max(len(grads_raw),1)}% of trail',
    f'  Moderate (5-15%): {bands["moderate (5-15%)"] * 100 // max(len(grads_raw),1)}% of trail',
    f'  Steep (15-30%):   {bands["steep (15-30%)"] * 100 // max(len(grads_raw),1)}% of trail',
    f'  Very steep (>30%): {bands["very steep (>30%)"] * 100 // max(len(grads_raw),1)}% of trail',
    f'  Uphill sections:  {bands["uphill"] * 100 // max(len(grads_raw),1)}% of trail',
]

with open(r'C:\Users\Shea\GIS_Work\gradient_summary.txt', 'w') as f:
    f.write('\n'.join(summary))

for line in summary:
    print(line)
