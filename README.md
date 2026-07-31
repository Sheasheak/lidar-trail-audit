# LiDAR Trail Audit Tool

Automated mountain-bike trail auditing using LINZ LiDAR elevation data, grading trails
against the **NZ MTB Trail Design & Construction Guidelines** (Recreation Aotearoa, 2022).

Test trail: **Askins**, Christchurch Port Hills — downhill only, Blue grade, 2116 m long, 192 m descent.

## Layout

| Path | Contents |
|---|---|
| `notebooks/` | `TrailAudit_v9.ipynb` (current), `TrailAudit_v8.ipynb` (previous) |
| `data/` | `askins.kml` trail line, 1 m bare-earth DEM + hillshade + slope rasters (EPSG:2193), editable `askins_trail.gpkg`, QGIS project `askins_trail_editor.qgz` |
| `scripts/` | Standalone / QGIS-console helper scripts |
| `trail_editor/` | Browser trail-line editor (Leaflet, LINZ basemaps) |
| `trail_grader/` | Flask grading app (source only — `build/`, `dist/` are gitignored) |
| `outputs/` | Generated maps, gradient profiles, CSV/summary reports |

## Setup

```bash
python -m pip install -r requirements.txt
cp .env.example .env      # then add your LINZ API key
```

Run the notebook on Python 3.13 with a standard Jupyter kernel.

## Pipeline

1. KML → `(lon, lat)` list in WGS84
2. Reproject WGS84 → NZTM2000 (EPSG:2193) for metre-based calculations
3. Interpolate to 1 m spacing along the line
4. Fetch elevation from the LINZ LiDAR Raster Query API (threaded)
5. Compute gradient between consecutive points, smoothed
6. Compute turn radius via circumradius of point triples
7. Grade against Recreation Aotearoa thresholds
8. Output interactive map + gradient profile chart

## Grading thresholds (descending trails)

| Grade | Avg gradient max | Max gradient | Exception % | Min turn radius |
|---|---|---|---|---|
| 1 | 3.5° (6.1%) | 4° (7.0%) | 2% | 6 m ± 1 m |
| 2 | 5° (8.8%) | 8° (14.1%) | 5% | 4 m |
| 3 | 6° (10.5%) | 11° (19.3%) | 10% | 2.5 m |
| 4 | 10° (17.5%) | 15° (27.0%) | 20% | 2 m |
| 5 | 14° (24.9%) | 20° (36.0%) | 20% | 1.5 m |
| 6 | no target | no target | no target | 1 m |

Overall grade = the worse of the gradient grade and the turn-radius grade.

## Coordinate systems

- **EPSG:4326 (WGS84)** — GPS lon/lat; used by KML, the LINZ API, and Folium
- **EPSG:2193 (NZTM2000)** — metres; used for all distance, gradient, and radius maths

Always pass `always_xy=True` to `pyproj.Transformer`.

## Status

- **Phase 1** ✅ Working pipeline (gradient + turn radius grading)
- **Phase 2** 🔨 Accurate trail-line extraction; QGIS hand-edit workspace built
- **Phase 3** Feature detection (jumps, drops, berms, rock gardens)
- **Phase 4** Full metrics (sideslope, tread width, obstacles)
- **Phase 5** Polish and write-up
- **Phase 6** Web app

Entered in the Prime Minister's Space Prize for Student Endeavour 2026.
