# Paste in the QGIS Python Console to open a 3D Map View.
# Terrain is already set to the DEM at project level, so it renders real relief.
from qgis.PyQt.QtWidgets import QAction
mw = iface.mainWindow()
act = next((a for a in mw.findChildren(QAction)
            if a.objectName() == "mActionNew3DMapCanvas"), None)
if act:
    act.trigger()
    print("3D Map View opened. Drag with the mouse to orbit; the hillshade + trail "
          "are draped on the DEM terrain.")
else:
    print("Use the menu: View > 3D Map Views > New 3D Map View")
