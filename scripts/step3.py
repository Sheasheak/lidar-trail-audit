import arcpy, os, math, csv
import arcpy.sa

arcpy.env.overwriteOutput = True
arcpy.CheckOutExtension('Spatial')
gdb = r'C:\Users\Shea\GIS_Work\gradient.gdb'
arcpy.env.workspace = gdb

askins_nztm = os.path.join(gdb, 'askins_nztm')
dsm_mosaic = os.path.join(gdb, 'dsm_mosaic')

# Step 4: Densify the trail line to get points every ~5m
askins_dense = os.path.join(gdb, 'askins_dense')
arcpy.management.CopyFeatures(askins_nztm, askins_dense)
arcpy.edit.Densify(askins_dense, 'DISTANCE', '5 Meters')
print('Densified trail')

# Step 5: Convert line vertices to points
trail_pts = os.path.join(gdb, 'trail_points')
arcpy.management.FeatureVerticesToPoints(askins_dense, trail_pts, 'ALL')
print('Vertices to points done')
count = int(arcpy.management.GetCount(trail_pts).getOutput(0))
print(f'Point count: {count}')

# Step 6: Add cumulative distance field then extract elevation from DSM
arcpy.management.AddField(trail_pts, 'dsm_elev', 'FLOAT')

# Extract DSM values to points
arcpy.sa.ExtractValuesToPoints(trail_pts, dsm_mosaic, os.path.join(gdb, 'trail_pts_elev'))
print('Elevation extracted')

# Step 7: Read points and calculate gradient
pts_with_elev = os.path.join(gdb, 'trail_pts_elev')
rows = []
with arcpy.da.SearchCursor(pts_with_elev, ['SHAPE@XY', 'RASTERVALU', 'ORIG_FID']) as cur:
    for row in cur:
        rows.append({'x': row[0][0], 'y': row[0][1], 'elev': row[1], 'fid': row[2]})

# Sort by ORIG_FID (preserve order)
rows.sort(key=lambda r: r['fid'])
print(f'Got {len(rows)} elevation points')

# Calculate cumulative distance and gradient
cum_dist = 0.0
for i, r in enumerate(rows):
    if i == 0:
        r['cum_dist'] = 0.0
        r['gradient_pct'] = 0.0
    else:
        prev = rows[i-1]
        horiz = math.sqrt((r['x']-prev['x'])**2 + (r['y']-prev['y'])**2)
        vert = r['elev'] - prev['elev']
        cum_dist += horiz
        r['cum_dist'] = cum_dist
        r['gradient_pct'] = (vert / horiz * 100) if horiz > 0 else 0.0

# Write CSV
csv_path = r'C:\Users\Shea\GIS_Work\askins_gradient.csv'
with open(csv_path, 'w', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=['cum_dist', 'elev', 'gradient_pct', 'x', 'y'])
    writer.writeheader()
    writer.writerows(rows)
print(f'Saved to {csv_path}')

# Summary stats
elevs = [r['elev'] for r in rows if r['elev'] > -9999]
grads = [r['gradient_pct'] for r in rows[1:] if r['elev'] > -9999]
total_dist = rows[-1]['cum_dist']
total_descent = rows[-1]['elev'] - rows[0]['elev']
avg_grad = (total_descent / total_dist * 100) if total_dist > 0 else 0

with open(r'C:\Users\Shea\GIS_Work\gradient_summary.txt', 'w') as f:
    f.write(f'Trail: Askins (Christchurch LiDAR DSM)\n')
    f.write(f'Total length: {total_dist:.0f} m\n')
    f.write(f'Start elevation: {rows[0]["elev"]:.1f} m\n')
    f.write(f'End elevation: {rows[-1]["elev"]:.1f} m\n')
    f.write(f'Total descent: {total_descent:.1f} m\n')
    f.write(f'Average gradient: {avg_grad:.1f}%\n')
    f.write(f'Max gradient (descent): {min(grads):.1f}%\n')
    f.write(f'Max gradient (ascent): {max(grads):.1f}%\n')
    steepest = min(grads)
    steep_idx = grads.index(steepest) + 1
    f.write(f'Steepest point at: {rows[steep_idx]["cum_dist"]:.0f} m from start\n')

print('Summary written')
