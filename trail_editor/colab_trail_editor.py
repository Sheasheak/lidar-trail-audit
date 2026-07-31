# =====================================================================
#  Askins Trail Line Editor — Google Colab cell (ipyleaflet)
#  Edit the trail against LINZ aerial + LiDAR hillshade, then export the
#  corrected line as KML/GeoJSON to feed the grading pipeline.
# =====================================================================
# --- Colab fix 1: install + enable third-party widgets ---
!pip -q install ipyleaflet
try:
    from google.colab import output
    output.enable_custom_widget_manager()
    IN_COLAB = True
except Exception:
    IN_COLAB = False

import base64, json, math
from ipyleaflet import Map, TileLayer, ImageOverlay, DrawControl, LayersControl

# --------------------------------------------------------------------
LINZ_KEY = "24d5daf2151847f7b7db497b10ea8712"   # restrict by domain before public use
AERIAL = ("https://tiles-cdn.koordinates.com/services;key=" + LINZ_KEY +
          "/tiles/v4/layer=123118,style=auto/EPSG:3857/{z}/{x}/{y}.png")

# Askins trail (Trailforks GPS line), [lon, lat]
TRAIL = [[172.64053,-43.59534],[172.64054,-43.59492],[172.64046,-43.5947],[172.64032,-43.59445],[172.64015,-43.59427],[172.63971,-43.59409],[172.63923,-43.59394],[172.63854,-43.59367],[172.63825,-43.59363],[172.63773,-43.59352],[172.6372,-43.59337],[172.63678,-43.59347],[172.63668,-43.59356],[172.63666,-43.59365],[172.63675,-43.59373],[172.6368,-43.59384],[172.63673,-43.5939],[172.63663,-43.59389],[172.63656,-43.59385],[172.63642,-43.59372],[172.63634,-43.59358],[172.63621,-43.59343],[172.63601,-43.59335],[172.63596,-43.59335],[172.6359,-43.59339],[172.63591,-43.59346],[172.63601,-43.5936],[172.636,-43.59362],[172.63595,-43.59361],[172.63587,-43.59351],[172.63581,-43.59347],[172.63576,-43.59347],[172.63573,-43.5935],[172.63576,-43.5936],[172.63574,-43.59362],[172.6357,-43.59361],[172.63567,-43.59358],[172.63552,-43.59338],[172.63548,-43.59335],[172.63543,-43.59334],[172.63539,-43.59336],[172.63536,-43.5934],[172.63539,-43.59348],[172.63554,-43.59367],[172.63552,-43.59379],[172.63544,-43.59379],[172.63533,-43.59366],[172.63514,-43.59351],[172.63509,-43.59333],[172.63511,-43.59316],[172.63516,-43.59302],[172.63531,-43.5929],[172.63557,-43.59276],[172.63586,-43.59269],[172.63618,-43.59259],[172.63633,-43.59252],[172.63635,-43.59246],[172.63632,-43.59242],[172.63625,-43.59241],[172.63597,-43.59248],[172.63566,-43.59258],[172.63537,-43.59262],[172.63497,-43.59274],[172.63484,-43.59281],[172.63473,-43.59282],[172.63458,-43.59286],[172.6344,-43.59299],[172.63437,-43.59306],[172.63442,-43.59328],[172.63437,-43.59333],[172.63429,-43.59332],[172.63423,-43.59324],[172.63423,-43.59312],[172.63416,-43.59296],[172.63407,-43.59292],[172.63402,-43.59294],[172.63398,-43.59303],[172.63404,-43.59318],[172.63406,-43.59331],[172.63402,-43.59336],[172.63396,-43.59336],[172.63365,-43.59287],[172.63364,-43.59279],[172.63368,-43.59271],[172.63392,-43.59254],[172.63419,-43.5924],[172.63452,-43.59224],[172.63474,-43.59218],[172.63475,-43.59211],[172.63472,-43.59208],[172.63451,-43.59208],[172.63426,-43.59214],[172.63324,-43.59252],[172.6332,-43.59251],[172.63316,-43.59245],[172.63318,-43.59239],[172.63329,-43.59236],[172.63429,-43.59188],[172.6343,-43.59182],[172.63424,-43.59178],[172.63329,-43.59198],[172.63291,-43.59212],[172.63272,-43.59224],[172.63264,-43.59237],[172.63261,-43.59255],[172.6326,-43.59311],[172.63267,-43.59335],[172.63271,-43.5936],[172.63266,-43.59365],[172.63257,-43.59361],[172.63238,-43.59332],[172.6322,-43.59323]]
CENTER = (-43.59316, 172.63529)
# hillshade/slope PNG bounds: ((south, west), (north, east))
DEM_BOUNDS = ((-43.59793864850719, 172.6312889697974),
              (-43.59141633500593, 172.6432222561067))

# --- Colab fix 2: local PNGs can't be served, so upload + base64 them ---
# Option A: host hillshade.png / slope.png and paste raw URLs here:
HILLSHADE_URL = ""
SLOPE_URL = ""
# Option B: leave the URLs blank -> you'll be prompted to upload the two PNGs
if IN_COLAB and not (HILLSHADE_URL and SLOPE_URL):
    try:
        from google.colab import files
        print("Upload hillshade.png and/or slope.png (or press Cancel to skip DEM overlays):")
        up = files.upload()
        uri = lambda b: "data:image/png;base64," + base64.b64encode(b).decode()
        for name, blob in up.items():
            low = name.lower()
            if "hillshade" in low: HILLSHADE_URL = uri(blob)
            elif "slope" in low:   SLOPE_URL = uri(blob)
            else: print("  (ignored unrecognised file:", name, ")")
        print("  overlays loaded ->",
              "hillshade" if HILLSHADE_URL else "",
              "slope" if SLOPE_URL else "" or "(none)")
    except Exception as e:
        print("Skipping overlays:", e)

# --------------------------------------------------------------------
aerial = TileLayer(url=AERIAL, name="LINZ aerial (2025)", max_zoom=22)
m = Map(center=CENTER, zoom=16, layers=[aerial], scroll_wheel_zoom=True)

if HILLSHADE_URL:
    m.add(ImageOverlay(url=HILLSHADE_URL, bounds=DEM_BOUNDS, name="LiDAR hillshade", opacity=0.6))
if SLOPE_URL:
    m.add(ImageOverlay(url=SLOPE_URL, bounds=DEM_BOUNDS, name="LiDAR slope", opacity=0.6))

# editable trail (leaflet-draw). Preload the line, disable other shapes.
draw = DrawControl(
    polyline={"shapeOptions": {"color": "#ff00ff", "weight": 3}},
    polygon={}, rectangle={}, circle={}, circlemarker={}, marker={},
    edit=True, remove=True)
draw.data = [{"type": "Feature", "properties": {},
              "geometry": {"type": "LineString", "coordinates": TRAIL}}]

corrected_coords = [c[:] for c in TRAIL]   # updated live as you edit


def _line_length_m(coords):
    """Great-circle length of a [lon,lat] polyline, in metres."""
    R = 6371000.0
    total = 0.0
    for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:]):
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlmb = math.radians(lon2 - lon1)
        a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dlmb/2)**2
        total += R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return total


def _on_draw(control, action, geo_json):
    global corrected_coords
    for f in control.data:
        if f["geometry"]["type"] == "LineString":
            corrected_coords = f["geometry"]["coordinates"]
    print(f"[{action}] vertices: {len(corrected_coords)}  "
          f"length: {_line_length_m(corrected_coords):.0f} m")


draw.on_draw(_on_draw)
m.add(draw)
m.add(LayersControl(position="topright"))
print(f"Loaded Askins line: {len(corrected_coords)} vertices, "
      f"{_line_length_m(corrected_coords):.0f} m")
print("Edit: click the ✎ (edit) tool, drag vertices, then click Save. "
      "Then run the export cell below.")
m   # display the map (last expression in the cell)


# =====================================================================
#  EXPORT CELL — run this AFTER editing (in a new cell) to hand the
#  corrected line to the grading pipeline.
# =====================================================================
def to_kml(coords, name="Askins (corrected)"):
    """LineString KML matching what the pykml grading pipeline ingests."""
    ring = " ".join(f"{lon},{lat},0" for lon, lat in coords)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f'<Placemark><name>{name}</name><LineString>'
        '<tessellate>1</tessellate>'
        f'<coordinates>{ring}</coordinates>'
        '</LineString></Placemark></Document></kml>')


def to_geojson(coords, name="Askins (corrected)"):
    return json.dumps({"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {"name": name},
        "geometry": {"type": "LineString", "coordinates": coords}}]}, indent=1)


def export_corrected(fmt="kml", download=True):
    """Write corrected_coords to file (and download in Colab). fmt: 'kml'|'geojson'|'both'."""
    written = []
    if fmt in ("kml", "both"):
        with open("askins_corrected.kml", "w", encoding="utf-8") as f:
            f.write(to_kml(corrected_coords))
        written.append("askins_corrected.kml")
    if fmt in ("geojson", "both"):
        with open("askins_corrected.geojson", "w", encoding="utf-8") as f:
            f.write(to_geojson(corrected_coords))
        written.append("askins_corrected.geojson")
    print(f"Exported {len(corrected_coords)} vertices, "
          f"{_line_length_m(corrected_coords):.0f} m -> {', '.join(written)}")
    if download and IN_COLAB:
        from google.colab import files
        for w in written:
            files.download(w)
    return written

# export_corrected("both")   # <- uncomment / run in a cell after you finish editing
