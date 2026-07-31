import math, csv

csv_in = r'C:\Users\Shea\GIS_Work\askins_gradient_kml.csv'
rows = []
with open(csv_in) as f:
    for r in csv.DictReader(f):
        rows.append({'seq':int(r['seq']),'cum_dist':float(r['cum_dist']),'elev_m':float(r['elev_m']),'x':float(r['x']),'y':float(r['y'])})

def grad_at(rows, i, half_window=25):
    ci = rows[i]['cum_dist']
    rear = rows[0]
    for j in range(i-1, -1, -1):
        if ci - rows[j]['cum_dist'] >= half_window:
            rear = rows[j]; break
    front = rows[-1]
    for j in range(i+1, len(rows)):
        if rows[j]['cum_dist'] - ci >= half_window:
            front = rows[j]; break
    horiz = front['cum_dist'] - rear['cum_dist']
    vert = front['elev_m'] - rear['elev_m']
    return (vert / horiz * 100) if horiz > 0 else 0.0

results = []
for i, r in enumerate(rows):
    results.append({'dist_m': r['cum_dist'], 'elev_m': r['elev_m'], 'gradient_50m': round(grad_at(rows, i, 25), 1)})

csv_out = r'C:\Users\Shea\GIS_Work\askins_gradient_final.csv'
with open(csv_out, 'w', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=['dist_m','elev_m','gradient_50m'])
    writer.writeheader(); writer.writerows(results)

total_dist = rows[-1]['cum_dist']
total_descent = rows[-1]['elev_m'] - rows[0]['elev_m']
avg_grad = total_descent / total_dist * 100
grads = [r['gradient_50m'] for r in results]
steepest_d = min(grads); steepest_u = max(grads)
idx_d = grads.index(steepest_d); idx_u = grads.index(steepest_u)
mid = results[5:-5]; n = len(mid)
flat = sum(1 for r in mid if abs(r['gradient_50m']) < 5)
mod  = sum(1 for r in mid if 5 <= abs(r['gradient_50m']) < 15)
steep= sum(1 for r in mid if 15 <= abs(r['gradient_50m']) < 30)
vstep= sum(1 for r in mid if abs(r['gradient_50m']) >= 30)
up   = sum(1 for r in mid if r['gradient_50m'] > 2)

lines = [
    'Trail: Askins MTB (Christchurch)',
    'Source: KML GPS elevations, 50m smoothing window',
    '',
    f'Total length:      {total_dist:.0f} m',
    f'Start elevation:   {rows[0]["elev_m"]:.0f} m',
    f'End elevation:     {rows[-1]["elev_m"]:.0f} m',
    f'Total descent:     {total_descent:.0f} m',
    f'Average gradient:  {avg_grad:.1f}%',
    '',
    f'Max descent (50m): {steepest_d:.1f}% at {results[idx_d]["dist_m"]:.0f}m from start',
    f'Max ascent (50m):  {steepest_u:.1f}% at {results[idx_u]["dist_m"]:.0f}m from start',
    '',
    'Gradient distribution (50m smoothed):',
    f'  Flat       (0-5%):  {flat*100//n}%',
    f'  Moderate (5-15%):  {mod*100//n}%',
    f'  Steep   (15-30%):  {steep*100//n}%',
    f'  Very steep (>30%): {vstep*100//n}%',
    f'  Uphill sections:   {up*100//n}%',
]
with open(r'C:\Users\Shea\GIS_Work\gradient_summary.txt', 'w') as f:
    f.write('\n'.join(lines))
for l in lines: print(l)
