import csv, os

csv_path = r'C:\Users\Shea\GIS_Work\askins_gradient.csv'
rows = []
with open(csv_path) as f:
    reader = csv.DictReader(f)
    for row in reader:
        rows.append(row)

# Find the extreme gradient points
extreme = [(i, float(rows[i]['gradient_pct']), float(rows[i]['elev']), float(rows[i]['cum_dist'])) 
           for i in range(1, len(rows)) if rows[i]['gradient_pct'] and abs(float(rows[i]['gradient_pct'])) > 50]

extreme.sort(key=lambda x: x[1])
with open(r'C:\Users\Shea\GIS_Work\extreme_grads.txt', 'w') as f:
    f.write(f'Extreme gradient points (>50%):\n')
    for idx, grad, elev, dist in extreme[:20]:
        prev_elev = float(rows[idx-1]['elev'])
        f.write(f'  dist={dist:.0f}m  elev={elev:.1f}m (prev={prev_elev:.1f}m)  grad={grad:.1f}%\n')

print(f'Total extreme points: {len(extreme)}')

# Show first 20 elevation values
print('First 20 elevations:')
for r in rows[:20]:
    print(f'  dist={r["cum_dist"]}m  elev={r["elev"]}m  grad={r["gradient_pct"]}%')
