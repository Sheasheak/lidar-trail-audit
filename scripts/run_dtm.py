import arcpy, os, math, csv

arcpy.env.overwriteOutput = True
arcpy.CheckOutExtension('Spatial')

dtm_folder = r'C:\Users\Shea\Downloads\lds-canterbury-christchurch-lidar-1m-dem-2024-2025-AAIGrid'
gdb = r'C:\Users\Shea\GIS_Work\gradient.gdb'
arcpy.env.workspace = gdb

askins_nztm = os.path.join(gdb, 'askins_nztm')
nztm = arcpy.SpatialReference(2193)

# --- Step 1: Find relevant DTM tiles ---
desc = arcpy.Describe(askins_nztm)
ext = desc.extent
print(f'Trail extent NZTM: {ext.XMin:.0f},{ext.YMin:.0f} to {ext.XMax:.0f},{ext.YMax:.0f}')

asc_files = [os.path.join(dtm_folder, f) for f in os.listdir(dtm_folder) if f.endswith('.asc')]
print(f'Total DTM tiles: {len(asc_files)}')

relevant_tiles = []
for asc in asc_files:
    try:
        rd = arcpy.Describe(asc)
        re = rd.extent
        if re.XMin < ext.XMax and re.XMax > ext.XMin and re.YMin < ext.YMax and re.YMax > ext.YMin:
            relevant_tiles.append(asc)
            print(f'  Using: {os.path.basename(asc)}')
    except:
        pass

print(f'Found {len(relevant_tiles)} relevant tiles')
if not relevant_tiles:
    raise RuntimeError('No tiles found — check DTM folder path or trail extent')

# --- Step 2: Mosaic relevant tiles ---
arcpy.management.MosaicToNewRaster(relevant_tiles, gdb, 'dtm_mosaic', nztm, '32_BIT_FLOAT', 1, 1)
print('DTM mosaic created')

dtm_mosaic = os.path.join(gdb, 'dtm_mosaic')

# --- Step 3: Get densified vertices from trail polyline ---
vertices = []
with arcpy.da.SearchCursor(askins_nztm, ['SHAPE@']) as cur:
    for row in cur:
        for part in row[0]:
            for pnt in part:
                if pnt:
                    vertices.append((pnt.X, pnt.Y))

print(f'Got {len(vertices)} vertices from polyline')

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

# --- Step 4: Sample DTM at each point ---
tmp_pts = os.path.join(gdb, 'tmp_dtm_pts')
arcpy.management.CreateFeatureclass(gdb, 'tmp_dtm_pts', 'POINT', spatial_reference=nztm)
arcpy.management.AddField(tmp_pts, 'PT_ID', 'LONG')

with arcpy.da.InsertCursor(tmp_pts, ['SHAPE@XY', 'PT_ID']) as ins:
    for i, (x, y) in enumerate(vertices):
        ins.insertRow([(x, y), i])

tmp_elev = os.path.join(gdb, 'tmp_dtm_elev_pts')
arcpy.sa.ExtractValuesToPoints(tmp_pts, dtm_mosaic, tmp_elev)
print('Elevation sampled from DTM')

elev_map = {}
with arcpy.da.SearchCursor(tmp_elev, ['PT_ID', 'RASTERVALU']) as cur:
    for row in cur:
        elev_map[row[0]] = row[1]

# --- Step 5: Calculate gradient with 50m smoothing ---
def grad_at(pts, i, half_window=25):
    ci = pts[i]['cum_dist']
    rear = pts[0]
    for j in range(i-1, -1, -1):
        if ci - pts[j]['cum_dist'] >= half_window:
            rear = pts[j]; break
    front = pts[-1]
    for j in range(i+1, len(pts)):
        if pts[j]['cum_dist'] - ci >= half_window:
            front = pts[j]; break
    horiz = front['cum_dist'] - rear['cum_dist']
    vert = front['elev'] - rear['elev']
    return (vert / horiz * 100) if horiz > 0 else 0.0

pts = []
cum_dist = 0.0
for i, (x, y) in enumerate(vertices):
    elev = elev_map.get(i, -9999)
    if i == 0:
        pts.append({'seq': i, 'x': x, 'y': y, 'elev': elev, 'cum_dist': 0.0})
    else:
        prev = pts[i-1]
        horiz = math.sqrt((x-prev['x'])**2 + (y-prev['y'])**2)
        cum_dist += horiz
        pts.append({'seq': i, 'x': x, 'y': y, 'elev': elev, 'cum_dist': round(cum_dist, 1)})

for i, p in enumerate(pts):
    p['gradient_50m'] = round(grad_at(pts, i, 25), 1) if p['elev'] > -9000 else None

# --- Step 6: Write CSV ---
csv_path = r'C:\Users\Shea\GIS_Work\askins_dtm_gradient.csv'
with open(csv_path, 'w', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=['seq', 'cum_dist', 'elev', 'gradient_50m', 'x', 'y'])
    writer.writeheader()
    writer.writerows(pts)
print(f'CSV saved: {csv_path}')

# --- Summary ---
valid = [p for p in pts if p['elev'] > -9000 and p['gradient_50m'] is not None]
grads = [p['gradient_50m'] for p in valid[5:-5]]
total_dist = pts[-1]['cum_dist']
total_descent = pts[-1]['elev'] - pts[0]['elev']
avg_grad = (total_descent / total_dist * 100) if total_dist > 0 else 0
n = len(grads)

lines = [
    'Trail: Askins MTB (Christchurch)',
    'Source: LINZ LiDAR 1m DTM/DEM (bare earth), 50m smoothing',
    '',
    f'Total length:      {total_dist:.0f} m',
    f'Start elevation:   {pts[0]["elev"]:.1f} m',
    f'End elevation:     {pts[-1]["elev"]:.1f} m',
    f'Total descent:     {total_descent:.1f} m',
    f'Average gradient:  {avg_grad:.1f}%',
    '',
    f'Max descent (50m): {min(grads):.1f}% at {valid[5+grads.index(min(grads))]["cum_dist"]:.0f}m from start',
    f'Max ascent (50m):  {max(grads):.1f}% at {valid[5+grads.index(max(grads))]["cum_dist"]:.0f}m from start',
    '',
    'Gradient distribution (50m smoothed):',
    f'  Flat       (0-5%):   {sum(1 for g in grads if abs(g)<5)*100//n}%',
    f'  Moderate (5-15%):   {sum(1 for g in grads if 5<=abs(g)<15)*100//n}%',
    f'  Steep   (15-30%):   {sum(1 for g in grads if 15<=abs(g)<30)*100//n}%',
    f'  Very steep (>30%):  {sum(1 for g in grads if abs(g)>=30)*100//n}%',
    f'  Uphill sections:    {sum(1 for g in grads if g>2)*100//n}%',
]

with open(r'C:\Users\Shea\GIS_Work\gradient_summary_dtm.txt', 'w') as f:
    f.write('\n'.join(lines))
for l in lines:
    print(l)
