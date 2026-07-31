import arcpy, os, csv, math
import arcpy.sa

arcpy.env.overwriteOutput = True
out_folder = r'C:\Users\Shea\GIS_Work'
dsm_folder = r'C:\Users\Shea\Downloads\lds-canterbury-christchurch-lidar-1m-dsm-2024-2025-AAIGrid (3)'
gdb = os.path.join(out_folder, 'gradient.gdb')

if not arcpy.Exists(gdb):
    arcpy.management.CreateFileGDB(out_folder, 'gradient')
arcpy.env.workspace = gdb

# Step 1: Get polyline from KML GDB
kml_gdb = os.path.join(out_folder, 'askins_lyr.gdb')
askins_line = os.path.join(kml_gdb, 'Tracks', 'Polylines')

# Step 2: Project to NZTM2000 (EPSG:2193) to match DSM
nztm = arcpy.SpatialReference(2193)
askins_nztm = os.path.join(gdb, 'askins_nztm')
arcpy.management.Project(askins_line, askins_nztm, nztm)
print('Projected to NZTM2000')

# Get extent of trail
desc = arcpy.Describe(askins_nztm)
ext = desc.extent
with open(r'C:\Users\Shea\GIS_Work\extent.txt', 'w') as f:
    f.write(f'XMin: {ext.XMin}\nXMax: {ext.XMax}\nYMin: {ext.YMin}\nYMax: {ext.YMax}\n')
print(f'Trail extent: {ext.XMin:.0f},{ext.YMin:.0f} to {ext.XMax:.0f},{ext.YMax:.0f}')

# Step 3: Find which DSM tiles intersect the trail
asc_files = [os.path.join(dsm_folder, f) for f in os.listdir(dsm_folder) if f.endswith('.asc')]
print(f'Total DSM tiles: {len(asc_files)}')

# Check each tile
relevant_tiles = []
for asc in asc_files:
    try:
        rast_desc = arcpy.Describe(asc)
        rext = rast_desc.extent
        # Check overlap
        if (rext.XMin < ext.XMax and rext.XMax > ext.XMin and
            rext.YMin < ext.YMax and rext.YMax > ext.YMin):
            relevant_tiles.append(asc)
            print(f'  Using: {os.path.basename(asc)}')
    except:
        pass

print(f'Relevant tiles: {len(relevant_tiles)}')
with open(r'C:\Users\Shea\GIS_Work\tiles.txt', 'w') as f:
    f.write('\n'.join(relevant_tiles))
