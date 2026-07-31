import json, os, sys, traceback, threading, webbrowser
from flask import Flask, request, render_template, render_template_string, redirect
from config import get_dtm_folder, save_config
from grader import analyse_trail

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024

MAP_TEMPLATE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{{ trail_name }} — NZ Grade Assessment</title>
<link rel="stylesheet" href="/static/leaflet.css"/>
<script src="/static/leaflet.js"></script>
<style>
  body{margin:0;font-family:sans-serif;}
  #map{height:100vh;}
  .legend{background:white;padding:12px 16px;border-radius:6px;box-shadow:0 1px 5px rgba(0,0,0,0.3);line-height:1.9;font-size:12px;min-width:230px;}
  .sw{display:inline-block;width:22px;height:10px;border-radius:2px;margin-right:6px;vertical-align:middle;border:1px solid #ccc;}
  #stats-panel{position:fixed;top:16px;left:50%;transform:translateX(-50%);background:rgba(255,255,255,0.97);border-radius:8px;box-shadow:0 2px 10px rgba(0,0,0,0.25);padding:14px 18px;z-index:1000;font-size:13px;min-width:440px;}
  #stats-panel h3{margin:0 0 10px 0;font-size:14px;text-align:center;}
  #stats-panel table{border-collapse:collapse;width:100%;}
  #stats-panel td,#stats-panel th{padding:5px 12px;border:1px solid #ddd;text-align:left;}
  #stats-panel th{background:#f5f5f5;font-weight:600;}
  #stats-panel tr.overall td{font-weight:700;background:#f0f4ff;}
  #stats-panel .tbd{color:#aaa;font-style:italic;}
  .back-btn{position:fixed;bottom:20px;left:20px;z-index:1000;background:white;border:1px solid #ddd;border-radius:6px;padding:8px 14px;font-size:13px;cursor:pointer;box-shadow:0 1px 4px rgba(0,0,0,0.15);text-decoration:none;color:#333;}
  .back-btn:hover{background:#f5f5f5;}
  #modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.55);z-index:2000;display:flex;align-items:center;justify-content:center;}
  #modal{background:white;border-radius:10px;padding:28px 32px;max-width:520px;width:90%;box-shadow:0 4px 24px rgba(0,0,0,0.3);max-height:90vh;overflow-y:auto;}
  #modal h2{margin:0 0 6px 0;font-size:17px;}
  #modal .subtitle{color:#666;font-size:12px;margin-bottom:20px;}
  .q-block{margin-bottom:18px;}
  .q-block label.q-title{display:block;font-weight:600;margin-bottom:6px;font-size:13px;}
  .q-block .hint{color:#888;font-size:11px;margin-bottom:6px;}
  .q-option{display:flex;align-items:center;gap:8px;padding:5px 8px;border-radius:5px;cursor:pointer;font-size:13px;}
  .q-option:hover{background:#f5f5f5;}
  .q-option input{cursor:pointer;}
  #modal-submit{margin-top:8px;width:100%;padding:10px;background:#0044cc;color:white;border:none;border-radius:6px;font-size:14px;font-weight:600;cursor:pointer;}
  #modal-submit:hover{background:#0033aa;}
</style>
</head>
<body>
<div id="modal-overlay">
  <div id="modal">
    <h2>Trail Field Assessment — {{ trail_name }}</h2>
    <p class="subtitle">Answer based on your observations. Combines with LiDAR + rainfall data for a full NZ grade.</p>
    <div class="q-block">
      <label class="q-title">1. Tread obstacles — typical max height of rocks, roots or ruts?</label>
      <div class="hint">NZ MTB Guidelines p.6</div>
      <label class="q-option"><input type="radio" name="obstacles" value="0"> None</label>
      <label class="q-option"><input type="radio" name="obstacles" value="1"> Up to 50mm</label>
      <label class="q-option"><input type="radio" name="obstacles" value="2"> Up to 100mm</label>
      <label class="q-option"><input type="radio" name="obstacles" value="3"> Up to 200mm</label>
      <label class="q-option"><input type="radio" name="obstacles" value="4"> Up to 600mm</label>
      <label class="q-option"><input type="radio" name="obstacles" value="5"> No limit / bigger</label>
    </div>
    <div class="q-block">
      <label class="q-title">2. Trail surface?</label>
      <div class="hint">NZ MTB Guidelines p.6-7</div>
      <label class="q-option"><input type="radio" name="surface" value="0"> Hardened and smooth (sealed)</label>
      <label class="q-option"><input type="radio" name="surface" value="1"> Firm and stable (compacted gravel)</label>
      <label class="q-option"><input type="radio" name="surface" value="2"> Mostly stable, some loose sections</label>
      <label class="q-option"><input type="radio" name="surface" value="3"> Generally firm but loose/broken in places</label>
      <label class="q-option"><input type="radio" name="surface" value="4"> Highly variable — natural surface, mud, ruts</label>
    </div>
    <div class="q-block">
      <label class="q-title">3. Technical features (jumps, drops)?</label>
      <div class="hint">NZ MTB Guidelines p.5</div>
      <label class="q-option"><input type="radio" name="features" value="0"> All rollable — no jumps or drops</label>
      <label class="q-option"><input type="radio" name="features" value="1"> Rollable — stepped drops up to 200mm</label>
      <label class="q-option"><input type="radio" name="features" value="2"> Rollable — stepped drops up to 400mm, small tabletops</label>
      <label class="q-option"><input type="radio" name="features" value="3"> Mostly rollable — drops up to 600mm, gap jumps with B-lines</label>
      <label class="q-option"><input type="radio" name="features" value="4"> Stepped drops up to 1.5m, large jumps</label>
      <label class="q-option"><input type="radio" name="features" value="5"> No limits</label>
    </div>
    <button id="modal-submit">Calculate Grade</button>
  </div>
</div>

<div id="map"></div>

<div id="stats-panel">
  <h3>{{ trail_name }} — NZ Grade Assessment</h3>
  <table>
    <tr><th>Criterion</th><th>Result</th><th>Grade</th></tr>
    <tr><td>Total length</td><td>{{ r.total_dist|int }} m</td><td>—</td></tr>
    <tr><td>Total descent</td><td>{{ r.total_descent }} m</td><td>—</td></tr>
    <tr><td>Average gradient</td><td>{{ r.avg_grad }}%</td><td>{{ r.avg_grad_grade }}</td></tr>
    <tr><td>Max gradient (50m smooth)</td><td>{{ r.max_grad }}%</td><td>{{ r.max_grad_grade_raw }} (raw)</td></tr>
    <tr><td>Gradient — with exceptions rule</td><td>{{ r.over_pct }}% out-of-spec</td><td>{{ r.max_grad_grade }}</td></tr>
    <tr><td>Turn radius (typical)</td><td>{{ r.p10_radius }} m</td><td>{{ r.p10_radius_grade }}</td></tr>
    <tr><td>Turn radius (tightest corner)</td><td>{{ r.min_radius }} m</td><td>{{ r.min_radius_grade }}</td></tr>
    <tr><td>Annual rainfall (5yr avg)</td><td>{{ r.avg_rainfall }} mm/yr</td><td>{{ r.rain_label }}</td></tr>
    <tr><td>Exposure (90th pct drop)</td><td>{{ r.p90_drop }}m fall height</td><td>{{ r.exposure_grade }} — {{ r.exposure_label }}</td></tr>
    <tr id="row-obstacles"><td class="tbd">Tread obstacles</td><td class="tbd">—</td><td class="tbd">Answer questionnaire</td></tr>
    <tr id="row-surface"><td class="tbd">Trail surface</td><td class="tbd">—</td><td class="tbd">Answer questionnaire</td></tr>
    <tr id="row-features"><td class="tbd">Technical features</td><td class="tbd">—</td><td class="tbd">Answer questionnaire</td></tr>
    <tr class="overall" id="row-overall"><td><b>Overall grade</b></td><td colspan="2"><b>Complete questionnaire to calculate</b></td></tr>
  </table>
</div>

<a href="/" class="back-btn">&#8592; Grade another trail</a>

<script>
const GRADES = {{ r.grades_json | safe }};
const dataGrade = {{ r.overall_grade }};
const gradSegs  = {{ grad_segs_json | safe }};
const corners   = {{ corners_json | safe }};
const center    = {{ center_json | safe }};
const startPt   = {{ start_json | safe }};
const endPt     = {{ end_json | safe }};

const map = L.map('map').setView(center, 15);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{attribution:'© OpenStreetMap contributors',maxZoom:19}).addTo(map);

gradSegs.forEach(s => {
  const dir = s.gradient < 0 ? 'descent' : 'ascent';
  const popup = `<b>${s.grade}</b><br><table style="font-size:13px;min-width:170px">
    <tr><td style="color:#888;padding-right:10px">Distance</td><td><b>${s.dist}m</b> from start</td></tr>
    <tr><td style="color:#888;padding-right:10px">Elevation</td><td><b>${s.elev}m</b></td></tr>
    <tr><td style="color:#888;padding-right:10px">Gradient</td><td><b>${Math.abs(s.gradient)}%</b> ${dir}</td></tr>
  </table>`;
  L.polyline(s.coords,{color:s.color,weight:6,opacity:0.92}).addTo(map).bindPopup(popup);
});

corners.forEach(c => {
  const popup = `<b>${c.grade} (turn radius)</b><br><table style="font-size:13px;min-width:170px">
    <tr><td style="color:#888;padding-right:10px">Distance</td><td><b>${c.dist}m</b> from start</td></tr>
    <tr><td style="color:#888;padding-right:10px">Turn radius</td><td><b>${c.radius}m</b></td></tr>
  </table>`;
  L.circleMarker([c.lat,c.lon],{radius:7,color:'#fff',fillColor:c.color,fillOpacity:0.9,weight:2}).addTo(map).bindPopup(popup);
});

L.circleMarker([startPt.lat,startPt.lon],{radius:8,color:'#fff',fillColor:'#00b300',fillOpacity:1,weight:2}).addTo(map).bindPopup('Start — '+startPt.elev+'m');
L.circleMarker([endPt.lat,endPt.lon],{radius:8,color:'#fff',fillColor:'#cc0000',fillOpacity:1,weight:2}).addTo(map).bindPopup('End — '+endPt.elev+'m');

const leg = L.control({position:'bottomright'});
leg.onAdd = () => {
  const d = L.DomUtil.create('div','legend');
  d.innerHTML = `<b>NZ Grade (by gradient)</b><br>
    <span class="sw" style="background:#00b300"></span>Grade 1 Easiest<br>
    <span class="sw" style="background:#66cc00"></span>Grade 2 Easy<br>
    <span class="sw" style="background:#00aaff"></span>Grade 3 Intermediate<br>
    <span class="sw" style="background:#0044cc"></span>Grade 4 Advanced<br>
    <span class="sw" style="background:#444444"></span>Grade 5 Expert<br>
    <span class="sw" style="background:#660000"></span>Grade 6 Extreme<br>
    <hr style="margin:6px 0">
    <b>Trail line</b> = gradient grade<br>
    <b>Circles</b> = tight corners`;
  return d;
};
leg.addTo(map);

document.getElementById('modal-submit').addEventListener('click', () => {
  const get = n => { const el = document.querySelector('input[name="'+n+'"]:checked'); return el ? parseInt(el.value) : null; };
  const obstacles=get('obstacles'),surface=get('surface'),features=get('features');
  if([obstacles,surface,features].some(v=>v===null)){alert('Please answer all questions.');return;}
  const overall=Math.min(5,Math.max(dataGrade,obstacles,surface,features));
  const g=GRADES[overall];
  document.getElementById('row-obstacles').innerHTML='<td>Tread obstacles</td><td>'+['None','Up to 50mm','Up to 100mm','Up to 200mm','Up to 600mm','No limit'][obstacles]+'</td><td>'+GRADES[obstacles].name+' '+GRADES[obstacles].label+'</td>';
  document.getElementById('row-surface').innerHTML='<td>Trail surface</td><td>'+['Hardened/smooth','Firm & stable','Mostly stable','Generally firm, loose patches','Highly variable'][surface]+'</td><td>'+GRADES[surface].name+' '+GRADES[surface].label+'</td>';
  document.getElementById('row-features').innerHTML='<td>Technical features</td><td>'+['All rollable','Drops <=200mm','Drops <=400mm','Drops <=600mm','Drops <=1.5m','No limit'][features]+'</td><td>'+GRADES[features].name+' '+GRADES[features].label+'</td>';
  document.getElementById('row-overall').innerHTML='<td><b>Overall grade</b></td><td colspan="2"><b style="font-size:14px;color:'+g.hex+'">'+g.name+' — '+g.label+' ('+g.color_nz+')</b></td>';
  document.getElementById('modal-overlay').style.display='none';
});
</script>
</body>
</html>"""

@app.route('/')
def index():
    if not get_dtm_folder() or not os.path.isdir(get_dtm_folder()):
        return redirect('/setup')
    return render_template('index.html')

@app.route('/setup', methods=['GET', 'POST'])
def setup():
    if request.method == 'POST':
        folder = request.form.get('dtm_folder', '').strip()
        if not os.path.isdir(folder):
            return render_template('setup.html', error='Folder not found: ' + folder, prefill=folder)
        asc_files = [f for f in os.listdir(folder) if f.endswith('.asc')]
        if not asc_files:
            return render_template('setup.html', error='No .asc tile files found in that folder. Make sure you extracted the zip.', prefill=folder)
        save_config({'dtm_folder': folder})
        return redirect('/')
    prefill = get_dtm_folder()
    return render_template('setup.html', prefill=prefill)

@app.route('/grade', methods=['POST'])
def grade():
    f = request.files.get('kml')
    if not f or not f.filename.endswith('.kml'):
        return render_template('index.html', error='Please upload a .kml file.')
    trail_name = f.filename.replace('.kml','').replace('_',' ').replace('-',' ').title()
    kml_bytes  = f.read()
    try:
        r = analyse_trail(kml_bytes, get_dtm_folder())
    except Exception as e:
        return render_template('index.html', error='Processing failed: ' + str(e))
    return render_template_string(MAP_TEMPLATE,
        trail_name    = trail_name,
        r             = type('R', (), r)(),
        grad_segs_json= json.dumps(r['grad_segs']),
        corners_json  = json.dumps(r['tight_corners']),
        center_json   = json.dumps(r['map_center']),
        start_json    = json.dumps(r['start']),
        end_json      = json.dumps(r['end']),
    )

def open_browser():
    webbrowser.open('http://127.0.0.1:5000')

if __name__ == '__main__':
    threading.Timer(1.5, open_browser).start()
    app.run(debug=False, port=5000)
