# -*- coding: utf-8 -*-
"""Paste-and-run in the QGIS Python Console to build the trail-editing workspace.
Loads hillshade + DEM + editable trail, sets snapping, zooms in.
Re-runnable: it removes its own layers first."""
from qgis.core import (QgsProject, QgsRasterLayer, QgsVectorLayer,
                       QgsCoordinateReferenceSystem, QgsSnappingConfig,
                       QgsTolerance, QgsLineSymbol)
from qgis.utils import iface

OUT   = r"C:\Users\Shea\GIS_Work"
DEM   = OUT + r"\askins_dem.tif"
HS    = OUT + r"\askins_hillshade.tif"
GPKG  = OUT + r"\askins_trail.gpkg"

prj = QgsProject.instance()
prj.setCrs(QgsCoordinateReferenceSystem("EPSG:2193"))   # NZTM2000

# clean previous run
for lyr in list(prj.mapLayers().values()):
    if lyr.name() in ("askins_trail", "askins_hillshade", "askins_dem"):
        prj.removeMapLayer(lyr.id())

# --- rasters (hillshade on top of DEM) ---
dem = QgsRasterLayer(DEM, "askins_dem")
hs  = QgsRasterLayer(HS,  "askins_hillshade")
prj.addMapLayer(dem)
prj.addMapLayer(hs)

# --- editable trail line ---
trail = QgsVectorLayer(GPKG + "|layername=askins_trail", "askins_trail", "ogr")
prj.addMapLayer(trail)

# bright magenta line so it pops on grey hillshade; vertices show in edit mode
sym = QgsLineSymbol.createSimple({"color": "255,0,255,255", "width": "0.6"})
trail.renderer().setSymbol(sym)
trail.triggerRepaint()

# layer order: trail > hillshade > dem ; hide raw DEM by default
root = prj.layerTreeRoot()
node = root.findLayer(dem.id())
if node:
    node.setItemVisibilityChecked(False)

# --- snapping: snap to trail vertices, topological editing on ---
cfg = prj.snappingConfig()
cfg.setEnabled(True)
try:
    cfg.setMode(QgsSnappingConfig.AllLayers)
except Exception:
    pass
for setter, val in (("setTypeFlag", None), ("setType", None)):
    try:
        from qgis.core import Qgis
        getattr(cfg, "setTypeFlag")(Qgis.SnappingType.Vertex)
        break
    except Exception:
        try:
            getattr(cfg, "setType")(QgsSnappingConfig.Vertex)
            break
        except Exception:
            pass
cfg.setTolerance(12)
cfg.setUnits(QgsTolerance.Pixels)
prj.setSnappingConfig(cfg)
prj.setTopologicalEditing(True)

# zoom to the trail
iface.setActiveLayer(trail)
iface.mapCanvas().setExtent(trail.extent())
iface.mapCanvas().zoomByFactor(1.15)
iface.mapCanvas().refresh()

print("Workspace ready. Layers loaded:", [l.name() for l in prj.mapLayers().values()])
print("To edit: select 'askins_trail' -> toggle editing (pencil) -> Vertex Tool -> drag nodes onto the tread -> Save.")
