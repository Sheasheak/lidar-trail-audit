import arcpy, os, math, csv

arcpy.env.overwriteOutput = True
gdb = r'C:\Users\Shea\GIS_Work\gradient.gdb'

pts_with_elev = os.path.join(gdb, 'trail_pts_elev')
rows = []
with arcpy.da.SearchCursor(pts_with_elev, ['SHAPE@XY', 'RASTERVALU']) as cur:
    for row in cur:
        rows.append({'x': row[0][0], 'y': row[0][1], 'elev': row[1]})

print(f'Got {len(rows)} points')
print(f'Sample elevations: {[r["elev"] for r in rows[:5]]}')

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
        r['cum_dist'] = round(cum_dist, 1)
        r['gradient_pct'] = round((vert / horiz * 100) if horiz > 0 else 0.0, 2)

# Write CSV
csv_path = r'C:\Users\Shea\GIS_Work\askins_gradient.csv'
with open(csv_path, 'w', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=['cum_dist', 'elev', 'gradient_pct', 'x', 'y'])
    writer.writeheader()
    writer.writerows(rows)
print(f'CSV saved: {csv_path}')

# Summary stats
valid = [r for r in rows if r['elev'] > -9999]
grads = [r['gradient_pct'] for r in valid[1:]]
total_dist = valid[-1]['cum_dist']
total_descent = valid[-1]['elev'] - valid[0]['elev']
avg_grad = (total_descent / total_dist * 100) if total_dist > 0 else 0
steepest_d = min(grads)
steepest_u = max(grads)
steep_d_idx = grads.index(steepest_d) + 1

summary = [
    'Trail: Askins MTB (Christchurch LiDAR DSM 1m)',
    f'Total length: {total_dist:.0f} m',
    f'Start elevation: {valid[0]["elev"]:.1f} m',
    f'End elevation: {valid[-1]["elev"]:.1f} m',
    f'Total descent: {total_descent:.1f} m',
    f'Average gradient: {avg_grad:.1f}%',
    f'Max descent gradient: {steepest_d:.1f}%',
    f'Max ascent gradient: {steepest_u:.1f}%',
    f'Steepest descent at: {valid[steep_d_idx]["cum_dist"]:.0f} m from start',
]
with open(r'C:\Users\Shea\GIS_Work\gradient_summary.txt', 'w') as f:
    f.write('\n'.join(summary))

for line in summary:
    print(line)
