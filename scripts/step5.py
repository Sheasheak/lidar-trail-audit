import arcpy, os, math, csv

arcpy.env.overwriteOutput = True
arcpy.CheckOutExtension('Spatial')
gdb = r'C:\Users\Shea\GIS_Work\gradient.gdb'
dsm_mosaic = os.path.join(gdb, 'dsm_mosaic')
askins_nztm = os.path.join(gdb, 'askins_nztm')

# Get vertices directly from the polyline in order
dsm_raster = arcpy.Raster(dsm_mosaic)

vertices = []
with arcpy.da.SearchCursor(askins_nztm, ['SHAPE@']) as cur:
    for row in cur:
        geom = row[0]
        for part in geom:
            for pnt in part:
                if pnt:
                    vertices.append((pnt.X, pnt.Y))

print(f'Got {len(vertices)} vertices from polyline')

# Densify: add points every 5m along the line
def densify_line(verts, step=5.0):
    dense = [verts[0]]
    for i in range(1, len(verts)):
        x1, y1 = verts[i-1]
        x2, y2 = verts[i]
        seg_len = math.sqrt((x2-x1)**2 + (y2-y1)**2)
        if seg_len > 0:
            n = int(seg_len / step)
            for j in range(1, n+1):
                t = j * step / seg_len
                if t < 1.0:
                    dense.append((x1 + t*(x2-x1), y1 + t*(y2-y1)))
        dense.append(verts[i])
    return dense

vertices = densify_line(vertices, step=5.0)
print(f'Densified to {len(vertices)} points (5m interval)')

# Sample DSM at each point using the raster object
# Build a temporary point feature class
tmp_pts = os.path.join(gdb, 'tmp_sample_pts')
sr = arcpy.SpatialReference(2193)
arcpy.management.CreateFeatureclass(gdb, 'tmp_sample_pts', 'POINT', spatial_reference=sr)
arcpy.management.AddField(tmp_pts, 'PT_ID', 'LONG')

with arcpy.da.InsertCursor(tmp_pts, ['SHAPE@XY', 'PT_ID']) as ins:
    for i, (x, y) in enumerate(vertices):
        ins.insertRow([(x, y), i])

# Extract values
tmp_elev = os.path.join(gdb, 'tmp_elev_pts')
arcpy.sa.ExtractValuesToPoints(tmp_pts, dsm_mosaic, tmp_elev)
print('Elevation sampled')

# Read back in PT_ID order
elev_map = {}
with arcpy.da.SearchCursor(tmp_elev, ['PT_ID', 'RASTERVALU']) as cur:
    for row in cur:
        elev_map[row[0]] = row[1]

# Build ordered list
pts = []
cum_dist = 0.0
for i, (x, y) in enumerate(vertices):
    elev = elev_map.get(i, -9999)
    if i == 0:
        pts.append({'seq': i, 'x': x, 'y': y, 'elev': elev, 'cum_dist': 0.0, 'gradient_pct': None})
    else:
        prev = pts[i-1]
        horiz = math.sqrt((x - prev['x'])**2 + (y - prev['y'])**2)
        cum_dist += horiz
        if horiz > 0 and elev > -9000 and prev['elev'] > -9000:
            grad = (elev - prev['elev']) / horiz * 100
        else:
            grad = None
        pts.append({'seq': i, 'x': x, 'y': y, 'elev': elev, 'cum_dist': round(cum_dist, 1), 'gradient_pct': round(grad, 2) if grad is not None else None})

# Smooth gradient using 10-point rolling average (~50m window)
window = 10
for i in range(len(pts)):
    start = max(0, i - window // 2)
    end = min(len(pts), i + window // 2 + 1)
    grads = [pts[j]['gradient_pct'] for j in range(start, end) if pts[j]['gradient_pct'] is not None]
    pts[i]['gradient_smooth'] = round(sum(grads)/len(grads), 2) if grads else None

# Write CSV
csv_path = r'C:\Users\Shea\GIS_Work\askins_gradient.csv'
with open(csv_path, 'w', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=['seq', 'cum_dist', 'elev', 'gradient_pct', 'gradient_smooth', 'x', 'y'])
    writer.writeheader()
    writer.writerows(pts)
print(f'CSV saved')

# Summary - use smoothed gradients
valid = [p for p in pts if p['elev'] > -9000 and p['gradient_smooth'] is not None]
raw_grads = [p['gradient_pct'] for p in pts[1:] if p['gradient_pct'] is not None]
smooth_grads = [p['gradient_smooth'] for p in valid]
total_dist = pts[-1]['cum_dist']
total_descent = pts[-1]['elev'] - pts[0]['elev']
avg_grad = (total_descent / total_dist * 100) if total_dist > 0 else 0

summary = [
    'Trail: Askins MTB (Christchurch LiDAR DSM 1m)',
    f'Total length: {total_dist:.0f} m',
    f'Start elevation: {pts[0]["elev"]:.1f} m',
    f'End elevation: {pts[-1]["elev"]:.1f} m',
    f'Total descent: {total_descent:.1f} m',
    f'Average gradient: {avg_grad:.1f}%',
    f'Max descent gradient (raw): {min(raw_grads):.1f}%',
    f'Max descent gradient (50m smooth): {min(smooth_grads):.1f}%',
    f'Max ascent gradient (50m smooth): {max(smooth_grads):.1f}%',
]
with open(r'C:\Users\Shea\GIS_Work\gradient_summary.txt', 'w') as f:
    f.write('\n'.join(summary))
for line in summary:
    print(line)
